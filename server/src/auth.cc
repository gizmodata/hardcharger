#include "auth.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <array>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "logging.hpp"

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

Authenticator::Authenticator(const Config& config, const License& license)
    : username_(config.username),
      password_(config.password),
      allowed_issuer_(config.token_allowed_issuer),
      allowed_audience_(config.token_allowed_audience),
      default_role_(config.token_default_role),
      license_(license) {
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
  if (username == kTokenUser && !verify_key_pem_.empty()) {
    return ExchangeToken(password);
  }
  const bool user_ok = SameSecret(username, username_);
  const bool password_ok = SameSecret(password, password_);
  if (!user_ok || !password_ok) {
    Log(LogLevel::kInfo, "sign-in refused", {{"user", std::string(username)}, {"method", "password"}});
    throw Refused();
  }
  return warpsql::Principal{std::string(username), "admin", "password", {}};
}

warpsql::Principal Authenticator::ExchangeToken(std::string_view subject_token) {
  if (verify_key_pem_.empty()) {
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
    auto verifier = jwt::verify<Traits>().allow_algorithm(
        jwt::algorithm::rs256(verify_key_pem_, "", "", ""));
    if (!allowed_issuer_.empty()) verifier.with_issuer(allowed_issuer_);
    if (!allowed_audience_.empty()) verifier.with_audience(allowed_audience_);
    verifier.verify(decoded);
  } catch (const std::exception& e) {
    Log(LogLevel::kInfo, "sign-in token refused", {{"reason", e.what()}});
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
  if (decoded.has_payload_claim("role")) {
    principal.role = decoded.get_payload_claim("role").as_string();
  } else if (!default_role_.empty()) {
    principal.role = default_role_;
  } else {
    Log(LogLevel::kInfo, "sign-in token has no role", {{"user", principal.name}});
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
  return principal;
}

boost::json::array Authenticator::GrantTypes() {
  boost::json::array grants{"password"};
  if (!verify_key_pem_.empty()) {
    grants.push_back("token_exchange");
  }
  return grants;
}

}  // namespace hardcharger
