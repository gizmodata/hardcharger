// EXEMPT ROW POLICIES and EXEMPT MASKING POLICIES, the rules that need no server: who is
// exempt in a snapshot of the store, who may grant them, how they parse, and where the store
// keeps them. The behaviour through a server is in server/tests/test_rbac_policy_exemptions.py.
#include <gtest/gtest.h>

#include <string>
#include <variant>
#include <vector>

#include "authz/ddl.hpp"
#include "authz/locations.hpp"
#include "authz/model.hpp"

using namespace hardcharger::authz;  // NOLINT: the tests read as the module does

namespace {

constexpr auto kRows = SystemPrivilege::kExemptRowPolicies;
constexpr auto kMasks = SystemPrivilege::kExemptMaskingPolicies;

Snapshot Make(std::vector<SystemPrivilegeRecord> privileges) {
  std::vector<UserRecord> users = {{"u-rep", "rep", std::nullopt, true, {}},
                                   {"u-ann", "ann", std::nullopt, true, {}},
                                   {"u-tom", "acme/tom", std::nullopt, true, {}}};
  std::vector<RoleRecord> roles = {{kPublicRoleId, kPublicRoleName, true},
                                   {"r-repl", "replicator", false},
                                   {"r-staff", "staff", false},
                                   {"r-acme", "acme/loaders", false}};
  std::vector<MembershipRecord> memberships = {{PrincipalKind::kUser, "u-rep", "r-repl", false, ""},
                                               {PrincipalKind::kUser, "u-ann", "r-staff", false, ""},
                                               {PrincipalKind::kUser, "u-tom", "r-acme", false, ""}};
  std::vector<TenantRecord> tenants = {{"t-acme", "acme", ""}};
  std::vector<PrincipalTenantRecord> principal_tenants = {{PrincipalKind::kUser, "u-tom", "t-acme"},
                                                          {PrincipalKind::kRole, "r-acme", "t-acme"}};
  return Snapshot(1, users, roles, memberships, {}, {}, std::move(privileges), {}, {}, {}, tenants,
                  principal_tenants);
}

TEST(PolicyExemption, HeldThroughARoleAndOnlyAsGranted) {
  const Snapshot snap = Make({{"r-repl", kRows, false, ""}});
  const Principal rep = Principal::User("u-rep", "rep");
  EXPECT_TRUE(snap.ExemptFromPolicies(rep, kRows));
  EXPECT_FALSE(snap.ExemptFromPolicies(rep, kMasks));
  EXPECT_FALSE(snap.ExemptFromPolicies(Principal::User("u-ann", "ann"), kRows));
  // A role principal (a trigger's owner) holds what it was granted.
  EXPECT_TRUE(snap.ExemptFromPolicies(Principal::Role("r-repl", "replicator"), kRows));
  // The administrator: no policy applies to it anyway.
  EXPECT_TRUE(snap.ExemptFromPolicies(Principal::Superuser("admin"), kMasks));
  // Only the two exemptions are asked this way.
  EXPECT_FALSE(snap.ExemptFromPolicies(rep, SystemPrivilege::kCheckpoint));
}

TEST(PolicyExemption, NeverForALimitedSessionOrATenant) {
  const Snapshot snap = Make({{"r-repl", kRows, false, ""}, {"r-repl", kMasks, false, ""},
                              // A tenant's role should never hold one; a store row that says so counts for nothing.
                              {"r-acme", kRows, false, ""}});
  Principal scoped = Principal::User("u-rep", "rep");
  scoped.scope = TokenScope{"s-1", "loader", {Privilege::kSelect}, {}, 4102444800};
  EXPECT_FALSE(snap.ExemptFromPolicies(scoped, kRows));
  Principal catalog_access = Principal::User("u-rep", "rep");
  catalog_access.catalog_access_token = true;
  EXPECT_FALSE(snap.ExemptFromPolicies(catalog_access, kMasks));
  Principal unverified = Principal::User("u-rep", "rep");
  unverified.policy_exemptions_unverified = true;
  EXPECT_FALSE(snap.ExemptFromPolicies(unverified, kRows));
  EXPECT_FALSE(snap.ExemptFromPolicies(Principal::User("u-tom", "acme/tom"), kRows));
  // The twin: the same user's own session.
  EXPECT_TRUE(snap.ExemptFromPolicies(Principal::User("u-rep", "rep"), kRows));
}

TEST(PolicyExemption, OnlyThroughMembershipsTheAdministratorGranted) {
  std::vector<UserRecord> users = {{"u-rep", "rep", std::nullopt, true, {}}, {"u-bob", "bob", std::nullopt, true, {}},
                                   {"u-cy", "cy", std::nullopt, true, {}}};
  std::vector<RoleRecord> roles = {{kPublicRoleId, kPublicRoleName, true}, {"r-repl", "replicator", false},
                                   {"r-team", "team", false}};
  // rep holds replicator from the administrator, WITH ADMIN OPTION; bob holds it from rep;
  // cy holds team from the administrator, and team holds replicator from rep.
  std::vector<MembershipRecord> memberships = {{PrincipalKind::kUser, "u-rep", "r-repl", true, ""},
                                               {PrincipalKind::kUser, "u-bob", "r-repl", false, "u-rep"},
                                               {PrincipalKind::kUser, "u-cy", "r-team", false, ""},
                                               {PrincipalKind::kRole, "r-team", "r-repl", false, "u-rep"}};
  const Snapshot snap(1, users, roles, memberships, {}, {}, {{"r-repl", kRows, false, ""}, {kPublicRoleId, kMasks, false, ""}});
  EXPECT_TRUE(snap.ExemptFromPolicies(Principal::User("u-rep", "rep"), kRows));
  EXPECT_FALSE(snap.ExemptFromPolicies(Principal::User("u-bob", "bob"), kRows));
  EXPECT_FALSE(snap.ExemptFromPolicies(Principal::User("u-cy", "cy"), kRows));
  // PUBLIC (everyone's) never carries one, whatever the store says.
  EXPECT_FALSE(snap.ExemptFromPolicies(Principal::User("u-rep", "rep"), kMasks));
  // SET ROLE to another role leaves it behind.
  Principal narrowed = Principal::User("u-rep", "rep");
  narrowed.active_role_id = kPublicRoleId;
  EXPECT_FALSE(snap.ExemptFromPolicies(narrowed, kRows));
}

TEST(PolicyExemption, NeverDelegatedWhateverTheStoreSays) {
  // A row claiming an admin option gives its holder no right to grant.
  const Snapshot snap = Make({{"r-repl", kRows, true, ""}, {"r-repl", SystemPrivilege::kManageConfig, true, ""}});
  const Principal rep = Principal::User("u-rep", "rep");
  EXPECT_FALSE(snap.CanGrantSystemPrivilege(rep, kRows));
  EXPECT_FALSE(snap.CanGrantSystemPrivilege(rep, kMasks));
  EXPECT_TRUE(snap.CanGrantSystemPrivilege(rep, SystemPrivilege::kManageConfig));  // the twin
  EXPECT_TRUE(snap.CanGrantSystemPrivilege(Principal::Superuser("admin"), kRows));
}

TEST(PolicyExemption, ParsedAsSystemPrivilegesAndKeptWhereOlderServersSkipThem) {
  auto parsed = ParseRbacStatement("GRANT EXEMPT ROW POLICIES, exempt masking policies TO ROLE replicator");
  ASSERT_TRUE(parsed.ok()) << parsed.status().message();
  const auto& grant = std::get<SystemPrivilegeCommand>(*parsed);
  EXPECT_EQ(grant.privileges, (std::vector<SystemPrivilege>{kRows, kMasks}));
  EXPECT_EQ(SystemPrivilegeName(kRows), "EXEMPT ROW POLICIES");
  EXPECT_EQ(SystemPrivilegeFromName("EXEMPT MASKING POLICIES"), kMasks);
  // One word is a role's name, as before.
  auto role = ParseRbacStatement("GRANT exempt TO bob");
  ASSERT_TRUE(role.ok()) << role.status().message();
  EXPECT_FALSE(std::holds_alternative<SystemPrivilegeCommand>(*role));
  // A server before 2.14 refuses a store whose system_privileges names a privilege it does
  // not know: they are kept in storage_privileges, which it skips.
  EXPECT_FALSE(IsOriginalSystemPrivilege(kRows));
  EXPECT_FALSE(IsOriginalSystemPrivilege(kMasks));
  EXPECT_FALSE(StorageOf(kRows).has_value());
  EXPECT_TRUE(IsPolicyExemption(kMasks));
  EXPECT_FALSE(IsPolicyExemption(SystemPrivilege::kReadFilesystem));
}

}  // namespace

