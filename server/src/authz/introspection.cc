#include "authz/introspection.hpp"

#include <algorithm>
#include <map>
#include <set>
#include <variant>
#include <vector>

#include "authz/model.hpp"
#include "authz/types.hpp"
#include "chargewire/errors.hpp"

namespace hardcharger::authz {

namespace {

using chargewire::Error;
using chargewire::ErrorCode;

// ---- A small columnar builder ----

enum class Kind { kText, kBool, kTextList, kTextMap };

struct Column {
  const char* name;
  Kind kind;
};

using TextMap = std::map<std::string, std::string>;
using Cell = std::variant<std::monostate, std::string, bool, std::vector<std::string>, TextMap>;

std::shared_ptr<arrow::DataType> TypeOf(Kind kind) {
  switch (kind) {
    case Kind::kText:
      return arrow::utf8();
    case Kind::kBool:
      return arrow::boolean();
    case Kind::kTextList:
      return arrow::list(arrow::utf8());
    case Kind::kTextMap:
      return arrow::map(arrow::utf8(), arrow::utf8());
  }
  return arrow::utf8();
}

void Check(const arrow::Status& status) {
  if (!status.ok()) throw Error(ErrorCode::kExecutionError, status.message());
}

class Rows {
 public:
  explicit Rows(std::vector<Column> columns) : columns_(std::move(columns)) {}

  void Add(std::vector<Cell> row) { rows_.push_back(std::move(row)); }

  std::shared_ptr<arrow::RecordBatch> Finish() const {
    arrow::FieldVector fields;
    std::vector<std::shared_ptr<arrow::Array>> arrays;
    for (size_t c = 0; c < columns_.size(); c++) {
      fields.push_back(arrow::field(columns_[c].name, TypeOf(columns_[c].kind)));
      arrays.push_back(Build(c));
    }
    return arrow::RecordBatch::Make(arrow::schema(fields), static_cast<int64_t>(rows_.size()), arrays);
  }

 private:
  std::shared_ptr<arrow::Array> Build(size_t c) const {
    std::shared_ptr<arrow::Array> out;
    switch (columns_[c].kind) {
      case Kind::kText: {
        arrow::StringBuilder b;
        for (const auto& row : rows_) {
          if (const auto* v = std::get_if<std::string>(&row[c])) {
            Check(b.Append(*v));
          } else {
            Check(b.AppendNull());
          }
        }
        Check(b.Finish(&out));
        break;
      }
      case Kind::kBool: {
        arrow::BooleanBuilder b;
        for (const auto& row : rows_) {
          if (const auto* v = std::get_if<bool>(&row[c])) {
            Check(b.Append(*v));
          } else {
            Check(b.AppendNull());
          }
        }
        Check(b.Finish(&out));
        break;
      }
      case Kind::kTextList: {
        auto items = std::make_shared<arrow::StringBuilder>();
        arrow::ListBuilder b(arrow::default_memory_pool(), items);
        for (const auto& row : rows_) {
          if (const auto* v = std::get_if<std::vector<std::string>>(&row[c])) {
            Check(b.Append());
            for (const auto& item : *v) Check(items->Append(item));
          } else {
            Check(b.AppendNull());
          }
        }
        Check(b.Finish(&out));
        break;
      }
      case Kind::kTextMap: {
        auto keys = std::make_shared<arrow::StringBuilder>();
        auto items = std::make_shared<arrow::StringBuilder>();
        arrow::MapBuilder b(arrow::default_memory_pool(), keys, items, TypeOf(Kind::kTextMap));
        for (const auto& row : rows_) {
          if (const auto* v = std::get_if<TextMap>(&row[c])) {
            Check(b.Append());
            for (const auto& [k, value] : *v) {
              Check(keys->Append(k));
              Check(items->Append(value));
            }
          } else {
            Check(b.AppendNull());
          }
        }
        Check(b.Finish(&out));
        break;
      }
    }
    return out;
  }

