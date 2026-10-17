#include "tokens.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/rand.h>

#include <array>
#include <format>
#include <stdexcept>

namespace hardcharger {

namespace {

using Traits = jwt::traits::boost_json;
using warpsql::Error;
using warpsql::ErrorCode;

// Issuers whose server-signed tokens Hardcharger accepts: its own, and GizmoSQL 1.x's
// (1.x tooling mints server tokens with iss=gizmosql).
constexpr std::string_view kIssuer = "hardcharger";
constexpr std::string_view kLegacyIssuer = "gizmosql";

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
                                 std::string instance_id, bool allow_cross_instance)
    : secret_key_(std::move(secret_key)),
      lifetime_(lifetime),
      instance_id_(std::move(instance_id)),
      allow_cross_instance_(allow_cross_instance) {}

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
  if (const auto* access = principal.claims.if_contains("catalog_access")) {
    token.set_payload_claim("catalog_access", jwt::basic_claim<Traits>(*access));
  }
  return {token.sign(jwt::algorithm::hs256{secret_key_}), lifetime_};
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
    if (issuer != kIssuer && issuer != kLegacyIssuer) {
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
  if (decoded.has_payload_claim("catalog_access")) {
    principal.claims["catalog_access"] = decoded.get_payload_claim("catalog_access").to_json();
  }
  if (principal.name.empty()) {
    throw invalid("a valid access token is required");
  }
  return principal;
}

}  // namespace hardcharger
