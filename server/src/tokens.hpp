// Access tokens Hardcharger issues: JWTs signed with the server's secret key (HS256),
// so they survive a restart and, with a shared key, work on every replica of a cluster.
// The claims are GizmoSQL 1.x's, so 1.x tooling that mints its own tokens keeps working.
#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>

#include "warpsql/backend.hpp"

namespace hardcharger {

namespace authz {
class AccessControl;
}

class JwtTokenService : public warpsql::TokenService {
 public:
  // `access`: access control, or null. With it, every token of a store user is checked
  // against the store on every request, so a dropped or disabled user, a changed
  // password or a revoked scoped token stops working at once, on every replica.
  JwtTokenService(std::string secret_key, std::chrono::seconds lifetime, std::string instance_id,
                  bool allow_cross_instance, std::shared_ptr<authz::AccessControl> access = nullptr);

  Issued Issue(const warpsql::Principal& principal) override;
  warpsql::Principal Validate(std::string_view token) override;

  // A scoped token (CREATE SCOPED TOKEN) for `user_name`: an access token limited by the
  // store's record `scope_id`, valid until `expires_at` (seconds since the epoch).
  std::string IssueScoped(const std::string& user_name, const std::string& scope_id,
                          int64_t expires_at);

 private:
  std::string secret_key_;
  std::chrono::seconds lifetime_;
  std::string instance_id_;
  bool allow_cross_instance_;
  std::shared_ptr<authz::AccessControl> access_;
};

// A random 256-bit key, hex encoded, for servers started without a secret key.
std::string RandomSecretKey();

// A random UUID (version 4).
std::string RandomUuid();

}  // namespace hardcharger
