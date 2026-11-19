// The in-memory model of the access-control store: an immutable snapshot of
// users, roles, role memberships, object grants and owners, plus the
// authorization questions asked of it. A snapshot is replaced as a whole when
// the store changes, so a check never sees a half-applied GRANT or REVOKE.

#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "authz/locations.hpp"
#include "authz/types.hpp"

namespace hardcharger::authz {

// Hardcharger's read-only catalog of views (hardcharger_index_info, the
// hardcharger_information_schema views), which every principal may read.
constexpr const char* kServerSystemCatalog = "_hardcharger_system";

// Every user is implicitly a member of PUBLIC.
constexpr const char* kPublicRoleId = "public";
constexpr const char* kPublicRoleName = "PUBLIC";

struct UserRecord {
  std::string id;
  std::string name;
  std::optional<std::string> password_hash;  // nullopt: no password login
  bool enabled = true;
  // Set by the administrator (ALTER USER ... SET ATTRIBUTE); row and masking
  // policies read them with session_attribute('key').
  std::map<std::string, std::string> attributes;
};

struct RoleRecord {
  std::string id;
  std::string name;
  bool builtin = false;
};

enum class PrincipalKind { kUser, kRole };

// `member` (a user or a role) is granted `role_id`.
struct MembershipRecord {
  PrincipalKind member_kind = PrincipalKind::kUser;
  std::string member_id;
  std::string role_id;
  bool admin_option = false;
  std::string granted_by;  // user id of the grantor; empty for the administrator
};

struct ObjectGrantRecord {
  std::string id;
  std::string role_id;
  GrantScope scope = GrantScope::kObject;
  ObjectPath target;  // catalog [+ schema path [+ object]] per scope
  Privilege privilege = Privilege::kSelect;
  std::vector<std::string> columns;  // SELECT only; empty = every column
  bool grant_option = false;
  std::string granted_by;  // user id of the grantor; empty for the administrator
};

// CREATE TENANT name. A tenant's users and roles are named "tenant/name";
// its catalogs are assigned with ALTER CATALOG ... SET TENANT.
struct TenantRecord {
  std::string id;
  std::string name;
  std::string default_catalog;  // empty: none
};

// Which tenant a user or role belongs to (none: system scope).
struct PrincipalTenantRecord {
  PrincipalKind kind = PrincipalKind::kUser;
  std::string principal_id;
  std::string tenant_id;
};

// Which tenant a catalog belongs to (none: only system-scope users see it,
// and only if they could before).
struct CatalogTenantRecord {
  std::string catalog;
  std::string tenant_id;
};

// CREATE GROUP MAPPING 'group' [ISSUER 'iss'] TO ROLE role: a user whose
// login token lists the group (from that issuer, or any) holds the role.
struct GroupMappingRecord {
  std::string id;
  std::string issuer;  // empty: any issuer the server accepts
  std::string group;
  std::string role_id;
};

// CREATE SCOPED TOKEN: a token a user mints for a program, allowed only some
// verbs on some catalogs until it expires, and never more than the user. A
// token minted under another (parent_id) is narrower still and dies with it.
struct ScopedTokenRecord {
  std::string id;
  std::string name;
  std::string user_id;
  std::string parent_id;              // empty: minted by a user or the administrator
  std::vector<Privilege> privileges;  // SELECT, INSERT, UPDATE, DELETE
  std::vector<std::string> catalogs;  // empty: any the user can reach
  int64_t expires_at = 0;             // seconds since the epoch
  std::string created_by;             // user id; empty: the administrator
};

// What a session that logged in with a scoped token may do at most.
struct TokenScope {
  std::string id;
  std::string name;
  std::set<Privilege> privileges;
  std::vector<std::string> catalogs;
  int64_t expires_at = 0;
};

// The two roles every tenant has.
constexpr const char* kTenantPublicSuffix = "/PUBLIC";
constexpr const char* kTenantAdminSuffix = "/tenant_admin";

// CREATE ROW POLICY name ON table [AS PERMISSIVE | RESTRICTIVE] [TO roles] USING (predicate)
struct RowPolicyRecord {
  std::string id;
  std::string name;
  ObjectPath table;
  bool permissive = true;
  std::vector<std::string> role_ids;  // empty: every role
  std::string predicate;              // SQL, as written
};

// CREATE MASKING POLICY name AS (parameter type) RETURNS type -> body
struct MaskingPolicyRecord {
  std::string id;
  std::string name;
  std::string parameter;
  std::string parameter_type;
  std::string return_type;
  std::string body;  // SQL over `parameter`, as written
};

// ALTER TABLE t MODIFY COLUMN c SET MASKING POLICY p [EXEMPT ROLE r, ...]
struct MaskBindingRecord {
  ObjectPath table;
  std::string column;
  std::string policy_id;
  std::vector<std::string> exempt_role_ids;
};

// A system privilege a role holds (one record per grantor and location).
struct SystemPrivilegeRecord {
  std::string role_id;
  SystemPrivilege privilege = SystemPrivilege::kCheckpoint;
  bool admin_option = false;
  std::string granted_by;  // user id of the grantor; empty for the administrator
  // A storage privilege's location, as ParseGrantLocation stores it; empty: everywhere.
  std::string location = {};
};

// GRANT SYSTEM ADMIN TO user | ROLE role: the principal may act as the administrator in a
// session that says SET ROLE SYSTEM ADMIN (and only there, until RESET ROLE). Nothing else
// in the store confers it: it is no role and no privilege of ALL, and has no admin option.
struct SystemAdminRecord {
  PrincipalKind kind = PrincipalKind::kUser;
  std::string principal_id;
  // The user who granted it while acting as System Admin (for SHOW GRANTS; the grant
  // stands without them); empty: the administrator.
  std::string granted_by;
};

// A password someone other than its user set and the user must change before doing anything
// else (2.14): one a delegate (an ALTER USER or CREATE USER privilege holder, a tenant admin)
// set, or one an administrator set WITH ... EXPIRE (or ALTER USER ... PASSWORD EXPIRE). It
// holds only for the password it was made for (`password_fingerprint`, PasswordFingerprint
// of the stored hash): any later password, set by anyone, leaves the record standing for
// nothing.
struct PasswordResetRecord {
  std::string user_id;
  std::string password_fingerprint;
  std::string set_by;  // user id of who set it; empty for the administrator
  int64_t set_at_ms = 0;
};

// The capability's name, as GRANT, SET ROLE and the audit records write it.
constexpr const char* kSystemAdminName = "SYSTEM ADMIN";
// The active role of a session that said SET ROLE SYSTEM ADMIN: the id of no role (role
// ids are random, or "public"), so a principal that carries it without holding SYSTEM
// ADMIN acts with no role at all, never with a role's rights.
constexpr const char* kSystemAdminRoleId = "\x1dsystem-admin";

// kSchema: the object is the schema itself (catalog + schema path, no name).
// kSequence is asked of (USAGE), never recorded: sequences have no owner records.
// kTableFunction is asked of (EXECUTE) and never recorded either: a table function is
// the engine's, in no catalog (ObjectPath::IsTableFunction). kTrigger is recorded (a user's
// trigger body runs with its owner's rights) in a key space of its own: a trigger may be
// named like a table of its schema.
enum class ObjectType { kTable, kView, kSchema, kSequence, kTableFunction, kTrigger };

enum class OwnerKind { kSystem, kUser, kRole };

struct OwnerRecord {
  ObjectPath object;
  ObjectType type = ObjectType::kTable;
  OwnerKind owner_kind = OwnerKind::kSystem;
  std::string owner_id;  // user or role id; empty for kSystem
  // Views: a hash of the view's definition when ownership was recorded. A view
  // reads with its owner's rights only while its definition still matches, so
  // a record left behind by a dropped view never applies to a new one.
  std::string definition_hash;
};

// A catalog_access rule from a sign-in token (per-catalog permissions): the first rule
// whose pattern
// matches a catalog decides; no match is no access.
enum class CatalogAccessLevel { kNone, kRead, kWrite };
struct CatalogRule {
  std::string pattern;  // '*' any run, '?' one character; case-sensitive
  CatalogAccessLevel level = CatalogAccessLevel::kNone;
};
// The level the rules give `catalog`. DuckDB's system catalog (which holds
// information_schema) and Hardcharger's _hardcharger_system are read for all.
CatalogAccessLevel CatalogRuleLevel(const std::vector<CatalogRule>& rules, const std::string& catalog);
// The system privileges an administrator's token with catalog_access rules holds: CHECKPOINT
// only. The others reach data its rules don't name, or (MANAGE CONFIG: SET GLOBAL, settings
// that hold credentials) change the server for every other tenant.
bool IsRulesAdministratorPrivilege(SystemPrivilege privilege);
// The words of a refusal of one of the others to it.
std::string RulesAdministratorRefusal(const std::string& what, SystemPrivilege privilege);
// The words of a refusal of a system privilege to a store user's session whose sign-in
// token carries catalog_access rules (Principal::catalog_access_token): it uses none.
std::string CatalogAccessTokenRefusal(const std::string& what, SystemPrivilege privilege, const std::string& user);
// The words of a catalog_access refusal.
std::string CatalogAccessDenied(const std::string& catalog, CatalogAccessLevel needed, const std::string& user);

// Who is asking. The administrator (a superuser) is never looked up in the
// store and is allowed everything. A view owned by a role reads with that
// role's rights: a role principal has `role_id` set and no `user_id`.
struct Principal {
  bool superuser = false;
  std::string user_id;    // for a store user
  std::string user_name;  // for messages and audit (a role principal: the role's name)
  std::string role_id;    // for a role principal
  // SET ROLE: the session acts with this role (what it inherits, and PUBLIC)
  // only. Empty: with all of the user's roles.
  std::string active_role_id;
  std::string active_role_name;  // for audit records
  // The identity provider's groups from the login token, and its issuer
  // (CREATE GROUP MAPPING turns them into roles).
  std::string idp_issuer;
  std::vector<std::string> idp_groups;
  // Logged in with a scoped token: never more than this.
  std::optional<TokenScope> scope;
  // The token's catalog_access rules decide alone, by catalog: without a store
  // (rules-only access control), and for an administrator's token under RBAC too. Such
  // a principal holds no system privilege (no files, URLs, secrets, extensions, ATTACH,
  // server-wide or credential settings), unless it is the administrator with rules.
  std::optional<std::vector<CatalogRule>> catalog_rules;
  bool rules_administrator = false;
  // Catalogs the server keeps for itself (instrumentation, logs), which no one but
  // the administrator sees.
  std::vector<std::string> hidden_catalogs;
  // A store user acting as System Admin (SET ROLE SYSTEM ADMIN): `superuser` is set, as
  // for the administrator, and this is the user (for audit records and for checking, in
  // the store transaction of each change it makes, that it still holds SYSTEM ADMIN).
  // Empty for the administrator itself. Only RbacSessionState::principal() makes one.
  std::string acting_user_id;
  // The session holds a policy exemption (EXEMPT ROW / MASKING POLICIES) and the store did
  // not answer when this request asked it (SessionGuard::VerifySystemAdmin): no exemption
  // applies (Snapshot::ExemptFromPolicies). Only RbacSessionState::principal() sets it.
  bool policy_exemptions_unverified = false;
  // A store user's session whose sign-in token carries catalog_access rules (which grant a
  // store user nothing under access control, and are ignored for its data): like a scoped
  // token's, it uses no system privilege (token_holds_no_system_privilege): no account
  // privilege (CREATE USER, ...), no policy exemption, and it never acts as System Admin.
  bool catalog_access_token = false;

