// Port of GizmoSQL 1.x tests/integration/test_client_config.cpp (5 cases), with
// Hardcharger's names: GIZMOSQL_USER is HARDCHARGER_USER, and the server's variable the
// client falls back to, GIZMOSQL_USERNAME, is HARDCHARGER_ADMIN_USERNAME. Added: no
// GIZMOSQL_* variable is read, and the password never falls back to the server's
// administrator password.
#include <gtest/gtest.h>

#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <tuple>
#include <vector>

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

// ---- Connection URIs: the Hardcharger ADBC driver's rules (hardcharger-adbc ToChargewireURI,
// chargewire-adbc parseURI), every form it accepts and refuses. ----

namespace {

struct Parsed {
  bool ok;
  ClientConfig config;
  std::string error;  // what went to stderr
};

Parsed Parse(const std::string& uri) {
  Parsed parsed{false, {}, {}};
  std::ostringstream captured;
  auto* old = std::cerr.rdbuf(captured.rdbuf());
  parsed.ok = ParseConnectionURI(uri, parsed.config);
  std::cerr.rdbuf(old);
  parsed.error = captured.str();
  return parsed;
}

}  // namespace

TEST(ClientConnectionUriTest, AcceptedFormsAndWhatTheyMean) {
  // uri, host, port, tls, base path
  const std::vector<std::tuple<std::string, std::string, int, bool, std::string>> cases = {
      {"hardcharger://db.example.com", "db.example.com", 31337, true, "/chargewire"},
      {"hardcharger://db.example.com:8443", "db.example.com", 8443, true, "/chargewire"},
      {"hardcharger://db.example.com:443", "db.example.com", 443, true, "/chargewire"},
      {"hardcharger://localhost?tls=false", "localhost", 31337, false, "/chargewire"},
      {"hardcharger://127.0.0.1:4000?tls=false", "127.0.0.1", 4000, false, "/chargewire"},
      {"hardcharger://127.0.0.1:443?tls=false", "127.0.0.1", 443, false, "/chargewire"},
      {"hardcharger://h?tls=0", "h", 31337, false, "/chargewire"},
      {"hardcharger://h?tls=FALSE", "h", 31337, false, "/chargewire"},
      {"hardcharger://h?tls=true", "h", 31337, true, "/chargewire"},
      {"hardcharger://h?tls=1", "h", 31337, true, "/chargewire"},
      {"hardcharger://h?tls=", "h", 31337, true, "/chargewire"},  // empty: the default, as in Go
      {"hardcharger://h/", "h", 31337, true, "/chargewire"},
      {"hardcharger://h:1/custom/prefix", "h", 1, true, "/custom/prefix"},
      {"hardcharger://h/custom/prefix/?tls=false", "h", 31337, false, "/custom/prefix"},
      {"HARDCHARGER://h", "h", 31337, true, "/chargewire"},
      {"hardcharger://[::1]", "::1", 31337, true, "/chargewire"},
      {"hardcharger://[::1]:4000?tls=false", "::1", 4000, false, "/chargewire"},
      {"hardcharger://h#fragment", "h", 31337, true, "/chargewire"},
      // The Chargewire spellings, with their own default ports.
      {"chargewire://h:31337", "h", 31337, true, "/chargewire"},
      {"chargewire://h", "h", 443, true, "/chargewire"},
      {"chargewire://h:31337?tls=false", "h", 31337, false, "/chargewire"},
      {"https://h:31337", "h", 31337, true, "/chargewire"},
      {"https://h", "h", 443, true, "/chargewire"},
      {"http://h:31337/custom", "h", 31337, false, "/custom"},
      {"http://h", "h", 80, false, "/chargewire"},
  };
  for (const auto& [uri, host, port, tls, base_path] : cases) {
    const Parsed parsed = Parse(uri);
    ASSERT_TRUE(parsed.ok) << uri << ": " << parsed.error;
    EXPECT_EQ(parsed.config.host, host) << uri;
    EXPECT_EQ(parsed.config.port, port) << uri;
    EXPECT_EQ(parsed.config.use_tls, tls) << uri;
    EXPECT_EQ(parsed.config.base_path, base_path) << uri;
    EXPECT_TRUE(parsed.config.uri_given) << uri;
    EXPECT_TRUE(parsed.error.empty()) << uri << ": " << parsed.error;
  }
}

