#include "authz/engine.hpp"

#include <algorithm>
#include <cctype>
#include <ctime>
#include <set>
#include <sstream>

#include "authz/audit_fields.hpp"
#include "authz/password.hpp"
#include "authz/policies.hpp"
#include "logging.hpp"
#include "platform.hpp"

namespace hardcharger::authz {

arrow::Status AccessControlError(const std::string& message) {
  return arrow::Status::Invalid("Hardcharger access control: " + message);
}

namespace {

// Marks the refusal IsWrongCurrentPassword() recognizes (never shown to the client).
class WrongCurrentPasswordDetail : public arrow::StatusDetail {
 public:
  static constexpr const char* kTypeId = "hardcharger.authz.wrong_current_password";
  const char* type_id() const override { return kTypeId; }
  std::string ToString() const override { return "wrong current password"; }
};

}  // namespace

bool IsWrongCurrentPassword(const arrow::Status& status) {
  const auto& detail = status.detail();
  return detail != nullptr && std::string(detail->type_id()) == WrongCurrentPasswordDetail::kTypeId;
}

namespace {

int64_t NowMilliseconds() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

CommandResult Message(const std::string& text) {
  CommandResult result;
  result.column_names = {"result"};
  result.rows = {{text}};
  return result;
}

std::string Join(const std::vector<std::string>& items, const std::string& sep = ", ") {
  std::string out;
  for (size_t i = 0; i < items.size(); ++i) out += (i ? sep : "") + items[i];
  return out;
}

std::string ScopeLabel(GrantScope scope) {
  switch (scope) {
    case GrantScope::kObject:
      return "TABLE";
    case GrantScope::kSchema:
      return "SCHEMA";
    case GrantScope::kCatalog:
      return "CATALOG";
  }
  return "TABLE";
}

bool SamePath(const ObjectPath& a, const ObjectPath& b) { return ObjectKey(a) == ObjectKey(b); }

// "argument 2", "parameter files": an EXECUTE grant's stored path declarations, for people.
std::vector<std::string> PathArgumentTexts(const std::vector<std::string>& stored) {
  std::vector<std::string> texts;
  for (const auto& s : stored) {
    if (auto argument = PathArgument::FromStored(s)) texts.push_back(argument->Text());
  }
  return texts;
}

// The privileges one GRANT or REVOKE names, one record each (ALL: every privilege that
// applies at the scope).
std::vector<Privilege> Expand(Privilege p, GrantScope scope) {
  if (p == Privilege::kAll) return PrivilegesOfAll(scope);
  return {p};
}

struct ResolvedGrantee {
  PrincipalKind kind;
  std::string id;
  std::string name;
};

// Users and roles share one namespace, so an unqualified name is unambiguous.
arrow::Result<ResolvedGrantee> ResolveGrantee(const Snapshot& snap, const Grantee& g) {
  if (g.kind != GranteeKind::kRole) {
    if (const auto* user = snap.FindUserByName(g.name)) {
      return ResolvedGrantee{PrincipalKind::kUser, user->id, user->name};
    }
    if (g.kind == GranteeKind::kUser)
      return AccessControlError("user " + g.name + " does not exist");
  }
  if (const auto* role = snap.FindRoleByName(g.name)) {
    return ResolvedGrantee{PrincipalKind::kRole, role->id, role->name};
  }
  return AccessControlError((g.kind == GranteeKind::kRole ? "role " : "user or role ") + g.name +
                            " does not exist");
}

arrow::Status CheckNameFree(const Snapshot& snap, const std::string& name) {
  if (IdentifiersEqual(name, kPublicRoleName)) {
    return AccessControlError("PUBLIC is a reserved role name");
  }
  if (IdentifiersEqual(name, "CURRENT_USER")) {
    return AccessControlError("CURRENT_USER is how ALTER USER names the session's own user: no user or role can "
                              "take it");
  }
  if (IdentifiersEqual(name, kSystemAdminName)) {
    return AccessControlError(std::string(kSystemAdminName) +
                              " is the name of the System Admin capability: no user or role can take it");
  }
  if (snap.FindUserByName(name))
    return AccessControlError("a user named " + name + " already exists");
  if (snap.FindRoleByName(name))
    return AccessControlError("a role named " + name + " already exists");
  return arrow::Status::OK();
}

// A URI-form name ("spiffe://example.org/ns/etl/sa/loader"): a scheme (a letter, then
// letters, digits, '+', '-', '.') followed by "://", right at the start. Such a name is a
// system-scope user's, never a tenant's, whatever slashes follow.
bool IsUriName(const std::string& name) {
  const auto colon = name.find("://");
  if (colon == std::string::npos || colon == 0 || !std::isalpha(static_cast<unsigned char>(name[0]))) return false;
  for (std::size_t i = 1; i < colon; i++) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    if (!std::isalnum(c) && c != '+' && c != '-' && c != '.') return false;
  }
  return colon + 3 < name.size();
}

// The tenant a user or role name belongs to: "tenant/name" names a tenant's
// principal, a plain name or a URI-form one (a SPIFFE ID) a system-scope one. "" for
// system scope. A URI-form name is a user's only (`for_user`).
arrow::Result<std::string> TenantForName(const Snapshot& snap, const std::string& name, bool for_user = true) {
  if (IsUriName(name)) {
    if (!for_user) return AccessControlError("a role name cannot be a URI (" + name + ")");
    return std::string();
  }
  const auto slash = name.find('/');
  if (slash == std::string::npos) return std::string();
  const std::string tenant_name = name.substr(0, slash), rest = name.substr(slash + 1);
  if (tenant_name.empty() || rest.empty() || rest.find('/') != std::string::npos) {
    return AccessControlError("a tenant's users and roles are named tenant/name");
  }
  const auto* tenant = snap.FindTenantByName(tenant_name);
  if (!tenant) return AccessControlError("tenant " + tenant_name + " does not exist");
  return tenant->id;
}

// A name CREATE USER (or a first token sign-in, RbacEngine::ProvisionUser) may give a
// new user: free, and neither the administrator's name nor a certificate administrator's.
arrow::Status CheckNewUserName(const Snapshot& snap, const std::string& name, const RbacEngine::Options& options) {
  ARROW_RETURN_NOT_OK(CheckNameFree(snap, name));
  if (!options.administrator_name.empty() && IdentifiersEqual(name, options.administrator_name)) {
    return AccessControlError(name + " is the server administrator's name");
  }
  for (const auto& administrator : options.certificate_administrators) {
    if (IdentifiersEqual(name, administrator)) {
      return AccessControlError(name +
                                " signs in as the server administrator with a client certificate "
                                "(mtls_admin_identities)");
    }
  }
  return arrow::Status::OK();
}

// Does the principal hold the tenant's tenant_admin role?
bool IsTenantAdmin(const Snapshot& snap, const Principal& actor, const std::string& tenant_id) {
  if (tenant_id.empty() || actor.superuser) return false;
  const RoleRecord* admin = snap.TenantRole(tenant_id, kTenantAdminSuffix);
  return admin && snap.RolesOf(actor).count(admin->id) > 0;
}

// Who alone grants and revokes SYSTEM ADMIN, and changes what carries it.
constexpr const char* kOnlySystemAdministrators =
    "only the administrator, or a user acting as System Admin (SET ROLE SYSTEM ADMIN)";

// A user acting as System Admin changes the store only while it still holds SYSTEM ADMIN
// in the store as the change's own transaction reads it: a revoke committed on any replica
// before this transaction, which this replica's snapshot may not show yet, stops it.
arrow::Status RequireStillSystemAdmin(const Snapshot& snap, const Principal& actor, const std::string& kind) {
  if (!actor.acts_as_system_admin()) return arrow::Status::OK();
  Principal user = Principal::User(actor.acting_user_id, actor.user_name);
  user.idp_issuer = actor.idp_issuer;
  user.idp_groups = actor.idp_groups;
  if (snap.HoldsSystemAdmin(user)) return arrow::Status::OK();
  return AccessControlError("Hardcharger blocked " + kind + ": user " + actor.user_name + " no longer holds " +
                            kSystemAdminName + ", which SET ROLE made active. Run RESET ROLE to go on as " +
                            actor.user_name);
}

std::string TenantName(const Snapshot& snap, const std::string& tenant_id) {
  if (tenant_id.empty()) return "the system scope";
  const auto* t = snap.FindTenantById(tenant_id);
  return "tenant " + (t ? t->name : tenant_id);
}

// ---- account management by a holder of an account privilege ------------------------------
//
// CREATE USER, ALTER USER, DROP USER, CREATE ROLE and DROP ROLE are system privileges: a role
// granted one (by an unrestricted administrator, never with an admin option) may run that
// statement on system-scope users and roles that are no more than the session itself. Each
// check below is asked of the store as the change's own transaction reads it, so a revoke
// committed on any replica stops the very next statement.

std::string RoleNamesOf(const Snapshot& snap, const std::set<std::string>& ids) {
  std::vector<std::string> names;
  for (const auto& id : ids) {
    const auto* role = snap.FindRoleById(id);
    names.push_back(role ? role->name : id);
  }
  std::sort(names.begin(), names.end());
  return Join(names);
}

// What lets the principal run an account statement (CREATE / ALTER / DROP USER, CREATE / DROP
// ROLE): the administrator's authority, its tenant's tenant_admin role, or an account
// privilege, which then limits whom it reaches (RequireUserWithinReach,
// RequireRoleWithinReach).
struct AccountAuthority {
  enum class Kind { kAdministrator, kTenantAdmin, kPrivilege, kSelf };
  Kind kind = Kind::kAdministrator;
  std::string text;  // the audit record's authorized_by
  bool delegated() const { return kind == Kind::kPrivilege; }
  // A password this authority sets for another user is temporary, whatever the statement
  // says: everyone's but the administrator's (an unrestricted one decides with EXPIRE).
  bool sets_temporary_passwords() const { return kind == Kind::kTenantAdmin || kind == Kind::kPrivilege; }
};

arrow::Result<AccountAuthority> AuthorizeAccountStatement(const Snapshot& snap, const Principal& actor,
                                                          const RbacCommand& command, const std::string& name) {
  using Kind = AccountAuthority::Kind;
  if (actor.is_unrestricted_administrator()) {
    return AccountAuthority{Kind::kAdministrator, actor.acts_as_system_admin() ? "System Admin" : "administrator"};
  }
  const std::string kind = CommandKind(command);
  // A tenant admin manages the users and roles of its own tenant, as before.
  auto tenant = TenantForName(snap, name);
  if (tenant.ok() && IsTenantAdmin(snap, actor, *tenant)) {
    return AccountAuthority{Kind::kTenantAdmin, "tenant_admin of " + TenantName(snap, *tenant)};
  }
  const SystemPrivilege privilege = *AccountPrivilegeOf(command);
  const std::string privilege_name = SystemPrivilegeName(privilege);
  if (!snap.HasSystemPrivilege(actor, privilege)) {
    // Say so when the session's sign-in is what keeps the privilege away.
    Principal unlimited = actor;
    unlimited.catalog_access_token = false;
    if (actor.catalog_access_token && snap.HasSystemPrivilege(unlimited, privilege)) {
      return AccessControlError("Hardcharger blocked " + kind +
                                ": this session's sign-in token carries catalog_access rules, and such a session "
                                "never uses the " +
                                privilege_name + " privilege");
    }
    return AccessControlError("Hardcharger blocked " + kind +
                              ": only administrators manage users and roles on this server (and, for system-scope "
                              "users and roles, a role granted the " +
                              privilege_name + " privilege)");
  }
  if (!tenant.ok()) return tenant.status();
  if (!tenant->empty()) {
    return AccessControlError("Hardcharger blocked " + kind + ": " + name + " belongs to " + TenantName(snap, *tenant) +
                              ", and the " + privilege_name +
                              " privilege reaches system-scope users and roles only (the tenant's own tenant_admin "
                              "role manages the tenant's)");
  }
  std::set<std::string> through;
  const auto roles = snap.AccountPrivilegeRoles(actor);
  for (const auto& r : snap.system_privileges()) {
    if (r.privilege == privilege && roles.count(r.role_id)) through.insert(r.role_id);
  }
  return AccountAuthority{Kind::kPrivilege, privilege_name + " privilege, through role " + RoleNamesOf(snap, through)};
}

// The server administrator (or a certificate administrator) is configuration, not a store
// user: a holder of an account privilege is told so rather than "does not exist".
arrow::Status RefuseAdministratorAccount(const std::string& name, const std::string& kind,
                                         const RbacEngine::Options& options) {
  bool administrator = !options.administrator_name.empty() && IdentifiersEqual(name, options.administrator_name);
  for (const auto& certificate : options.certificate_administrators) {
    administrator = administrator || IdentifiersEqual(name, certificate);
  }
  if (!administrator) return arrow::Status::OK();
  return AccessControlError("Hardcharger blocked " + kind + ": " + name +
                            " is the server administrator, which is set in the server's configuration and never "
                            "managed with SQL");
}

// Of `roles` (held by the administrator's hand by the user or role an account statement
// changes or drops), the ones the session holds only through a membership a role's
// admin-option holder granted: only the administrator's memberships carry an account
// privilege or a policy exemption, so a new password, ENABLE or DROP would hand over (or take
// away) what the session itself does not carry. The single helper is
// AdministratorGrantedActingRoles, which the privilege and exemption checks use.
std::set<std::string> HeldOnlyByDelegation(const Snapshot& snap, const Principal& actor,
                                           const std::set<std::string>& roles) {
  const auto carried = snap.AdministratorGrantedActingRoles(actor);
  std::set<std::string> weaker;
  for (const auto& id : roles) {
    if (id != kPublicRoleId && !carried.count(id)) weaker.insert(id);
  }
  return weaker;
}

std::string ByDelegationOnly(const Snapshot& snap, const Principal& actor, const std::set<std::string>& roles) {
  return "role " + RoleNamesOf(snap, roles) + " through a membership an administrator granted, and user " +
         actor.user_name +
         " holds it in this session only through one a role's admin-option holder granted (which carries no account "
         "privilege or policy exemption)";
}

// May a holder of ALTER USER or DROP USER change or drop `target`? Only a user that is no more
// than the session itself, so that a new password, ENABLE or DROP never hands over (or takes
// away) authority the session lacks: every role it holds is one the session acts with, every
// admin option it holds is one the session could grant, every session attribute it has (row
// and masking policies read them) the actor has with the same value, and (changing it) it owns
// nothing. SYSTEM ADMIN and the administrator are refused before this.
arrow::Status RequireUserWithinReach(const Snapshot& snap, const Principal& actor, const UserRecord& target,
                                     const std::string& kind, bool dropping) {
  auto blocked = [&](const std::string& why) {
    return AccessControlError("Hardcharger blocked " + kind + ": user " + target.name + " " + why +
                              "; only an administrator " + (dropping ? "drops" : "changes") + " it");
  };
  const auto acting = snap.RolesOf(actor);
  std::set<std::string> missing;
  for (const auto& id : snap.EffectiveRoleIds(target.id)) {
    if (!acting.count(id)) missing.insert(id);
  }
  if (!missing.empty()) {
    return blocked("holds role " + RoleNamesOf(snap, missing) + ", which user " + actor.user_name +
                   " does not hold in this session");
  }
  // How each is held, too: a membership the administrator granted carries what one a role's
  // admin-option holder granted never does (an account privilege, a policy exemption), so a
  // role the target holds by the administrator's hand the session must hold that way as well.
  if (const auto weaker = HeldOnlyByDelegation(snap, actor, snap.AdministratorGrantedRolesOfStoredUser(target.id));
      !weaker.empty()) {
    return blocked("holds " + ByDelegationOnly(snap, actor, weaker));
  }
  std::set<std::string> options;
  for (const auto& m : snap.memberships()) {
    if (m.member_kind == PrincipalKind::kUser && m.member_id == target.id && m.admin_option &&
        !snap.CanAdminRole(actor, m.role_id)) {
      options.insert(m.role_id);
    }
  }
  if (!options.empty()) {
    return blocked("holds role " + RoleNamesOf(snap, options) + " WITH ADMIN OPTION, which user " + actor.user_name +
                   " does not");
  }
  const UserRecord* self = snap.FindUserById(actor.user_id);
  for (const auto& [key, value] : target.attributes) {
    const auto mine = self ? self->attributes.find(key) : std::map<std::string, std::string>::const_iterator();
    if (!self || mine == self->attributes.end() || mine->second != value) {
      return blocked("has the session attribute " + key + ", which row and masking policies read, and user " +
                     actor.user_name + " does not have it with the same value");
    }
  }
  if (!dropping) {
    std::vector<std::string> owned;
    for (const auto& o : snap.owners()) {
      if (o.owner_kind == OwnerKind::kUser && o.owner_id == target.id) owned.push_back(o.object.ToString());
    }
    if (!owned.empty()) {
      std::sort(owned.begin(), owned.end());
      return blocked("owns " + owned.front() +
                     (owned.size() > 1 ? " and " + std::to_string(owned.size() - 1) + " more" : std::string()) +
                     ", and signing in as it acts as their owner");
    }
  }
  return arrow::Status::OK();
}

// May a holder of DROP ROLE drop `role`? Only one whose loss takes away nothing the session
// does not hold, and that no policy names: everything it inherits the session acts with; it
// is a role the session acts with, or holds no privilege of its own; and no row policy names
// it and no masking policy exempts it (dropping it would change what those policies decide).
arrow::Status RequireRoleWithinReach(const Snapshot& snap, const Principal& actor, const RoleRecord& role,
                                     const std::string& kind) {
  auto blocked = [&](const std::string& why) {
    return AccessControlError("Hardcharger blocked " + kind + ": role " + role.name + " " + why +
                              "; only an administrator drops it");
  };
  const auto acting = snap.RolesOf(actor);
  std::set<std::string> missing;
  for (const auto& id : snap.RoleClosure(role.id)) {
    if (id != role.id && !acting.count(id)) missing.insert(id);
  }
  if (!missing.empty()) {
    return blocked("inherits role " + RoleNamesOf(snap, missing) + ", which user " + actor.user_name +
                   " does not hold in this session");
  }
  // How each is held: what the role (when the session acts with it) and the roles it inherits
  // by the administrator's hand carry, the session must carry the same way.
  std::set<std::string> carried_by_role;
  for (const auto& id : snap.AdministratorGrantedActingRoles(Principal::Role(role.id, role.name))) {
    if (id != role.id || acting.count(role.id)) carried_by_role.insert(id);
  }
  if (const auto weaker = HeldOnlyByDelegation(snap, actor, carried_by_role); !weaker.empty()) {
    return blocked(std::string(weaker.count(role.id) ? "is, or inherits, " : "inherits ") +
                   ByDelegationOnly(snap, actor, weaker));
  }
  if (!acting.count(role.id)) {
    const bool grants = std::any_of(snap.grants().begin(), snap.grants().end(),
                                    [&](const ObjectGrantRecord& g) { return g.role_id == role.id; });
    const bool system = std::any_of(snap.system_privileges().begin(), snap.system_privileges().end(),
                                    [&](const SystemPrivilegeRecord& r) { return r.role_id == role.id; });
    if (grants || system) {
      return blocked("holds privileges of its own, and user " + actor.user_name +
                     " does not hold the role in this session");
    }
  }
  for (const auto& p : snap.row_policies()) {
    if (std::find(p.role_ids.begin(), p.role_ids.end(), role.id) != p.role_ids.end()) {
      return blocked("is named by row policy " + p.name + " on " + p.table.ToString());
    }
  }
  for (const auto& b : snap.mask_bindings()) {
    if (std::find(b.exempt_role_ids.begin(), b.exempt_role_ids.end(), role.id) != b.exempt_role_ids.end()) {
      return blocked("is exempt from the masking policy on " + b.table.ToString() + "." +
                     QuoteIdentifierIfNeeded(b.column));
    }
  }
  return arrow::Status::OK();
}

// The account privileges a role holds, itself or through the roles it inherits.
std::string AccountPrivilegesCarried(const Snapshot& snap, const std::string& role_id) {
  const auto closure = snap.RoleClosure(role_id);
  std::set<std::string> names;
  for (const auto& r : snap.system_privileges()) {
    if (IsAccountPrivilege(r.privilege) && closure.count(r.role_id)) names.insert(SystemPrivilegeName(r.privilege));
  }
  return Join({names.begin(), names.end()});
}

// The password a CREATE USER or ALTER USER sets, whoever sets it: OK, or why the password
// policy (password_min_length, password_min_character_classes) refuses it, never quoting it.
// Asked after the statement is authorized (and, for a user's own change, after its current
// password is checked), so a session that may not set the password is told that instead.
arrow::Status CheckNewPassword(const std::string& password, const PasswordPolicy& policy) {
  return CheckPasswordPolicy(password, policy);
}

arrow::Result<std::vector<std::string>> OwnedObjects(StoreTransaction& txn, const Snapshot& snap,
                                                    OwnerKind kind, const std::string& id,
                                                    ObjectResolver& resolver) {
  std::vector<std::string> owned;
  for (const auto& owner : snap.owners()) {
    if (owner.owner_kind != kind || owner.owner_id != id) continue;
    const auto exists = resolver.ObjectExists(owner.object, owner.type);
    if (exists && !*exists) {
      ARROW_RETURN_NOT_OK(txn.DeleteOwner(owner.object, owner.type));
    } else {
      owned.push_back(owner.object.ToString());
    }
  }
  return owned;
}

}  // namespace