  static Principal Superuser(std::string name) {
    Principal p;
    p.superuser = true;
    p.user_name = std::move(name);
    return p;
  }
  static Principal User(std::string id, std::string name) {
    Principal p;
    p.user_id = std::move(id);
    p.user_name = std::move(name);
    return p;
  }
  static Principal Role(std::string id, std::string name) {
    Principal p;
    p.user_name = std::move(name);
    p.role_id = std::move(id);
    return p;
  }
  bool is_role() const { return !superuser && user_id.empty() && !role_id.empty(); }
  // THE question for every system privilege (READ FILESYSTEM, MANAGE CATALOGS, the policy
  // exemptions, ...), everywhere and on a location, used or granted on: a session limited by
  // its sign-in token, a scoped token or a store user's token with catalog_access rules,
  // uses none of them, whatever its user's roles hold.
  bool token_holds_no_system_privilege() const { return scope.has_value() || catalog_access_token; }
  // A store user's session that asked for SYSTEM ADMIN (whether or not it still holds it).
  bool asks_system_admin() const { return active_role_id == kSystemAdminRoleId; }
  bool acts_as_system_admin() const { return superuser && !acting_user_id.empty(); }
  // THE question for anything only an unrestricted administrator may do (granting SYSTEM
  // ADMIN, changing a user or role that carries it, and whatever later privileges are
  // reserved the same way): is this principal the administrator, signed in without
  // catalog_access rules, or a store user's session acting as System Admin right now? Ask
  // it here, of the principal taken for the statement at hand, and never keep the answer:
  // RbacSessionState::principal() hands out the second kind only while the session said SET
  // ROLE SYSTEM ADMIN, its user holds SYSTEM ADMIN, and the store answered for this request
  // (SessionGuard::VerifySystemAdmin). A session's own view of it is
  // SessionGuard::unrestricted(); a store change re-checks it in its own transaction
  // (RequireStillSystemAdmin in engine.cc).
  bool is_unrestricted_administrator() const { return superuser; }
};

// One required privilege on one object, as the enforcement layer derives it
// from a statement's plan.
struct AccessRequest {
  ObjectPath object;  // full path to a table or view; for CREATE: the new object
  ObjectType type = ObjectType::kTable;
  Privilege privilege = Privilege::kSelect;
  std::vector<std::string> columns;  // SELECT: the columns read
};

struct AccessDecision {
  bool allowed = false;
  std::string reason;           // which grant satisfied it, or what is missing
  std::string satisfying_role;  // role (or "owner") whose grant allowed it
};

class Snapshot {
 public:
  Snapshot(int64_t version, std::vector<UserRecord> users, std::vector<RoleRecord> roles,
           std::vector<MembershipRecord> memberships, std::vector<ObjectGrantRecord> grants,
           std::vector<OwnerRecord> owners,
           std::vector<SystemPrivilegeRecord> system_privileges = {},
           std::vector<RowPolicyRecord> row_policies = {},
           std::vector<MaskingPolicyRecord> masking_policies = {},
           std::vector<MaskBindingRecord> mask_bindings = {},
           std::vector<TenantRecord> tenants = {},
           std::vector<PrincipalTenantRecord> principal_tenants = {},
           std::vector<CatalogTenantRecord> catalog_tenants = {},
           std::vector<GroupMappingRecord> group_mappings = {},
           std::vector<ScopedTokenRecord> scoped_tokens = {},
           std::vector<SystemAdminRecord> system_admins = {},
           std::vector<PasswordResetRecord> password_resets = {});