  std::vector<Column> columns_;
  std::vector<std::vector<Cell>> rows_;
};

// ---- The tables' columns ----

const std::vector<Column>& ColumnsOf(AccessTable table) {
  static const std::vector<Column> kGrants = {
      {"grantee", Kind::kText},           {"grantee_type", Kind::kText}, {"privilege", Kind::kText},
      {"object_type", Kind::kText},       {"catalog_name", Kind::kText}, {"schema_name", Kind::kText},
      {"object_name", Kind::kText},       {"column_name", Kind::kText},  {"grantor", Kind::kText},
      {"with_grant_option", Kind::kBool}, {"tenant", Kind::kText}};
  static const std::vector<Column> kRoleMembers = {
      {"role", Kind::kText},          {"member", Kind::kText},  {"member_type", Kind::kText},
      {"admin_option", Kind::kBool},  {"grantor", Kind::kText}, {"issuer", Kind::kText},
      {"tenant", Kind::kText}};
  static const std::vector<Column> kRoles = {
      {"name", Kind::kText}, {"builtin", Kind::kBool}, {"tenant", Kind::kText}, {"is_active", Kind::kBool}};
  static const std::vector<Column> kUsers = {
      {"name", Kind::kText},           {"enabled", Kind::kBool},       {"has_password", Kind::kBool},
      {"tenant", Kind::kText},         {"attributes", Kind::kTextMap}, {"is_current", Kind::kBool}};
  static const std::vector<Column> kPolicies = {
      {"name", Kind::kText},           {"kind", Kind::kText},           {"catalog_name", Kind::kText},
      {"schema_name", Kind::kText},    {"table_name", Kind::kText},     {"column_name", Kind::kText},
      {"permissive", Kind::kBool},     {"roles", Kind::kTextList},      {"exempt_roles", Kind::kTextList},
      {"expression", Kind::kText},     {"applies_to_current_user", Kind::kBool}};
  static const std::vector<Column> kEffective = {
      {"user_name", Kind::kText},    {"privilege", Kind::kText},   {"object_type", Kind::kText},
      {"catalog_name", Kind::kText}, {"schema_name", Kind::kText}, {"object_name", Kind::kText},
      {"column_name", Kind::kText},  {"via", Kind::kText},         {"with_grant_option", Kind::kBool}};
  switch (table) {
    case AccessTable::kGrants:
      return kGrants;
    case AccessTable::kRoleMembers:
      return kRoleMembers;
    case AccessTable::kRoles:
      return kRoles;
    case AccessTable::kUsers:
      return kUsers;
    case AccessTable::kPolicies:
      return kPolicies;
    case AccessTable::kEffectivePrivileges:
      return kEffective;
  }
  return kGrants;
}

// ---- Who sees what ----

enum class ViewerKind { kAdministrator, kRestrictedAdministrator, kUser };

ViewerKind KindOf(const Principal& viewer) {
  if (viewer.superuser) return ViewerKind::kAdministrator;
  if (viewer.catalog_rules) return ViewerKind::kRestrictedAdministrator;
  return ViewerKind::kUser;
}

class Visibility {
 public:
  Visibility(const Snapshot& snapshot, const Principal& viewer)
      : snapshot_(snapshot), viewer_(viewer), kind_(KindOf(viewer)) {
    if (kind_ == ViewerKind::kUser) {
      held_ = snapshot.HeldRoleIds(viewer);
      active_ = snapshot.RolesOf(viewer);
      // A tenant admin (it holds the tenant's tenant_admin role now, as when it manages
      // the tenant's users and roles) sees everything of its own tenant.
      const std::string tenant = snapshot.TenantOfPrincipal(viewer);
      const RoleRecord* admin = tenant.empty() ? nullptr : snapshot.TenantRole(tenant, kTenantAdminSuffix);
      if (admin && active_.count(admin->id)) tenant_admin_of_ = tenant;
    }
  }

