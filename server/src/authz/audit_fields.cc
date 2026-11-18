#include "authz/audit_fields.hpp"

#include <algorithm>
#include <variant>

namespace hardcharger::authz {

namespace {

using Fields = std::vector<std::pair<std::string, std::string>>;

// The users and roles a statement is about, as their audit fields.
class Targets {
 public:
  Targets(const Snapshot& before, const Snapshot& after) : before_(before), after_(after) {}

  void User(const std::string& name) {
    const UserRecord* user = before_.FindUserByName(name);
    if (!user) user = after_.FindUserByName(name);
    Add("user " + (user ? user->name : name), user ? user->id : "?");
  }
  void Role(const std::string& name) {
    const RoleRecord* role = before_.FindRoleByName(name);
    if (!role) role = after_.FindRoleByName(name);
    Add("role " + (role ? role->name : name), role ? role->id : "?");
  }
  // A grantee as GRANT resolves it: a user of that name, else a role.
  void Principal(const Grantee& grantee) {
    const bool user = grantee.kind != GranteeKind::kRole &&
                      (grantee.kind == GranteeKind::kUser || before_.FindUserByName(grantee.name) ||
                       (!before_.FindRoleByName(grantee.name) && after_.FindUserByName(grantee.name)));
    if (user) {
      User(grantee.name);
    } else {
      Role(grantee.name);
    }
  }
  void AddTo(Fields& fields, const char* names = "target", const char* ids = "target_ids") const {
    if (names_.empty()) return;
    fields.emplace_back(names, names_);
    fields.emplace_back(ids, ids_);
  }

 private:
  void Add(const std::string& name, const std::string& id) {
    names_ += (names_.empty() ? "" : ", ") + name;
    ids_ += (ids_.empty() ? "" : ", ") + id;
  }
  const Snapshot& before_;
  const Snapshot& after_;
  std::string names_, ids_;
};

std::string Joined(const std::vector<std::string>& items) {
  std::string out;
  for (const auto& item : items) out += (out.empty() ? "" : ", ") + item;
  return out;
}

struct Describe {
  const Snapshot& before;
  const Snapshot& after;