  int64_t version() const { return version_; }

  // This snapshot as `version`, with the owner record of `object` (of `type`) replaced by
  // `owner` (nullptr: removed): what the store holds after one owner change committed on
  // top of this one.
  std::shared_ptr<const Snapshot> WithOwner(int64_t version, const ObjectPath& object, ObjectType type,
                                            const OwnerRecord* owner) const;

  const UserRecord* FindUserByName(const std::string& name) const;
  const UserRecord* FindUserById(const std::string& id) const;
  const RoleRecord* FindRoleByName(const std::string& name) const;
  const RoleRecord* FindRoleById(const std::string& id) const;
  const std::vector<UserRecord>& users() const { return users_; }
  const std::vector<RoleRecord>& roles() const { return roles_; }
  const std::vector<MembershipRecord>& memberships() const { return memberships_; }
  const std::vector<ObjectGrantRecord>& grants() const { return grants_; }
  const std::vector<OwnerRecord>& owners() const { return owners_; }
  const std::vector<SystemPrivilegeRecord>& system_privileges() const { return system_privileges_; }
  const std::vector<RowPolicyRecord>& row_policies() const { return row_policies_; }
  const std::vector<MaskingPolicyRecord>& masking_policies() const { return masking_policies_; }
  const std::vector<MaskBindingRecord>& mask_bindings() const { return mask_bindings_; }
  const std::vector<TenantRecord>& tenants() const { return tenants_; }
  const std::vector<CatalogTenantRecord>& catalog_tenants() const { return catalog_tenants_; }
  const std::vector<GroupMappingRecord>& group_mappings() const { return group_mappings_; }
  const std::vector<ScopedTokenRecord>& scoped_tokens() const { return scoped_tokens_; }
  // The SYSTEM ADMIN grants that count: to a system-scope user, or to a system-scope role
  // that is not built in. A record that names anything else (a principal that is gone, a
  // tenant's, PUBLIC) is left out when the snapshot is made, so it gives nothing.
  const std::vector<SystemAdminRecord>& system_admins() const { return system_admins_; }
  // The user's password must be changed before anything else (its temporary-password record,
  // when it was made for the password the user has now); nullptr when not.
  const PasswordResetRecord* PasswordMustChange(const std::string& user_id) const;
  const std::vector<PasswordResetRecord>& password_resets() const { return password_resets_; }
  const ScopedTokenRecord* FindScopedToken(const std::string& id) const;
  // The scope of a token that still stands for `user_id` at `now` (its user's
  // own, unexpired, and every token it was minted under still there).
  std::optional<TokenScope> ValidScope(const std::string& token_id, const std::string& user_id,
                                       int64_t now) const;
  // Why a scoped token no longer stands for `user_id` at `now` (Chargewire's reason):
  // "token_revoked" when it or a token it was minted under is gone (revoked, or not the
  // user's), "token_expired" when one of them expired; nullopt when it stands.
  std::optional<std::string> ScopeRefusal(const std::string& token_id, const std::string& user_id,
                                          int64_t now) const;
  const TenantRecord* FindTenantByName(const std::string& name) const;
  const TenantRecord* FindTenantById(const std::string& id) const;
  // "" for a system-scope user, role or catalog.
  std::string TenantOf(PrincipalKind kind, const std::string& id) const;
  std::string TenantOfPrincipal(const Principal& principal) const;
  std::string TenantOfCatalog(const std::string& catalog) const;
  // The catalogs a tenant's users may see (folded names).
  std::set<std::string> TenantCatalogs(const std::string& tenant_id) const;
  // May the principal see objects of this catalog? System-scope principals:
  // always. Tenant users: their tenant's catalogs, plus temp and system.
  bool CatalogVisible(const Principal& principal, const std::string& catalog) const;
  // Does the principal see only some catalogs (a tenant's user, catalog_access
  // rules, the server's hidden catalogs)? Then listings are filtered for it.
  bool RestrictsCatalogs(const Principal& principal) const;
  const RoleRecord* TenantRole(const std::string& tenant_id, const char* suffix) const;