// A URI sets TLS both ways: ?tls=true is never plain HTTP, whatever the config held.
TEST(ClientConnectionUriTest, TlsTrueIsNeverPlainHttp) {
  for (const std::string uri : {"hardcharger://h?tls=true", "hardcharger://h", "chargewire://h:1?tls=1", "https://h:1"}) {
    ClientConfig config;
    config.use_tls = false;
    ASSERT_TRUE(ParseConnectionURI(uri, config)) << uri;
    EXPECT_TRUE(config.use_tls) << uri;
  }
  ClientConfig config;
  config.use_tls = true;
  ASSERT_TRUE(ParseConnectionURI("hardcharger://h?tls=false", config));
  EXPECT_FALSE(config.use_tls);
}

TEST(ClientConnectionUriTest, RefusedFormsSayWhy) {
  // uri, a part of the message
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"hardcharger+http://h", "hardcharger+http:// is not a Hardcharger URI scheme: use hardcharger://host:port for "
                               "HTTPS, or hardcharger://host:port?tls=false for plain HTTP"},
      {"HARDCHARGER+HTTPS://h", "is not a Hardcharger URI scheme"},
      {"gizmosql://h:31337", "the scheme must be hardcharger://, chargewire://, https:// or http://"},
      {"jdbc:hardcharger://h", "the scheme must be hardcharger://, chargewire://, https:// or http://"},
      {"h:31337", "it needs a scheme, e.g. hardcharger://host:31337"},
      {"", "it needs a scheme"},
      {"hardcharger://", "no host"},
      {"hardcharger://:31337", "no host"},
      {"hardcharger://h:0", "invalid port \"0\""},
      {"hardcharger://h:65536", "invalid port \"65536\""},
      {"hardcharger://h:abc", "invalid port \"abc\""},
      {"hardcharger://[::1", "unclosed [ in the host"},
      {"hardcharger://h?tls=maybe", "invalid value \"maybe\" for uri parameter tls: want \"true\" or \"false\""},
      {"https://h?tls=false", "unknown parameter(s) tls"},  // only hardcharger:// and chargewire:// take tls
      {"http://h?tls=true", "unknown parameter(s) tls"},
      {"hardcharger://h?catalog=x&b=1", "unknown parameter(s) b, catalog"},
      // What 1.x's URIs took: refused, with the flag that does it now.
      {"hardcharger://h?useEncryption=true", "unknown parameter(s) useEncryption (useEncryption: TLS is on unless the "
                                             "URI says ?tls=false)"},
      {"hardcharger://h?username=joe", "unknown parameter(s) username (username: --username)"},
      {"hardcharger://h?tlsRoots=ca.pem&disableCertificateVerification=true&authType=mtls",
       "unknown parameter(s) authType, disableCertificateVerification, tlsRoots (authType: --auth-type; "
       "disableCertificateVerification: --tls-skip-verify; tlsRoots: --tls-roots)"},
  };
  for (const auto& [uri, message] : cases) {
    const Parsed parsed = Parse(uri);
    EXPECT_FALSE(parsed.ok) << uri;
    EXPECT_NE(parsed.error.find(message), std::string::npos) << uri << ": " << parsed.error;
    EXPECT_EQ(parsed.error.rfind("Error: ", 0), 0U) << uri << ": " << parsed.error;
  }
}

TEST(ClientConnectionUriTest, CredentialsAreRefusedAndNotRepeated) {
  const Parsed parsed = Parse("hardcharger://joe:s3cret-pw@db.example.com");
  EXPECT_FALSE(parsed.ok);
  EXPECT_EQ(parsed.error,
            "Error: invalid uri hardcharger://***@db.example.com: credentials do not belong in the uri; use "
            "--username and HARDCHARGER_PASSWORD (or -W)\n");
  EXPECT_FALSE(Parse("hardcharger://joe@db.example.com").ok);
}