  // The five account statements name the account privilege they need (whoever runs them).
  Fields operator()(const CreateUserCommand& c) const { return Needs(OneUser(c.name), SystemPrivilege::kCreateUser); }
  Fields operator()(const DropUserCommand& c) const { return Needs(OneUser(c.name), SystemPrivilege::kDropUser); }
  Fields operator()(const AlterUserCommand& c) const {
    Fields fields = Needs(OneUser(c.name), SystemPrivilege::kAlterUser);
    std::string change;
    switch (c.action) {
      case AlterUserCommand::Action::kSetPassword:
        change = "password";
        break;
      case AlterUserCommand::Action::kClearPassword:
        change = "password removed";
        break;
      case AlterUserCommand::Action::kEnable:
        change = "enabled";
        break;
      case AlterUserCommand::Action::kDisable:
        change = "disabled";
        break;
      case AlterUserCommand::Action::kSetAttribute:
        change = "attribute " + c.attribute;  // never its value
        break;
      case AlterUserCommand::Action::kUnsetAttribute:
        change = "attribute " + c.attribute + " removed";
        break;
    }
    fields.emplace_back("change", change);
    return fields;
  }
  Fields operator()(const CreateRoleCommand& c) const { return Needs(OneRole(c.name), SystemPrivilege::kCreateRole); }
  Fields operator()(const DropRoleCommand& c) const { return Needs(OneRole(c.name), SystemPrivilege::kDropRole); }
  Fields operator()(const RoleMembershipCommand& c) const {
    Fields fields;
    Targets grantees(before, after), roles(before, after);
    for (const auto& g : c.grantees) grantees.Principal(g);
    for (const auto& r : c.roles) roles.Role(r);
    grantees.AddTo(fields);
    roles.AddTo(fields, "membership", "membership_ids");
    if (c.with_admin_option) fields.emplace_back("option", c.grant ? "WITH ADMIN OPTION" : "ADMIN OPTION FOR");
    return fields;
  }
  Fields operator()(const ObjectPrivilegeCommand& c) const {
    Fields fields = Grantees(c.grantees);
    std::vector<std::string> privileges;
    for (const auto& item : c.privileges) {
      privileges.push_back(PrivilegeName(item.privilege) +
                           (item.columns.empty() ? "" : " (" + Joined(item.columns) + ")"));
    }
    fields.emplace_back("privilege", Joined(privileges));
    const char* kind = c.table_function                      ? "TABLE FUNCTION "
                       : c.sequence                          ? "SEQUENCE "
                       : c.scope == GrantScope::kSchema      ? "SCHEMA "
                       : c.scope == GrantScope::kCatalog     ? "CATALOG "
                                                             : "TABLE ";
    fields.emplace_back("object", kind + c.target.ToString());
    if (c.with_grant_option) fields.emplace_back("option", c.grant ? "WITH GRANT OPTION" : "GRANT OPTION FOR");
    return fields;
  }
  Fields operator()(const SystemPrivilegeCommand& c) const {
    Fields fields = Grantees(c.grantees);
    std::vector<std::string> privileges;
    for (const auto p : c.privileges) privileges.push_back(SystemPrivilegeName(p));
    fields.emplace_back("privilege", Joined(privileges));
    if (c.location) fields.emplace_back("object", "LOCATION '" + *c.location + "'");
    if (c.with_admin_option) fields.emplace_back("option", c.grant ? "WITH ADMIN OPTION" : "ADMIN OPTION FOR");
    return fields;
  }
  Fields operator()(const SystemAdminCommand& c) const {
    Fields fields = Grantees(c.grantees);
    fields.emplace_back("privilege", kSystemAdminName);
    return fields;
  }
  Fields operator()(const AlterOwnerCommand& c) const {
    Fields fields = Grantees({c.new_owner});
    fields.emplace_back("object", std::string(c.is_view ? "VIEW " : "TABLE ") + c.target.ToString());
    fields.emplace_back("change", "owner");
    return fields;
  }
  Fields operator()(const ShowGrantsCommand& c) const {
    return c.principal ? Grantees({*c.principal}) : Fields{};
  }
  Fields operator()(const CreateRowPolicyCommand& c) const {
    Fields fields;
    Targets roles(before, after);
    for (const auto& r : c.roles) roles.Role(r);
    roles.AddTo(fields);
    fields.emplace_back("policy", c.name);
    fields.emplace_back("object", "TABLE " + c.table.ToString());
    return fields;
  }
  Fields operator()(const DropRowPolicyCommand& c) const {
    return {{"policy", c.name}, {"object", "TABLE " + c.table.ToString()}};
  }
  Fields operator()(const CreateMaskingPolicyCommand& c) const { return {{"policy", c.name}}; }
  Fields operator()(const DropMaskingPolicyCommand& c) const { return {{"policy", c.name}}; }
  Fields operator()(const SetColumnMaskCommand& c) const {
    Fields fields;
    Targets roles(before, after);
    for (const auto& r : c.exempt_roles) roles.Role(r);
    roles.AddTo(fields);
    if (c.policy) fields.emplace_back("policy", *c.policy);
    fields.emplace_back("object", "COLUMN " + c.table.ToString() + "." + QuoteIdentifierIfNeeded(c.column));
    return fields;
  }
  Fields operator()(const ShowPoliciesCommand& c) const {
    return c.table ? Fields{{"object", "TABLE " + c.table->ToString()}} : Fields{};
  }
  Fields operator()(const TenantCommand& c) const { return {{"tenant", c.name}}; }
  Fields operator()(const CatalogTenantCommand& c) const {
    Fields fields = {{"catalog", c.catalog}};
    if (c.tenant) fields.emplace_back("tenant", *c.tenant);
    return fields;
  }
  Fields operator()(const TenantDefaultCatalogCommand& c) const {
    Fields fields = {{"tenant", c.tenant}};
    if (!c.catalog.empty()) fields.emplace_back("catalog", c.catalog);
    return fields;
  }
  Fields operator()(const ShowTenantsCommand&) const { return {}; }
  Fields operator()(const SetRoleCommand& c) const {
    Fields fields;
    if (c.system_admin) {
      fields.emplace_back("privilege", kSystemAdminName);
    } else if (c.role) {
      Targets role(before, after);
      role.Role(*c.role);
      role.AddTo(fields, "membership", "membership_ids");
    }
    return fields;
  }
  Fields operator()(const GroupMappingCommand& c) const {
    Fields fields;
    if (c.kind == GroupMappingCommand::Kind::kShow) return fields;
    if (c.role) {
      Targets role(before, after);
      role.Role(*c.role);
      role.AddTo(fields);
    }
    fields.emplace_back("group", c.group);
    if (!c.issuer.empty()) fields.emplace_back("issuer", c.issuer);
    return fields;
  }
  Fields operator()(const ScopedTokenCommand& c) const {
    Fields fields;
    if (c.kind == ScopedTokenCommand::Kind::kShow) return fields;
    if (c.for_user) fields = OneUser(*c.for_user);
    fields.emplace_back("token", c.name);  // its name: the token itself is in no record
    if (c.kind == ScopedTokenCommand::Kind::kCreate) {
      std::vector<std::string> privileges;
      for (const auto p : c.privileges) privileges.push_back(PrivilegeName(p));
      fields.emplace_back("privilege", Joined(privileges));
      if (!c.catalogs.empty()) fields.emplace_back("catalog", Joined(c.catalogs));
    }
    return fields;
  }