namespace {

TEST(CatalogAccessToken, UsesNoSystemPrivilegeAsAScopedTokenUsesNone) {
  const Snapshot snap = Make({{"r-repl", SystemPrivilege::kManageConfig, true, ""},
                              {"r-repl", SystemPrivilege::kReadFilesystem, false, "", "/data/in/"}});
  Principal limited = Principal::User("u-rep", "rep");
  limited.catalog_access_token = true;
  EXPECT_TRUE(limited.token_holds_no_system_privilege());
  EXPECT_FALSE(snap.HasSystemPrivilege(limited, SystemPrivilege::kManageConfig));
  EXPECT_FALSE(snap.CanGrantSystemPrivilege(limited, SystemPrivilege::kManageConfig));
  EXPECT_EQ(snap.StorageLocationsOf(limited, SystemPrivilege::kReadFilesystem), std::vector<std::string>{});
  // The twin: the same user without the rules.
  const Principal plain = Principal::User("u-rep", "rep");
  EXPECT_FALSE(plain.token_holds_no_system_privilege());
  EXPECT_TRUE(snap.HasSystemPrivilege(plain, SystemPrivilege::kManageConfig));
  EXPECT_TRUE(snap.CanGrantSystemPrivilege(plain, SystemPrivilege::kManageConfig));
  EXPECT_EQ(snap.StorageLocationsOf(plain, SystemPrivilege::kReadFilesystem),
            std::optional<std::vector<std::string>>(std::vector<std::string>{"/data/in/"}));
}

}  // namespace