TEST(ClientConnectionUriTest, ARefusedUriChangesNothing) {
  ClientConfig config;
  config.host = "kept";
  config.port = 1234;
  EXPECT_FALSE(Parse("hardcharger://other:5?tls=maybe").ok);
  EXPECT_FALSE(ParseConnectionURI("hardcharger://other:5?username=x", config));
  EXPECT_EQ(config.host, "kept");
  EXPECT_EQ(config.port, 1234);
  EXPECT_FALSE(config.uri_given);
}

TEST(ClientConnectionUriTest, BuildRoundTrips) {
  for (const std::string uri : {"hardcharger://h:31337", "hardcharger://h:4000?tls=false",
                                "hardcharger://[::1]:5/custom/prefix?tls=false", "hardcharger://h:443/x"}) {
    ClientConfig config;
    ASSERT_TRUE(ParseConnectionURI(uri, config)) << uri;
    EXPECT_EQ(BuildConnectionURI(config), uri);
  }
}

// The URI is the whole address: HARDCHARGER_HOST, HARDCHARGER_PORT and HARDCHARGER_TLS
// never change it (HARDCHARGER_TLS=true would otherwise put TLS on a ?tls=false URI, and
// HARDCHARGER_HOST would replace hardcharger://localhost's host).
TEST(ClientConnectionUriTest, TheEnvironmentNeverChangesAUri) {
  ScopedEnv host("HARDCHARGER_HOST");
  ScopedEnv port("HARDCHARGER_PORT");
  ScopedEnv tls("HARDCHARGER_TLS");
  host.Set("elsewhere.example.com");
  port.Set("9");
  tls.Set("true");
  ClientConfig config;
  ASSERT_TRUE(ParseConnectionURI("hardcharger://localhost:31337?tls=false", config));
  ResolveEnvironmentVariables(config);
  EXPECT_EQ(config.host, "localhost");
  EXPECT_EQ(config.port, 31337);
  EXPECT_FALSE(config.use_tls);
  // Its twin: without a URI, the environment applies.
  ClientConfig flags;
  ResolveEnvironmentVariables(flags);
  EXPECT_EQ(flags.host, "elsewhere.example.com");
  EXPECT_EQ(flags.port, 9);
  EXPECT_TRUE(flags.use_tls);
}

// TLS is on by default, with or without a URI (1.x's client was plaintext unless --tls).
// HARDCHARGER_TLS=false turns it off for --host/--port; --tls on the command line and a URI
// win over the variable; a value that isn't true or false keeps TLS on.
TEST(ClientTlsDefaultTest, OnByDefaultAndOffOnlyWhenAsked) {
  ScopedEnv tls("HARDCHARGER_TLS");
  ClientConfig defaults;
  ResolveEnvironmentVariables(defaults);
  EXPECT_TRUE(defaults.use_tls);

  for (const std::string off : {"false", "FALSE", "0"}) {
    tls.Set(off);
    ClientConfig config;
    ResolveEnvironmentVariables(config);
    EXPECT_FALSE(config.use_tls) << off;
  }
  for (const std::string on : {"true", "1", "yes", "off", ""}) {
    tls.Set(on);
    ClientConfig config;
    std::ostringstream captured;
    auto* old = std::cerr.rdbuf(captured.rdbuf());
    ResolveEnvironmentVariables(config);
    std::cerr.rdbuf(old);
    EXPECT_TRUE(config.use_tls) << on;
  }

  tls.Set("false");
  ClientConfig flag;  // --tls (or --tls=true) given
  flag.tls_given = true;
  ResolveEnvironmentVariables(flag);
  EXPECT_TRUE(flag.use_tls);
  ClientConfig uri;
  ASSERT_TRUE(ParseConnectionURI("hardcharger://h", uri));
  ResolveEnvironmentVariables(uri);
  EXPECT_TRUE(uri.use_tls);
}

}  // namespace hardcharger::client