  const MaskingPolicyRecord* FindMaskingPolicyByName(const std::string& name) const;
  const MaskingPolicyRecord* FindMaskingPolicyById(const std::string& id) const;
  // Row policies on the table, and of those the ones that apply to the principal.
  std::vector<const RowPolicyRecord*> RowPoliciesOn(const ObjectPath& table) const;
  std::vector<const RowPolicyRecord*> ApplicableRowPolicies(const Principal& principal,
                                                            const ObjectPath& table) const;
  // Masked columns of the table the principal is not exempt from: column -> policy.
  std::vector<std::pair<std::string, const MaskingPolicyRecord*>> ApplicableMasks(
      const Principal& principal, const ObjectPath& table) const;

  // Does the principal hold the system privilege through one of its roles (a storage
  // privilege: everywhere)? Always true for the administrator.
  bool HasSystemPrivilege(const Principal& principal, SystemPrivilege privilege) const;
  // Is the principal exempt from the policies `exemption` names (EXEMPT ROW POLICIES: every
  // row policy on every table; EXEMPT MASKING POLICIES: every mask)? The administrator is;
  // a store user or role through a role that holds it, never through a scoped token, a
  // token with catalog_access rules, or as a tenant's user or role, and not while its
  // session's check with the store did not answer (Principal::policy_exemptions_unverified).
  bool ExemptFromPolicies(const Principal& principal, SystemPrivilege exemption) const;
  // Does it hold the storage privilege `privilege` everywhere or on a location that covers
  // `path`? Always true for the administrator.
  bool HasStorageAccess(const Principal& principal, SystemPrivilege privilege, const StoragePath& path,
                        const LocalPathContext& context) const;
  // The locations the principal holds the storage privilege `privilege` on, or nullopt
  // when it holds it everywhere (the administrator does).
  std::optional<std::vector<std::string>> StorageLocationsOf(const Principal& principal,
                                                             SystemPrivilege privilege) const;
  // May it grant and revoke the privilege (on `location`, a stored location key; empty:
  // everywhere)? It holds it WITH ADMIN OPTION there, or everywhere. An account privilege:
  // only an unrestricted administrator.
  bool CanGrantSystemPrivilege(const Principal& principal, SystemPrivilege privilege,
                               const std::string& location = {}) const;

