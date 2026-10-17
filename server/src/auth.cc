#include "auth.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <array>
#include <fstream>
#include <optional>
#include <sstream>
#include <stdexcept>

#include "logging.hpp"
#include "patterns.hpp"

namespace hardcharger {

namespace {

using Traits = jwt::traits::boost_json;
using warpsql::Error;
using warpsql::ErrorCode;

// User name under which GizmoSQL 1.x clients send a sign-in token as the password.
constexpr std::string_view kTokenUser = "token";

std::array<unsigned char, 32> Digest(std::string_view text) {
  std::array<unsigned char, 32> out{};
  unsigned int length = 0;
  EVP_Digest(text.data(), text.size(), out.data(), &length, EVP_sha256(), nullptr);
  return out;
}

// Compares digests, so neither timing nor length reveals where the inputs differ.
bool SameSecret(std::string_view a, std::string_view b) {
  const auto da = Digest(a);
  const auto db = Digest(b);
  return CRYPTO_memcmp(da.data(), db.data(), da.size()) == 0;
}

std::string ReadFile(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw std::runtime_error("cannot read " + path);
  }
  std::stringstream contents;
  contents << in.rdbuf();
  return contents.str();
}

// One answer for every wrong credential, so a refusal never says which part was wrong.
Error Refused() {
  return Error(ErrorCode::kInvalidCredentials, "the user name or password is not correct");
}

Error TokenRefused() {
  return Error(ErrorCode::kInvalidCredentials, "the sign-in token is not accepted");
}

}  // namespace

authz::Identity IdentityOf(const warpsql::Principal& principal) {
  authz::Identity who;
  who.user_name = principal.name;
  who.superuser = principal.role == "admin";
  auto text = [&](const char* claim) -> std::string {
    const auto* value = principal.claims.if_contains(claim);
    return value && value->is_string() ? std::string(value->as_string()) : std::string();
  };
  who.credential = text("credential");
  who.scope_id = text("scope_id");
  if (const auto* idp = principal.claims.if_contains("idp"); idp && idp->is_object()) {
    if (const auto* issuer = idp->as_object().if_contains("iss"); issuer && issuer->is_string()) {
      who.idp_issuer = std::string(issuer->as_string());
    }
    if (const auto* groups = idp->as_object().if_contains("groups"); groups && groups->is_array()) {
      for (const auto& group : groups->as_array()) {
        if (group.is_string()) who.idp_groups.emplace_back(group.as_string());
      }
    }
  }
  return who;
}

Authenticator::Authenticator(const Config& config, const License& license,
                             std::shared_ptr<authz::AccessControl> access,
                             std::function<warpsql::Principal(std::string_view)> server_tokens,
                             std::shared_ptr<Jwks> jwks)
    : username_(config.username),
      password_(config.password),
      jwks_(std::move(jwks)),
      allowed_issuer_(config.token_allowed_issuer),
      allowed_audience_(config.token_allowed_audience),
      default_role_(config.token_default_role),
      groups_claim_(config.token_groups_claim),
      authorized_emails_(config.token_authorized_emails),
      log_level_(ParseLogLevel(config.auth_log_level)),
      license_(license),
      access_(std::move(access)),
      server_tokens_(std::move(server_tokens)) {
  if (!config.token_signature_verify_cert_path.empty()) {
    std::string pem = ReadFile(config.token_signature_verify_cert_path);
    // A certificate or a bare public key both work.
    verify_key_pem_ = pem.find("BEGIN CERTIFICATE") != std::string::npos
                          ? jwt::helper::extract_pubkey_from_cert(pem)
                          : pem;
  }
}

warpsql::Principal Authenticator::AuthenticatePassword(std::string_view username,
                                                       std::string_view password) {
  if (username == kTokenUser) {
    // A scoped token this server (or its cluster, sharing the secret key) handed out,
    // sent as a password the way GizmoSQL 1.x clients send it.
    if (access_ && server_tokens_) {
      std::optional<warpsql::Principal> scoped;
      try {
        auto principal = server_tokens_(password);
        if (principal.claims.contains("scope_id")) scoped = std::move(principal);
      } catch (const Error&) {
        // not one of this server's tokens: the sign-in token path decides
      }
      if (scoped) return *scoped;
    }
    if (AcceptsTokens()) return ExchangeToken(password);
  }
  const bool user_ok = SameSecret(username, username_);
  const bool password_ok = SameSecret(password, password_);
  if (user_ok && password_ok) {
    LogAt(log_level_, LogLevel::kInfo, "signed in", {{"user", std::string(username)}, {"method", "password"}});
    return warpsql::Principal{std::string(username), "admin", "password", {}};
  }
  // A user of the access-control store (never under the administrator's name). The
  // token carries a fingerprint of the password, so changing it ends the token.
  if (access_) {
    // Also for the administrator's name with a wrong password (no store user may take
    // it, so this always refuses): every refusal then costs the same password check,
    // and timing does not say which name is the administrator's.
    auto store_user = access_->AuthenticatePassword(username, password);
    if (store_user && !user_ok) {
      LogAt(log_level_, LogLevel::kInfo, "signed in",
            {{"user", store_user->user_name}, {"method", "password"}, {"role", "user"}});
      return warpsql::Principal{store_user->user_name, "user", "password",
                                {{"credential", store_user->credential}}};
    }
  }
  LogAt(log_level_, LogLevel::kInfo, "sign-in refused", {{"user", std::string(username)}, {"method", "password"}});
  throw Refused();
}

