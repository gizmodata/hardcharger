// SYSTEM ADMIN's rules that need no server: who holds it in a snapshot of the store
// (through which grants and memberships, and which records count for nothing), what a
// session that asked for it without holding it acts with, and how its statements parse.
// The behaviour through a server is in server/tests/test_rbac_system_admin*.py.
#include <gtest/gtest.h>

#include <string>
#include <variant>
#include <vector>

#include "authz/ddl.hpp"
#include "authz/model.hpp"

using namespace hardcharger::authz;  // NOLINT: the tests read as the module does

namespace {

MembershipRecord Member(PrincipalKind kind, const std::string& member, const std::string& role,
                        const std::string& granted_by = "") {
  return {kind, member, role, false, granted_by};
}

struct Store {
  std::vector<UserRecord> users = {{"u-alice", "alice", std::nullopt, true, {}},
                                   {"u-bob", "bob", std::nullopt, true, {}},
                                   {"u-carol", "carol", std::nullopt, true, {}},
                                   {"u-off", "off", std::nullopt, false, {}},
                                   {"u-tom", "acme/tom", std::nullopt, true, {}}};
  std::vector<RoleRecord> roles = {{kPublicRoleId, kPublicRoleName, true},
                                   {"r-dba", "dba", false},
                                   {"r-ops", "ops", false},
                                   {"r-team", "team", false},
                                   {"r-staff", "acme/staff", false},
                                   {"r-tadmin", "acme/tenant_admin", true}};
  std::vector<MembershipRecord> memberships;
  std::vector<TenantRecord> tenants = {{"t-acme", "acme", ""}};
  std::vector<PrincipalTenantRecord> principal_tenants = {{PrincipalKind::kUser, "u-tom", "t-acme"},
                                                          {PrincipalKind::kRole, "r-staff", "t-acme"},
                                                          {PrincipalKind::kRole, "r-tadmin", "t-acme"}};
  std::vector<GroupMappingRecord> group_mappings;
  std::vector<SystemAdminRecord> system_admins;