  // The tenant this store user administers ("" for none).
  const std::string& tenant_admin_of() const { return tenant_admin_of_; }
  // A tenant admin: is this user or role one of its tenant's?
  bool InTenant(PrincipalKind kind, const std::string& id) const {
    return !tenant_admin_of_.empty() && snapshot_.TenantOf(kind, id) == tenant_admin_of_;
  }
  // A store user: may it see this role (held, or its tenant's when it is the tenant admin)?
  bool SeesRole(const std::string& role_id) const { return Holds(role_id) || InTenant(PrincipalKind::kRole, role_id); }

  ViewerKind kind() const { return kind_; }
  const std::set<std::string>& held() const { return held_; }
  bool Holds(const std::string& role_id) const { return held_.count(role_id) > 0; }
  bool Active(const std::string& role_id) const { return active_.count(role_id) > 0; }

  // A restricted administrator: may it see objects of this catalog?
  bool CatalogReached(const std::string& catalog) const {
    for (const auto& hidden : viewer_.hidden_catalogs) {
      if (IdentifiersEqual(hidden, catalog)) return false;
    }
    return CatalogRuleLevel(*viewer_.catalog_rules, catalog) != CatalogAccessLevel::kNone;
  }

  bool GrantVisible(const ObjectGrantRecord& grant) const {
    switch (kind_) {
      case ViewerKind::kAdministrator:
        return true;
      case ViewerKind::kRestrictedAdministrator:
        return CatalogReached(grant.target.catalog);
      case ViewerKind::kUser:
        return SeesRole(grant.role_id);
    }
    return false;
  }

  bool OwnerVisible(const OwnerRecord& owner) const {
    if (owner.owner_kind == OwnerKind::kSystem) return false;
    switch (kind_) {
      case ViewerKind::kAdministrator:
        return true;
      case ViewerKind::kRestrictedAdministrator:
        return CatalogReached(owner.object.catalog);
      case ViewerKind::kUser:
        if (owner.owner_kind == OwnerKind::kUser) {
          return owner.owner_id == viewer_.user_id || InTenant(PrincipalKind::kUser, owner.owner_id);
        }
        return SeesRole(owner.owner_id);
    }
    return false;
  }

  bool TableVisible(const ObjectPath& table) const {
    switch (kind_) {
      case ViewerKind::kAdministrator:
        return true;
      case ViewerKind::kRestrictedAdministrator:
        return CatalogReached(table.catalog);
      case ViewerKind::kUser:
        return snapshot_.ObjectListed(viewer_, table) ||
               (!tenant_admin_of_.empty() && snapshot_.TenantOfCatalog(table.catalog) == tenant_admin_of_);
    }
    return false;
  }

 private:
  const Snapshot& snapshot_;
  const Principal& viewer_;
  ViewerKind kind_;
  std::set<std::string> held_;    // every role the user holds
  std::set<std::string> active_;  // ... and those its checks use now (SET ROLE)
  std::string tenant_admin_of_;
};

// ---- Names ----

constexpr const char* kAdministratorGrantor = "administrator";

std::string SchemaText(const std::vector<std::string>& path) {
  if (path.size() == 1) return path[0];
  std::string out;
  for (const auto& part : path) out += (out.empty() ? "" : ".") + QuoteIdentifierIfNeeded(part);
  return out;
}

Cell Text(const std::string& text) { return text.empty() ? Cell{} : Cell{text}; }

class Names {
 public:
  explicit Names(const Snapshot& snapshot) : snapshot_(snapshot) {}

