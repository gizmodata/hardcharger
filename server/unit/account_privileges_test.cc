// The account privileges' rules that need no server: who holds CREATE USER, ... in a snapshot
// of the store (only through the administrator's memberships, never for a limited sign-in),
// that no admin option passes one on, which records count for nothing, how the statements
// parse, and that an account change's audit values never carry a password, a hash or an
// attribute's value. The behaviour through a server is in
// server/tests/test_rbac_account_privileges*.py.
#include <gtest/gtest.h>

#include <string>
#include <variant>
#include <vector>

#include "authz/audit_fields.hpp"
#include "authz/ddl.hpp"
#include "authz/model.hpp"

using namespace hardcharger::authz;  // NOLINT: the tests read as the module does

namespace {

MembershipRecord Member(PrincipalKind kind, const std::string& member, const std::string& role,
                        const std::string& granted_by = "", bool admin_option = false) {
  return {kind, member, role, admin_option, granted_by};
}

SystemPrivilegeRecord Holds(const std::string& role, SystemPrivilege privilege, bool admin_option = false,
                            const std::string& granted_by = "", const std::string& location = "") {
  return {role, privilege, admin_option, granted_by, location};
}

struct Store {
  std::vector<UserRecord> users = {{"u-amy", "amy", std::string("pbkdf2-sha256$1$c2FsdA==$aGFzaA=="), true, {}},
                                   {"u-bob", "bob", std::nullopt, true, {{"region", "eu-secret-value"}}},
                                   {"u-carl", "carl", std::nullopt, true, {}},
                                   {"u-off", "off", std::nullopt, false, {}},
                                   {"u-tom", "acme/tom", std::nullopt, true, {}}};
  std::vector<RoleRecord> roles = {{kPublicRoleId, kPublicRoleName, true},
                                   {"r-desk", "helpdesk", false},
                                   {"r-support", "support", false},
                                   {"r-other", "other", false},
                                   {"r-staff", "acme/staff", false},
                                   {"r-tadmin", "acme/tenant_admin", true}};
  std::vector<MembershipRecord> memberships;
  std::vector<SystemPrivilegeRecord> privileges;
  std::vector<TenantRecord> tenants = {{"t-acme", "acme", ""}};
  std::vector<PrincipalTenantRecord> principal_tenants = {{PrincipalKind::kUser, "u-tom", "t-acme"},
                                                          {PrincipalKind::kRole, "r-staff", "t-acme"},
                                                          {PrincipalKind::kRole, "r-tadmin", "t-acme"}};
  std::vector<GroupMappingRecord> group_mappings;

