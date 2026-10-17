#include "license.hpp"

#ifdef HARDCHARGER_TRUST_TEST_LICENSES
#include "test_license.hpp"
#endif

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace hardcharger {

namespace {

using Traits = jwt::traits::boost_json;

// GizmoData's license signing key (RSA-2048, public half). Same key as GizmoSQL 1.x.
constexpr const char* kGizmoDataPublicKey = R"(-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzZ8Eon6y/KQVhmEycg+j
OSe6Kga/mv3SCZVAg6t0DnmDvg3hK9tbXogIDicxKmfGyAVy9O0shJT3lh9dWCAL
VzEdWL/PHnuHlccTS8+W204ABdKvaVlPxBw/+tHjcgzjLfkJW9Fw9siVk/qH4CLf
tTr2u14qQqKXfQ9gzP3aeLyZIRhDM4ObhwoidlDDtKLO04aCbih67uUuxnmUOsrN
hJ2TJ95/bNbNJHV0pLyANBLKpLijr+ig+POT3zSmIVuejAb4FAwtDPHGoQRdMGus
oCHME83BVIOSn/x4NtjZg0DV1/tnQh9wm8MLj3Z91bMMZacQFywWDKG4w8hXbKmC
mwIDAQAB
-----END PUBLIC KEY-----)";

constexpr const char* kIssuer = "GizmoData LLC";

std::string Trim(const std::string& text) {
  const auto start = text.find_first_not_of(" \t\r\n");
  if (start == std::string::npos) return {};
  const auto end = text.find_last_not_of(" \t\r\n");
  return text.substr(start, end - start + 1);
}

[[noreturn]] void Fail(const std::string& why) { throw std::runtime_error("License Error: " + why); }

}  // namespace

std::string ReadLicenseKey(const std::string& inline_key, const std::string& key_file) {
  if (!Trim(inline_key).empty()) {
    return Trim(inline_key);
  }
  if (key_file.empty()) {
    return {};
  }
  std::ifstream in(key_file);
  if (!in) {
    Fail("cannot read license key file " + key_file);
  }
  std::stringstream contents;
  contents << in.rdbuf();
  return Trim(contents.str());
}

License LoadLicense(const std::string& key) {
  License license;
  if (key.empty()) {
    return license;  // Core edition
  }
  jwt::decoded_jwt<Traits> decoded = [&] {
    try {
      return jwt::decode<Traits>(key);
    } catch (const std::exception& e) {
      Fail(std::string("the license key is not a valid token: ") + e.what());
    }
  }();
  for (const char* claim : {"jti", "iss", "sub", "aud", "iat", "exp"}) {
    if (!decoded.has_payload_claim(claim)) {
      Fail(std::string("License missing required claim: ") + claim);
    }
  }
  auto verify = [&](const char* public_key) {
    jwt::verify<Traits>()
        .allow_algorithm(jwt::algorithm::rs256(public_key, "", "", ""))
        .with_issuer(kIssuer)
        .verify(decoded);
  };
  try {
#ifdef HARDCHARGER_TEST_LICENSE_PUBLIC_KEY
    try {
      verify(kGizmoDataPublicKey);
    } catch (const jwt::error::signature_verification_exception&) {
      verify(HARDCHARGER_TEST_LICENSE_PUBLIC_KEY);
      license.test_key = true;
    }
#else
    verify(kGizmoDataPublicKey);
#endif
  } catch (const jwt::error::token_verification_exception& e) {
    if (e.code() == jwt::error::token_verification_error::token_expired) {
      Fail("License has expired. Please contact sales@gizmodata.com to renew.");
    }
    Fail(std::string("the license key could not be verified: ") + e.what());
  } catch (const std::exception& e) {
    Fail(std::string("the license key could not be verified: ") + e.what());
  }

  license.enterprise = true;
  license.id = decoded.get_id();
  license.licensee = decoded.get_subject();
  const auto audience = decoded.get_audience();
  license.organization = audience.empty() ? "" : *audience.begin();
  license.expires = decoded.get_expires_at();
  if (decoded.has_payload_claim("features")) {
    for (const auto& feature : decoded.get_payload_claim("features").as_array()) {
      license.features.insert(std::string(feature.as_string()));
    }
  }
  return license;
}

bool TrustsTestLicenses() {
#ifdef HARDCHARGER_TEST_LICENSE_PUBLIC_KEY
  return true;
#else
  return false;
#endif
}

std::string LicenseRequired(const std::string& feature, const std::string& what) {
  return "Error: " + what +
         " is a commercially licensed enterprise feature (license feature '" + feature +
         "'). Provide a GizmoData Enterprise license key with license_key or license_key_file; "
         "contact sales@gizmodata.com.";
}

}  // namespace hardcharger