  std::string Role(const std::string& id) const {
    const auto* role = snapshot_.FindRoleById(id);
    return role ? role->name : id;
  }
  std::string User(const std::string& id) const {
    const auto* user = snapshot_.FindUserById(id);
    return user ? user->name : id;
  }
  // Who granted it: a user's name, or the administrator.
  std::string Grantor(const std::string& user_id) const {
    return user_id.empty() ? kAdministratorGrantor : User(user_id);
  }
  Cell Tenant(PrincipalKind kind, const std::string& id) const {
    const std::string tenant = snapshot_.TenantOf(kind, id);
    if (tenant.empty()) return {};
    const auto* record = snapshot_.FindTenantById(tenant);
    return record ? Cell{record->name} : Cell{tenant};
  }
  std::vector<std::string> RoleList(const std::vector<std::string>& ids, const Visibility& visibility) const {
    std::vector<std::string> out;
    for (const auto& id : ids) {
      // A store user is shown only the roles it holds.
      if (visibility.kind() == ViewerKind::kUser && !visibility.SeesRole(id)) continue;
      out.push_back(Role(id));
    }
    return out;
  }
  // TABLE or VIEW, as the object's ownership record says (TABLE when there is none).
  std::string ObjectType(const ObjectPath& object) const {
    const auto* owner = snapshot_.FindOwner(object);
    return owner && owner->type == ObjectType::kView ? "VIEW" : "TABLE";
  }

 private:
  const Snapshot& snapshot_;
};

std::string ScopeType(const Names& names, const ObjectGrantRecord& grant) {
  switch (grant.scope) {
    case GrantScope::kCatalog:
      return "CATALOG";
    case GrantScope::kSchema:
      return "SCHEMA";
    case GrantScope::kObject:
      return names.ObjectType(grant.target);
  }
  return "TABLE";
}

std::string OwnedType(ObjectType type) {
  return type == ObjectType::kView ? "VIEW" : type == ObjectType::kSchema ? "SCHEMA" : "TABLE";
}

// The catalog, schema and object cells of a target (empty parts are NULL).
std::vector<Cell> TargetCells(const ObjectPath& path) {
  return {Text(path.catalog), path.schema_path.empty() ? Cell{} : Cell{SchemaText(path.schema_path)},
          Text(path.object_name)};
}

// ---- hardcharger_grants() ----

std::shared_ptr<arrow::RecordBatch> Grants(const Snapshot& snapshot, const Visibility& visibility) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kGrants));
  for (const auto& g : snapshot.grants()) {
    if (!visibility.GrantVisible(g)) continue;
    const auto target = TargetCells(g.target);
    const std::vector<std::string> columns = g.columns.empty() ? std::vector<std::string>{""} : g.columns;
    for (const auto& column : columns) {
      rows.Add({names.Role(g.role_id), std::string("ROLE"), PrivilegeName(g.privilege), ScopeType(names, g),
                target[0], target[1], target[2], Text(column), names.Grantor(g.granted_by), g.grant_option,
                names.Tenant(PrincipalKind::kRole, g.role_id)});
    }
  }
  // System privileges reach no catalog: a restricted administrator sees none.
  if (visibility.kind() != ViewerKind::kRestrictedAdministrator) {
    for (const auto& r : snapshot.system_privileges()) {
      if (visibility.kind() == ViewerKind::kUser && !visibility.SeesRole(r.role_id)) continue;
      // A location grant names its location where an object grant names its object.
      const Cell location = r.location.empty() ? Cell{} : Text(r.location);
      rows.Add({names.Role(r.role_id), std::string("ROLE"), SystemPrivilegeName(r.privilege),
                std::string(r.location.empty() ? "SYSTEM" : "LOCATION"), Cell{}, Cell{}, location, Cell{},
                names.Grantor(r.granted_by), r.admin_option, names.Tenant(PrincipalKind::kRole, r.role_id)});
    }
  }
  // An owner holds every privilege on what it owns, and may grant them.
  for (const auto& o : snapshot.owners()) {
    if (!visibility.OwnerVisible(o)) continue;
    const auto target = TargetCells(o.object);
    const bool user = o.owner_kind == OwnerKind::kUser;
    rows.Add({user ? names.User(o.owner_id) : names.Role(o.owner_id), std::string(user ? "USER" : "ROLE"),
              std::string("OWNERSHIP"), OwnedType(o.type), target[0], target[1], target[2], Cell{}, Cell{}, true,
              names.Tenant(user ? PrincipalKind::kUser : PrincipalKind::kRole, o.owner_id)});
  }
  return rows.Finish();
}

