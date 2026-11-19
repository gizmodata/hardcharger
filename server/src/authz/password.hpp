// Password hashing for access-control users: PBKDF2-HMAC-SHA256 with a random
// 16-byte salt, stored as "pbkdf2-sha256$<iterations>$<salt b64>$<hash b64>".

#pragma once

#include <arrow/result.h>
#include <arrow/status.h>

#include <cstdint>
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

// What a new password must be (NIST SP 800-63B by default: a length, no composition rules,
// no expiry), and how long one lasts. The rules are checked only when a password is set or
// changed, so existing passwords keep working.
struct PasswordPolicy {
  int64_t min_length = 12;          // password_min_length: characters (UTF-8 code points)
  int64_t min_character_classes = 0;  // password_min_character_classes: of lower, upper, digit, other (0-4)
  // password_max_age_days: a password older than this signs in held, like a temporary one,
  // until its user changes it. 0: never.
  int64_t max_age_days = 0;
  // password_temporary_max_age_hours: a temporary password (one someone else set) older
  // than this no longer signs in or can be replaced; an administrator sets a new one. 0: no limit.
  int64_t temporary_max_age_hours = 72;
};

// OK, or Invalid saying what the password lacks (never quoting it).
arrow::Status CheckPasswordPolicy(const std::string& password, const PasswordPolicy& policy);

}  // namespace hardcharger::authz