  // Every role the user holds: PUBLIC, direct roles, and roles those roles
  // were granted, transitively.
  std::set<std::string> EffectiveRoleIds(const std::string& user_id) const;
  // Every role `role_id` holds, itself included.
  std::set<std::string> RoleClosure(const std::string& role_id) const;
  // Would granting `role_id` to role `member_role_id` create a cycle?
  bool WouldCreateCycle(const std::string& role_id, const std::string& member_role_id) const;

  // The owner record of a table, view or schema at `object` (never a trigger's).
  const OwnerRecord* FindOwner(const ObjectPath& object) const;
  // The owner record of the trigger at `object` (catalog, schema path, trigger name).
  const OwnerRecord* FindTriggerOwner(const ObjectPath& object) const;

  // The question every statement asks. Superusers are always allowed.
  AccessDecision Check(const Principal& principal, const AccessRequest& request) const;

  // ---- listings (Chargewire §9.4: metadata shows only what the principal may access) ----
  // Every listing asks Check() above, so it follows the same rules as SQL. DuckDB's own
  // views (catalog system) and the session's temporary objects (temp) are listed for every
  // principal that sees their catalog.
  // A table or view is listed when the principal holds SELECT on it or on any of its
  // columns, or INSERT, UPDATE or DELETE on it (owning it holds them all).
  bool ObjectListed(const Principal& principal, const ObjectPath& object) const;
  // A sequence: listed to whoever may use it (USAGE) or drop it (DROP SEQUENCE on its schema).
  bool SequenceListed(const Principal& principal, const ObjectPath& sequence) const;
  // The path arguments declared by the EXECUTE grants the principal's roles hold on the
  // table function `name` (every one of them, whichever grant declared it); nullopt when
  // none of its roles holds EXECUTE on it. A scoped token holds none, as with system
  // privileges; the administrator holds every function with nothing declared.
  std::optional<std::vector<PathArgument>> TableFunctionPathArguments(const Principal& principal,
                                                                      const std::string& name) const;
  // A column, when the principal may SELECT it, or holds INSERT, UPDATE or DELETE on its table.
  bool ColumnListed(const Principal& principal, const ObjectPath& table, const std::string& column) const;
  // A schema, when the principal holds a grant on it, on a schema it is in or holds, or on
  // an object in it, or owns it or an object in it.
  bool SchemaListed(const Principal& principal, const ObjectPath& schema) const;