// ---- hardcharger_role_members() ----

std::shared_ptr<arrow::RecordBatch> RoleMembers(const Snapshot& snapshot, const Principal& viewer,
                                                const Visibility& visibility) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kRoleMembers));
  if (visibility.kind() == ViewerKind::kRestrictedAdministrator) return rows.Finish();
  const bool admin = visibility.kind() == ViewerKind::kAdministrator;
  for (const auto& m : snapshot.memberships()) {
    const bool user = m.member_kind == PrincipalKind::kUser;
    // A store user: the memberships that make it hold its roles (its own, and its roles').
    // A tenant admin: every membership in its tenant's roles.
    if (!admin && !visibility.InTenant(PrincipalKind::kRole, m.role_id) &&
        !(user ? m.member_id == viewer.user_id : visibility.Holds(m.member_id))) {
      continue;
    }
    rows.Add({names.Role(m.role_id), user ? names.User(m.member_id) : names.Role(m.member_id),
              std::string(user ? "USER" : "ROLE"), m.admin_option, names.Grantor(m.granted_by), Cell{},
              names.Tenant(PrincipalKind::kRole, m.role_id)});
  }
  // CREATE GROUP MAPPING: a sign-in token's group holds the role. A store user sees the
  // mappings of the groups its own token lists.
  const std::string tenant = admin ? "" : snapshot.TenantOf(PrincipalKind::kUser, viewer.user_id);
  for (const auto& g : snapshot.group_mappings()) {
    if (!admin && !visibility.InTenant(PrincipalKind::kRole, g.role_id)) {
      if (!g.issuer.empty() && g.issuer != viewer.idp_issuer) continue;
      if (std::find(viewer.idp_groups.begin(), viewer.idp_groups.end(), g.group) == viewer.idp_groups.end()) continue;
      if (!snapshot.FindRoleById(g.role_id) || snapshot.TenantOf(PrincipalKind::kRole, g.role_id) != tenant) continue;
    }
    rows.Add({names.Role(g.role_id), g.group, std::string("GROUP"), false, Cell{}, Text(g.issuer),
              names.Tenant(PrincipalKind::kRole, g.role_id)});
  }
  return rows.Finish();
}

// ---- hardcharger_roles() ----

std::shared_ptr<arrow::RecordBatch> Roles(const Snapshot& snapshot, const Visibility& visibility) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kRoles));
  std::set<std::string> named;  // a restricted administrator: the roles its visible grants name
  if (visibility.kind() == ViewerKind::kRestrictedAdministrator) {
    for (const auto& g : snapshot.grants()) {
      if (visibility.GrantVisible(g)) named.insert(g.role_id);
    }
    for (const auto& o : snapshot.owners()) {
      if (o.owner_kind == OwnerKind::kRole && visibility.OwnerVisible(o)) named.insert(o.owner_id);
    }
  }
  for (const auto& r : snapshot.roles()) {
    bool active = false;
    switch (visibility.kind()) {
      case ViewerKind::kAdministrator:
        active = true;  // allowed everything
        break;
      case ViewerKind::kRestrictedAdministrator:
        if (!named.count(r.id)) continue;
        break;
      case ViewerKind::kUser:
        if (!visibility.SeesRole(r.id)) continue;
        active = visibility.Active(r.id);
        break;
    }
    rows.Add({r.name, r.builtin, names.Tenant(PrincipalKind::kRole, r.id), active});
  }
  return rows.Finish();
}

// ---- hardcharger_users() ----

std::shared_ptr<arrow::RecordBatch> Users(const Snapshot& snapshot, const Principal& viewer,
                                          const Visibility& visibility) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kUsers));
  if (visibility.kind() == ViewerKind::kRestrictedAdministrator) return rows.Finish();
  for (const auto& u : snapshot.users()) {
    const bool current = !viewer.superuser && u.id == viewer.user_id;
    if (visibility.kind() == ViewerKind::kUser && !current && !visibility.InTenant(PrincipalKind::kUser, u.id)) continue;
    // Never the password hash: only whether there is one.
    rows.Add({u.name, u.enabled, u.password_hash.has_value() && !u.password_hash->empty(),
              names.Tenant(PrincipalKind::kUser, u.id), TextMap(u.attributes.begin(), u.attributes.end()), current});
  }
  return rows.Finish();
}