  Snapshot Make() const {
    return Snapshot(1, users, roles, memberships, {}, {}, {}, {}, {}, {}, tenants, principal_tenants, {},
                    group_mappings, {}, system_admins);
  }
};

Principal User(const std::string& id, const std::string& name) { return Principal::User(id, name); }

TEST(SystemAdmin, AGrantToTheUserOrToARoleItWasGivenByTheAdministrator) {
  Store store;
  store.system_admins = {{PrincipalKind::kUser, "u-alice", ""}, {PrincipalKind::kRole, "r-dba", "u-alice"}};
  // carol holds ops, ops holds dba: both by the administrator.
  store.memberships = {Member(PrincipalKind::kUser, "u-carol", "r-ops"), Member(PrincipalKind::kRole, "r-ops", "r-dba")};
  const Snapshot snap = store.Make();
  EXPECT_EQ(snap.SystemAdminSources(User("u-alice", "alice")), std::vector<std::string>{""});
  EXPECT_EQ(snap.SystemAdminSources(User("u-carol", "carol")), std::vector<std::string>{"r-dba"});
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-bob", "bob")));
  // No role principal, no administrator, nobody the store does not have.
  EXPECT_FALSE(snap.HoldsSystemAdmin(Principal::Role("r-dba", "dba")));
  EXPECT_FALSE(snap.HoldsSystemAdmin(Principal::Superuser("admin")));
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-nobody", "nobody")));
  EXPECT_TRUE(snap.RoleCarriesSystemAdmin("r-dba"));
  EXPECT_TRUE(snap.RoleCarriesSystemAdmin("r-ops"));
  EXPECT_FALSE(snap.RoleCarriesSystemAdmin("r-team"));
  EXPECT_TRUE(snap.UserCarriesSystemAdmin("u-alice"));
  EXPECT_TRUE(snap.UserCarriesSystemAdmin("u-carol"));
  EXPECT_FALSE(snap.UserCarriesSystemAdmin("u-bob"));
}

TEST(SystemAdmin, AMembershipSomeoneElseGrantedNeverCarriesIt) {
  Store store;
  store.system_admins = {{PrincipalKind::kRole, "r-dba", ""}};
  // bob holds dba from alice (an admin option's grant); carol holds team from the
  // administrator, and team holds dba from alice.
  store.memberships = {Member(PrincipalKind::kUser, "u-bob", "r-dba", "u-alice"),
                       Member(PrincipalKind::kUser, "u-carol", "r-team"),
                       Member(PrincipalKind::kRole, "r-team", "r-dba", "u-alice")};
  const Snapshot snap = store.Make();
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-bob", "bob")));
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-carol", "carol")));
  // They do hold the roles, for everything a role gives...
  EXPECT_TRUE(snap.EffectiveRoleIds("u-bob").count("r-dba"));
  EXPECT_TRUE(snap.EffectiveRoleIds("u-carol").count("r-dba"));
  // ...and both the role and its users are the administrator's to change, whoever granted what.
  EXPECT_TRUE(snap.RoleCarriesSystemAdmin("r-team"));
  EXPECT_TRUE(snap.UserCarriesSystemAdmin("u-bob"));
  // The administrator's own grant of the same memberships makes them count.
  store.memberships = {Member(PrincipalKind::kUser, "u-bob", "r-dba"), Member(PrincipalKind::kUser, "u-carol", "r-team"),
                       Member(PrincipalKind::kRole, "r-team", "r-dba")};
  const Snapshot granted = store.Make();
  EXPECT_TRUE(granted.HoldsSystemAdmin(User("u-bob", "bob")));
  EXPECT_TRUE(granted.HoldsSystemAdmin(User("u-carol", "carol")));
}

TEST(SystemAdmin, ADisabledUserATenantsUserAndRecordsThatNameNobodyHoldNothing) {
  Store store;
  store.system_admins = {{PrincipalKind::kUser, "u-off", ""},    {PrincipalKind::kUser, "u-tom", ""},
                         {PrincipalKind::kRole, "r-staff", ""},  {PrincipalKind::kRole, "r-tadmin", ""},
                         {PrincipalKind::kRole, kPublicRoleId, ""}, {PrincipalKind::kUser, "u-gone", ""},
                         {PrincipalKind::kRole, "r-gone", ""},   {PrincipalKind::kRole, kSystemAdminRoleId, ""},
                         {PrincipalKind::kUser, "u-alice", ""},  {PrincipalKind::kUser, "u-alice", "u-bob"}};
  store.memberships = {Member(PrincipalKind::kUser, "u-tom", "r-staff")};
  const Snapshot snap = store.Make();
  // What counts: the disabled user's grant (it holds it again once enabled) and alice's, once.
  ASSERT_EQ(snap.system_admins().size(), 2u);
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-off", "off")));
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-tom", "acme/tom")));
  EXPECT_FALSE(snap.HoldsSystemAdmin(User("u-bob", "bob")));  // PUBLIC gives it to nobody
  EXPECT_TRUE(snap.HoldsSystemAdmin(User("u-alice", "alice")));
  EXPECT_FALSE(snap.RoleCarriesSystemAdmin(kPublicRoleId));
  EXPECT_FALSE(snap.RoleCarriesSystemAdmin("r-staff"));
}

TEST(SystemAdmin, AnIdentityProviderGroupMappedToTheRoleCarriesIt) {
  Store store;
  store.system_admins = {{PrincipalKind::kRole, "r-dba", ""}};
  store.memberships = {Member(PrincipalKind::kRole, "r-ops", "r-dba")};
  store.group_mappings = {{"m-1", "https://idp.example", "admins", "r-ops"}, {"m-2", "", "staff", "r-staff"}};
  const Snapshot snap = store.Make();
  Principal bob = User("u-bob", "bob");
  EXPECT_FALSE(snap.HoldsSystemAdmin(bob));
  bob.idp_issuer = "https://idp.example";
  bob.idp_groups = {"admins"};
  EXPECT_EQ(snap.SystemAdminSources(bob), std::vector<std::string>{"r-dba"});
  bob.idp_issuer = "https://other.example";
  EXPECT_FALSE(snap.HoldsSystemAdmin(bob));
  bob.idp_issuer = "https://idp.example";
  bob.idp_groups = {"Admins"};  // group names are exact
  EXPECT_FALSE(snap.HoldsSystemAdmin(bob));
}

TEST(SystemAdmin, ASessionThatAskedWithoutHoldingItActsWithNoRoleAtAll) {
  Store store;
  store.system_admins = {{PrincipalKind::kUser, "u-alice", ""}};
  store.memberships = {Member(PrincipalKind::kUser, "u-alice", "r-team"), Member(PrincipalKind::kUser, "u-bob", "r-team")};
  const Snapshot snap = store.Make();
  Principal alice = User("u-alice", "alice");
  EXPECT_TRUE(snap.RolesOf(alice).count("r-team"));
  alice.active_role_id = kSystemAdminRoleId;
  ASSERT_TRUE(alice.asks_system_admin());
  EXPECT_FALSE(alice.acts_as_system_admin());  // only the session's state makes it a superuser
  EXPECT_TRUE(snap.HoldsActiveRole(alice));
  EXPECT_TRUE(snap.RolesOf(alice).empty());  // not PUBLIC either: it is refused until RESET ROLE
  Principal bob = User("u-bob", "bob");
  bob.active_role_id = kSystemAdminRoleId;
  EXPECT_FALSE(snap.HoldsActiveRole(bob));
  EXPECT_TRUE(snap.RolesOf(bob).empty());
  EXPECT_FALSE(snap.HasSystemPrivilege(bob, SystemPrivilege::kCheckpoint));
  EXPECT_FALSE(snap.Check(bob, AccessRequest{{"memory", {"main"}, "t"}, ObjectType::kTable, Privilege::kSelect, {}}).allowed);
}

template <typename Command>
Command Parsed(const std::string& sql) {
  auto parsed = ParseRbacStatement(sql);
  EXPECT_TRUE(parsed.ok()) << sql << ": " << parsed.status().message();
  if (!parsed.ok() || !std::holds_alternative<Command>(*parsed)) {
    ADD_FAILURE() << sql << " did not parse as the expected statement";
    return Command{};
  }
  return std::get<Command>(*parsed);
}

std::string ParseError(const std::string& sql) {
  auto parsed = ParseRbacStatement(sql);
  return parsed.ok() ? std::string() : parsed.status().message();
}

TEST(SystemAdminGrammar, TwoUnquotedWordsAndNothingElseMeanIt) {
  const auto grant = Parsed<SystemAdminCommand>("GRANT SYSTEM ADMIN TO alice, ROLE dba, USER \"bob\";");
  EXPECT_TRUE(grant.grant);
  ASSERT_EQ(grant.grantees.size(), 3u);
  EXPECT_EQ(grant.grantees[1].kind, GranteeKind::kRole);
  EXPECT_EQ(grant.grantees[2].name, "bob");
  EXPECT_FALSE(Parsed<SystemAdminCommand>("revoke /* it */ system\n admin from alice").grant);
  EXPECT_EQ(CommandKind(RbacCommand(grant)), "GRANT SYSTEM ADMIN");
  EXPECT_TRUE(Parsed<SetRoleCommand>("SET ROLE SYSTEM ADMIN").system_admin);
  EXPECT_TRUE(Parsed<SetRoleCommand>("set role system admin ;").system_admin);
  // A role's name is one identifier: quoted, or one word.
  const auto quoted = Parsed<SetRoleCommand>("SET ROLE \"SYSTEM ADMIN\"");
  EXPECT_FALSE(quoted.system_admin);
  EXPECT_EQ(quoted.role.value_or(""), "SYSTEM ADMIN");
  EXPECT_EQ(Parsed<SetRoleCommand>("SET ROLE system").role.value_or(""), "system");
  EXPECT_EQ(Parsed<SetRoleCommand>("SET ROLE sysadmin").role.value_or(""), "sysadmin");
  EXPECT_FALSE(Parsed<SetRoleCommand>("RESET ROLE").system_admin);
  EXPECT_EQ(Parsed<RoleMembershipCommand>("GRANT system, admin TO alice").roles.size(), 2u);
  EXPECT_EQ(Parsed<RoleMembershipCommand>("GRANT \"SYSTEM ADMIN\" TO alice").roles.front(), "SYSTEM ADMIN");
  // A quoted word is a name, never the keyword: this is a role list that lacks its comma.
  EXPECT_EQ(ParseError("GRANT \"SYSTEM\" ADMIN TO alice"), "Hardcharger access control: expected TO but found 'ADMIN'");
}

TEST(SystemAdminGrammar, NoOptionNoListNoObject) {
  for (const char* sql : {"GRANT SYSTEM ADMIN TO alice WITH ADMIN OPTION", "GRANT SYSTEM ADMIN TO alice WITH GRANT OPTION"}) {
    EXPECT_NE(ParseError(sql).find("SYSTEM ADMIN has no ADMIN OPTION or GRANT OPTION"), std::string::npos) << sql;
  }
  for (const char* sql : {"REVOKE ADMIN OPTION FOR SYSTEM ADMIN FROM alice", "REVOKE GRANT OPTION FOR SYSTEM ADMIN FROM alice"}) {
    EXPECT_NE(ParseError(sql).find("has no ADMIN OPTION or GRANT OPTION to revoke"), std::string::npos) << sql;
  }
  for (const char* sql : {"GRANT SYSTEM ADMIN, CHECKPOINT TO r", "GRANT CHECKPOINT, SYSTEM ADMIN TO r",
                          "GRANT dba, SYSTEM ADMIN TO alice", "GRANT SELECT, SYSTEM ADMIN ON t TO r",
                          "REVOKE READ FILESYSTEM, SYSTEM ADMIN FROM r"}) {
    EXPECT_NE(ParseError(sql).find("SYSTEM ADMIN is granted and revoked on its own"), std::string::npos) << sql;
  }
  EXPECT_NE(ParseError("GRANT SYSTEM ADMIN ON CATALOG c TO alice").find("no ON clause"), std::string::npos);
  EXPECT_NE(ParseError("GRANT ALL PRIVILEGES TO alice"), "");  // ALL is an object's, and needs its ON
  EXPECT_NE(ParseError("SET ROLE SYSTEM ADMIN, dba"), "");
  EXPECT_NE(ParseError("GRANT SYSTEM ADMIN TO alice; GRANT SYSTEM ADMIN TO bob"), "");
}

}  // namespace
