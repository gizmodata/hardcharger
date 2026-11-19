// Password hashing for access-control users: PBKDF2-HMAC-SHA256 with a random
// 16-byte salt, stored as "pbkdf2-sha256$<iterations>$<salt b64>$<hash b64>".

#pragma once

#include <arrow/result.h>

#include <optional>
#include <string>

namespace hardcharger::authz {

// OWASP's current recommendation for PBKDF2-HMAC-SHA256.
constexpr int kDefaultPasswordIterations = 600000;

arrow::Result<std::string> HashPassword(const std::string& password,
                                        int iterations = kDefaultPasswordIterations);

// Constant-time comparison; false for a malformed stored hash.
bool VerifyPassword(const std::string& password, const std::string& stored_hash);

// A short fingerprint of a stored password hash (the first 16 bytes of its SHA-256, in hex),
// which says which password something was made for without carrying the hash: an access
// token's `credential` claim, a temporary password's mark. Empty for no password.
std::string PasswordFingerprint(const std::optional<std::string>& password_hash);

}  // namespace hardcharger::authz
