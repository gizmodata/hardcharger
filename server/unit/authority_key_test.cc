// AuthorityKey (auth.hpp): what a principal may do, as the Chargewire SDK compares it to bind
// sessions to their principal (spec §9.2). It must change with everything that changes the
// principal's authority, and must not change with what changes on every sign-in, or a
// client's new token couldn't reach its own sessions. tests/test_session_authority.py runs
// these and checks the same end to end.
#include <gtest/gtest.h>

#include <string>

#include "auth.hpp"
#include "chargewire/backend.hpp"

namespace {

using hardcharger::AuthorityKey;
using chargewire::Principal;

Principal Base() {
  Principal p;
  p.name = "wes";
  p.role = "user";
  p.auth_method = "password";
  return p;
}

TEST(AuthorityKey, IsAHashThatCarriesNothingOfTheClaims) {
  Principal p = Base();
  p.claims["credential"] = "fingerprint-of-the-password";
  const std::string key = AuthorityKey(p, {"user-id-1", "tenant-1"});
  EXPECT_EQ(key.size(), 64u);
  EXPECT_EQ(key.find_first_not_of("0123456789abcdef"), std::string::npos) << key;
}

TEST(AuthorityKey, IgnoresWhatChangesWithEverySignIn) {
  const std::string base = AuthorityKey(Base(), {"user-id-1", ""});
  Principal p = Base();
  p.claims["token_id"] = "hardcharger-another-token";
  p.claims["expires_at"] = 1234567890;
  p.claims["credential"] = "the password fingerprint";  // a stale one fails CheckToken instead
  p.auth_method = "client_certificate";
  p.certificate_thumbprint = "x5t";  // the SDK compares the binding itself
  EXPECT_EQ(AuthorityKey(p, {"user-id-1", ""}), base);
  // The identity provider's groups are a set: their order is not authority.
  Principal a = Base(), b = Base();
  a.claims["idp"] = boost::json::object{{"iss", "https://idp"}, {"groups", boost::json::array{"x", "y", "x"}}};
  b.claims["idp"] = boost::json::object{{"iss", "https://idp"}, {"groups", boost::json::array{"y", "x"}}};
  EXPECT_EQ(AuthorityKey(a), AuthorityKey(b));
}

TEST(AuthorityKey, ChangesWithEverythingThatChangesAuthority) {
  const std::string base = AuthorityKey(Base(), {"user-id-1", ""});
  auto differs = [&](Principal p, hardcharger::authz::AccessControl::TokenStanding standing, const char* what) {
    EXPECT_NE(AuthorityKey(p, standing), base) << what;
  };
  {
    Principal p = Base();
    p.role = "readonly";
    differs(p, {"user-id-1", ""}, "role");
  }
  {
    Principal p = Base();
    p.role = "admin";
    differs(p, {"user-id-1", ""}, "admin role");
  }
  {
    Principal p = Base();
    p.claims["scope_id"] = "scope-1";
    differs(p, {"user-id-1", ""}, "scope");
  }
  {
    Principal p = Base();
    p.claims["catalog_access"] = boost::json::array{boost::json::object{{"catalog", "*"}, {"access", "read"}}};
    differs(p, {"user-id-1", ""}, "catalog_access");
  }
  {
    Principal p = Base();
    p.claims["catalog_access"] = boost::json::array{};
    differs(p, {"user-id-1", ""}, "empty catalog_access (not the same as none)");
  }
  {
    Principal p = Base();
    p.claims["idp"] = boost::json::object{{"iss", "https://idp"}, {"groups", boost::json::array{"g"}}};
    differs(p, {"user-id-1", ""}, "idp groups");
  }
  differs(Base(), {"user-id-2", ""}, "store user");
  differs(Base(), {"user-id-1", "tenant-1"}, "tenant");
  // Values can't run together: ("ab", "") and ("a", "b") are different authorities.
  Principal ab = Base(), a = Base();
  ab.claims["scope_id"] = "ab";
  a.claims["scope_id"] = "a";
  EXPECT_NE(AuthorityKey(ab, {"", ""}), AuthorityKey(a, {"b", ""}));
  // Two scoped tokens of one user, even with the same allowance, are two authorities.
  Principal s1 = Base(), s2 = Base();
  s1.claims["scope_id"] = "scope-1";
  s2.claims["scope_id"] = "scope-2";
  EXPECT_NE(AuthorityKey(s1), AuthorityKey(s2));
}

}  // namespace