  Snapshot Make() const {
    return Snapshot(1, users, roles, memberships, {}, {}, privileges, {}, {}, {}, tenants, principal_tenants, {},
                    group_mappings, {}, {});
  }
};

Principal User(const std::string& id, const std::string& name) { return Principal::User(id, name); }

TEST(AccountPrivileges, NamedAsGrantWritesThem) {
  for (const auto& [privilege, name] : std::vector<std::pair<SystemPrivilege, std::string>>{
           {SystemPrivilege::kCreateUser, "CREATE USER"},
           {SystemPrivilege::kAlterUser, "ALTER USER"},
           {SystemPrivilege::kDropUser, "DROP USER"},
           {SystemPrivilege::kCreateRole, "CREATE ROLE"},
           {SystemPrivilege::kDropRole, "DROP ROLE"}}) {
    EXPECT_EQ(SystemPrivilegeName(privilege), name);
    EXPECT_EQ(SystemPrivilegeFromName(name), privilege);
    EXPECT_TRUE(IsAccountPrivilege(privilege));
  }
  EXPECT_FALSE(IsAccountPrivilege(SystemPrivilege::kManageCatalogs));
  EXPECT_FALSE(IsAccountPrivilege(SystemPrivilege::kReadFilesystem));
}

TEST(AccountPrivileges, HeldOnlyThroughTheAdministratorsMemberships) {
  Store store;
  store.privileges = {Holds("r-desk", SystemPrivilege::kCreateUser)};
  // amy holds helpdesk from the administrator; carl from amy (an admin option's grant);
  // support holds helpdesk from the administrator, and bob holds support from the administrator.
  store.memberships = {Member(PrincipalKind::kUser, "u-amy", "r-desk", "", true),
                       Member(PrincipalKind::kUser, "u-carl", "r-desk", "u-amy"),
                       Member(PrincipalKind::kRole, "r-support", "r-desk"),
                       Member(PrincipalKind::kUser, "u-bob", "r-support")};
  const Snapshot snap = store.Make();
  EXPECT_TRUE(snap.HasSystemPrivilege(User("u-amy", "amy"), SystemPrivilege::kCreateUser));
  EXPECT_TRUE(snap.HasSystemPrivilege(User("u-bob", "bob"), SystemPrivilege::kCreateUser));
  EXPECT_FALSE(snap.HasSystemPrivilege(User("u-carl", "carl"), SystemPrivilege::kCreateUser));
  EXPECT_FALSE(snap.HasSystemPrivilege(User("u-amy", "amy"), SystemPrivilege::kDropUser));
  EXPECT_TRUE(snap.RoleCarriesAccountPrivilege("r-desk"));
  EXPECT_TRUE(snap.RoleCarriesAccountPrivilege("r-support"));
  EXPECT_FALSE(snap.RoleCarriesAccountPrivilege("r-other"));
  // SET ROLE narrows it: acting with another role, the help desk's privilege is not the session's.
  Principal narrowed = User("u-amy", "amy");
  narrowed.active_role_id = "r-other";
  EXPECT_FALSE(snap.HasSystemPrivilege(narrowed, SystemPrivilege::kCreateUser));
  narrowed.active_role_id = "r-desk";
  EXPECT_TRUE(snap.HasSystemPrivilege(narrowed, SystemPrivilege::kCreateUser));
}

TEST(AccountPrivileges, NeverForALimitedSignIn) {
  Store store;
  store.privileges = {Holds("r-desk", SystemPrivilege::kCreateUser)};
  store.memberships = {Member(PrincipalKind::kUser, "u-amy", "r-desk"), Member(PrincipalKind::kUser, "u-off", "r-desk")};
  const Snapshot snap = store.Make();
  Principal scoped = User("u-amy", "amy");
  scoped.scope = TokenScope{"t1", "desk", {Privilege::kSelect}, {}, 0};
  Principal rules = User("u-amy", "amy");
  rules.catalog_access_token = true;
  Principal role = Principal::Role("r-desk", "helpdesk");
  for (const auto& who : {scoped, rules, role, User("u-off", "off")}) {
    EXPECT_FALSE(snap.HasSystemPrivilege(who, SystemPrivilege::kCreateUser)) << who.user_name;
  }
  EXPECT_TRUE(snap.HasSystemPrivilege(User("u-amy", "amy"), SystemPrivilege::kCreateUser));
}

TEST(AccountPrivileges, NoAdminOptionPassesOneOn) {
  Store store;
  // A store edited by hand: an admin option, a grantor who is not the administrator.
  store.privileges = {Holds("r-desk", SystemPrivilege::kCreateUser, /*admin_option=*/true),
                      Holds("r-other", SystemPrivilege::kAlterUser, false, /*granted_by=*/"u-amy"),
                      Holds("r-desk", SystemPrivilege::kManageConfig, /*admin_option=*/true)};
  store.memberships = {Member(PrincipalKind::kUser, "u-amy", "r-desk")};
  const Snapshot snap = store.Make();
  const Principal amy = User("u-amy", "amy");
  EXPECT_FALSE(snap.CanGrantSystemPrivilege(amy, SystemPrivilege::kCreateUser));
  EXPECT_TRUE(snap.CanGrantSystemPrivilege(amy, SystemPrivilege::kManageConfig));  // the twin
  EXPECT_TRUE(snap.CanGrantSystemPrivilege(Principal::Superuser("admin"), SystemPrivilege::kCreateUser));
  for (const auto& r : snap.system_privileges()) {
    if (IsAccountPrivilege(r.privilege)) EXPECT_FALSE(r.admin_option);
  }
  // A delegated grant of one never stands.
  const auto fallen = snap.FallenDelegations();
  ASSERT_EQ(fallen.system_privileges.size(), 1u);
  EXPECT_EQ(fallen.system_privileges[0].privilege, SystemPrivilege::kAlterUser);
}

TEST(AccountPrivileges, RecordsThatNameAnyoneElseCountForNothing) {
  Store store;
  store.privileges = {Holds(kPublicRoleId, SystemPrivilege::kCreateUser),
                      Holds("r-staff", SystemPrivilege::kCreateUser),
                      Holds("r-gone", SystemPrivilege::kCreateUser),
                      Holds("r-desk", SystemPrivilege::kCreateUser, false, "", "s3://bucket/")};
  store.memberships = {Member(PrincipalKind::kUser, "u-amy", "r-desk"), Member(PrincipalKind::kUser, "u-tom", "r-staff")};
  const Snapshot snap = store.Make();
  EXPECT_TRUE(snap.system_privileges().empty());
  EXPECT_FALSE(snap.HasSystemPrivilege(User("u-carl", "carl"), SystemPrivilege::kCreateUser));
  EXPECT_FALSE(snap.HasSystemPrivilege(User("u-tom", "acme/tom"), SystemPrivilege::kCreateUser));
  EXPECT_FALSE(snap.HasSystemPrivilege(User("u-amy", "amy"), SystemPrivilege::kCreateUser));
}

TEST(AccountPrivilegesGrammar, PhrasesNotRoles) {
  auto parsed = ParseRbacStatement("GRANT CREATE USER, ALTER USER, DROP USER, CREATE ROLE, DROP ROLE TO helpdesk");
  ASSERT_TRUE(parsed.ok()) << parsed.status().ToString();
  const auto* grant = std::get_if<SystemPrivilegeCommand>(&*parsed);
  ASSERT_NE(grant, nullptr);
  EXPECT_EQ(grant->privileges.size(), 5u);
  // A role called "create" is still granted as a role.
  parsed = ParseRbacStatement("GRANT create TO bob");
  ASSERT_TRUE(parsed.ok());
  EXPECT_TRUE(std::holds_alternative<RoleMembershipCommand>(*parsed));
  EXPECT_EQ(AccountPrivilegeOf(*ParseRbacStatement("ALTER USER bob DISABLE")), SystemPrivilege::kAlterUser);
  EXPECT_EQ(AccountPrivilegeOf(*ParseRbacStatement("DROP ROLE r")), SystemPrivilege::kDropRole);
  EXPECT_FALSE(AccountPrivilegeOf(*ParseRbacStatement("GRANT r TO bob")).has_value());
}

TEST(AccountPrivilegesAudit, ValuesNeverCarryACredentialOrAnAttributesValue) {
  Store store;
  const Snapshot before = store.Make();
  store.users[0].password_hash = "pbkdf2-sha256$1$c2FsdA==$bmV3";
  store.users[0].attributes = {{"region", "eu-secret-value"}};
  store.memberships = {Member(PrincipalKind::kUser, "u-amy", "r-desk", "", true)};
  const Snapshot after = store.Make();
  auto values = AccountChangeValues(*ParseRbacStatement("ALTER USER amy WITH PASSWORD 'hunter2-secret'"), before, after);
  ASSERT_TRUE(values.has_value());
  EXPECT_EQ(values->first, "enabled; password login; roles: none");
  EXPECT_EQ(values->second,
            "enabled; password login (new password); roles: helpdesk WITH ADMIN OPTION; attributes: region");
  for (const auto& text : {values->first, values->second}) {
    EXPECT_EQ(text.find("pbkdf2"), std::string::npos);
    EXPECT_EQ(text.find("hunter2"), std::string::npos);
    EXPECT_EQ(text.find("eu-secret-value"), std::string::npos);
  }
  EXPECT_EQ(AccountChangeValues(*ParseRbacStatement("DROP USER nobody"), before, after),
            std::make_pair(std::string("none"), std::string("none")));
  EXPECT_FALSE(AccountChangeValues(*ParseRbacStatement("GRANT SELECT ON t TO r"), before, after).has_value());
  EXPECT_FALSE(AccountChangeValues(*ParseRbacStatement("GRANT READ FILESYSTEM TO r"), before, after).has_value());
}

}  // namespace