warpsql::Principal Authenticator::ExchangeToken(std::string_view subject_token) {
  if (!AcceptsTokens()) {
    throw Error(ErrorCode::kInvalidCredentials, "this server does not accept sign-in tokens");
  }
  jwt::decoded_jwt<Traits> decoded = [&] {
    try {
      return jwt::decode<Traits>(std::string(subject_token));
    } catch (const std::exception&) {
      throw TokenRefused();
    }
  }();
  try {
    auto verifier = jwt::verify<Traits>();
    AllowSigningKey(decoded, verifier);
    if (!allowed_issuer_.empty()) verifier.with_issuer(allowed_issuer_);
    if (!allowed_audience_.empty()) verifier.with_audience(allowed_audience_);
    verifier.verify(decoded);
  } catch (const Error&) {
    throw;
  } catch (const std::exception& e) {
    LogAt(log_level_, LogLevel::kInfo, "sign-in token refused", {{"reason", e.what()}});
    throw TokenRefused();
  }

  warpsql::Principal principal;
  principal.auth_method = "token";
  principal.name = decoded.has_payload_claim("email")
                       ? decoded.get_payload_claim("email").as_string()
                       : (decoded.has_subject() ? decoded.get_subject() : "");
  if (principal.name.empty()) {
    throw TokenRefused();
  }
  // Which users a trusted issuer may sign in (globs such as *@example.com).
  if (!MatchesAnyIgnoringCase(authorized_emails_, principal.name)) {
    LogAt(log_level_, LogLevel::kInfo, "sign-in token names an unauthorized user", {{"user", principal.name}});
    throw Error(ErrorCode::kInvalidCredentials,
                "User '" + principal.name + "' is not authorized. Contact your administrator.");
  }
  if (decoded.has_payload_claim("role")) {
    principal.role = decoded.get_payload_claim("role").as_string();
  } else if (!default_role_.empty()) {
    principal.role = default_role_;
  } else {
    LogAt(log_level_, LogLevel::kInfo, "sign-in token has no role", {{"user", principal.name}});
    throw TokenRefused();
  }
  if (principal.role != "admin" && principal.role != "user" && principal.role != "readonly") {
    throw Error(ErrorCode::kInvalidCredentials, "unknown role '" + principal.role + "'");
  }
  if (decoded.has_payload_claim("catalog_access")) {
    if (!license_.Has("catalog_permissions")) {
      throw Error(ErrorCode::kInvalidCredentials,
                  LicenseRequired("catalog_permissions", "Per-catalog permissions (catalog_access claim)"));
    }
    principal.claims["catalog_access"] = decoded.get_payload_claim("catalog_access").to_json();
  }
  // The identity provider's groups (CREATE GROUP MAPPING turns them into roles): a list,
  // or one group as a plain string.
  if (decoded.has_payload_claim(groups_claim_)) {
    const auto value = decoded.get_payload_claim(groups_claim_).to_json();
    boost::json::array groups;
    if (value.is_array()) {
      for (const auto& group : value.as_array()) {
        if (group.is_string()) groups.push_back(group);
      }
    } else if (value.is_string()) {
      groups.push_back(value);
    }
    if (!groups.empty()) {
      principal.claims["idp"] = boost::json::object{
          {"iss", decoded.has_issuer() ? decoded.get_issuer() : std::string()},
          {"groups", std::move(groups)}};
    }
  }
  // Under access control a sign-in token signs in one of the store's users (the
  // administrator role aside); an administrator creates them with CREATE USER.
  if (access_ && principal.role != "admin") {
    access_->RequireStoreUser(principal.name);
  }
  LogAt(log_level_, LogLevel::kInfo, "signed in", {{"user", principal.name}, {"method", "token"},
                                                   {"role", principal.role}});
  return principal;
}

template <class Decoded, class Verifier>
void Authenticator::AllowSigningKey(const Decoded& decoded, Verifier& verifier) {
  if (jwks_ == nullptr) {
    verifier.allow_algorithm(jwt::algorithm::rs256(verify_key_pem_, "", "", ""));
    return;
  }
  if (!license_.Has("external_auth")) {
    throw Error(ErrorCode::kInvalidCredentials,
                LicenseRequired("external_auth", "Sign-in with an identity provider's tokens (JWKS)"));
  }
  // The provider says which of its keys signed the token; its JWK may fix the algorithm.
  if (!decoded.has_header_claim("kid")) {
    throw std::runtime_error("the token has no 'kid' header, which JWKS verification needs");
  }
  const Jwks::Key key = jwks_->Find(decoded.get_header_claim("kid").as_string());
  std::string algorithm = key.algorithm;
  if (algorithm.empty() && decoded.has_header_claim("alg")) algorithm = decoded.get_header_claim("alg").as_string();
  if (algorithm == "RS256" || algorithm.empty()) {
    verifier.allow_algorithm(jwt::algorithm::rs256(key.pem, "", "", ""));
  } else if (algorithm == "RS384") {
    verifier.allow_algorithm(jwt::algorithm::rs384(key.pem, "", "", ""));
  } else if (algorithm == "RS512") {
    verifier.allow_algorithm(jwt::algorithm::rs512(key.pem, "", "", ""));
  } else if (algorithm == "ES256") {
    verifier.allow_algorithm(jwt::algorithm::es256(key.pem, "", "", ""));
  } else if (algorithm == "ES384") {
    verifier.allow_algorithm(jwt::algorithm::es384(key.pem, "", "", ""));
  } else if (algorithm == "ES512") {
    verifier.allow_algorithm(jwt::algorithm::es512(key.pem, "", "", ""));
  } else {
    throw std::runtime_error("unsupported signing algorithm '" + algorithm + "'");
  }
}

boost::json::array Authenticator::GrantTypes() {
  boost::json::array grants{"password"};
  if (AcceptsTokens()) {
    grants.push_back("token_exchange");
  }
  return grants;
}

}  // namespace hardcharger