// ---------------------------------------------------------------------------

arrow::Result<std::shared_ptr<RbacEngine>> RbacEngine::Create(const Options& options) {
  std::shared_ptr<RbacEngine> engine(new RbacEngine(options));
  ARROW_ASSIGN_OR_RAISE(engine->dummy_hash_,
                        HashPassword("hardcharger-timing-equalizer", options.password_iterations));
  StoreOptions store_options;
  store_options.uri = options.store_uri;
  store_options.timeout = options.store_timeout;
  store_options.cluster_id = options.cluster_id;
  store_options.shared = options.store_shared;
  store_options.read_only = options.read_only;
  store_options.record_sign_ins = options.record_sign_ins;
  ARROW_ASSIGN_OR_RAISE(engine->store_, RbacStore::Open(store_options));
  if (!options.policy_service_url.empty()) {
    engine->policy_service_ =
        std::make_unique<PolicyService>(options.policy_service_url, options.policy_service_timeout);
  }
  ARROW_ASSIGN_OR_RAISE(auto snapshot, engine->store_->Load());
  engine->Publish(std::move(snapshot));
  engine->NoteStoreAnswer(arrow::Status::OK());
  if (options.poll_interval.count() > 0 && !options.read_only) {
    engine->poller_ = std::thread([raw = engine.get()] { raw->PollLoop(); });
  }
  return engine;
}

RbacEngine::~RbacEngine() { Stop(); }

void RbacEngine::Stop() {
  std::lock_guard<std::mutex> stop(stop_mutex_);
  {
    std::lock_guard<std::mutex> lock(poll_mutex_);
    stopping_ = true;
  }
  poll_cv_.notify_all();  // a poller waiting out its interval wakes at once
  if (poller_.joinable() && poller_.get_id() != std::this_thread::get_id()) poller_.join();
}

void RbacEngine::PollLoop() {
  std::unique_lock<std::mutex> lock(poll_mutex_);
  while (!stopping_) {
    poll_cv_.wait_for(lock, options_.poll_interval, [this] { return stopping_; });
    if (stopping_) break;
    lock.unlock();
    NoteStoreAnswer(Refresh());  // a store that does not answer keeps the last good snapshot
    lock.lock();
  }
}

namespace {
int64_t SteadyNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch())
      .count();
}
std::string Seconds(double seconds) {
  std::ostringstream out;
  out << static_cast<int64_t>(seconds);
  return out.str();
}
}  // namespace

double RbacEngine::SnapshotAgeSeconds() const {
  const int64_t last = last_answer_ns_.load();
  if (last == 0) return 0;
  return static_cast<double>(SteadyNs() - last) / 1e9;
}

arrow::Status RbacEngine::RequireFreshSnapshot() const {
  if (options_.max_snapshot_age.count() <= 0) return arrow::Status::OK();
  const double age = SnapshotAgeSeconds();
  if (age <= static_cast<double>(options_.max_snapshot_age.count())) return arrow::Status::OK();
  return AccessControlError("the access control store has not answered for " + Seconds(age) +
                            " s, longer than rbac_max_snapshot_age_seconds (" +
                            std::to_string(options_.max_snapshot_age.count()) +
                            "), so the grants this server holds may be out of date: store users are "
                            "refused until the store answers again");
}

// Sign-ins and statements go on with the last snapshot while the store does not answer
// (availability), but not silently: the first failed poll logs a warning, then one at
// most every 30 s with the snapshot's age, and the store's return is logged too.
void RbacEngine::NoteStoreAnswer(const arrow::Status& status) {
  const int64_t now = SteadyNs();
  if (status.ok()) {
    last_answer_ns_.store(now);
    if (!store_up_.exchange(true)) {
      Log(LogLevel::kInfo, "the access control store answers again",
          {{"outage_seconds", Seconds(static_cast<double>(now - outage_started_ns_) / 1e9)}});
    }
    return;
  }
  const bool was_up = store_up_.exchange(false);
  if (was_up) outage_started_ns_ = now;
  if (was_up || now - outage_logged_ns_ >= int64_t{30} * 1000000000) {
    outage_logged_ns_ = now;
    LogFields fields = {{"snapshot_age_seconds", Seconds(SnapshotAgeSeconds())},
                        {"error", status.message()}};
    if (options_.max_snapshot_age.count() > 0) {
      fields.push_back({"rbac_max_snapshot_age_seconds", std::to_string(options_.max_snapshot_age.count())});
    }
    Log(LogLevel::kWarning,
        options_.max_snapshot_age.count() > 0
            ? "the access control store does not answer: serving the last grants it gave until "
              "rbac_max_snapshot_age_seconds, then refusing store users"
            : "the access control store does not answer: serving the last grants it gave (no "
              "rbac_max_snapshot_age_seconds limit)",
        fields);
  }
}

std::shared_ptr<const Snapshot> RbacEngine::snapshot() const {
  std::lock_guard<std::mutex> lock(snapshot_mutex_);
  return snapshot_;
}

void RbacEngine::Publish(std::shared_ptr<const Snapshot> snapshot) {
  bool replaced = false;
  {
    std::lock_guard<std::mutex> lock(snapshot_mutex_);
    // Never go backwards (a slow reload must not undo a newer local write).
    if (!snapshot_ || snapshot->version() > snapshot_->version()) {
      snapshot_ = snapshot;
      replaced = true;
    }
  }
  if (!replaced) return;
  std::lock_guard<std::mutex> lock(listener_mutex_);
  if (listener_) listener_(*snapshot);
}

void RbacEngine::SetSnapshotListener(SnapshotListener listener) {
  std::lock_guard<std::mutex> lock(listener_mutex_);
  listener_ = std::move(listener);
}

arrow::Status RbacEngine::Refresh() {
  ARROW_ASSIGN_OR_RAISE(auto version, store_->CurrentVersion());
  if (version == snapshot()->version()) return arrow::Status::OK();
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  return arrow::Status::OK();
}

arrow::Result<Principal> RbacEngine::AuthenticatePassword(const std::string& user_name,
                                                          const std::string& password) const {
  static const auto kFailure = AccessControlError("invalid user name or password");
  auto snap = snapshot();
  const UserRecord* user = snap->FindUserByName(user_name);
  if (!user || !user->password_hash) {
    // Spend the same time as a real check so a probe cannot tell which names exist.
    (void)VerifyPassword(password, dummy_hash_);
    return kFailure;
  }
  if (!VerifyPassword(password, *user->password_hash) || !user->enabled) return kFailure;
  return Principal::User(user->id, user->name);
}

arrow::Result<Principal> RbacEngine::PrincipalForUser(const std::string& user_name) const {
  auto snap = snapshot();
  const UserRecord* user = snap->FindUserByName(user_name);
  if (!user) {
    return AccessControlError("user " + user_name +
                              " is not defined on this server. An administrator creates it with "
                              "CREATE USER");
  }
  if (!user->enabled) return AccessControlError("user " + user->name + " is disabled");
  return Principal::User(user->id, user->name);
}

arrow::Result<Principal> RbacEngine::ProvisionUser(const std::string& user_name,
                                                   const std::map<std::string, std::string>& attributes,
                                                   bool* created) {
  *created = false;
  if (snapshot()->FindUserByName(user_name)) return PrincipalForUser(user_name);  // disabled stays disabled
  if (options_.read_only) {
    // The same checks a creation makes, with nothing written: the answer a server would give.
    const auto snap = snapshot();
    ARROW_RETURN_NOT_OK(CheckNewUserName(*snap, user_name, options_));
    ARROW_RETURN_NOT_OK(TenantForName(*snap, user_name).status());
    *created = true;
    return Principal::User(StoreTransaction::NewId(), user_name);
  }
  bool inserted = false;
  const auto written = store_->Mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
    inserted = false;
    // Read inside the transaction, after the writers before it: another replica's first
    // sign-in of the same user may have created it since this replica's snapshot.
    if (snap.FindUserByName(user_name)) return arrow::Status::OK();
    ARROW_RETURN_NOT_OK(CheckNewUserName(snap, user_name, options_));
    ARROW_ASSIGN_OR_RAISE(auto tenant, TenantForName(snap, user_name));
    UserRecord user{StoreTransaction::NewId(), user_name, std::nullopt, true, {}, std::nullopt};
    ARROW_RETURN_NOT_OK(txn.InsertUser(user));
    for (const auto& [attribute, value] : attributes) {
      ARROW_RETURN_NOT_OK(txn.SetUserAttribute(user.id, attribute, value));
    }
    ARROW_RETURN_NOT_OK(txn.SetPrincipalTenant(PrincipalKind::kUser, user.id, tenant));
    inserted = true;
    return arrow::Status::OK();
  });
  // Re-read either way: a write that failed may have lost a race to the same user's
  // first sign-in elsewhere, which then counts (and its user is the one to sign in).
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  if (!written.ok()) {
    if (snapshot()->FindUserByName(user_name)) return PrincipalForUser(user_name);
    return written;
  }
  *created = inserted;
  return PrincipalForUser(user_name);
}

