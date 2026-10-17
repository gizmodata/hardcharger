// GizmoData Enterprise license keys: the same RS256 JWTs GizmoSQL 1.x accepts, so
// existing keys work unchanged.
#pragma once

#include <chrono>
#include <set>
#include <string>

namespace hardcharger {

struct License {
  bool enterprise = false;  // false: Core edition (no key)
  std::string id;           // jti
  std::string licensee;     // sub (customer email)
  std::string organization;  // aud (customer name)
  std::chrono::system_clock::time_point expires;
  std::set<std::string> features;
  // Signed with the tests' key, which only a test build trusts (never shipped).
  bool test_key = false;

  bool Has(const std::string& feature) const { return enterprise && features.contains(feature); }
  std::string Edition() const {
    return enterprise ? (test_key ? "Enterprise (test license)" : "Enterprise") : "Core";
  }
};

// Verifies `key` (empty: Core edition). Throws std::runtime_error("License Error: ...")
// for any invalid, expired or incomplete key: a bad key stops the server rather than
// silently running without the features it names.
License LoadLicense(const std::string& key);

// The key from config: the inline key wins over the file (as in 1.x), even when it is
// empty or invalid. `inline_set`: license_key was given at all (an empty value too).
std::string ReadLicenseKey(const std::string& inline_key, const std::string& key_file, bool inline_set = false);

// Does this build trust test licenses (HARDCHARGER_TRUST_TEST_LICENSES)?
bool TrustsTestLicenses();

// The message for an option that needs a feature the license doesn't grant.
std::string LicenseRequired(const std::string& feature, const std::string& what);

}  // namespace hardcharger