  static Fields Needs(Fields fields, SystemPrivilege privilege) {
    fields.emplace_back("privilege", SystemPrivilegeName(privilege));
    return fields;
  }
  Fields OneUser(const std::string& name) const {
    Fields fields;
    Targets target(before, after);
    target.User(name);
    target.AddTo(fields);
    return fields;
  }
  Fields OneRole(const std::string& name) const {
    Fields fields;
    Targets target(before, after);
    target.Role(name);
    target.AddTo(fields);
    return fields;
  }
  Fields Grantees(const std::vector<Grantee>& grantees) const {
    Fields fields;
    Targets targets(before, after);
    for (const auto& g : grantees) targets.Principal(g);
    targets.AddTo(fields);
    return fields;
  }
};

std::string RoleName(const Snapshot& snap, const std::string& id) {
  const RoleRecord* role = snap.FindRoleById(id);
  return role ? role->name : id;
}

std::string UserState(const Snapshot& snap, const std::string& name, const UserRecord* other) {
  const UserRecord* user = snap.FindUserByName(name);
  if (!user) return "none";
  std::string out = user->enabled ? "enabled" : "disabled";
  if (user->password_hash) {
    out += "; password login";
    // Compared, never shown.
    if (other && other->password_hash && *other->password_hash != *user->password_hash) out += " (new password)";
  } else {
    out += "; no password login";
  }
  std::vector<std::string> roles;
  for (const auto& m : snap.memberships()) {
    if (m.member_kind == PrincipalKind::kUser && m.member_id == user->id) {
      roles.push_back(RoleName(snap, m.role_id) + (m.admin_option ? " WITH ADMIN OPTION" : ""));
    }
  }
  std::sort(roles.begin(), roles.end());
  out += "; roles: " + (roles.empty() ? std::string("none") : Joined(roles));
  std::vector<std::string> attributes;
  for (const auto& [key, value] : user->attributes) attributes.push_back(key);  // never the value
  if (!attributes.empty()) out += "; attributes: " + Joined(attributes);
  return out;
}

std::string RoleState(const Snapshot& snap, const std::string& name) {
  const RoleRecord* role = snap.FindRoleByName(name);
  if (!role) return "none";
  size_t members = 0;
  std::vector<std::string> inherits;
  for (const auto& m : snap.memberships()) {
    if (m.role_id == role->id) ++members;
    if (m.member_kind == PrincipalKind::kRole && m.member_id == role->id) inherits.push_back(RoleName(snap, m.role_id));
  }
  std::sort(inherits.begin(), inherits.end());
  std::vector<std::string> privileges;
  size_t grants = 0;
  for (const auto& g : snap.grants()) grants += g.role_id == role->id ? 1 : 0;
  if (grants) privileges.push_back(std::to_string(grants) + " object grant" + (grants == 1 ? "" : "s"));
  for (const auto& r : snap.system_privileges()) {
    if (r.role_id == role->id) {
      privileges.push_back(SystemPrivilegeName(r.privilege) + (r.location.empty() ? "" : " on '" + r.location + "'"));
    }
  }
  for (const auto& r : snap.system_admins()) {
    if (r.kind == PrincipalKind::kRole && r.principal_id == role->id) privileges.push_back(kSystemAdminName);
  }
  return "members: " + std::to_string(members) + "; inherits: " + (inherits.empty() ? "none" : Joined(inherits)) +
         "; privileges: " + (privileges.empty() ? "none" : Joined(privileges));
}

// "user bob in role clerks: yes, WITH ADMIN OPTION", for each grantee and role.
std::string MembershipState(const Snapshot& snap, const RoleMembershipCommand& c) {
  std::vector<std::string> parts;
  for (const auto& g : c.grantees) {
    for (const auto& role_name : c.roles) {
      const RoleRecord* role = snap.FindRoleByName(role_name);
      const UserRecord* user = g.kind != GranteeKind::kRole ? snap.FindUserByName(g.name) : nullptr;
      const RoleRecord* member_role = user ? nullptr : snap.FindRoleByName(g.name);
      std::string state = "no";
      for (const auto& m : snap.memberships()) {
        if (!role || m.role_id != role->id) continue;
        if ((user && m.member_kind == PrincipalKind::kUser && m.member_id == user->id) ||
            (member_role && m.member_kind == PrincipalKind::kRole && m.member_id == member_role->id)) {
          state = m.admin_option ? "yes, WITH ADMIN OPTION" : "yes";
        }
      }
      parts.push_back((member_role ? "role " : "user ") + g.name + " in role " + role_name + ": " + state);
    }
  }
  std::string out;
  for (const auto& part : parts) out += (out.empty() ? "" : "; ") + part;
  return out;
}

// "role helpdesk: CREATE USER: yes", for each grantee and account privilege.
std::string AccountPrivilegeState(const Snapshot& snap, const SystemPrivilegeCommand& c) {
  std::vector<std::string> parts;
  for (const auto& g : c.grantees) {
    const RoleRecord* role = snap.FindRoleByName(g.name);
    for (const auto p : c.privileges) {
      if (!IsAccountPrivilege(p)) continue;
      const bool held = role && std::any_of(snap.system_privileges().begin(), snap.system_privileges().end(),
                                            [&](const SystemPrivilegeRecord& r) {
                                              return r.role_id == role->id && r.privilege == p;
                                            });
      parts.push_back("role " + g.name + ": " + SystemPrivilegeName(p) + ": " + (held ? "yes" : "no"));
    }
  }
  std::string out;
  for (const auto& part : parts) out += (out.empty() ? "" : "; ") + part;
  return out;
}

}  // namespace

std::optional<std::pair<std::string, std::string>> AccountChangeValues(const RbacCommand& command,
                                                                       const Snapshot& before,
                                                                       const Snapshot& after) {
  auto user = [&](const std::string& name) -> std::pair<std::string, std::string> {
    return {UserState(before, name, nullptr), UserState(after, name, before.FindUserByName(name))};
  };
  auto role = [&](const std::string& name) -> std::pair<std::string, std::string> {
    return {RoleState(before, name), RoleState(after, name)};
  };
  if (const auto* c = std::get_if<CreateUserCommand>(&command)) return user(c->name);
  if (const auto* c = std::get_if<AlterUserCommand>(&command)) return user(c->name);
  if (const auto* c = std::get_if<DropUserCommand>(&command)) return user(c->name);
  if (const auto* c = std::get_if<CreateRoleCommand>(&command)) return role(c->name);
  if (const auto* c = std::get_if<DropRoleCommand>(&command)) return role(c->name);
  if (const auto* c = std::get_if<RoleMembershipCommand>(&command)) {
    return std::make_pair(MembershipState(before, *c), MembershipState(after, *c));
  }
  if (const auto* c = std::get_if<SystemPrivilegeCommand>(&command)) {
    if (std::none_of(c->privileges.begin(), c->privileges.end(), IsAccountPrivilege)) return std::nullopt;
    return std::make_pair(AccountPrivilegeState(before, *c), AccountPrivilegeState(after, *c));
  }
  return std::nullopt;
}

std::vector<std::pair<std::string, std::string>> AuditFieldsOf(const RbacCommand& command, const Snapshot& before,
                                                               const Snapshot& after) {
  return std::visit(Describe{before, after}, command);
}

}  // namespace hardcharger::authz