// ---- hardcharger_policies() ----

std::shared_ptr<arrow::RecordBatch> Policies(const Snapshot& snapshot, const Principal& viewer,
                                             const Visibility& visibility) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kPolicies));
  const bool unchecked = viewer.superuser;  // policies don't apply to the administrator
  for (const auto& p : snapshot.row_policies()) {
    if (!visibility.TableVisible(p.table)) continue;
    bool applies = false;
    if (!unchecked) {
      for (const auto* a : snapshot.ApplicableRowPolicies(viewer, p.table)) applies = applies || a->id == p.id;
    }
    const auto target = TargetCells(p.table);
    rows.Add({p.name, std::string("ROW"), target[0], target[1], target[2], Cell{}, p.permissive,
              p.role_ids.empty() ? Cell{} : Cell{names.RoleList(p.role_ids, visibility)}, Cell{}, p.predicate,
              applies});
  }
  std::set<std::string> bound;
  for (const auto& b : snapshot.mask_bindings()) {
    bound.insert(b.policy_id);
    if (!visibility.TableVisible(b.table)) continue;
    const auto* policy = snapshot.FindMaskingPolicyById(b.policy_id);
    if (policy == nullptr) continue;
    bool applies = false;
    if (!unchecked) {
      for (const auto& [column, mask] : snapshot.ApplicableMasks(viewer, b.table)) {
        applies = applies || IdentifiersEqual(column, b.column);
      }
    }
    const auto target = TargetCells(b.table);
    rows.Add({policy->name, std::string("MASK"), target[0], target[1], target[2], b.column, Cell{}, Cell{},
              names.RoleList(b.exempt_role_ids, visibility),
              "(" + policy->parameter + " " + policy->parameter_type + ") RETURNS " + policy->return_type + " -> " +
                  policy->body,
              applies});
  }
  // A masking policy bound to no column is on no object: only the administrator sees it.
  if (visibility.kind() == ViewerKind::kAdministrator) {
    for (const auto& policy : snapshot.masking_policies()) {
      if (bound.count(policy.id)) continue;
      rows.Add({policy.name, std::string("MASK"), Cell{}, Cell{}, Cell{}, Cell{}, Cell{}, Cell{}, Cell{},
                "(" + policy.parameter + " " + policy.parameter_type + ") RETURNS " + policy.return_type + " -> " +
                    policy.body,
                false});
    }
  }
  return rows.Finish();
}

// ---- hardcharger_effective_privileges() ----

// The privileges a stored grant of `privilege` gives. GRANT never stores ALL (it stores each
// privilege ALL stands for); a record of ALL itself counts as the table-level ones, as the
// plan check reads it.
std::vector<Privilege> Expand(Privilege privilege) {
  if (privilege == Privilege::kAll) return TableLevelPrivileges();
  return {privilege};
}

// Every object privilege but ALL, for a catalog_access "write" rule.
const std::vector<Privilege>& WritePrivileges() {
  static const std::vector<Privilege> kAll = {
      Privilege::kSelect,     Privilege::kInsert,     Privilege::kUpdate,       Privilege::kDelete,
      Privilege::kCreateTable, Privilege::kCreateView, Privilege::kCreateSchema, Privilege::kDropTable,
      Privilege::kDropView,   Privilege::kDropSchema, Privilege::kAlterTable,   Privilege::kAlterView};
  return kAll;
}

