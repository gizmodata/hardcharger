// Keys of an OpenID Connect identity provider: its
// discovery document, and the JSON Web Key Set that verifies the ID tokens it signs.
#pragma once

#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>

namespace hardcharger {

struct OidcEndpoints {
  std::string jwks_uri;
  std::string authorization_endpoint;
  std::string token_endpoint;
};

// Reads <issuer>/.well-known/openid-configuration. Throws std::runtime_error.
OidcEndpoints DiscoverOidc(const std::string& issuer);

class Jwks {
 public:
  struct Key {
    std::string pem;        // the public key
    std::string algorithm;  // the JWK's "alg", may be empty
  };

  explicit Jwks(std::string uri, std::chrono::minutes ttl = std::chrono::minutes(5));

  // The key `kid` names. Fetches the set when it is stale or lacks the key (a provider
  // rotating keys), at most every few seconds. Throws std::runtime_error.
  Key Find(const std::string& kid);

  const std::string& uri() const { return uri_; }

  // The RSA and EC keys of a JWKS document; keys without a kid, or of other types, are
  // skipped. Throws std::runtime_error when none is usable.
  static std::unordered_map<std::string, Key> Parse(const std::string& document);

 private:
  const std::string uri_;
  const std::chrono::minutes ttl_;
  std::mutex mutex_;
  std::unordered_map<std::string, Key> keys_;
  std::chrono::steady_clock::time_point fetched_at_{};
  bool fetched_ = false;
};

}  // namespace hardcharger
