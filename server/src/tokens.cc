#include "tokens.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/evp.h>
#include <openssl/rand.h>

#include <array>
#include <format>
#include <stdexcept>

#include "auth.hpp"
#include "authz/authz.hpp"

namespace hardcharger {

namespace {

using Traits = jwt::traits::boost_json;
using warpsql::Error;
using warpsql::ErrorCode;

// The issuer of the tokens this server signs; it accepts no other.
constexpr std::string_view kIssuer = "hardcharger";

std::string Hex(const unsigned char* data, std::size_t size) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  for (std::size_t i = 0; i < size; i++) {
    out += kHex[data[i] >> 4];
    out += kHex[data[i] & 15];
  }
  return out;
}

}  // namespace

std::string RandomSecretKey() {
  std::array<unsigned char, 32> bytes{};
  if (RAND_bytes(bytes.data(), static_cast<int>(bytes.size())) != 1) {
    throw std::runtime_error("no randomness available from OpenSSL");
  }
  return Hex(bytes.data(), bytes.size());
}

std::string RandomUuid() {
  std::array<unsigned char, 16> b{};
  if (RAND_bytes(b.data(), static_cast<int>(b.size())) != 1) {
    throw std::runtime_error("no randomness available from OpenSSL");
  }
  b[6] = static_cast<unsigned char>((b[6] & 0x0f) | 0x40);  // version 4
  b[8] = static_cast<unsigned char>((b[8] & 0x3f) | 0x80);  // RFC 4122 variant
  const std::string hex = Hex(b.data(), b.size());
  return std::format("{}-{}-{}-{}-{}", hex.substr(0, 8), hex.substr(8, 4), hex.substr(12, 4),
                     hex.substr(16, 4), hex.substr(20));
}

JwtTokenService::JwtTokenService(std::string secret_key, std::chrono::seconds lifetime,
                                 std::string instance_id, bool allow_cross_instance, LogLevel log_level,
                                 std::shared_ptr<authz::AccessControl> access)
    : secret_key_(std::move(secret_key)),
      lifetime_(lifetime),
      instance_id_(std::move(instance_id)),
      allow_cross_instance_(allow_cross_instance),
      log_level_(log_level),
      access_(std::move(access)) {}

warpsql::TokenService::Issued JwtTokenService::Issue(const warpsql::Principal& principal) {
  const auto now = std::chrono::system_clock::now();
  auto token = jwt::create<Traits>()
                   .set_type("JWT")
                   .set_issuer(std::string(kIssuer))
                   .set_id("hardcharger-" + RandomUuid())
                   .set_subject(principal.name)
                   .set_issued_at(now)
                   .set_expires_at(now + lifetime_)
                   .set_payload_claim("role", jwt::basic_claim<Traits>(principal.role))
                   .set_payload_claim("auth_method", jwt::basic_claim<Traits>(principal.auth_method))
                   .set_payload_claim("instance_id", jwt::basic_claim<Traits>(instance_id_));
  // Claims access control reads: the password fingerprint
  // a store user signed in with, a scoped token's record, the identity provider's groups.
  for (const char* claim : {"catalog_access", "credential", "scope_id", "idp"}) {
    if (const auto* value = principal.claims.if_contains(claim)) {
      token.set_payload_claim(claim, jwt::basic_claim<Traits>(*value));
    }
  }
  // Bound to a client certificate (RFC 8705 §3): the SDK checks every request's
  // connection presents it.
  if (!principal.certificate_thumbprint.empty()) {
    token.set_payload_claim(
        "cnf", jwt::basic_claim<Traits>(boost::json::value(
                   boost::json::object{{"x5t#S256", principal.certificate_thumbprint}})));
  }
  // A token never outlives the scoped token it was exchanged for.
  auto lifetime = lifetime_;
  if (const auto* expires = principal.claims.if_contains("expires_at"); expires && expires->is_int64()) {
    const auto until = std::chrono::system_clock::time_point(std::chrono::seconds(expires->as_int64()));
    if (until < now + lifetime_) {
      lifetime = std::chrono::duration_cast<std::chrono::seconds>(until - now);
      token.set_expires_at(until);
    }
  }
  return {token.sign(jwt::algorithm::hs256{secret_key_}), lifetime};
}

