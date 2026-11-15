#include "authz/audit_fields.hpp"

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

  Fields operator()(const CreateUserCommand& c) const { return OneUser(c.name); }
  Fields operator()(const DropUserCommand& c) const { return OneUser(c.name); }
  Fields operator()(const AlterUserCommand& c) const {
    Fields fields = OneUser(c.name);
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
  Fields operator()(const CreateRoleCommand& c) const { return OneRole(c.name); }
  Fields operator()(const DropRoleCommand& c) const { return OneRole(c.name); }
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

}  // namespace

std::vector<std::pair<std::string, std::string>> AuditFieldsOf(const RbacCommand& command, const Snapshot& before,
                                                               const Snapshot& after) {
  return std::visit(Describe{before, after}, command);
}

}  // namespace hardcharger::authz