  // Is the principal the owner of the object (directly, or through a role)?
  bool IsOwner(const Principal& principal, const OwnerRecord& owner) const;

  // Every role whose grants apply to the principal (empty for an unknown or
  // disabled user, or an unknown role).
  std::set<std::string> RolesOf(const Principal& principal) const;
  // The roles a user holds: granted ones and those their login token's groups
  // map to, with what each inherits (not narrowed by SET ROLE).
  std::set<std::string> HeldRoleIds(const Principal& principal) const;
  // False when the session's SET ROLE role is no longer granted to the user (SET ROLE
  // SYSTEM ADMIN: when the user no longer holds SYSTEM ADMIN).
  bool HoldsActiveRole(const Principal& principal) const;

  // ---- account privileges (CREATE USER, ALTER USER, DROP USER, CREATE ROLE, DROP ROLE) ----
  // ---- and policy exemptions (EXEMPT ROW POLICIES, EXEMPT MASKING POLICIES) ----
  // Of AdministratorGrantedRolesOf, the roles the session acts with (RolesOf: SET ROLE
  // narrows them), never PUBLIC or a tenant's role: the roles through which the principal
  // holds a policy exemption or (AccountPrivilegeRoles) an account privilege. A membership a
  // role's admin-option holder granted never carries one, so they spread only by an
  // administrator's hand, like SYSTEM ADMIN. Empty for the administrator (which needs none),
  // a disabled or tenant's user, a scoped token, a token with catalog_access rules and a
  // session under catalog rules.
  std::set<std::string> AdministratorGrantedActingRoles(const Principal& principal) const;
  // AdministratorGrantedActingRoles for a user's session; empty for a role principal.
  std::set<std::string> AccountPrivilegeRoles(const Principal& principal) const;
  // Does the role hold an account privilege, itself or through a role it was granted (by
  // anyone)? Membership in it is then the administrator's to grant.
  bool RoleCarriesAccountPrivilege(const std::string& role_id) const;
  // Does the role hold a policy exemption, itself or through a role it was granted (by
  // anyone)? The administrator's GRANT of it then takes a delegated membership over.
  bool RoleCarriesPolicyExemption(const std::string& role_id) const;