bool RbacEngine::IsActive(const Principal& principal) const {
  // A rules-only principal has no store record: its token decides.
  if (principal.superuser || principal.catalog_rules) return true;
  const UserRecord* user = snapshot()->FindUserById(principal.user_id);
  return user && user->enabled;
}

arrow::Status RbacEngine::RecordOwner(const ObjectPath& object, ObjectType type,
                                      const Principal& owner, const std::string& definition_hash) {
  OwnerRecord record;
  record.object = object;
  record.type = type;
  record.owner_kind = owner.superuser ? OwnerKind::kSystem : OwnerKind::kUser;
  record.owner_id = owner.superuser ? "" : owner.user_id;
  record.definition_hash = definition_hash;
  return WriteOwner(object, type, &record);
}

arrow::Status RbacEngine::PutOwner(const OwnerRecord& record) {
  return WriteOwner(record.object, record.type, &record);
}

arrow::Status RbacEngine::ForgetOwner(const ObjectPath& object, ObjectType type) {
  const auto current = snapshot();
  if (!(type == ObjectType::kTrigger ? current->FindTriggerOwner(object) : current->FindOwner(object))) {
    return arrow::Status::OK();
  }
  return WriteOwner(object, type, nullptr);
}

namespace {

// Compare identifier components, never dotted strings: a schema named "a.b" is
// distinct from a nested schema a.b. Trigger keys add the table to schema_path.
bool InObject(const ObjectPath& path, const ObjectPath& object, ObjectType type,
              bool trigger = false) {
  if (!IdentifiersEqual(path.catalog, object.catalog)) return false;
  if (path.schema_path.size() < object.schema_path.size()) return false;
  for (size_t i = 0; i < object.schema_path.size(); ++i) {
    if (!IdentifiersEqual(path.schema_path[i], object.schema_path[i])) return false;
  }
  if (type == ObjectType::kSchema) {
    // Trigger keys append their table name; that last component is not a schema.
    return !trigger || path.schema_path.size() > object.schema_path.size();
  }
  if (trigger && type == ObjectType::kTable) {
    return path.schema_path.size() == object.schema_path.size() + 1 &&
           IdentifiersEqual(path.schema_path.back(), object.object_name);
  }
  return path.schema_path.size() == object.schema_path.size() &&
         IdentifiersEqual(path.object_name, object.object_name);
}

ObjectAccessRecords ObjectRecords(const Snapshot& snap, const ObjectPath& object, ObjectType type) {
  ObjectAccessRecords records;
  for (const auto& grant : snap.grants()) {
    if (type != ObjectType::kTrigger && InObject(grant.target, object, type) &&
        (type == ObjectType::kSchema || (type == ObjectType::kSequence) == (grant.privilege == Privilege::kUsage)))
      records.grants.push_back(grant);
  }
  if (type == ObjectType::kSequence) return records;
  for (const auto& owner : snap.owners()) {
    if ((type != ObjectType::kTrigger || owner.type == type) &&
        InObject(owner.object, object, type, owner.type == ObjectType::kTrigger)) records.owners.push_back(owner);
  }
  if (type == ObjectType::kTrigger) return records;
  for (const auto& policy : snap.row_policies()) {
    if (InObject(policy.table, object, type)) records.policies.push_back(policy);
  }
  for (const auto& mask : snap.mask_bindings()) {
    if (InObject(mask.table, object, type)) records.masks.push_back(mask);
  }
  return records;
}

arrow::Status RemoveObject(StoreTransaction& txn, const Snapshot& snap,
                           const ObjectPath& object, ObjectType type) {
  if (type == ObjectType::kTrigger) return txn.DeleteOwner(object, type);
  for (const auto& grant : snap.grants()) {
    // Tables/views and sequences can share a name. USAGE is a sequence grant;
    // only a schema drop removes both namespaces.
    if (InObject(grant.target, object, type) &&
        (type == ObjectType::kSchema || (type == ObjectType::kSequence) == (grant.privilege == Privilege::kUsage)))
      ARROW_RETURN_NOT_OK(txn.DeleteGrant(grant.id));
  }
  if (type == ObjectType::kSequence) return arrow::Status::OK();
  for (const auto& owner : snap.owners()) {
    if (InObject(owner.object, object, type, owner.type == ObjectType::kTrigger))
      ARROW_RETURN_NOT_OK(txn.DeleteOwner(owner.object, owner.type));
  }
  for (const auto& policy : snap.row_policies()) {
    if (InObject(policy.table, object, type)) ARROW_RETURN_NOT_OK(txn.DeleteRowPolicy(policy.id));
  }
  for (const auto& binding : snap.mask_bindings()) {
    if (InObject(binding.table, object, type)) ARROW_RETURN_NOT_OK(txn.SetMaskBinding(binding, true));
  }
  return arrow::Status::OK();
}

}  // namespace

arrow::Status RbacEngine::ForgetObject(const ObjectPath& object, ObjectType type, ObjectAccessRecords* previous) {
  ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& snap) {
    if (previous) *previous = ObjectRecords(snap, object, type);
    return RemoveObject(txn, snap, object, type);
  }));
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  return arrow::Status::OK();
}

arrow::Status RbacEngine::CopyObjectAccess(const ObjectPath& object, ObjectType type,
                                      const ObjectPath& destination, ObjectAccessRecords* previous) {
  ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& snap) {
    if (previous) *previous = ObjectRecords(snap, destination, type);
    // Install the destination's protections before DuckDB exposes the new name.
    // Keep the source protected until the rename commits (other transactions can
    // still see it). A failed statement removes only the destination copy.
    ARROW_RETURN_NOT_OK(RemoveObject(txn, snap, destination, type));
    for (auto grant : snap.grants()) {
      if (!InObject(grant.target, object, type) || grant.privilege == Privilege::kUsage) continue;
      grant.target = destination;
      grant.id = StoreTransaction::NewId();
      ARROW_RETURN_NOT_OK(txn.InsertGrant(grant));
    }
    for (auto owner : snap.owners()) {
      if (!InObject(owner.object, object, type, owner.type == ObjectType::kTrigger)) continue;
      if (owner.type == ObjectType::kTrigger) {
        owner.object.schema_path = destination.schema_path;
        owner.object.schema_path.push_back(destination.object_name);
        owner.object.catalog = destination.catalog;
      } else {
        owner.object = destination;
      }
      ARROW_RETURN_NOT_OK(txn.UpsertOwner(owner));
    }
    for (auto policy : snap.row_policies()) {
      if (!InObject(policy.table, object, type)) continue;
      policy.table = destination;
      policy.id = StoreTransaction::NewId();
      ARROW_RETURN_NOT_OK(txn.UpsertRowPolicy(policy));
    }
    for (auto binding : snap.mask_bindings()) {
      if (!InObject(binding.table, object, type)) continue;
      binding.table = destination;
      ARROW_RETURN_NOT_OK(txn.SetMaskBinding(binding, false));
    }
    return arrow::Status::OK();
  }));
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  return arrow::Status::OK();
}

arrow::Status RbacEngine::RestoreObjectAccess(const ObjectPath& object, ObjectType type,
                                               const ObjectAccessRecords& records) {
  ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& snap) {
    ARROW_RETURN_NOT_OK(RemoveObject(txn, snap, object, type));
    for (const auto& grant : records.grants) ARROW_RETURN_NOT_OK(txn.InsertGrant(grant));
    for (const auto& owner : records.owners) ARROW_RETURN_NOT_OK(txn.UpsertOwner(owner));
    for (const auto& policy : records.policies) ARROW_RETURN_NOT_OK(txn.UpsertRowPolicy(policy));
    for (const auto& mask : records.masks) ARROW_RETURN_NOT_OK(txn.SetMaskBinding(mask, false));
    return arrow::Status::OK();
  }));
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  return arrow::Status::OK();
}

arrow::Status RbacEngine::WriteOwner(const ObjectPath& object, ObjectType type, const OwnerRecord* record) {
  ARROW_ASSIGN_OR_RAISE(auto version, store_->Write([&](StoreTransaction& txn) {
    return record ? txn.UpsertOwner(*record) : txn.DeleteOwner(object, type);
  }));
  auto current = snapshot();
  if (current->version() >= version) return arrow::Status::OK();  // a reload already has it
  if (current->version() == version - 1) {
    // Nothing else changed in between: the store now holds exactly this.
    Publish(current->WithOwner(version, object, type, record));
    return arrow::Status::OK();
  }
  ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
  Publish(std::move(fresh));
  return arrow::Status::OK();
}

// ---------------------------------------------------------------------------
// Statements
// ---------------------------------------------------------------------------

namespace {

// "8 hours", "30 minutes", "2 days", "45 seconds" -> seconds.
std::optional<int64_t> ParseDuration(const std::string& text) {
  std::istringstream in(text);
  int64_t n = 0;
  std::string unit, rest;
  if (!(in >> n) || n <= 0 || !(in >> unit) || (in >> rest)) return std::nullopt;
  for (auto& ch : unit) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
  if (!unit.empty() && unit.back() == 's') unit.pop_back();
  if (unit == "second") return n;
  if (unit == "minute") return n * 60;
  if (unit == "hour") return n * 3600;
  if (unit == "day") return n * 86400;
  if (unit == "week") return n * 7 * 86400;
  if (unit == "month") return n * 30 * 86400;
  if (unit == "year") return n * 365 * 86400;
  return std::nullopt;
}

std::string IsoTime(int64_t seconds) {
  std::time_t t = static_cast<std::time_t>(seconds);
  const std::tm tm = UtcTime(t);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buf;
}

int64_t NowSeconds() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

constexpr int64_t kMaxScopedTokenSeconds = 30 * 86400;

}  // namespace

arrow::Result<CommandResult> RbacEngine::ExecuteScopedToken(const ScopedTokenCommand& c,
                                                            const Principal& actor) {
  auto snap = snapshot();
  auto privilege_list = [](const std::vector<Privilege>& ps) {
    std::vector<std::string> names;
    for (auto p : ps) names.push_back(PrivilegeName(p));
    return Join(names);
  };
  if (c.kind == ScopedTokenCommand::Kind::kShow) {
    CommandResult result;
    result.column_names = {"name", "user", "allows", "catalogs", "expires_at", "minted_under"};
    for (const auto& t : snap->scoped_tokens()) {
      if (!actor.superuser && t.user_id != actor.user_id) continue;
      const auto* user = snap->FindUserById(t.user_id);
      const auto* parent = t.parent_id.empty() ? nullptr : snap->FindScopedToken(t.parent_id);
      result.rows.push_back({t.name, user ? user->name : t.user_id, privilege_list(t.privileges),
                             t.catalogs.empty() ? "(any)" : Join(t.catalogs), IsoTime(t.expires_at),
                             parent ? parent->name : ""});
    }
    return result;
  }
  // Whose token: your own, or (the administrator) the user named.
  const UserRecord* user = nullptr;
  if (actor.superuser) {
    if (!c.for_user) {
      return AccessControlError("the administrator names the user: " + CommandKind(c) + " " +
                                c.name + " FOR USER name ...");
    }
    user = snap->FindUserByName(*c.for_user);
    if (!user) return AccessControlError("user " + *c.for_user + " does not exist");
  } else {
    user = snap->FindUserById(actor.user_id);
    if (!user) return AccessControlError("user " + actor.user_name + " does not exist");
    if (c.for_user && !IdentifiersEqual(*c.for_user, user->name)) {
      return AccessControlError(actor.scope ? "a scoped token mints tokens only for the user it "
                                              "belongs to"
                                            : "users mint scoped tokens only for yourself; the "
                                              "administrator mints them for others");
    }
  }
  const ScopedTokenRecord* existing = nullptr;
  for (const auto& t : snap->scoped_tokens()) {
    if (t.user_id == user->id && IdentifiersEqual(t.name, c.name)) existing = &t;
  }

  if (c.kind == ScopedTokenCommand::Kind::kDrop) {
    if (!existing) {
      return AccessControlError("user " + user->name + " has no scoped token named " + c.name);
    }
    // The token and every token minted under it.
    std::set<std::string> doomed{existing->id};
    for (bool grew = true; grew;) {
      grew = false;
      for (const auto& t : snap->scoped_tokens()) {
        if (!t.parent_id.empty() && doomed.count(t.parent_id) && doomed.insert(t.id).second)
          grew = true;
      }
    }
    ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& now) {
      ARROW_RETURN_NOT_OK(RequireStillSystemAdmin(now, actor, CommandKind(c)));
      for (const auto& id : doomed) ARROW_RETURN_NOT_OK(txn.DeleteScopedToken(id));
      return arrow::Status::OK();
    }));
    {
      ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
      Publish(std::move(fresh));
    }
    return Message("Scoped token " + existing->name + " of user " + user->name + " revoked" +
                   (doomed.size() > 1
                        ? " with " + std::to_string(doomed.size() - 1) + " token(s) minted under it"
                        : ""));
  }

  // CREATE
  if (existing) {
    return AccessControlError("user " + user->name + " already has a scoped token named " + c.name);
  }
  int64_t seconds = 3600;
  if (c.valid_for) {
    auto parsed = ParseDuration(*c.valid_for);
    if (!parsed) {
      return AccessControlError(
          "VALID FOR takes a duration such as '8 hours', '30 minutes' or "
          "'7 days'");
    }
    seconds = *parsed;
  }
  if (seconds > kMaxScopedTokenSeconds) {
    return AccessControlError("a scoped token is valid for at most 30 days");
  }
  ScopedTokenRecord token;
  token.id = StoreTransaction::NewId();
  token.name = c.name;
  token.user_id = user->id;
  token.privileges = c.privileges;
  token.catalogs = c.catalogs;
  token.expires_at = NowSeconds() + seconds;
  token.created_by = actor.superuser ? "" : actor.user_id;
  if (actor.scope) {
    // Minted under a scoped token: never more than it.
    const auto& parent = *actor.scope;
    for (auto p : token.privileges) {
      if (!parent.privileges.count(p)) {
        return AccessControlError("a token minted under scoped token " + parent.name +
                                  " cannot allow " + PrivilegeName(p));
      }
    }
    if (!parent.catalogs.empty()) {
      if (token.catalogs.empty()) token.catalogs = parent.catalogs;
      for (const auto& cat : token.catalogs) {
        bool inside = false;
        for (const auto& pc : parent.catalogs) inside = inside || IdentifiersEqual(pc, cat);
        if (!inside) {
          return AccessControlError("a token minted under scoped token " + parent.name +
                                    " cannot reach catalog " + cat);
        }
      }
    }
    if (token.expires_at > parent.expires_at) {
      if (c.valid_for) {
        return AccessControlError("a token minted under scoped token " + parent.name +
                                  " cannot outlast it (it expires " + IsoTime(parent.expires_at) +
                                  ")");
      }
      token.expires_at = parent.expires_at;
    }
    token.parent_id = parent.id;
  }
  ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& now) {
    ARROW_RETURN_NOT_OK(RequireStillSystemAdmin(now, actor, CommandKind(c)));
    for (const auto& t : now.scoped_tokens()) {
      if (t.user_id == user->id && IdentifiersEqual(t.name, c.name)) {
        return AccessControlError("user " + user->name + " already has a scoped token named " +
                                  c.name);
      }
    }
    return txn.InsertScopedToken(token);
  }));
  {
    ARROW_ASSIGN_OR_RAISE(auto fresh, store_->Load());
    Publish(std::move(fresh));
  }
  CommandResult result;
  result.column_names = {"name", "user", "allows", "catalogs", "expires_at"};
  result.rows.push_back({token.name, user->name, privilege_list(token.privileges),
                         token.catalogs.empty() ? "(any)" : Join(token.catalogs),
                         IsoTime(token.expires_at)});
  result.minted = CommandResult::MintedToken{token.id, user->name, token.expires_at};
  return result;
}

