#include "auth.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <fstream>
#include <optional>
#include <sstream>
#include <stdexcept>

#include "audit.hpp"
#include "logging.hpp"
#include "patterns.hpp"

namespace hardcharger {

namespace {

using Traits = jwt::traits::boost_json;
using warpsql::Error;
using warpsql::ErrorCode;

// User name under which a client sends a sign-in token as the password.
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

// A user name a client sent, for the log: cut at 256 bytes (it may be 64 MiB).
std::string LoggableName(std::string_view name) {
  constexpr std::size_t kMax = 256;
  if (name.size() <= kMax) return std::string(name);
  return std::string(name.substr(0, kMax)) + "... (" + std::to_string(name.size()) + " bytes)";
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
  // [{"catalog": "prod_*", "access": "read"}, ...]; a malformed rule grants nothing, and
  // so does a claim that is there but isn't a list (null, a string, one object): rules
  // with no rule in them, never no rules (which, for an administrator, is no limit).
  if (const auto* rules = principal.claims.if_contains("catalog_access")) {
    std::vector<authz::CatalogAccessRule> parsed;
    for (const auto& rule : rules->is_array() ? rules->as_array() : boost::json::array{}) {
      const auto* object = rule.if_object();
      const auto* catalog = object ? object->if_contains("catalog") : nullptr;
      const auto* access = object ? object->if_contains("access") : nullptr;
      parsed.push_back({catalog && catalog->is_string() ? std::string(catalog->as_string()) : std::string(),
                        access && access->is_string() ? std::string(access->as_string()) : "none"});
    }
    who.catalog_access = std::move(parsed);
  }
  who.scope_id = text("scope_id");
  who.certificate_thumbprint = principal.certificate_thumbprint;
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

Authenticator::Authenticator(const Config& config,
                             std::shared_ptr<authz::AccessControl> access,
                             std::function<warpsql::Principal(std::string_view)> server_tokens,
                             std::shared_ptr<Jwks> jwks)
    : username_(config.admin_username),
      password_(config.admin_password),
      jwks_(std::move(jwks)),
      allowed_issuer_(config.token_allowed_issuer),
      allowed_audience_(config.token_allowed_audience),
      default_role_(config.token_default_role),
      groups_claim_(config.token_groups_claim),
      authorized_emails_(config.token_authorized_emails),
      auto_provision_users_(config.token_auto_provision_users),
      auto_provision_issuer_(config.token_auto_provision_issuer),
      log_level_(ParseLogLevel(config.auth_log_level)),
      access_(std::move(access)),
      server_tokens_(std::move(server_tokens)),
      mutual_tls_(!config.tls_cert.empty() && !config.mtls_ca_cert.empty() && config.mtls_certificate_sign_in),
      certificate_admins_(config.mtls_admin_identities) {
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
    // sent as the password of the user "token".
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
    LogAt(log_level_, LogLevel::kInfo, "signed in", {{"user", LoggableName(username)}, {"method", "password"}});
    return warpsql::Principal{std::string(username), "admin", "password", {}, {}};
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
                                {{"credential", store_user->credential}}, {}};
    }
  }
  // The name is the client's, unauthenticated: cut, so a refusal can't fill the log.
  LogAt(log_level_, LogLevel::kInfo, "sign-in refused", {{"user", LoggableName(username)}, {"method", "password"}});
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
  // administrator role aside); an administrator creates them with CREATE USER, or, with
  // token_auto_provision_users, the first sign-in of a token from that issuer does.
  if (access_ && principal.role != "admin") {
    const std::string issuer = decoded.has_issuer() ? decoded.get_issuer() : std::string();
    authz::AccessControl::TokenUser user;
    if (auto_provision_users_ && issuer == auto_provision_issuer_) {
      user = access_->ProvisionStoreUser(principal.name, principal.role, issuer);
    } else {
      user = access_->RequireStoreUser(principal.name);
    }
    // A user provisioned from a readonly token stays readonly, whatever later tokens say,
    // until an administrator removes its provisioned_role attribute.
    if (user.readonly) principal.role = "readonly";
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

warpsql::Principal Authenticator::AuthenticateCertificate(std::string_view identity,
                                                          const warpsql::ClientCertificate& certificate) {
  const std::string name(identity);
  LogFields audit{{"kind", "authentication"},
                  {"method", "client_certificate"},
                  {"certificate_identity", name},
                  {"certificate_subject", certificate.subject},
                  {"certificate_issuer", certificate.issuer},
                  {"certificate_serial", certificate.serial},
                  {"certificate_thumbprint", certificate.thumbprint}};
  auto signed_in = [&](const warpsql::Principal& principal) {
    LogAt(log_level_, LogLevel::kInfo, "signed in",
          {{"user", principal.name}, {"method", "client_certificate"}, {"role", principal.role},
           {"certificate_identity", name}});
    audit.emplace_back("status", "allowed");
    audit.emplace_back("user", principal.name);
    audit.emplace_back("role", principal.role);
    Audit(LogLevel::kInfo, "signed in with a client certificate", audit);
    return principal;
  };
  // The administrator's certificate identities (compared ignoring ASCII case, as user
  // names are), with or without access control.
  for (const auto& admin : certificate_admins_) {
    // URI identities exactly, e-mail and CN ignoring ASCII case.
    if (warpsql::CertificateIdentitiesMatch(admin, name)) {
      // A store user of the same name would share the administrator's sessions (handles
      // belong to a principal by name): neither sign-in may be ambiguous.
      if (access_ && access_->CertificateUser(name)) {
        throw Error(ErrorCode::kUnknownCertificateIdentity,
                    "the client certificate's identity '" + name +
                        "' is both one of mtls_admin_identities and a user of the access-control "
                        "store; an administrator drops the user or removes the identity");
      }
      return signed_in(warpsql::Principal{admin, "admin", "client_certificate", {}, {}});
    }
  }
  // A URI identity differing from an administrator identity only in case is another
  // identity (URI paths are case-sensitive): refused, saying so.
  if (warpsql::IsUriIdentity(name)) {
    for (const auto& admin : certificate_admins_) {
      if (admin.size() == name.size() &&
          std::equal(admin.begin(), admin.end(), name.begin(), [](char a, char b) {
            return std::tolower(static_cast<unsigned char>(a)) == std::tolower(static_cast<unsigned char>(b));
          })) {
        throw Error(ErrorCode::kUnknownCertificateIdentity,
                    "the client certificate's URI identity '" + name + "' differs from '" + admin +
                        "' only in case; URI identities must match exactly");
      }
    }
  }
  // Under access control, one of the store's enabled users (a password is not needed).
  if (access_) {
    if (auto user = access_->CertificateUser(name)) {
      return signed_in(warpsql::Principal{*user, "user", "client_certificate", {}, {}});
    }
  }
  // The SDK reports the refusal as a security event (logged and audited, rate-limited per
  // source, in main.cc): nothing is logged here, so refusals can't fill the disk.
  throw Error(ErrorCode::kUnknownCertificateIdentity,
              "the client certificate's identity '" + name + "' is not a user of this server" +
                  (access_ ? ". An administrator creates it with CREATE USER" : ""));
}

boost::json::array Authenticator::GrantTypes() {
  boost::json::array grants{"password"};
  if (AcceptsTokens()) {
    grants.push_back("token_exchange");
  }
  // Certificate sign-in, when some identity could use it.
  if (mutual_tls_ && (!certificate_admins_.empty() || access_)) {
    grants.push_back("client_certificate");
  }
  return grants;
}

}  // namespace hardcharger