  // ---- SYSTEM ADMIN ----
  // How the store user holds SYSTEM ADMIN, one entry per way: "" for a grant to the user
  // itself, else the id of a role granted it that the user holds. Empty: it does not hold
  // it. Only an enabled system-scope user holds it, and a role gives it only through
  // memberships the administrator (or a user acting as System Admin) granted, and through
  // identity-provider group mappings, which only they create: a membership a role's
  // admin-option holder granted never carries it, whoever wrote it and with whatever
  // server version. Not narrowed by SET ROLE; a session's own limits (a scoped token, a
  // read-only connection) are the session's to apply.
  std::vector<std::string> SystemAdminSources(const Principal& principal) const;
  bool HoldsSystemAdmin(const Principal& principal) const { return !SystemAdminSources(principal).empty(); }
  // Does the role hold SYSTEM ADMIN, itself or through a role it was granted (by anyone)?
  // Membership in such a role, and the role itself, are the administrator's to change.
  bool RoleCarriesSystemAdmin(const std::string& role_id) const;
  // Does the stored user hold SYSTEM ADMIN by a grant to itself or through a role it was
  // granted (by anyone)? Such a user is the administrator's to change.
  bool UserCarriesSystemAdmin(const std::string& user_id) const;

  // Everything the principal may do, as comparable strings (each grant through
  // its roles, each object it owns). A right in one snapshot's set that is
  // missing from a later one was taken away.
  std::set<std::string> RightsOf(const Principal& principal) const;
  // Did the principal lose a right between `before` and this snapshot? Owning
  // an object that has no owner at all any more (it was dropped) is not one.
  bool LostRightsSince(const Snapshot& before, const Principal& principal) const;