std::string JwtTokenService::IssueScoped(const std::string& user_name, const std::string& scope_id,
                                         int64_t expires_at, const std::string& certificate_thumbprint) {
  auto token = jwt::create<Traits>()
      .set_type("JWT")
      .set_issuer(std::string(kIssuer))
      .set_id("hardcharger-scoped-" + RandomUuid())
      .set_subject(user_name)
      .set_issued_at(std::chrono::system_clock::now())
      .set_expires_at(std::chrono::system_clock::time_point(std::chrono::seconds(expires_at)))
      .set_payload_claim("role", jwt::basic_claim<Traits>(std::string("user")))
      .set_payload_claim("auth_method", jwt::basic_claim<Traits>(std::string("scoped_token")))
      .set_payload_claim("instance_id", jwt::basic_claim<Traits>(instance_id_))
      .set_payload_claim("scope_id", jwt::basic_claim<Traits>(scope_id));
  if (!certificate_thumbprint.empty()) {
    token.set_payload_claim("cnf", jwt::basic_claim<Traits>(boost::json::value(
                                       boost::json::object{{"x5t#S256", certificate_thumbprint}})));
  }
  return token.sign(jwt::algorithm::hs256{secret_key_});
}

warpsql::Principal JwtTokenService::Validate(std::string_view token) {
  auto invalid = [](std::string_view why) {
    return Error(ErrorCode::kInvalidToken, std::string(why));
  };
  if (token.empty()) {
    throw invalid("a valid access token is required");
  }
  jwt::decoded_jwt<Traits> decoded = [&] {
    try {
      return jwt::decode<Traits>(std::string(token));
    } catch (const std::exception&) {
      throw invalid("a valid access token is required");
    }
  }();
  try {
    const std::string issuer = decoded.has_issuer() ? decoded.get_issuer() : "";
    if (issuer != kIssuer) {
      throw invalid("a valid access token is required");
    }
    jwt::verify<Traits>()
        .allow_algorithm(jwt::algorithm::hs256{secret_key_})
        .with_issuer(issuer)
        .verify(decoded);
  } catch (const Error&) {
    throw;
  } catch (const std::exception&) {
    // Forged, tampered, expired or signed with another key: one answer for all.
    throw invalid("a valid access token is required");
  }

  if (decoded.has_payload_claim("instance_id") && !allow_cross_instance_) {
    const auto issued_by = decoded.get_payload_claim("instance_id").as_string();
    if (!issued_by.empty() && issued_by != instance_id_) {
      throw invalid("this token was issued by another server instance");
    }
  }
  warpsql::Principal principal;
  principal.name = decoded.get_subject();
  if (decoded.has_payload_claim("role")) principal.role = decoded.get_payload_claim("role").as_string();
  if (decoded.has_payload_claim("auth_method")) {
    principal.auth_method = decoded.get_payload_claim("auth_method").as_string();
  }
  for (const char* claim : {"catalog_access", "credential", "scope_id", "idp"}) {
    if (decoded.has_payload_claim(claim)) {
      principal.claims[claim] = decoded.get_payload_claim(claim).to_json();
    }
  }
  if (decoded.has_payload_claim("cnf")) {
    // A cnf this server can't read binds the token to nothing it can check: refused.
    const auto cnf = decoded.get_payload_claim("cnf").to_json();
    const auto* thumbprint = cnf.is_object() ? cnf.as_object().if_contains("x5t#S256") : nullptr;
    if (thumbprint == nullptr || !thumbprint->is_string() || thumbprint->as_string().empty()) {
      throw invalid("a valid access token is required");
    }
    principal.certificate_thumbprint = std::string(thumbprint->as_string());
  }
  if (principal.name.empty()) {
    throw invalid("a valid access token is required");
  }
  if (!access_) {
    // Without access control nothing could hold a scoped token to its scope.
    if (principal.claims.contains("scope_id")) {
      throw invalid("this server does not run access control, so it accepts no scoped tokens");
    }
  } else {
    if (const auto* scope = principal.claims.if_contains("scope_id")) {
      if (!scope->is_string()) throw invalid("a valid access token is required");
      // (After this insertion `scope` may dangle: it is not used again.)
      principal.claims["expires_at"] = std::chrono::duration_cast<std::chrono::seconds>(
                                           decoded.get_expires_at().time_since_epoch())
                                           .count();
    }
    access_->CheckToken(IdentityOf(principal));
  }
  bool first_use = false;
  {
    constexpr std::size_t kMaxSeen = 50000;
    std::array<unsigned char, 32> digest{};
    unsigned int length = 0;
    EVP_Digest(token.data(), token.size(), digest.data(), &length, EVP_sha256(), nullptr);
    std::string key(reinterpret_cast<const char*>(digest.data()), length);
    std::lock_guard lock(seen_mutex_);
    if (seen_.size() >= kMaxSeen) seen_.clear();
    first_use = seen_.insert(std::move(key)).second;
  }
  LogAt(log_level_, first_use ? LogLevel::kInfo : LogLevel::kDebug, "Bearer Token was validated successfully",
        {{"user", principal.name}, {"role", principal.role}, {"first_use", first_use ? "true" : "false"}});
  return principal;
}

}  // namespace hardcharger