std::shared_ptr<arrow::RecordBatch> EffectivePrivileges(const Snapshot& snapshot, const Principal& viewer,
                                                        const std::optional<std::string>& user_name) {
  Names names(snapshot);
  Rows rows(ColumnsOf(AccessTable::kEffectivePrivileges));
  const ViewerKind kind = KindOf(viewer);
  const bool asked_for_self = !user_name || (kind != ViewerKind::kAdministrator && !viewer.user_id.empty() &&
                                             IdentifiersEqual(*user_name, viewer.user_name));
  // A tenant admin may ask for a user of its own tenant.
  const UserRecord* tenant_mate = nullptr;
  if (!asked_for_self && kind == ViewerKind::kUser) {
    const Visibility visibility(snapshot, viewer);
    const auto* user = snapshot.FindUserByName(*user_name);
    if (user && visibility.InTenant(PrincipalKind::kUser, user->id)) tenant_mate = user;
  }
  if (!asked_for_self && !tenant_mate) {
    // The same refusal whether or not the user exists: it tells nothing about other users.
    if (kind == ViewerKind::kRestrictedAdministrator) {
      throw Error(ErrorCode::kPermissionDenied,
                  "Hardcharger blocked hardcharger_effective_privileges() for another user: that needs an "
                  "administrator without catalog_access rules",
                  "42501");
    }
    if (kind == ViewerKind::kUser) {
      throw Error(ErrorCode::kPermissionDenied,
                  "Hardcharger blocked hardcharger_effective_privileges() for another user. Users see their own "
                  "privileges, tenant admins their tenant's users', administrators everyone's",
                  "42501");
    }
  }
  if (asked_for_self && kind == ViewerKind::kAdministrator) {
    rows.Add({viewer.user_name, std::string("ALL"), std::string("SERVER"), Cell{}, Cell{}, Cell{}, Cell{},
              std::string("SUPERUSER"), true});
    return rows.Finish();
  }
  if (asked_for_self && kind == ViewerKind::kRestrictedAdministrator) {
    // Its token's rules, in order: the first whose pattern matches a catalog decides.
    for (const auto& rule : *viewer.catalog_rules) {
      std::vector<Privilege> privileges;
      if (rule.level == CatalogAccessLevel::kRead) privileges = {Privilege::kSelect};
      if (rule.level == CatalogAccessLevel::kWrite) privileges = WritePrivileges();
      if (privileges.empty()) {
        rows.Add({viewer.user_name, std::string("NONE"), std::string("CATALOG"), rule.pattern, Cell{}, Cell{}, Cell{},
                  std::string("CATALOG_ACCESS"), false});
      }
      for (const auto p : privileges) {
        rows.Add({viewer.user_name, PrivilegeName(p), std::string("CATALOG"), rule.pattern, Cell{}, Cell{}, Cell{},
                  std::string("CATALOG_ACCESS"), false});
      }
    }
    for (const auto p : AllSystemPrivileges()) {
      if (!snapshot.HasSystemPrivilege(viewer, p)) continue;
      rows.Add({viewer.user_name, SystemPrivilegeName(p), std::string("SYSTEM"), Cell{}, Cell{}, Cell{}, Cell{},
                std::string("ADMINISTRATOR"), false});
    }
    return rows.Finish();
  }

  // A store user: the viewer itself (its SET ROLE, groups and scoped token apply), or, for
  // the administrator, the named user with every role it was granted.
  Principal who;
  if (asked_for_self) {
    who = viewer;
  } else if (tenant_mate) {
    who = Principal::User(tenant_mate->id, tenant_mate->name);
  } else {
    const auto* user = snapshot.FindUserByName(*user_name);
    if (user == nullptr) throw Error(ErrorCode::kSqlError, "Hardcharger access control: user " + *user_name +
                                                               " does not exist");
    who = Principal::User(user->id, user->name);
  }
  const auto roles = snapshot.RolesOf(who);
  auto allowed = [&](Privilege privilege, const std::string& catalog) {
    if (!snapshot.CatalogVisible(who, catalog)) return false;
    if (!who.scope) return true;
    if (!who.scope->privileges.count(privilege)) return false;
    if (who.scope->catalogs.empty() || IdentifiersEqual(catalog, "temp")) return true;
    return std::any_of(who.scope->catalogs.begin(), who.scope->catalogs.end(),
                       [&](const std::string& c) { return IdentifiersEqual(c, catalog); });
  };
  for (const auto& g : snapshot.grants()) {
    if (!roles.count(g.role_id)) continue;
    const auto target = TargetCells(g.target);
    const std::string type = ScopeType(names, g);
    const std::vector<std::string> columns = g.columns.empty() ? std::vector<std::string>{""} : g.columns;
    for (const auto p : Expand(g.privilege)) {
      if (!allowed(p, g.target.catalog)) continue;
      for (const auto& column : columns) {
        rows.Add({who.user_name, PrivilegeName(p), type, target[0], target[1], target[2], Text(column),
                  names.Role(g.role_id), g.grant_option});
      }
    }
  }
  for (const auto& o : snapshot.owners()) {
    if (o.owner_kind == OwnerKind::kSystem || !snapshot.IsOwner(who, o)) continue;
    const auto target = TargetCells(o.object);
    std::vector<std::string> privileges;
    if (o.type != ObjectType::kSchema) {
      for (const auto p : TableLevelPrivileges()) {
        if (allowed(p, o.object.catalog)) privileges.push_back(PrivilegeName(p));
      }
    }
    if (!who.scope && snapshot.CatalogVisible(who, o.object.catalog)) privileges.push_back("OWNERSHIP");
    for (const auto& p : privileges) {
      rows.Add({who.user_name, p, OwnedType(o.type), target[0], target[1], target[2], Cell{}, std::string("OWNER"),
                true});
    }
  }
  for (const auto p : AllSystemPrivileges()) {
    // The roles that give it, everywhere or on a location.
    const bool system = snapshot.HasSystemPrivilege(who, p);
    if (!system && (!StorageOf(p) || who.catalog_rules || who.scope)) continue;
    for (const auto& r : snapshot.system_privileges()) {
      if (r.privilege != p || !roles.count(r.role_id) || (system && !r.location.empty())) continue;
      const Cell location = r.location.empty() ? Cell{} : Text(r.location);
      rows.Add({who.user_name, SystemPrivilegeName(p), std::string(r.location.empty() ? "SYSTEM" : "LOCATION"),
                Cell{}, Cell{}, location, Cell{}, names.Role(r.role_id), r.admin_option});
    }
  }
  return rows.Finish();
}

}  // namespace