  // ---- delegation ----
  // May the principal give `privilege` on `target` (at `scope`, on `columns`
  // for SELECT) to others: as the owner of the object or of its schema, or
  // holding it WITH GRANT OPTION through one of its roles? Always true for
  // the administrator.
  bool CanGrant(const Principal& principal, GrantScope scope, const ObjectPath& target,
                Privilege privilege, const std::vector<std::string>& columns) const;
  // May the principal grant and revoke membership in the role (it holds the
  // role WITH ADMIN OPTION)? Always true for the administrator.
  bool CanAdminRole(const Principal& principal, const std::string& role_id) const;

  // Delegated grants and memberships that no longer stand, because their
  // grantor can no longer make them. What stands is worked out from what the
  // administrator and the owners granted, so grants made through each other
  // in a cycle do not keep one another alive.
  struct Fallen {
    std::vector<std::string> grant_ids;
    std::vector<MembershipRecord> memberships;
    std::vector<SystemPrivilegeRecord> system_privileges;
    bool empty() const {
      return grant_ids.empty() && memberships.empty() && system_privileges.empty();
    }
  };
  Fallen FallenDelegations() const;

 private:
  bool GrantCovers(const ObjectGrantRecord& grant, const AccessRequest& request) const;
  // Every role `role_id` holds through memberships the administrator granted, itself included.
  std::set<std::string> AdministratorGrantedClosure(const std::string& role_id) const;
  // The one walk behind SYSTEM ADMIN, the account privileges and the policy exemptions: every
  // system-scope role the user holds through memberships the administrator granted and through
  // its identity-provider groups' mappings, with what those inherit the same way (a role
  // principal: the role and what it inherits so). Empty for the administrator, an unknown or
  // disabled user and a tenant's user.
  std::set<std::string> AdministratorGrantedRolesOf(const Principal& principal) const;

  int64_t version_;
  std::vector<UserRecord> users_;
  std::vector<RoleRecord> roles_;
  std::vector<MembershipRecord> memberships_;
  std::vector<ObjectGrantRecord> grants_;
  std::vector<OwnerRecord> owners_;
  std::vector<SystemPrivilegeRecord> system_privileges_;
  std::vector<RowPolicyRecord> row_policies_;
  std::vector<MaskingPolicyRecord> masking_policies_;
  std::vector<MaskBindingRecord> mask_bindings_;
  std::vector<TenantRecord> tenants_;
  std::vector<PrincipalTenantRecord> principal_tenants_;
  std::vector<CatalogTenantRecord> catalog_tenants_;
  std::vector<GroupMappingRecord> group_mappings_;
  std::vector<ScopedTokenRecord> scoped_tokens_;
  std::vector<SystemAdminRecord> system_admins_;
  std::vector<PasswordResetRecord> password_resets_;
  std::map<std::string, size_t> user_by_key_, user_by_id_, role_by_key_, role_by_id_;
  std::map<std::string, std::vector<const ObjectGrantRecord*>> grants_by_role_;
};

// Case-folded key for an object path, used for owner lookup and dedupe.
std::string ObjectKey(const ObjectPath& path);

// The key an owner record is stored and found under: ObjectKey for a table, view or
// schema; a trigger's lives in a key space of its own.
std::string OwnerKey(ObjectType type, const ObjectPath& path);

}  // namespace hardcharger::authz
