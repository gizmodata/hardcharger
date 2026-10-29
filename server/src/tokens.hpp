// Access tokens Hardcharger issues: JWTs signed with the server's secret key (HS256),
// so they survive a restart and, with a shared key, work on every replica of a cluster.
#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_set>

#include "logging.hpp"

#include "chargewire/backend.hpp"

namespace hardcharger {

namespace authz {
class AccessControl;
}

class JwtTokenService : public chargewire::TokenService {
 public:
  // `access`: access control, or null. With it, every token of a store user is checked
  // against the store on every request, so a dropped or disabled user, a changed
  // password or a revoked scoped token stops working at once, on every replica.
  JwtTokenService(std::string secret_key, std::chrono::seconds lifetime, std::string instance_id,
                  bool allow_cross_instance, LogLevel log_level = LogLevel::kInfo,
                  std::shared_ptr<authz::AccessControl> access = nullptr);

  Issued Issue(const chargewire::Principal& principal) override;
  chargewire::Principal Validate(std::string_view token) override;
  // A certificate-bound token (spec §4.4) carries RFC 8705's cnf claim,
  // {"x5t#S256": thumbprint}, signed with the rest; Validate() returns it.
  bool BindsCertificates() const override { return true; }
  // Logs a token's use (its first at INFO, later ones at DEBUG).
  void Accepted(std::string_view token, const chargewire::Principal& principal) override;

  // A scoped token (CREATE SCOPED TOKEN) for `user_name`: an access token limited by the
  // store's record `scope_id`, valid until `expires_at` (seconds since the epoch).
  // Bound to `certificate_thumbprint` (the minter's binding) unless that is empty.
  std::string IssueScoped(const std::string& user_name, const std::string& scope_id,
                          int64_t expires_at, const std::string& certificate_thumbprint = {});

 private:
  std::string secret_key_;
  std::chrono::seconds lifetime_;
  std::string instance_id_;
  bool allow_cross_instance_;
  LogLevel log_level_;
  std::shared_ptr<authz::AccessControl> access_;
  // Tokens seen validated (SHA-256), so a token's first use logs at INFO and its
  // repeats only at DEBUG. Bounded: cleared when full.
  std::mutex seen_mutex_;
  std::unordered_set<std::string> seen_;
};

// A random 256-bit key, hex encoded, for servers started without a secret key.
std::string RandomSecretKey();

// A random UUID (version 4).
std::string RandomUuid();

}  // namespace hardcharger
