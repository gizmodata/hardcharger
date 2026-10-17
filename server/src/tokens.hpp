// Access tokens Hardcharger issues: JWTs signed with the server's secret key (HS256),
// so they survive a restart and, with a shared key, work on every replica of a cluster.
// The claims are GizmoSQL 1.x's, so 1.x tooling that mints its own tokens keeps working.
#pragma once

#include <chrono>
#include <string>

#include "warpsql/backend.hpp"

namespace hardcharger {

class JwtTokenService : public warpsql::TokenService {
 public:
  JwtTokenService(std::string secret_key, std::chrono::seconds lifetime, std::string instance_id,
                  bool allow_cross_instance);

  Issued Issue(const warpsql::Principal& principal) override;
  warpsql::Principal Validate(std::string_view token) override;

 private:
  std::string secret_key_;
  std::chrono::seconds lifetime_;
  std::string instance_id_;
  bool allow_cross_instance_;
};

// A random 256-bit key, hex encoded, for servers started without a secret key.
std::string RandomSecretKey();

// A random UUID (version 4).
std::string RandomUuid();

}  // namespace hardcharger