arrow::Result<CommandResult> RbacEngine::Execute(const RbacCommand& command, const Principal& actor,
                                                 ObjectResolver& resolver, AuditNotes* notes) {
  // A scoped token may read its grants, narrow itself and mint narrower
  // tokens; it never changes access control.
  if (actor.scope && !std::holds_alternative<ShowGrantsCommand>(command) &&
      !std::holds_alternative<SetRoleCommand>(command) &&
      !std::holds_alternative<ScopedTokenCommand>(command)) {
    return AccessControlError("Hardcharger blocked " + CommandKind(command) +
                              ": a scoped token cannot change access control");
  }
  if (const auto* c = std::get_if<ScopedTokenCommand>(&command))
    return ExecuteScopedToken(*c, actor);

  // SHOW GRANTS is a read; everything else changes the store.
  if (const auto* show = std::get_if<ShowGrantsCommand>(&command)) {
    auto snap = snapshot();
    CommandResult result;
    result.column_names = {"privilege",  "object_type",  "object",       "columns",
                           "granted_to", "grantee_type", "grant_option", "granted_by"};
    auto grantor_name = [&](const std::string& id) -> std::string {
      if (id.empty()) return "administrator";
      const auto* user = snap->FindUserById(id);
      return user ? user->name : id;
    };

    // SYSTEM ADMIN, as a user holds it: by a grant of its own, and through each role
    // granted it. Holding it changes nothing until the session says SET ROLE SYSTEM ADMIN.
    auto system_admin_rows = [&](const Principal& user) {
      for (const auto& source : snap->SystemAdminSources(user)) {
        for (const auto& r : snap->system_admins()) {
          if (source.empty() ? (r.kind != PrincipalKind::kUser || r.principal_id != user.user_id)
                             : (r.kind != PrincipalKind::kRole || r.principal_id != source)) {
            continue;
          }
          const auto* role = source.empty() ? nullptr : snap->FindRoleById(source);
          result.rows.push_back({kSystemAdminName, "SERVER", "*", "", role ? role->name : user.user_name,
                                 source.empty() ? "USER" : "ROLE", "NO", grantor_name(r.granted_by)});
        }
      }
    };

    // Which roles to list: one role, or every role a user holds.
    std::vector<std::string> role_ids;
    if (!show->principal) {
      if (actor.superuser) {
        // A user acting as System Admin holds what the administrator holds, and says so.
        result.rows.push_back({"ALL", "SERVER", "*", "", actor.user_name,
                               actor.acts_as_system_admin() ? kSystemAdminName : "SUPERUSER", "YES", ""});
        return result;
      }
      // The roles the session acts with (SET ROLE narrows them).
      for (const auto& id : snap->RolesOf(actor)) role_ids.push_back(id);
      system_admin_rows(actor);
    } else {
      ARROW_ASSIGN_OR_RAISE(auto who, ResolveGrantee(*snap, *show->principal));
      if (who.kind == PrincipalKind::kUser) {
        if (!actor.superuser && who.id != actor.user_id) {
          return AccessControlError(
              "Hardcharger blocked SHOW GRANTS for another user. Users see "
              "their own grants; administrators see everyone's");
        }
        for (const auto& id : snap->EffectiveRoleIds(who.id)) role_ids.push_back(id);
        // Its own session's groups count for the caller itself; for another user (the
        // administrator asks), what the store alone gives.
        system_admin_rows(!actor.superuser ? actor : Principal::User(who.id, who.name));
      } else {
        if (!actor.superuser && !snap->EffectiveRoleIds(actor.user_id).count(who.id)) {
          return AccessControlError("Hardcharger blocked SHOW GRANTS for role " + who.name +
                                    ", which you do not hold");
        }
        role_ids.push_back(who.id);
        for (const auto& r : snap->system_admins()) {
          if (r.kind != PrincipalKind::kRole || r.principal_id != who.id) continue;
          result.rows.push_back({kSystemAdminName, "SERVER", "*", "", who.name, "ROLE", "NO",
                                 grantor_name(r.granted_by)});
        }
      }
      // Role memberships of the principal itself.
      for (const auto& m : snap->memberships()) {
        const bool member = (who.kind == PrincipalKind::kUser &&
                             m.member_kind == PrincipalKind::kUser && m.member_id == who.id) ||
                            (who.kind == PrincipalKind::kRole &&
                             m.member_kind == PrincipalKind::kRole && m.member_id == who.id);
        if (!member) continue;
        const auto* role = snap->FindRoleById(m.role_id);
        result.rows.push_back({"ROLE", "ROLE", role ? role->name : m.role_id, "", who.name,
                               who.kind == PrincipalKind::kUser ? "USER" : "ROLE",
                               m.admin_option ? "YES" : "NO", grantor_name(m.granted_by)});
      }
    }
    for (const auto& role_id : role_ids) {
      const auto* role = snap->FindRoleById(role_id);
      for (const auto& g : snap->grants()) {
        if (g.role_id != role_id) continue;
        // USAGE on one object is granted ON SEQUENCE (and no other object takes it); EXECUTE
        // on a table function, whose "columns" are the path arguments it declared.
        const std::string label = g.scope == GrantScope::kObject && g.privilege == Privilege::kUsage ? "SEQUENCE"
                                  : g.privilege == Privilege::kExecute                             ? "TABLE FUNCTION"
                                                                                                   : ScopeLabel(g.scope);
        std::vector<std::string> columns = g.columns;
        if (g.privilege == Privilege::kExecute) {
          columns.clear();
          for (const auto& text : PathArgumentTexts(g.columns)) columns.push_back("path " + text);
        }
        result.rows.push_back({PrivilegeName(g.privilege), label, g.target.ToString(), Join(columns),
                               role ? role->name : role_id, "ROLE", g.grant_option ? "YES" : "NO",
                               grantor_name(g.granted_by)});
      }
    }
    // An account privilege or a policy exemption is listed for a user only through the roles
    // that give it to the user (the administrator's memberships): one a delegated membership
    // reaches gives nothing.
    std::optional<std::set<std::string>> account_roles;
    if (!show->principal) {
      account_roles = snap->AccountPrivilegeRoles(actor);
    } else if (const auto* user = snap->FindUserByName(show->principal->name);
               user && show->principal->kind != GranteeKind::kRole) {
      account_roles = snap->AccountPrivilegeRoles(actor.superuser ? Principal::User(user->id, user->name) : actor);
    }
    for (const auto& role_id : role_ids) {
      const auto* role = snap->FindRoleById(role_id);
      for (const auto& r : snap->system_privileges()) {
        if (r.role_id != role_id) continue;
        if ((IsAccountPrivilege(r.privilege) || IsPolicyExemption(r.privilege)) && account_roles &&
            !account_roles->count(role_id)) {
          continue;
        }
        result.rows.push_back({SystemPrivilegeName(r.privilege), r.location.empty() ? "SYSTEM" : "LOCATION",
                               r.location, "", role ? role->name : role_id, "ROLE", r.admin_option ? "YES" : "NO",
                               grantor_name(r.granted_by)});
      }
    }
    for (const auto& o : snap->owners()) {
      if (show->principal ? false : snap->IsOwner(actor, o)) {
        result.rows.push_back({"OWNERSHIP",
                               o.type == ObjectType::kView      ? "VIEW"
                               : o.type == ObjectType::kSchema  ? "SCHEMA"
                               : o.type == ObjectType::kTrigger ? "TRIGGER"
                                                                : "TABLE",
                               o.object.ToString(), "", actor.user_name, "USER", "YES", ""});
      }
    }
    return result;
  }

  // Users and roles are the administrator's, a tenant admin's in its tenant, and (for
  // system-scope users and roles no more than the session itself) an account privilege
  // holder's. Grants can be delegated: an owner grants on what it owns, and WITH GRANT /
  // ADMIN OPTION passes that on.
  std::string account_name;
  if (const auto* c = std::get_if<CreateUserCommand>(&command)) account_name = c->name;
  if (const auto* c = std::get_if<DropUserCommand>(&command)) account_name = c->name;
  if (const auto* c = std::get_if<CreateRoleCommand>(&command)) account_name = c->name;
  if (const auto* c = std::get_if<DropRoleCommand>(&command)) account_name = c->name;
  // The session's own store user (a user acting as System Admin: that user); empty for the
  // administrator.
  const std::string own_id = actor.acts_as_system_admin() ? actor.acting_user_id : actor.user_id;
  // ALTER USER ... REPLACE 'current': a user changing its own password, which it proves by
  // giving the current one. No account privilege is needed or used.
  bool self_service = false;
  if (const auto* c = std::get_if<AlterUserCommand>(&command)) {
    // ALTER USER CURRENT_USER is ALTER USER <the session's own user>.
    if (c->current_user) {
      const UserRecord* own = own_id.empty() ? nullptr : snapshot()->FindUserById(own_id);
      if (!own) {
        return AccessControlError(
            "Hardcharger blocked ALTER USER CURRENT_USER: the administrator is not a user of the access control "
            "store; its password is set in the server's configuration");
      }
      AlterUserCommand resolved = *c;
      resolved.current_user = false;
      resolved.name = own->name;
      return Execute(resolved, actor, resolver, notes);
    }
    account_name = c->name;
    const bool own_user = !own_id.empty() && IdentifiersEqual(account_name, actor.user_name);
    self_service = c->current_password.has_value();
    if (self_service && !own_user) {
      return AccessControlError("Hardcharger blocked ALTER USER: REPLACE gives your own current password, so it "
                                "changes only your own password, not user " +
                                account_name + "'s");
    }
    // A session alone never changes its own password: whoever holds it (a stolen access token
    // included) must also know the current one.
    if (!self_service && own_user && !actor.is_unrestricted_administrator() &&
        c->action == AlterUserCommand::Action::kSetPassword) {
      return AccessControlError(
          "Hardcharger blocked ALTER USER: to change your own password, give the current one: ALTER USER "
          "CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'");
    }
  }
  const bool account_statement = AccountPrivilegeOf(command).has_value() && !self_service;
  // Asked first of this replica's snapshot (and, when that refuses, of the store again, so a
  // grant made on another replica counts at once), before any password is hashed; then again,
  // authoritatively, inside the change's own store transaction (`authorize`).
  if (account_statement && !actor.is_unrestricted_administrator()) {
    auto first = AuthorizeAccountStatement(*snapshot(), actor, command, account_name);
    if (!first.ok()) {
      if (!Refresh().ok()) return first.status();
      ARROW_RETURN_NOT_OK(AuthorizeAccountStatement(*snapshot(), actor, command, account_name).status());
    }
  }
  AccountAuthority authority;
  auto authorize = [&](const Snapshot& snap) -> arrow::Status {
    if (self_service) {
      authority = AccountAuthority{AccountAuthority::Kind::kSelf, "the user's own current password"};
      if (notes) *notes = {{"authorized_by", authority.text}};
      return arrow::Status::OK();
    }
    ARROW_ASSIGN_OR_RAISE(authority, AuthorizeAccountStatement(snap, actor, command, account_name));
    if (notes) *notes = {{"authorized_by", authority.text}};
    return arrow::Status::OK();
  };

  CommandResult result;
  // Every change runs in one store transaction, which also removes the grants
  // and memberships the change left without a grantor able to make them
  // (REVOKE ... CASCADE, the default; RESTRICT refuses instead).
  size_t cascaded = 0;
  auto mutate_with = [&](bool restrict,
                         const std::function<arrow::Status(StoreTransaction&, const Snapshot&)>& fn)
      -> arrow::Status {
    ARROW_RETURN_NOT_OK(store_->Mutate([&](StoreTransaction& txn, const Snapshot& snap) {
      cascaded = 0;
      if (notes) notes->clear();  // a retried transaction starts over
      ARROW_RETURN_NOT_OK(RequireStillSystemAdmin(snap, actor, CommandKind(command)));
      ARROW_RETURN_NOT_OK(fn(txn, snap));
      ARROW_ASSIGN_OR_RAISE(auto after, txn.Reload());
      // An account change's state before and after, as this transaction reads and leaves it.
      auto note_values = [&](const Snapshot& final_state) {
        if (!notes) return;
        if (auto values = AccountChangeValues(command, snap, final_state)) {
          notes->emplace_back("before", values->first);
          notes->emplace_back("after", values->second);
        }
      };
      const auto fallen = after->FallenDelegations();
      if (fallen.empty()) {
        note_values(*after);
        return arrow::Status::OK();
      }
      if (restrict) {
        return AccessControlError(
            "other grants depend on this one; REVOKE ... CASCADE removes them too");
      }
      for (const auto& id : fallen.grant_ids) ARROW_RETURN_NOT_OK(txn.DeleteGrant(id));
      for (const auto& m : fallen.memberships) {
        ARROW_RETURN_NOT_OK(txn.DeleteMembership(m.member_kind, m.member_id, m.role_id));
      }
      for (const auto& r : fallen.system_privileges) {
        ARROW_RETURN_NOT_OK(txn.DeleteSystemPrivilege(r));
      }
      cascaded =
          fallen.grant_ids.size() + fallen.memberships.size() + fallen.system_privileges.size();
      if (notes) {
        ARROW_ASSIGN_OR_RAISE(auto settled, txn.Reload());
        note_values(*settled);
      }
      return arrow::Status::OK();
    }));
    ARROW_ASSIGN_OR_RAISE(auto snap, store_->Load());
    Publish(std::move(snap));
    return arrow::Status::OK();
  };
  auto mutate = [&](const std::function<arrow::Status(StoreTransaction&, const Snapshot&)>& fn) {
    return mutate_with(false, fn);
  };
  auto with_cascade_note = [&](std::string text) {
    if (cascaded > 0) {
      text += "; also revoked " + std::to_string(cascaded) + " grant(s) that were made through it";
    }
    return Message(text);
  };
  const std::string grantor = actor.superuser ? "" : actor.user_id;
  std::string message;

  // ---- CREATE USER -------------------------------------------------------
  if (const auto* c = std::get_if<CreateUserCommand>(&command)) {
    std::optional<std::string> hash;
    if (c->password) {
      ARROW_ASSIGN_OR_RAISE(auto h, HashPassword(*c->password, options_.password_iterations));
      hash = std::move(h);
    }
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      ARROW_RETURN_NOT_OK(authorize(snap));
      if (c->if_not_exists && snap.FindUserByName(c->name)) {
        // Its password stays as it is: no policy applies to a password nothing sets (an init
        // SQL written before the policy keeps starting the server).
        message = "User " + c->name + " already exists";
        return arrow::Status::OK();
      }
      if (c->password) ARROW_RETURN_NOT_OK(CheckNewPassword(*c->password, options_.password_policy));
      ARROW_RETURN_NOT_OK(CheckNewUserName(snap, c->name, options_));
      ARROW_ASSIGN_OR_RAISE(auto tenant, TenantForName(snap, c->name));
      // A new user holds nothing but PUBLIC, whoever creates it.
      message = "User " + c->name + " created";
      UserRecord user{StoreTransaction::NewId(), c->name, hash, true, {}, std::nullopt};
      ARROW_RETURN_NOT_OK(txn.InsertUser(user));
      if (hash) ARROW_RETURN_NOT_OK(txn.SetPasswordChangedAt(user.id, NowMilliseconds()));
      // A password someone else chose is the user's to replace before anything else: always
      // when a delegate chose it, and when the administrator says EXPIRE.
      if (hash && (authority.sets_temporary_passwords() || c->expire)) {
        ARROW_RETURN_NOT_OK(txn.SetPasswordReset(
            user.id, PasswordResetRecord{user.id, PasswordFingerprint(hash), own_id, NowMilliseconds()}));
        message += ", with a temporary password: it must be changed at the first sign-in";
        if (notes) notes->emplace_back("password", "temporary password set");
      } else if (hash && notes) {
        notes->emplace_back("password", "password set");
      }
      return txn.SetPrincipalTenant(PrincipalKind::kUser, user.id, tenant);
    }));
    return Message(message);
  }

  // ---- ALTER USER --------------------------------------------------------
  if (const auto* c = std::get_if<AlterUserCommand>(&command)) {
    using Action = AlterUserCommand::Action;
    std::optional<std::string> hash;
    // A user's own change: the current password, checked against what this replica has (and,
    // when that fails, against the store again), before anything is hashed; the store
    // transaction then confirms the password is still that one.
    std::string replaced;
    if (self_service) {
      auto verify = [&]() -> std::optional<std::string> {
        const UserRecord* own = snapshot()->FindUserById(own_id);
        if (own && own->password_hash && VerifyPassword(*c->current_password, *own->password_hash)) {
          return *own->password_hash;
        }
        return std::nullopt;
      };
      auto verified = verify();
      if (!verified && Refresh().ok()) verified = verify();
      const UserRecord* own = snapshot()->FindUserById(own_id);
      if (own && !own->password_hash) {
        return AccessControlError("Hardcharger blocked ALTER USER: user " + own->name +
                                  " has no password to replace (it signs in another way); an administrator sets "
                                  "its first one");
      }
      if (!verified) {
        return AccessControlError("Hardcharger blocked ALTER USER: the current password given with REPLACE is not "
                                  "user " + account_name + "'s password")
            .WithDetail(std::make_shared<WrongCurrentPasswordDetail>());
      }
      // A temporary password past password_temporary_max_age_hours no longer replaces itself
      // either (said only to whoever knows it): an administrator sets a new one.
      if (snapshot()
              ->PasswordStandingOf(own_id, options_.password_policy.max_age_days,
                                   options_.password_policy.temporary_max_age_hours, NowMilliseconds())
              .temporary_expired) {
        return AccessControlError("Hardcharger blocked ALTER USER: the temporary password of user " + account_name +
                                  " has expired (it was set more than " +
                                  std::to_string(options_.password_policy.temporary_max_age_hours) +
                                  " hours ago): an administrator must set a new one");
      }
      replaced = *verified;
    }
    if (c->action == Action::kSetPassword) {
      ARROW_RETURN_NOT_OK(CheckNewPassword(c->password, options_.password_policy));
      ARROW_ASSIGN_OR_RAISE(auto h, HashPassword(c->password, options_.password_iterations));
      hash = std::move(h);
    }
    std::string user_id;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      ARROW_RETURN_NOT_OK(authorize(snap));
      const bool attribute = c->action == Action::kSetAttribute || c->action == Action::kUnsetAttribute;
      if (authority.delegated()) {
        ARROW_RETURN_NOT_OK(RefuseAdministratorAccount(account_name, "ALTER USER", options_));
        // Session attributes decide what row and masking policies show: setting one is
        // granting data, not managing an account.
        if (attribute) {
          return AccessControlError(
              "Hardcharger blocked ALTER USER: session attributes decide what row and masking policies show, and "
              "only an administrator sets them (the ALTER USER privilege sets passwords and enables and disables "
              "users)");
        }
      }
      const auto* existing = snap.FindUserByName(account_name);
      if (!existing) return AccessControlError("user " + account_name + " does not exist");
      if (self_service && existing->password_hash != replaced) {
        return AccessControlError("Hardcharger blocked ALTER USER: the password of user " + existing->name +
                                  " changed while this statement ran; sign in again");
      }
      // A new password, ENABLE or an attribute of a user that can act as System Admin would
      // hand that to whoever makes the change (its own change, proven, is its own).
      if (!self_service && !actor.is_unrestricted_administrator() && snap.UserCarriesSystemAdmin(existing->id)) {
        return AccessControlError("Hardcharger blocked ALTER USER: user " + existing->name + " holds " +
                                  kSystemAdminName + "; " + kOnlySystemAdministrators + " changes it");
      }
      if (authority.delegated()) {
        ARROW_RETURN_NOT_OK(RequireUserWithinReach(snap, actor, *existing, "ALTER USER", /*dropping=*/false));
      }
      UserRecord user = *existing;
      user_id = user.id;
      auto temporary = [&](const std::optional<std::string>& password_hash) -> arrow::Status {
        return txn.SetPasswordReset(user.id, PasswordResetRecord{user.id, PasswordFingerprint(password_hash),
                                                                 own_id, NowMilliseconds()});
      };
      auto note = [&](const char* what) {
        if (notes) notes->emplace_back("password", what);
      };
      switch (c->action) {
        case Action::kSetPassword:
          user.password_hash = hash;
          // Every way of setting a password dates it (password_max_age_days).
          ARROW_RETURN_NOT_OK(txn.SetPasswordChangedAt(user.id, NowMilliseconds()));
          if (self_service) {
            message = "Password changed for user " + user.name + ": sign in again with the new one";
            note("password changed by the user");
            ARROW_RETURN_NOT_OK(txn.SetPasswordReset(user.id, std::nullopt));
          } else if (authority.sets_temporary_passwords() || c->expire) {
            message = "Temporary password set for user " + user.name +
                      ": it must be changed at the next sign-in (ALTER USER CURRENT_USER WITH PASSWORD ... REPLACE "
                      "...)";
            note("temporary password set");
            ARROW_RETURN_NOT_OK(temporary(hash));
          } else {
            message = "Password changed for user " + user.name;
            note("password set");
            ARROW_RETURN_NOT_OK(txn.SetPasswordReset(user.id, std::nullopt));
          }
          break;
        case Action::kClearPassword:
          user.password_hash.reset();
          message = "Password login removed for user " + user.name;
          note("password removed");
          ARROW_RETURN_NOT_OK(txn.SetPasswordReset(user.id, std::nullopt));
          ARROW_RETURN_NOT_OK(txn.SetPasswordChangedAt(user.id, std::nullopt));
          break;
        case Action::kExpirePassword:
          if (!user.password_hash) {
            return AccessControlError("user " + user.name + " has no password to expire");
          }
          message = "Password of user " + user.name + " expired: it must be changed at the next statement";
          note("password expired");
          return temporary(user.password_hash);
        case Action::kEnable:
          user.enabled = true;
          message = "User " + user.name + " enabled";
          break;
        case Action::kDisable:
          user.enabled = false;
          message = "User " + user.name + " disabled";
          break;
        case Action::kSetAttribute:
          message = "Attribute " + c->attribute + " set for user " + user.name;
          return txn.SetUserAttribute(user.id, c->attribute, c->value);
        case Action::kUnsetAttribute:
          message = "Attribute " + c->attribute + " removed for user " + user.name;
          return txn.SetUserAttribute(user.id, c->attribute, std::nullopt);
      }
      return txn.UpdateUser(user);
    }));
    result = Message(message);
    if (c->action == Action::kDisable || c->action == Action::kSetPassword || c->action == Action::kClearPassword) {
      result.terminate_user_ids.push_back(user_id);
    }
    return result;
  }

  // ---- DROP USER ---------------------------------------------------------
  if (const auto* c = std::get_if<DropUserCommand>(&command)) {
    std::string user_id;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      ARROW_RETURN_NOT_OK(authorize(snap));
      if (authority.delegated()) ARROW_RETURN_NOT_OK(RefuseAdministratorAccount(c->name, "DROP USER", options_));
      const auto* user = snap.FindUserByName(c->name);
      if (!user) {
        if (c->if_exists) {
          message = "User " + c->name + " does not exist";
          return arrow::Status::OK();
        }
        return AccessControlError("user " + c->name + " does not exist");
      }
      if (!actor.is_unrestricted_administrator() && snap.UserCarriesSystemAdmin(user->id)) {
        return AccessControlError("Hardcharger blocked DROP USER: user " + user->name + " holds " +
                                  kSystemAdminName + "; " + kOnlySystemAdministrators + " drops it");
      }
      if (authority.delegated()) {
        ARROW_RETURN_NOT_OK(RequireUserWithinReach(snap, actor, *user, "DROP USER", /*dropping=*/true));
      }
      ARROW_ASSIGN_OR_RAISE(auto owned, OwnedObjects(txn, snap, OwnerKind::kUser, user->id, resolver));
      if (!owned.empty()) {
        return AccessControlError("user " + user->name + " owns " + Join(owned) +
                                  ". Change the owner first (ALTER TABLE|VIEW ... OWNER TO)");
      }
      user_id = user->id;
      message = "User " + user->name + " dropped";
      return txn.DeleteUser(user->id);
    }));
    result = Message(message);
    if (!user_id.empty()) result.terminate_user_ids.push_back(user_id);
    return result;
  }

  // ---- CREATE ROLE -------------------------------------------------------
  if (const auto* c = std::get_if<CreateRoleCommand>(&command)) {
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      ARROW_RETURN_NOT_OK(authorize(snap));
      if (c->if_not_exists && snap.FindRoleByName(c->name)) {
        message = "Role " + c->name + " already exists";
        return arrow::Status::OK();
      }
      ARROW_RETURN_NOT_OK(CheckNameFree(snap, c->name));
      ARROW_ASSIGN_OR_RAISE(auto tenant, TenantForName(snap, c->name, /*for_user=*/false));
      // A new role holds nothing, and its creator gets no admin option on it: what goes into
      // it, and who holds it, is up to those who may grant that.
      message = "Role " + c->name + " created";
      RoleRecord role{StoreTransaction::NewId(), c->name, false};
      ARROW_RETURN_NOT_OK(txn.InsertRole(role));
      return txn.SetPrincipalTenant(PrincipalKind::kRole, role.id, tenant);
    }));
    return Message(message);
  }

  // ---- DROP ROLE ---------------------------------------------------------
  if (const auto* c = std::get_if<DropRoleCommand>(&command)) {
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      ARROW_RETURN_NOT_OK(authorize(snap));
      const auto* role = snap.FindRoleByName(c->name);
      if (!role) {
        if (c->if_exists) {
          message = "Role " + c->name + " does not exist";
          return arrow::Status::OK();
        }
        return AccessControlError("role " + c->name + " does not exist");
      }
      if (role->builtin)
        return AccessControlError("the built-in role " + role->name + " cannot be dropped");
      if (!actor.is_unrestricted_administrator() && snap.RoleCarriesSystemAdmin(role->id)) {
        return AccessControlError("Hardcharger blocked DROP ROLE: role " + role->name + " holds " +
                                  kSystemAdminName + "; " + kOnlySystemAdministrators + " drops it");
      }
      if (authority.delegated()) ARROW_RETURN_NOT_OK(RequireRoleWithinReach(snap, actor, *role, "DROP ROLE"));
      ARROW_ASSIGN_OR_RAISE(auto owned, OwnedObjects(txn, snap, OwnerKind::kRole, role->id, resolver));
      if (!owned.empty()) {
        return AccessControlError("role " + role->name + " owns " + Join(owned) +
                                  ". Change the owner first (ALTER TABLE|VIEW ... OWNER TO)");
      }
      message = "Role " + role->name + " dropped";
      return txn.DeleteRole(role->id);
    }));
    return Message(message);
  }

  // ---- GRANT / REVOKE role -----------------------------------------------
  if (const auto* c = std::get_if<RoleMembershipCommand>(&command)) {
    std::vector<std::string> done;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      for (const auto& role_name : c->roles) {
        const auto* role = snap.FindRoleByName(role_name);
        if (!role) return AccessControlError("role " + role_name + " does not exist");
        if (role->id == kPublicRoleId) {
          return AccessControlError(
              "every user already holds PUBLIC; it cannot be granted or revoked");
        }
        const std::string role_tenant = snap.TenantOf(PrincipalKind::kRole, role->id);
        if (!snap.CanAdminRole(actor, role->id) && !IsTenantAdmin(snap, actor, role_tenant)) {
          return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": user " +
                                    actor.user_name + " may not grant or revoke role " +
                                    role->name +
                                    " (only an administrator, or a holder of it "
                                    "WITH ADMIN OPTION)");
        }
        // Membership in a role that holds SYSTEM ADMIN (itself, or through a role it was
        // granted) is the capability: its admin option gives and takes it no longer. (Said
        // to someone who administers the role, and so sees what it holds. A membership
        // someone else did grant, on an older replica, carries nothing:
        // Snapshot::SystemAdminSources.)
        if (!actor.is_unrestricted_administrator() && snap.RoleCarriesSystemAdmin(role->id)) {
          return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": role " + role->name +
                                    " holds " + kSystemAdminName + "; " + kOnlySystemAdministrators +
                                    " grants or revokes it");
        }
        // Likewise an account privilege (CREATE USER, ...): it spreads only by an
        // administrator's hand, so its admin option does not give membership in a role that
        // holds one (and a membership it gave would not carry it: AccountPrivilegeRoles).
        // Revoking one stays the grantor's, as for any membership.
        if (c->grant && !actor.is_unrestricted_administrator() && snap.RoleCarriesAccountPrivilege(role->id)) {
          return AccessControlError("Hardcharger blocked GRANT: role " + role->name + " holds the account privilege " +
                                    AccountPrivilegesCarried(snap, role->id) +
                                    "; only an administrator grants membership in it");
        }
        if (notes && notes->empty()) {
          notes->emplace_back("authorized_by", actor.is_unrestricted_administrator()
                                                   ? (actor.acts_as_system_admin() ? "System Admin" : "administrator")
                                               : snap.CanAdminRole(actor, role->id)
                                                   ? "ADMIN OPTION on role " + role->name
                                                   : "tenant_admin of " + TenantName(snap, role_tenant));
        }
        for (const auto& g : c->grantees) {
          ARROW_ASSIGN_OR_RAISE(auto grantee, ResolveGrantee(snap, g));
          if (snap.TenantOf(grantee.kind, grantee.id) != role_tenant) {
            return AccessControlError("role " + role->name + " belongs to " +
                                      TenantName(snap, role_tenant) + " and " + grantee.name +
                                      " does not: roles never cross tenants");
          }
          const MembershipRecord* existing = nullptr;
          for (const auto& m : snap.memberships()) {
            if (m.member_kind == grantee.kind && m.member_id == grantee.id &&
                m.role_id == role->id) {
              existing = &m;
            }
          }
          if (c->grant) {
            if (grantee.kind == PrincipalKind::kRole) {
              if (grantee.id == role->id)
                return AccessControlError("a role cannot be granted to itself");
              if (snap.WouldCreateCycle(role->id, grantee.id)) {
                return AccessControlError("granting " + role->name + " to " + grantee.name +
                                          " would create a cycle of roles");
              }
            }
            if (existing) {
              // A membership in a role that holds SYSTEM ADMIN, an account privilege or a
              // policy exemption carries it only when the administrator granted it: the
              // administrator's grant of one that a role's admin-option holder made makes it the
              // administrator's, from now on.
              if (actor.superuser && !existing->granted_by.empty() &&
                  (snap.RoleCarriesSystemAdmin(role->id) || snap.RoleCarriesAccountPrivilege(role->id) ||
                   snap.RoleCarriesPolicyExemption(role->id))) {
                ARROW_RETURN_NOT_OK(txn.DeleteMembership(grantee.kind, grantee.id, role->id));
                MembershipRecord taken = *existing;
                taken.admin_option = existing->admin_option || c->with_admin_option;
                taken.granted_by = grantor;
                ARROW_RETURN_NOT_OK(txn.InsertMembership(taken));
                done.push_back("Granted " + role->name + " to " + grantee.name +
                               (c->with_admin_option ? " WITH ADMIN OPTION" : ""));
                continue;
              }
              if (c->with_admin_option && !existing->admin_option) {
                ARROW_RETURN_NOT_OK(txn.DeleteMembership(grantee.kind, grantee.id, role->id));
                MembershipRecord updated = *existing;
                updated.admin_option = true;
                updated.granted_by = grantor;
                ARROW_RETURN_NOT_OK(txn.InsertMembership(updated));
                done.push_back("Granted " + role->name + " to " + grantee.name +
                               " WITH ADMIN OPTION");
                continue;
              }
              done.push_back(grantee.name + " already holds " + role->name);
              continue;
            }
            ARROW_RETURN_NOT_OK(txn.InsertMembership(
                {grantee.kind, grantee.id, role->id, c->with_admin_option, grantor}));
            done.push_back("Granted " + role->name + " to " + grantee.name +
                           (c->with_admin_option ? " WITH ADMIN OPTION" : ""));
          } else {
            if (!existing) {
              done.push_back(grantee.name + " does not hold " + role->name);
              continue;
            }
            // Others revoke only what they granted; the administrator, anything.
            if (!actor.superuser && existing->granted_by != grantor &&
                !IsTenantAdmin(snap, actor, role_tenant)) {
              return AccessControlError("Hardcharger blocked REVOKE: " + grantee.name + " holds " +
                                        role->name +
                                        " from another grantor; only that grantor "
                                        "or an administrator can revoke it");
            }
            ARROW_RETURN_NOT_OK(txn.DeleteMembership(grantee.kind, grantee.id, role->id));
            if (c->with_admin_option) {  // REVOKE ADMIN OPTION FOR: keep the membership
              MembershipRecord updated = *existing;
              updated.admin_option = false;
              ARROW_RETURN_NOT_OK(txn.InsertMembership(updated));
              done.push_back("Revoked the admin option on " + role->name + " from " + grantee.name);
            } else {
              done.push_back("Revoked " + role->name + " from " + grantee.name);
            }
          }
        }
      }
      return arrow::Status::OK();
    }));
    return with_cascade_note(Join(done, "; "));
  }

  // ---- GRANT / REVOKE privileges -----------------------------------------
  if (const auto* c = std::get_if<ObjectPrivilegeCommand>(&command)) {
    ObjectPath target;
    std::vector<std::string> table_columns;
    switch (c->scope) {
      case GrantScope::kObject: {
        if (c->sequence) {
          ARROW_ASSIGN_OR_RAISE(target, resolver.ResolveSequence(c->target));
          break;
        }
        if (c->table_function) {
          ARROW_ASSIGN_OR_RAISE(target, resolver.ResolveTableFunction(c->target, c->grant));
          break;
        }
        ARROW_ASSIGN_OR_RAISE(auto obj, resolver.ResolveObject(c->target));
        target = obj.path;
        table_columns = obj.columns;
        break;
      }
      case GrantScope::kSchema: {
        ARROW_ASSIGN_OR_RAISE(target, resolver.ResolveSchema(c->target));
        break;
      }
      case GrantScope::kCatalog: {
        ARROW_ASSIGN_OR_RAISE(target.catalog, resolver.ResolveCatalog(c->target.parts.at(0)));
        break;
      }
    }
    // Columns must exist, and are recorded as the table spells them.
    for (const auto& item : c->privileges) {
      for (const auto& col : item.columns) {
        auto it = std::find_if(table_columns.begin(), table_columns.end(),
                               [&](const std::string& t) { return IdentifiersEqual(t, col); });
        if (it == table_columns.end()) {
          return AccessControlError("column " + col + " does not exist in " + target.ToString());
        }
      }
    }
    std::vector<std::string> done;
    ARROW_RETURN_NOT_OK(mutate_with(
        !c->grant && !c->cascade,
        [&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
          std::vector<ResolvedGrantee> grantees;
          for (const auto& g : c->grantees) {
            ARROW_ASSIGN_OR_RAISE(auto grantee, ResolveGrantee(snap, g));
            if (grantee.kind != PrincipalKind::kRole) {
              return AccessControlError(
                  "privileges are granted to roles, not users. Grant them to a "
                  "role, then GRANT the role TO USER " +
                  grantee.name);
            }
            // A table function is in no catalog: a tenant's role may hold EXECUTE on it (what the
            // function reads is still the role's storage privileges' business).
            const std::string role_tenant = snap.TenantOf(PrincipalKind::kRole, grantee.id);
            if (!role_tenant.empty() && !target.IsTableFunction() && snap.TenantOfCatalog(target.catalog) != role_tenant) {
              return AccessControlError("role " + grantee.name + " belongs to " +
                                        TenantName(snap, role_tenant) +
                                        "; it can hold privileges "
                                        "only on that tenant's catalogs");
            }
            grantees.push_back(std::move(grantee));
          }
          for (const auto& item : c->privileges) {
            for (Privilege p : Expand(item.privilege, c->scope)) {
              // EXECUTE keeps its path declarations where SELECT keeps its columns.
              std::vector<std::string> columns;
              if (p == Privilege::kSelect) columns = item.columns;
              if (p == Privilege::kExecute) {
                for (const auto& argument : c->path_arguments) columns.push_back(argument.Stored());
              }
              const std::string what =
                  p == Privilege::kExecute
                      ? "EXECUTE on table function " + target.object_name +
                            (columns.empty() ? "" : " (path " + Join(PathArgumentTexts(columns)) + ")")
                      : PrivilegeName(p) + (columns.empty() ? "" : " (" + Join(columns) + ")") + " on " +
                            target.ToString();
              if (c->grant && !snap.CanGrant(actor, c->scope, target, p, columns)) {
                return AccessControlError(
                    "Hardcharger blocked GRANT: user " + actor.user_name + " may not grant " +
                    what + " (only its owner, or a role granted it WITH GRANT OPTION)");
              }
              for (const auto& grantee : grantees) {
                // One record per grantor, so each grant stands or falls with its own grantor.
                std::vector<const ObjectGrantRecord*> matching;
                const ObjectGrantRecord* own = nullptr;
                for (const auto& g : snap.grants()) {
                  if (g.role_id == grantee.id && g.scope == c->scope && g.privilege == p &&
                      SamePath(g.target, target)) {
                    matching.push_back(&g);
                    if (g.granted_by == grantor) own = &g;
                  }
                }
                if (c->grant) {
                  if (own) {
                    ObjectGrantRecord updated = *own;
                    bool changed = false;
                    if (c->with_grant_option && !own->grant_option) {
                      updated.grant_option = true;
                      changed = true;
                    }
                    if (p == Privilege::kExecute) {
                      // The latest GRANT's declaration of the path arguments stands.
                      if (own->columns != columns) {
                        updated.columns = columns;
                        changed = true;
                      }
                    } else if (!own->columns.empty() && columns.empty()) {
                      updated.columns.clear();  // widen a column grant to the whole table
                      changed = true;
                    } else if (!own->columns.empty()) {
                      for (const auto& col : columns) {
                        if (std::none_of(
                                updated.columns.begin(), updated.columns.end(),
                                [&](const std::string& m) { return IdentifiersEqual(m, col); })) {
                          updated.columns.push_back(col);
                          changed = true;
                        }
                      }
                    }
                    if (changed) {
                      ARROW_RETURN_NOT_OK(txn.UpdateGrant(updated));
                      done.push_back("Granted " + what + " to " + grantee.name +
                                     (updated.grant_option ? " WITH GRANT OPTION" : ""));
                    } else {
                      done.push_back(grantee.name + " already has " + what);
                    }
                    continue;
                  }
                  ObjectGrantRecord record;
                  record.id = StoreTransaction::NewId();
                  record.role_id = grantee.id;
                  record.scope = c->scope;
                  record.target = target;
                  record.privilege = p;
                  record.columns = columns;
                  record.grant_option = c->with_grant_option;
                  record.granted_by = grantor;
                  ARROW_RETURN_NOT_OK(txn.InsertGrant(record));
                  done.push_back("Granted " + what + " to " + grantee.name +
                                 (c->with_grant_option ? " WITH GRANT OPTION" : ""));
                  continue;
                }

                // REVOKE: the administrator's applies to every grantor's record;
                // anyone else's only to the records they granted.
                std::vector<const ObjectGrantRecord*> revocable;
                for (const auto* g : matching) {
                  if (actor.superuser || g->granted_by == grantor) revocable.push_back(g);
                }
                if (revocable.empty()) {
                  if (!matching.empty()) {
                    return AccessControlError("Hardcharger blocked REVOKE: " + grantee.name +
                                              " has " + what +
                                              " from another grantor; only that grantor or "
                                              "an administrator can revoke it");
                  }
                  done.push_back(grantee.name + " does not have " + what);
                  continue;
                }
                for (const auto* existing : revocable) {
                  if (c->with_grant_option) {  // REVOKE GRANT OPTION FOR: keep the privilege
                    ObjectGrantRecord updated = *existing;
                    updated.grant_option = false;
                    ARROW_RETURN_NOT_OK(txn.UpdateGrant(updated));
                    continue;
                  }
                  if (columns.empty()) {
                    ARROW_RETURN_NOT_OK(txn.DeleteGrant(existing->id));
                    continue;
                  }
                  // Revoke some columns: from a whole-table grant, keep every other column
                  // (not a silent no-op as in Postgres).
                  ObjectGrantRecord narrowed = *existing;
                  std::vector<std::string> remaining;
                  const auto& base = existing->columns.empty() ? table_columns : existing->columns;
                  for (const auto& col : base) {
                    if (std::none_of(columns.begin(), columns.end(), [&](const std::string& r) {
                          return IdentifiersEqual(r, col);
                        })) {
                      remaining.push_back(col);
                    }
                  }
                  if (remaining.empty()) {
                    ARROW_RETURN_NOT_OK(txn.DeleteGrant(existing->id));
                  } else {
                    narrowed.columns = remaining;
                    ARROW_RETURN_NOT_OK(txn.UpdateGrant(narrowed));
                  }
                }
                done.push_back(
                    (c->with_grant_option ? "Revoked the grant option for " : "Revoked ") + what +
                    " from " + grantee.name);
              }
            }
          }
          return arrow::Status::OK();
        }));
    return with_cascade_note(Join(done, "; "));
  }

  // ---- GRANT / REVOKE system privileges -----------------------------------
  if (const auto* c = std::get_if<SystemPrivilegeCommand>(&command)) {
    // ON LOCATION: the key the location is stored and compared as; its kind must be each
    // privilege's.
    std::string location;
    if (c->location) {
      auto parsed = ParseGrantLocation(*c->location);
      if (!parsed.ok()) return AccessControlError("Hardcharger blocked this location: " + parsed.status().message());
      for (SystemPrivilege p : c->privileges) {
        if (StorageOf(p)->first != parsed->first) {
          return AccessControlError(SystemPrivilegeName(p) + " can't be granted on '" + *c->location +
                                    "': it is a location in " + StorageKindName(parsed->first) + ", and " +
                                    SystemPrivilegeName(p) + " covers " + StorageKindName(StorageOf(p)->first));
        }
      }
      location = parsed->second;
    }
    const std::string where = location.empty() ? "" : " on location '" + location + "'";
    // A policy exemption takes away every row policy, or every mask, at once: only an
    // unrestricted administrator grants or revokes one, and never with an admin option.
    for (SystemPrivilege p : c->privileges) {
      if (!IsPolicyExemption(p)) continue;
      if (!actor.is_unrestricted_administrator()) {
        return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": " + kOnlySystemAdministrators +
                                  " grants and revokes " + SystemPrivilegeName(p));
      }
      if (c->with_admin_option) {
        return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": " + SystemPrivilegeName(p) +
                                  " has no admin option; only the administrator, or a user acting as System Admin, "
                                  "grants it");
      }
    }
    std::vector<std::string> done;
    ARROW_RETURN_NOT_OK(mutate_with(
        !c->grant && !c->cascade,
        [&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
          for (SystemPrivilege p : c->privileges) {
            const std::string name = SystemPrivilegeName(p) + where;
            if (IsAccountPrivilege(p)) {
              // Granted and revoked by an unrestricted administrator alone, with no admin
              // option to pass on, to system-scope roles that are not built in.
              if (!actor.is_unrestricted_administrator()) {
                return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": only an administrator " +
                                          (c->grant ? "grants " : "revokes ") + name +
                                          " (an account privilege is never delegated)");
              }
              if (c->with_admin_option) {
                return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": " + name +
                                          " has no ADMIN OPTION; only an administrator grants and revokes an "
                                          "account privilege");
              }
              if (notes && notes->empty()) {
                notes->emplace_back("authorized_by", actor.acts_as_system_admin() ? "System Admin" : "administrator");
              }
            }
            if (c->grant && !snap.CanGrantSystemPrivilege(actor, p, location)) {
              return AccessControlError(
                  "Hardcharger blocked GRANT: user " + actor.user_name + " may not grant " + name +
                  " (only an administrator, or a holder of it WITH ADMIN OPTION there or everywhere)");
            }
            for (const auto& g : c->grantees) {
              ARROW_ASSIGN_OR_RAISE(auto grantee, ResolveGrantee(snap, g));
              if (grantee.kind != PrincipalKind::kRole) {
                return AccessControlError(
                    "privileges are granted to roles, not users. Grant them to a "
                    "role, then GRANT the role TO USER " +
                    grantee.name);
              }
              if (c->grant && !snap.TenantOf(PrincipalKind::kRole, grantee.id).empty()) {
                return AccessControlError(
                    "system privileges reach the whole server; a tenant's role "
                    "(" +
                    grantee.name + ") cannot hold them");
              }
              if (c->grant && IsAccountPrivilege(p)) {
                const RoleRecord* record = snap.FindRoleById(grantee.id);
                if (grantee.id == kPublicRoleId || (record && record->builtin)) {
                  return AccessControlError("Hardcharger blocked GRANT: " + name +
                                            " cannot be granted to the built-in role " + grantee.name +
                                            ", which every user holds");
                }
              }
              if (c->grant && IsPolicyExemption(p) && grantee.id == kPublicRoleId) {
                return AccessControlError("Hardcharger blocked GRANT: " + SystemPrivilegeName(p) +
                                          " is never granted to PUBLIC (that would take the policies away from "
                                          "everyone: drop them instead)");
              }
              std::vector<SystemPrivilegeRecord> matching;
              const SystemPrivilegeRecord* own = nullptr;
              for (const auto& r : snap.system_privileges()) {
                if (r.role_id == grantee.id && r.privilege == p && r.location == location) {
                  matching.push_back(r);
                  if (r.granted_by == grantor) own = &r;
                }
              }
              if (c->grant) {
                SystemPrivilegeRecord record{grantee.id, p, c->with_admin_option, grantor, location};
                if (own && (own->admin_option || !c->with_admin_option)) {
                  done.push_back(grantee.name + " already has " + name);
                  continue;
                }
                ARROW_RETURN_NOT_OK(txn.UpsertSystemPrivilege(record));
                done.push_back("Granted " + name + " to " + grantee.name +
                               (c->with_admin_option ? " WITH ADMIN OPTION" : ""));
                continue;
              }
              std::vector<SystemPrivilegeRecord> revocable;
              for (const auto& r : matching) {
                if (actor.superuser || r.granted_by == grantor) revocable.push_back(r);
              }
              if (revocable.empty()) {
                if (!matching.empty()) {
                  return AccessControlError("Hardcharger blocked REVOKE: " + grantee.name +
                                            " has " + name +
                                            " from another grantor; only that grantor or an "
                                            "administrator can revoke it");
                }
                done.push_back(grantee.name + " does not have " + name);
                continue;
              }
              for (auto r : revocable) {
                if (c->with_admin_option) {
                  r.admin_option = false;
                  ARROW_RETURN_NOT_OK(txn.UpsertSystemPrivilege(r));
                } else {
                  ARROW_RETURN_NOT_OK(txn.DeleteSystemPrivilege(r));
                }
              }
              done.push_back((c->with_admin_option ? "Revoked the admin option on " : "Revoked ") +
                             name + " from " + grantee.name);
            }
          }
          return arrow::Status::OK();
        }));
    return with_cascade_note(Join(done, "; "));
  }

  // ---- GRANT / REVOKE SYSTEM ADMIN -------------------------------------------
  if (const auto* c = std::get_if<SystemAdminCommand>(&command)) {
    // Never delegated: no option, no owner, no tenant admin, no ALL stands for it.
    if (!actor.is_unrestricted_administrator()) {
      return AccessControlError("Hardcharger blocked " + CommandKind(command) + ": " + kOnlySystemAdministrators +
                                " grants and revokes " + kSystemAdminName);
    }
    std::vector<std::string> done;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      done.clear();
      for (const auto& g : c->grantees) {
        ARROW_ASSIGN_OR_RAISE(auto grantee, ResolveGrantee(snap, g));
        const bool role = grantee.kind == PrincipalKind::kRole;
        const bool held = std::any_of(snap.system_admins().begin(), snap.system_admins().end(),
                                      [&](const SystemAdminRecord& r) {
                                        return r.kind == grantee.kind && r.principal_id == grantee.id;
                                      });
        if (!c->grant) {
          if (!held) {
            done.push_back(grantee.name + " does not hold " + kSystemAdminName);
            continue;
          }
          ARROW_RETURN_NOT_OK(txn.DeleteSystemAdmin(grantee.kind, grantee.id));
          done.push_back(std::string("Revoked ") + kSystemAdminName + " from " + grantee.name);
          continue;
        }
        if (!snap.TenantOf(grantee.kind, grantee.id).empty()) {
          return AccessControlError(std::string(kSystemAdminName) + " reaches the whole server; a tenant's " +
                                    (role ? "role" : "user") + " (" + grantee.name + ") cannot hold it");
        }
        if (role) {
          const RoleRecord* record = snap.FindRoleById(grantee.id);
          if (grantee.id == kPublicRoleId || (record && record->builtin)) {
            return AccessControlError(std::string(kSystemAdminName) + " cannot be granted to the built-in role " +
                                      grantee.name + ": every user holds it");
          }
        }
        if (held) {
          done.push_back(grantee.name + " already holds " + kSystemAdminName);
          continue;
        }
        ARROW_RETURN_NOT_OK(txn.UpsertSystemAdmin({grantee.kind, grantee.id, actor.acting_user_id}));
        done.push_back(std::string("Granted ") + kSystemAdminName + " to " + grantee.name);
      }
      return arrow::Status::OK();
    }));
    return Message(Join(done, "; "));
  }

  // ---- tenants ---------------------------------------------------------------
  if (std::holds_alternative<TenantCommand>(command) ||
      std::holds_alternative<CatalogTenantCommand>(command) ||
      std::holds_alternative<TenantDefaultCatalogCommand>(command)) {
    if (!actor.superuser) {
      return AccessControlError("Hardcharger blocked " + CommandKind(command) +
                                ": only administrators manage tenants");
    }
  }
  if (const auto* c = std::get_if<TenantCommand>(&command)) {
    CommandResult result;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      const auto* existing = snap.FindTenantByName(c->name);
      if (c->create) {
        if (existing) {
          if (c->if_exists) {
            message = "Tenant " + c->name + " already exists";
            return arrow::Status::OK();
          }
          return AccessControlError("tenant " + c->name + " already exists");
        }
        if (c->name.find('/') != std::string::npos)
          return AccessControlError("a tenant name cannot contain /");
        TenantRecord tenant{StoreTransaction::NewId(), c->name, ""};
        ARROW_RETURN_NOT_OK(txn.UpsertTenant(tenant));
        for (const char* suffix : {kTenantPublicSuffix, kTenantAdminSuffix}) {
          ARROW_RETURN_NOT_OK(CheckNameFree(snap, c->name + suffix));
          RoleRecord role{StoreTransaction::NewId(), c->name + suffix, true};
          ARROW_RETURN_NOT_OK(txn.InsertRole(role));
          ARROW_RETURN_NOT_OK(txn.SetPrincipalTenant(PrincipalKind::kRole, role.id, tenant.id));
        }
        message = "Tenant " + c->name + " created, with roles " + c->name + kTenantPublicSuffix +
                  " and " + c->name + kTenantAdminSuffix;
        return arrow::Status::OK();
      }
      if (!existing) {
        if (c->if_exists) {
          message = "Tenant " + c->name + " does not exist";
          return arrow::Status::OK();
        }
        return AccessControlError("tenant " + c->name + " does not exist");
      }
      std::vector<std::string> members;
      for (const auto& u : snap.users()) {
        if (snap.TenantOf(PrincipalKind::kUser, u.id) == existing->id) members.push_back(u.name);
      }
      if (!members.empty()) {
        std::sort(members.begin(), members.end());
        return AccessControlError("tenant " + existing->name + " still has users (" +
                                  Join(members) + "); drop them first");
      }
      if (!snap.TenantCatalogs(existing->id).empty()) {
        return AccessControlError("tenant " + existing->name +
                                  " still has catalogs; ALTER CATALOG ... UNSET TENANT first");
      }
      for (const auto& r : snap.roles()) {
        if (snap.TenantOf(PrincipalKind::kRole, r.id) == existing->id) {
          ARROW_RETURN_NOT_OK(txn.DeleteRole(r.id));
        }
      }
      message = "Tenant " + existing->name + " dropped";
      return txn.DeleteTenant(existing->id);
    }));
    return Message(message);
  }
  if (const auto* c = std::get_if<CatalogTenantCommand>(&command)) {
    ARROW_ASSIGN_OR_RAISE(auto catalog, resolver.ResolveCatalog(c->catalog));
    CommandResult result;
    std::vector<std::string> affected_users;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      const std::string current = snap.TenantOfCatalog(catalog);
      auto users_of = [&](const std::string& tenant_id) {
        for (const auto& u : snap.users()) {
          if (!tenant_id.empty() && snap.TenantOf(PrincipalKind::kUser, u.id) == tenant_id) {
            affected_users.push_back(u.id);
          }
        }
      };
      if (!c->tenant) {
        if (current.empty()) {
          message = "Catalog " + catalog + " belongs to no tenant";
          return arrow::Status::OK();
        }
        // The tenant's roles lose what they held there.
        for (const auto& g : snap.grants()) {
          if (IdentifiersEqual(g.target.catalog, catalog) &&
              snap.TenantOf(PrincipalKind::kRole, g.role_id) == current) {
            ARROW_RETURN_NOT_OK(txn.DeleteGrant(g.id));
          }
        }
        users_of(current);
        message = "Catalog " + catalog + " no longer belongs to " + TenantName(snap, current);
        return txn.SetCatalogTenant(catalog, "");
      }
      const auto* tenant = snap.FindTenantByName(*c->tenant);
      if (!tenant) return AccessControlError("tenant " + *c->tenant + " does not exist");
      if (!current.empty() && current != tenant->id) {
        return AccessControlError("catalog " + catalog + " belongs to " +
                                  TenantName(snap, current) + "; UNSET TENANT first");
      }
      ARROW_RETURN_NOT_OK(txn.SetCatalogTenant(catalog, tenant->id));
      // The tenant admin may do everything there, and grant it on.
      const RoleRecord* admin = snap.TenantRole(tenant->id, kTenantAdminSuffix);
      if (!admin) return AccessControlError("tenant " + tenant->name + " has no tenant_admin role");
      for (Privilege p : {Privilege::kSelect, Privilege::kInsert, Privilege::kUpdate,
                          Privilege::kDelete, Privilege::kCreateTable, Privilege::kCreateView,
                          Privilege::kCreateSchema, Privilege::kDropTable, Privilege::kDropView,
                          Privilege::kDropSchema, Privilege::kAlterTable, Privilege::kAlterView,
                          Privilege::kCreateMacro, Privilege::kDropMacro, Privilege::kCreateType,
                          Privilege::kDropType, Privilege::kCreateTrigger, Privilege::kDropTrigger,
                          Privilege::kUsage, Privilege::kCreateSequence, Privilege::kDropSequence,
                          Privilege::kMaintain}) {
        bool have = false;
        for (const auto& g : snap.grants()) {
          have = have ||
                 (g.role_id == admin->id && g.scope == GrantScope::kCatalog && g.privilege == p &&
                  IdentifiersEqual(g.target.catalog, catalog) && g.granted_by.empty());
        }
        if (have) continue;
        ObjectGrantRecord grant;
        grant.id = StoreTransaction::NewId();
        grant.role_id = admin->id;
        grant.scope = GrantScope::kCatalog;
        grant.target.catalog = catalog;
        grant.privilege = p;
        grant.grant_option = true;
        ARROW_RETURN_NOT_OK(txn.InsertGrant(grant));
      }
      users_of(tenant->id);
      message = "Catalog " + catalog + " now belongs to tenant " + tenant->name;
      return arrow::Status::OK();
    }));
    result = Message(message);
    // What the tenant's users may see changed: their sessions start over.
    result.terminate_user_ids = affected_users;
    return result;
  }
  if (const auto* c = std::get_if<TenantDefaultCatalogCommand>(&command)) {
    std::string catalog;
    if (!c->catalog.empty()) {
      ARROW_ASSIGN_OR_RAISE(catalog, resolver.ResolveCatalog(c->catalog));
    }
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      const auto* tenant = snap.FindTenantByName(c->tenant);
      if (!tenant) return AccessControlError("tenant " + c->tenant + " does not exist");
      if (!catalog.empty() && snap.TenantOfCatalog(catalog) != tenant->id) {
        return AccessControlError("catalog " + catalog + " does not belong to tenant " +
                                  tenant->name);
      }
      TenantRecord updated = *tenant;
      updated.default_catalog = catalog;
      message = catalog.empty() ? "Tenant " + tenant->name + " has no default catalog"
                                : "Tenant " + tenant->name + "'s default catalog is " + catalog;
      return txn.UpsertTenant(updated);
    }));
    return Message(message);
  }

  // ---- row policies --------------------------------------------------------
  if (std::holds_alternative<CreateRowPolicyCommand>(command) ||
      std::holds_alternative<DropRowPolicyCommand>(command) ||
      std::holds_alternative<CreateMaskingPolicyCommand>(command) ||
      std::holds_alternative<DropMaskingPolicyCommand>(command) ||
      std::holds_alternative<SetColumnMaskCommand>(command) ||
      std::holds_alternative<ShowPoliciesCommand>(command)) {
    if (!actor.superuser) {
      return AccessControlError("Hardcharger blocked " + CommandKind(command) +
                                ": only administrators manage row and masking policies");
    }
  }
  if (const auto* c = std::get_if<CreateRowPolicyCommand>(&command)) {
    ARROW_ASSIGN_OR_RAISE(auto table, resolver.ResolveObject(c->table));
    if (table.type != ObjectType::kTable) {
      return AccessControlError("row policies go on tables; " + table.path.ToString() +
                                " is a view");
    }
    ARROW_RETURN_NOT_OK(ValidatePolicySql(c->predicate));
    ARROW_RETURN_NOT_OK(resolver.CheckRowPredicate(table, c->predicate));
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      RowPolicyRecord record;
      for (const auto* p : snap.RowPoliciesOn(table.path)) {
        if (IdentifiersEqual(p->name, c->name)) {
          if (!c->or_replace) {
            return AccessControlError("row policy " + c->name + " already exists on " +
                                      table.path.ToString());
          }
          record.id = p->id;
        }
      }
      if (record.id.empty()) record.id = StoreTransaction::NewId();
      record.name = c->name;
      record.table = table.path;
      record.permissive = c->permissive;
      record.predicate = c->predicate;
      for (const auto& role_name : c->roles) {
        const auto* role = snap.FindRoleByName(role_name);
        if (!role) return AccessControlError("role " + role_name + " does not exist");
        record.role_ids.push_back(role->id);
      }
      message = "Row policy " + c->name + " on " + table.path.ToString() +
                (c->or_replace ? " created or replaced" : " created");
      return txn.UpsertRowPolicy(record);
    }));
    return Message(message);
  }
  if (const auto* c = std::get_if<DropRowPolicyCommand>(&command)) {
    ARROW_ASSIGN_OR_RAISE(auto table, resolver.ResolveObject(c->table));
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      for (const auto* p : snap.RowPoliciesOn(table.path)) {
        if (IdentifiersEqual(p->name, c->name)) {
          message = "Row policy " + p->name + " on " + table.path.ToString() + " dropped";
          return txn.DeleteRowPolicy(p->id);
        }
      }
      if (c->if_exists) {
        message = "Row policy " + c->name + " does not exist on " + table.path.ToString();
        return arrow::Status::OK();
      }
      return AccessControlError("row policy " + c->name + " does not exist on " +
                                table.path.ToString());
    }));
    return Message(message);
  }

  // ---- masking policies ----------------------------------------------------
  if (const auto* c = std::get_if<CreateMaskingPolicyCommand>(&command)) {
    ARROW_RETURN_NOT_OK(ValidatePolicySql(c->body, c->parameter));
    MaskingPolicyRecord candidate{"",     c->name, c->parameter, c->parameter_type, c->return_type,
                                  c->body};
    ARROW_RETURN_NOT_OK(resolver.CheckMaskingPolicy(candidate));
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      MaskingPolicyRecord record = candidate;
      if (const auto* existing = snap.FindMaskingPolicyByName(c->name)) {
        if (!c->or_replace)
          return AccessControlError("masking policy " + c->name + " already exists");
        record.id = existing->id;
      } else {
        record.id = StoreTransaction::NewId();
      }
      message = "Masking policy " + c->name + (c->or_replace ? " created or replaced" : " created");
      return txn.UpsertMaskingPolicy(record);
    }));
    return Message(message);
  }
  if (const auto* c = std::get_if<DropMaskingPolicyCommand>(&command)) {
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      const auto* existing = snap.FindMaskingPolicyByName(c->name);
      if (!existing) {
        if (c->if_exists) {
          message = "Masking policy " + c->name + " does not exist";
          return arrow::Status::OK();
        }
        return AccessControlError("masking policy " + c->name + " does not exist");
      }
      for (const auto& b : snap.mask_bindings()) {
        if (b.policy_id == existing->id) {
          return AccessControlError("masking policy " + existing->name + " masks " +
                                    b.table.ToString() + "." + b.column +
                                    "; unset it there first (ALTER TABLE ... MODIFY COLUMN ... "
                                    "UNSET MASKING POLICY)");
        }
      }
      message = "Masking policy " + existing->name + " dropped";
      return txn.DeleteMaskingPolicy(existing->id);
    }));
    return Message(message);
  }
  if (const auto* c = std::get_if<SetColumnMaskCommand>(&command)) {
    ARROW_ASSIGN_OR_RAISE(auto table, resolver.ResolveObject(c->table));
    if (table.type != ObjectType::kTable) {
      return AccessControlError("masking policies go on table columns; " + table.path.ToString() +
                                " is a view");
    }
    auto column =
        std::find_if(table.columns.begin(), table.columns.end(),
                     [&](const std::string& t) { return IdentifiersEqual(t, c->column); });
    if (column == table.columns.end()) {
      return AccessControlError("column " + c->column + " does not exist in " +
                                table.path.ToString());
    }
    const std::string column_name = *column;
    if (c->policy) {
      auto snap = snapshot();
      const auto* policy = snap->FindMaskingPolicyByName(*c->policy);
      if (!policy) return AccessControlError("masking policy " + *c->policy + " does not exist");
      ARROW_RETURN_NOT_OK(resolver.CheckMaskBinding(table, column_name, *policy));
    }
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      MaskBindingRecord binding;
      binding.table = table.path;
      binding.column = column_name;
      if (!c->policy) {
        message = "Masking policy removed from " + table.path.ToString() + "." + column_name;
        return txn.SetMaskBinding(binding, /*remove=*/true);
      }
      const auto* policy = snap.FindMaskingPolicyByName(*c->policy);
      if (!policy) return AccessControlError("masking policy " + *c->policy + " does not exist");
      binding.policy_id = policy->id;
      for (const auto& role_name : c->exempt_roles) {
        const auto* role = snap.FindRoleByName(role_name);
        if (!role) return AccessControlError("role " + role_name + " does not exist");
        binding.exempt_role_ids.push_back(role->id);
      }
      message = table.path.ToString() + "." + column_name + " is masked by " + policy->name;
      return txn.SetMaskBinding(binding, /*remove=*/false);
    }));
    return Message(message);
  }
  if (const auto* c = std::get_if<SetRoleCommand>(&command)) {
    if (actor.superuser) {
      return AccessControlError(
          "Hardcharger blocked SET ROLE: the administrator holds every right "
          "already; SET ROLE is for store users");
    }
    auto snap = snapshot();
    CommandResult result;
    result.column_names = {"result"};
    if (c->system_admin) {
      // What the session is limited to is never widened: a scoped token stays inside its
      // scope, a tenant's user inside its tenant. (The session's own limits, a read-only
      // connection and a token with catalog_access rules, are refused by its guard.)
      if (actor.scope) {
        return AccessControlError(std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName +
                                  ": this session signed in with scoped token " + actor.scope->name +
                                  ", and a scoped token never acts as System Admin");
      }
      if (!snap->TenantOfPrincipal(actor).empty()) {
        return AccessControlError(std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName + ": user " +
                                  actor.user_name + " belongs to a tenant, and a tenant's user never acts as "
                                  "System Admin");
      }
      if (!snap->HoldsSystemAdmin(actor)) {
        return AccessControlError(std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName + ": user " +
                                  actor.user_name + " does not hold " + kSystemAdminName);
      }
      result.active_role_id = kSystemAdminRoleId;
      result.rows.push_back({std::string("Active role: ") + kSystemAdminName +
                             " (this session acts as the administrator until RESET ROLE)"});
      return result;
    }
    if (!c->role) {
      result.active_role_id = "";
      result.rows.push_back({"All of " + actor.user_name + "'s roles are active"});
      return result;
    }
    // An unqualified name means the user's own tenant's role first.
    const RoleRecord* role = nullptr;
    const std::string tenant = snap->TenantOfPrincipal(actor);
    if (!tenant.empty() && c->role->find('/') == std::string::npos) {
      if (const auto* t = snap->FindTenantById(tenant))
        role = snap->FindRoleByName(t->name + "/" + *c->role);
    }
    if (!role) role = snap->FindRoleByName(*c->role);
    if (!role || !snap->HeldRoleIds(actor).count(role->id)) {
      // Not telling a role that exists from one that does not.
      return AccessControlError("Hardcharger blocked SET ROLE: user " + actor.user_name +
                                " does not hold role " + *c->role);
    }
    result.active_role_id = role->id;
    result.rows.push_back({"Active role: " + role->name});
    return result;
  }
  if (const auto* c = std::get_if<GroupMappingCommand>(&command)) {
    if (!actor.superuser) {
      return AccessControlError("Hardcharger blocked " + CommandKind(command) +
                                ": only administrators manage group mappings");
    }
    auto snap = snapshot();
    auto role_name = [&](const std::string& id) {
      const auto* r = snap->FindRoleById(id);
      return r ? r->name : id;
    };
    if (c->kind == GroupMappingCommand::Kind::kShow) {
      CommandResult result;
      result.column_names = {"group", "issuer", "role"};
      for (const auto& m : snap->group_mappings()) {
        result.rows.push_back(
            {m.group, m.issuer.empty() ? "(any)" : m.issuer, role_name(m.role_id)});
      }
      return result;
    }
    const RoleRecord* role = nullptr;
    if (c->role) {
      role = snap->FindRoleByName(*c->role);
      if (!role) return AccessControlError("role " + *c->role + " does not exist");
      if (role->id == kPublicRoleId) {
        return AccessControlError("everyone holds PUBLIC already; map the group to another role");
      }
    }
    std::string mapping_message;
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& now) -> arrow::Status {
      if (c->kind == GroupMappingCommand::Kind::kCreate) {
        for (const auto& m : now.group_mappings()) {
          if (m.group == c->group && m.issuer == c->issuer && m.role_id == role->id) {
            return AccessControlError("group '" + c->group + "' is already mapped to role " +
                                      role->name);
          }
        }
        mapping_message = "Users in group '" + c->group + "' hold role " + role->name;
        return txn.InsertGroupMapping({StoreTransaction::NewId(), c->issuer, c->group, role->id});
      }
      std::vector<std::string> ids;
      for (const auto& m : now.group_mappings()) {
        if (m.group == c->group && m.issuer == c->issuer && (!role || m.role_id == role->id)) {
          ids.push_back(m.id);
        }
      }
      if (ids.empty()) return AccessControlError("group '" + c->group + "' has no such mapping");
      for (const auto& id : ids) ARROW_RETURN_NOT_OK(txn.DeleteGroupMapping(id));
      mapping_message = "Removed " + std::to_string(ids.size()) + " mapping" +
                        (ids.size() == 1 ? "" : "s") + " of group '" + c->group + "'";
      return arrow::Status::OK();
    }));
    return Message(mapping_message);
  }
  if (std::holds_alternative<ShowTenantsCommand>(command)) {
    // The administrator sees every tenant; a tenant's users see their own.
    auto snap = snapshot();
    const std::string own = actor.superuser ? "" : snap->TenantOfPrincipal(actor);
    CommandResult result;
    result.column_names = {"tenant", "default_catalog", "catalogs", "users", "roles"};
    for (const auto& t : snap->tenants()) {
      if (!actor.superuser && t.id != own) continue;
      const auto tenant_catalogs = snap->TenantCatalogs(t.id);
      const std::vector<std::string> catalogs(tenant_catalogs.begin(), tenant_catalogs.end());
      int64_t users = 0, roles = 0;
      for (const auto& u : snap->users())
        users += snap->TenantOf(PrincipalKind::kUser, u.id) == t.id;
      for (const auto& r : snap->roles())
        roles += snap->TenantOf(PrincipalKind::kRole, r.id) == t.id;
      result.rows.push_back({t.name, t.default_catalog, Join(catalogs), std::to_string(users),
                             std::to_string(roles)});
    }
    return result;
  }
  if (const auto* c = std::get_if<ShowPoliciesCommand>(&command)) {
    auto snap = snapshot();
    CommandResult result;
    auto role_names = [&](const std::vector<std::string>& ids) {
      std::vector<std::string> names;
      for (const auto& id : ids) {
        const auto* role = snap->FindRoleById(id);
        names.push_back(role ? role->name : id);
      }
      return Join(names);
    };
    if (c->masking) {
      result.column_names = {"policy",      "parameter", "parameter_type",
                             "return_type", "body",      "masks"};
      for (const auto& m : snap->masking_policies()) {
        std::vector<std::string> bound;
        for (const auto& b : snap->mask_bindings()) {
          if (b.policy_id == m.id) {
            bound.push_back(b.table.ToString() + "." + b.column +
                            (b.exempt_role_ids.empty()
                                 ? ""
                                 : " (exempt: " + role_names(b.exempt_role_ids) + ")"));
          }
        }
        result.rows.push_back(
            {m.name, m.parameter, m.parameter_type, m.return_type, m.body, Join(bound, "; ")});
      }
      return result;
    }
    std::optional<ObjectPath> only;
    if (c->table) {
      ARROW_ASSIGN_OR_RAISE(auto table, resolver.ResolveObject(*c->table));
      only = table.path;
    }
    result.column_names = {"policy", "table", "kind", "roles", "using"};
    for (const auto& p : snap->row_policies()) {
      if (only && ObjectKey(p.table) != ObjectKey(*only)) continue;
      result.rows.push_back({p.name, p.table.ToString(),
                             p.permissive ? "PERMISSIVE" : "RESTRICTIVE",
                             p.role_ids.empty() ? "(all)" : role_names(p.role_ids), p.predicate});
    }
    return result;
  }

  // ---- ALTER TABLE | VIEW ... OWNER TO -------------------------------------
  if (const auto* c = std::get_if<AlterOwnerCommand>(&command)) {
    ARROW_ASSIGN_OR_RAISE(auto obj, resolver.ResolveObject(c->target));
    if ((obj.type == ObjectType::kView) != c->is_view) {
      return AccessControlError(
          obj.path.ToString() + " is a " + (obj.type == ObjectType::kView ? "view" : "table") +
          "; use ALTER " + (obj.type == ObjectType::kView ? "VIEW" : "TABLE"));
    }
    ARROW_RETURN_NOT_OK(mutate([&](StoreTransaction& txn, const Snapshot& snap) -> arrow::Status {
      const OwnerRecord* current = snap.FindOwner(obj.path);
      OwnerRecord record;
      record.object = obj.path;
      record.type = obj.type;
      record.definition_hash = obj.definition_hash;
      const bool to_administrator =
          !options_.administrator_name.empty() && c->new_owner.kind != GranteeKind::kRole &&
          IdentifiersEqual(c->new_owner.name, options_.administrator_name);
      ResolvedGrantee owner{PrincipalKind::kUser, "", ""};
      if (to_administrator) {
        record.owner_kind = OwnerKind::kSystem;
      } else {
        ARROW_ASSIGN_OR_RAISE(owner, ResolveGrantee(snap, c->new_owner));
        record.owner_kind =
            owner.kind == PrincipalKind::kUser ? OwnerKind::kUser : OwnerKind::kRole;
        record.owner_id = owner.id;
        const std::string owner_tenant = snap.TenantOf(owner.kind, owner.id);
        if (!owner_tenant.empty() && snap.TenantOfCatalog(obj.path.catalog) != owner_tenant) {
          return AccessControlError(owner.name + " belongs to " + TenantName(snap, owner_tenant) +
                                    "; it can own objects only in that tenant's catalogs");
        }
      }
      if (!actor.superuser) {
        // An owner may hand the object to itself or to a role it holds; giving
        // it to anyone else could lend them, or take from them, the object's rights.
        const bool owns = current && snap.IsOwner(actor, *current);
        const bool to_self_or_role =
            !to_administrator &&
            ((owner.kind == PrincipalKind::kUser && owner.id == actor.user_id) ||
             (owner.kind == PrincipalKind::kRole && snap.RolesOf(actor).count(owner.id)));
        if (!owns || !to_self_or_role) {
          return AccessControlError("Hardcharger blocked ALTER OWNER: user " + actor.user_name +
                                    " may not change the "
                                    "owner of " +
                                    obj.path.ToString() +
                                    " (only its owner, to itself or a role it "
                                    "holds, or an administrator)");
        }
      }
      // Grants the previous owner made as owner stay in force: they become the
      // administrator's, rather than falling with the ownership.
      if (current && current->owner_kind != OwnerKind::kSystem) {
        for (const auto& g : snap.grants()) {
          if (g.granted_by.empty() || g.scope != GrantScope::kObject ||
              !SamePath(g.target, obj.path)) {
            continue;
          }
          const UserRecord* by = snap.FindUserById(g.granted_by);
          if (by && snap.IsOwner(Principal::User(by->id, by->name), *current)) {
            ObjectGrantRecord kept = g;
            kept.granted_by.clear();
            ARROW_RETURN_NOT_OK(txn.UpdateGrant(kept));
          }
        }
      }
      message = obj.path.ToString() + " is now owned by " +
                (to_administrator ? std::string("the administrator") : owner.name);
      return txn.UpsertOwner(record);
    }));
    return with_cascade_note(message);
  }

  return AccessControlError("unsupported statement");
}

}  // namespace hardcharger::authz
