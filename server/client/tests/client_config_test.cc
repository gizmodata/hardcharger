// Port of GizmoSQL 1.x tests/integration/test_client_config.cpp (5 cases), with
// Hardcharger's names: GIZMOSQL_USER is HARDCHARGER_USER, and the server's variable the
// client falls back to, GIZMOSQL_USERNAME, is HARDCHARGER_ADMIN_USERNAME. Added: no
// GIZMOSQL_* variable is read, and the password never falls back to the server's
// administrator password.
#include <gtest/gtest.h>

#include <cstdlib>
#include <string>

#include "client_config.hpp"

namespace {

// Cross-platform env-var helpers: POSIX has setenv/unsetenv; MSVC only
// has _putenv_s ("VAR=value" to set, "VAR=" to unset).
inline void EnvSet(const char* name, const std::string& value) {
#ifdef _WIN32
  _putenv_s(name, value.c_str());
#else
  ::setenv(name, value.c_str(), /*overwrite=*/1);
#endif
}

inline void EnvUnset(const char* name) {
#ifdef _WIN32
  _putenv_s(name, "");
#else
  ::unsetenv(name);
#endif
}

// RAII helper: scope-bounded set/unset so each test starts from a clean
// slate regardless of how the parent shell or earlier tests left the
// relevant env vars.
class ScopedEnv {
 public:
  explicit ScopedEnv(const char* name) : name_(name) {
    if (const char* old = std::getenv(name)) {
      had_old_ = true;
      old_value_ = old;
    }
    Unset();
  }

  ~ScopedEnv() {
    if (had_old_) {
      EnvSet(name_, old_value_);
    } else {
      EnvUnset(name_);
    }
  }

  void Set(const std::string& value) { EnvSet(name_, value); }
  void Unset() { EnvUnset(name_); }

 private:
  const char* name_;
  bool had_old_ = false;
  std::string old_value_;
};

}  // namespace

namespace hardcharger::client {

// HARDCHARGER_USER is the documented client env var; populate it directly.
TEST(ClientConfigEnvTest, UsernameFromHardchargerUser) {
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  user.Set("alice");

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "alice");
}

// HARDCHARGER_ADMIN_USERNAME is the SERVER's env var (its administrator); the client
// accepts it as a fallback so a single env-var setup works for both binaries.
TEST(ClientConfigEnvTest, UsernameFallsBackToHardchargerAdminUsername) {
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  username.Set("bob");

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "bob");
}

// When BOTH are set the client's own name (HARDCHARGER_USER) wins.
TEST(ClientConfigEnvTest, HardchargerUserWinsOverHardchargerAdminUsername) {
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  user.Set("alice");
  username.Set("bob");

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "alice");
}

// An explicit --username (already set on the config) is not clobbered
// by either env var.
TEST(ClientConfigEnvTest, ExplicitUsernameNotOverriddenByEnv) {
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  user.Set("alice");
  username.Set("bob");

  ClientConfig config;
  config.username = "carol";
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "carol");
}

// With neither env var set, username stays empty (CLI-only config).
TEST(ClientConfigEnvTest, NoEnvLeavesUsernameEmpty) {
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  // both unset by ScopedEnv ctor

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "");
}

}  // namespace hardcharger::client

namespace hardcharger::client {

// Added: Hardcharger takes only its own names; a GizmoSQL 1.x variable does nothing.
TEST(ClientConfigEnvTest, GizmosqlVariablesAreNotRead) {
  ScopedEnv gizmo_user("GIZMOSQL_USER");
  ScopedEnv gizmo_password("GIZMOSQL_PASSWORD");
  ScopedEnv gizmo_host("GIZMOSQL_HOST");
  ScopedEnv user("HARDCHARGER_USER");
  ScopedEnv username("HARDCHARGER_ADMIN_USERNAME");
  ScopedEnv password("HARDCHARGER_PASSWORD");
  ScopedEnv host("HARDCHARGER_HOST");
  gizmo_user.Set("alice");
  gizmo_password.Set("secret");
  gizmo_host.Set("db.example.com");

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.username, "");
  EXPECT_FALSE(config.password_provided);
  EXPECT_EQ(config.host, "localhost");

  // The positive twin: the Hardcharger names are read.
  user.Set("alice");
  password.Set("secret");
  host.Set("db.example.com");
  ClientConfig named;
  ResolveEnvironmentVariables(named);
  EXPECT_EQ(named.username, "alice");
  EXPECT_TRUE(named.password_provided);
  EXPECT_EQ(named.password, "secret");
  EXPECT_EQ(named.host, "db.example.com");
}

// Added: HARDCHARGER_PASSWORD is the user's own credential; the server administrator's
// password variable is never used as the client's.
TEST(ClientConfigEnvTest, PasswordDoesNotFallBackToTheAdministratorsPassword) {
  ScopedEnv password("HARDCHARGER_PASSWORD");
  ScopedEnv admin_password("HARDCHARGER_ADMIN_PASSWORD");
  admin_password.Set("server-admin-secret");

  ClientConfig config;
  ResolveEnvironmentVariables(config);
  EXPECT_FALSE(config.password_provided);
  EXPECT_EQ(config.password, "");

  password.Set("mine");
  ClientConfig mine;
  ResolveEnvironmentVariables(mine);
  EXPECT_TRUE(mine.password_provided);
  EXPECT_EQ(mine.password, "mine");
}

}  // namespace hardcharger::client
