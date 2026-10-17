// Password hashing for access-control users: PBKDF2-HMAC-SHA256 with a random
// 16-byte salt, stored as "pbkdf2-sha256$<iterations>$<salt b64>$<hash b64>".

#pragma once

#include <string>

#include <arrow/result.h>

namespace hardcharger::authz {

// OWASP's current recommendation for PBKDF2-HMAC-SHA256.
constexpr int kDefaultPasswordIterations = 600000;

arrow::Result<std::string> HashPassword(const std::string& password,
                                        int iterations = kDefaultPasswordIterations);

// Constant-time comparison; false for a malformed stored hash.
bool VerifyPassword(const std::string& password, const std::string& stored_hash);

}  // namespace hardcharger::authz