const char* AccessTableName(AccessTable table) {
  switch (table) {
    case AccessTable::kGrants:
      return "hardcharger_grants";
    case AccessTable::kRoleMembers:
      return "hardcharger_role_members";
    case AccessTable::kRoles:
      return "hardcharger_roles";
    case AccessTable::kUsers:
      return "hardcharger_users";
    case AccessTable::kPolicies:
      return "hardcharger_policies";
    case AccessTable::kEffectivePrivileges:
      return "hardcharger_effective_privileges";
  }
  return "hardcharger_grants";
}

std::shared_ptr<arrow::RecordBatch> EmptyAccessTable(AccessTable table) { return Rows(ColumnsOf(table)).Finish(); }

std::shared_ptr<arrow::RecordBatch> AccessTableRows(AccessTable table, const Snapshot& snapshot,
                                                    const Principal& viewer,
                                                    const std::optional<std::string>& user_name) {
  const Visibility visibility(snapshot, viewer);
  switch (table) {
    case AccessTable::kGrants:
      return Grants(snapshot, visibility);
    case AccessTable::kRoleMembers:
      return RoleMembers(snapshot, viewer, visibility);
    case AccessTable::kRoles:
      return Roles(snapshot, visibility);
    case AccessTable::kUsers:
      return Users(snapshot, viewer, visibility);
    case AccessTable::kPolicies:
      return Policies(snapshot, viewer, visibility);
    case AccessTable::kEffectivePrivileges:
      return EffectivePrivileges(snapshot, viewer, user_name);
  }
  return EmptyAccessTable(table);
}

}  // namespace hardcharger::authz
