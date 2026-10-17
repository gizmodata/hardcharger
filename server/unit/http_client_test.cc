// The outbound HTTPS trust store: GizmoSQL 1.x's SystemCaCerts cases, against
// Hardcharger's FindSystemCaCertFile (server/tests/test_system_ca_certs.py runs them).
#include <gtest/gtest.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>

#include "http_client.hpp"

namespace {

// Sets an environment variable for one scope, then puts back what was there.
class ScopedEnv {
 public:
  ScopedEnv(const char* name, const char* value) : name_(name) {
    if (const char* previous = std::getenv(name)) previous_ = previous;
    if (value != nullptr) {
      setenv(name, value, 1);
    } else {
      unsetenv(name);
    }
  }
  ~ScopedEnv() {
    if (previous_) {
      setenv(name_, previous_->c_str(), 1);
    } else {
      unsetenv(name_);
    }
  }

 private:
  const char* name_;
  std::optional<std::string> previous_;
};

}  // namespace

TEST(SystemCaCerts, RespectsSslCertFileEnv) {
  const auto bundle = std::filesystem::temp_directory_path() / ("hc_ca_" + std::to_string(::getpid()) + ".pem");
  std::ofstream(bundle) << "# test bundle\n";
  {
    ScopedEnv env("SSL_CERT_FILE", bundle.c_str());
    EXPECT_EQ(hardcharger::FindSystemCaCertFile(), bundle.string());
  }
  std::filesystem::remove(bundle);
}

// A missing SSL_CERT_FILE is passed over for the system's bundle (1.x accepted finding
// nothing; every host this runs on has one).
TEST(SystemCaCerts, IgnoresNonexistentSslCertFileEnv) {
  ScopedEnv env("SSL_CERT_FILE", "/definitely/does/not/exist/ca.pem");
  const auto found = hardcharger::FindSystemCaCertFile();
  ASSERT_TRUE(found.has_value());
  EXPECT_NE(*found, "/definitely/does/not/exist/ca.pem");
}

TEST(SystemCaCerts, FindsASystemBundle) {
  ScopedEnv env("SSL_CERT_FILE", nullptr);
  const auto found = hardcharger::FindSystemCaCertFile();
  ASSERT_TRUE(found.has_value()) << "no system CA bundle found: the list in http_client.cc is stale for this OS";
  EXPECT_TRUE(std::ifstream(*found).good()) << *found;
}

TEST(UrlEncode, KeepsUnreservedAndEncodesTheRest) {
  EXPECT_EQ(hardcharger::UrlEncode("openid profile email"), "openid%20profile%20email");
  EXPECT_EQ(hardcharger::UrlEncode("a-b_c.d~e"), "a-b_c.d~e");
  EXPECT_EQ(hardcharger::UrlEncode("https://h:1/cb?x=1&y"), "https%3A%2F%2Fh%3A1%2Fcb%3Fx%3D1%26y");
}
