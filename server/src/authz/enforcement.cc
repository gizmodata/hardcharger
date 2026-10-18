#include "authz/enforcement.hpp"

#include <algorithm>
#include <chrono>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry_retriever.hpp>
#include <duckdb/catalog/catalog_search_path.hpp>
#include <duckdb/catalog/catalog_entry/schema_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/execution/expression_executor.hpp>
#include <duckdb/function/scalar/sequence_utils.hpp>
#include <duckdb/execution/operator/helper/physical_set.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/main/prepared_statement_data.hpp>
#include <duckdb/optimizer/optimizer_extension.hpp>
#include <duckdb/parser/parsed_data/alter_info.hpp>
#include <duckdb/parser/parsed_data/create_info.hpp>
#include <duckdb/parser/parsed_data/create_table_info.hpp>
#include <duckdb/parser/parsed_data/create_view_info.hpp>
#include <duckdb/parser/parsed_data/drop_info.hpp>
#include <duckdb/parser/qualified_name.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/planner/binder.hpp>
#include <duckdb/planner/expression/bound_function_expression.hpp>
#include <duckdb/planner/expression_iterator.hpp>
#include <duckdb/planner/logical_operator_visitor.hpp>
#include <duckdb/planner/operator/logical_alter.hpp>
#include <duckdb/planner/operator/logical_create.hpp>
#include <duckdb/planner/operator/logical_create_index.hpp>
#include <duckdb/planner/operator/logical_create_table.hpp>
#include <duckdb/planner/operator/logical_delete.hpp>
#include <duckdb/planner/operator/logical_drop.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <duckdb/planner/operator/logical_insert.hpp>
#include <duckdb/planner/operator/logical_merge_into.hpp>
#include <duckdb/planner/operator/logical_reset.hpp>
#include <duckdb/planner/operator/logical_set.hpp>
#include <duckdb/planner/operator/logical_update.hpp>
#include <duckdb/planner/parsed_data/bound_create_table_info.hpp>
#include <duckdb/planner/planner_extension.hpp>
#include <set>
#include <unordered_set>

#include "authz/duckdb_internal.hpp"
#include "authz/policies.hpp"
#include "authz/resolver.hpp"
#include "authz/views.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;
using OpType = dd::LogicalOperatorType;

// Table functions anyone may call under access control: they read no table
// and no file. Metadata functions list names, not data.
const std::unordered_set<std::string>& AllowedTableFunctions() {
  static const std::unordered_set<std::string> kNames = {
      "range", "generate_series", "unnest", "repeat", "repeat_row", "json_each", "json_tree",
      "test_all_types", "sql_auto_complete", "duckdb_tables", "duckdb_columns", "duckdb_views",
      "duckdb_schemas", "duckdb_databases", "duckdb_indexes", "duckdb_constraints",
      "duckdb_functions", "duckdb_types", "duckdb_keywords", "duckdb_settings", "duckdb_sequences",
      "duckdb_dependencies", "duckdb_extensions", "duckdb_optimizers", "pragma_table_info",
      "pragma_show", "pragma_version", "pragma_platform", "pragma_database_size",
      "pragma_collations", "pragma_user_agent",
      // Bulk ingest reads the caller's own
      // upload through it; it reads nothing
      // else (the SDK refuses any other call).
      "warp_upload",
      // A prepare describes its result by binding it (never run); it converts
      // the prepared statement's own columns, nothing else (the SDK refuses
      // any other call).
      "warp_result_schema",
      // The server's own, which read only what the session may see (its own sessions,
      // the settings rows that aren't admin-only).
      "hardcharger_settings", "hardcharger_sessions", "hardcharger_metrics"};
  return kNames;
}

// Session settings an access-controlled user may not change: turning the
// optimizer (and with it the plan check) off, or switching the SQL grammar.
const std::unordered_set<std::string>& BlockedSettings() {
  static const std::unordered_set<std::string> kNames = {
      "enable_optimizer", "disabled_optimizers", "current_dialect", "active_grammar_extensions",
      "enable_legacy_parser"};
  return kNames;
}

std::string Lower(std::string s) {
  for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

bool IsTempCatalog(const std::string& catalog) { return IdentifiersEqual(catalog, TEMP_CATALOG); }

std::string TableFunctionName(const dd::LogicalGet& get) {
  return compat::Str(get.function.GetName());
}

// The parts of a (bound) qualified name: catalog, schema path..., name (any
// part the statement left out is missing).
std::vector<std::string> NameParts(const dd::QualifiedName& name) {
  const auto path = compat::Strs(name.Path());
  std::vector<std::string> parts(path.begin(), path.end());
  if (!parts.empty() && parts.back().empty()) parts.pop_back();  // CREATE SCHEMA has no name
  return parts;
}

template <class Info>
std::vector<std::string> PartsOf(const Info& info) {
  return NameParts(info.GetQualifiedName());
}

// The catalog and schema path a CREATE / DROP SCHEMA names: a lone name is a
// schema of the session's default catalog, as DuckDB resolves it.
ObjectPath SchemaTarget(dd::ClientContext& context, const std::vector<std::string>& parts) {
  ObjectPath target;
  if (parts.size() == 1) {
    target.catalog = compat::Str(dd::DatabaseManager::GetDefaultDatabase(context));
    target.schema_path = parts;
  } else if (!parts.empty()) {
    target.catalog = parts[0];
    target.schema_path.assign(parts.begin() + 1, parts.end());
  }
  return target;
}

dd::optional_ptr<dd::CatalogEntry> LookUpSchema(dd::ClientContext& context,
                                                const ObjectPath& schema) {
  if (schema.catalog.empty() || schema.schema_path.empty()) return nullptr;
  try {
    dd::vector<dd::Identifier> path;
    for (const auto& p : schema.schema_path) path.emplace_back(p);
    auto entry = dd::Catalog::GetSchema(context, dd::Identifier(schema.catalog), path,
                                        dd::OnEntryNotFound::RETURN_NULL);
    return entry ? dd::optional_ptr<dd::CatalogEntry>(entry.get()) : nullptr;
  } catch (...) {
    return nullptr;
  }
}

// Look an entry up inside the current transaction (the hook runs while the
// statement is being planned, so no new transaction or lock may be taken).
dd::optional_ptr<dd::CatalogEntry> LookUp(dd::ClientContext& context, dd::CatalogType type,
                                          const std::vector<std::string>& parts) {
  try {
    dd::vector<dd::Identifier> path;
    for (const auto& p : parts) path.emplace_back(p);
    return dd::Catalog::GetEntry(context,
                                 dd::EntryLookupInfo(type, dd::QualifiedName::FromPath(path)),
                                 dd::OnEntryNotFound::RETURN_NULL);
  } catch (...) {
    return nullptr;
  }
}

ObjectType ObjectTypeOf(const dd::CatalogEntry& entry) {
  if (entry.type == dd::CatalogType::VIEW_ENTRY) return ObjectType::kView;
  if (entry.type == dd::CatalogType::SCHEMA_ENTRY) return ObjectType::kSchema;
  return ObjectType::kTable;
}

std::string TypeWord(ObjectType type) {
  switch (type) {
    case ObjectType::kView:
      return "view";
    case ObjectType::kSchema:
      return "schema";
    case ObjectType::kTable:
      return "table";
  }
  return "object";
}

// A CREATE VIEW's definition hash (see OwnerRecord::definition_hash).
std::string DefinitionHashOf(dd::CreateInfo& info) {
  auto& view = info.Cast<dd::CreateViewInfo>();
  return view.query ? ViewDefinitionHash(*view.query) : "";
}

// What a scoped token allows, for refusals: "only SELECT, INSERT only on catalog x".
std::string ScopeLimits(const TokenScope& scope) {
  std::vector<std::string> verbs;
  for (auto p : {Privilege::kSelect, Privilege::kInsert, Privilege::kUpdate, Privilege::kDelete}) {
    if (scope.privileges.count(p)) verbs.push_back(PrivilegeName(p));
  }
  std::string text = "allows only ";
  for (size_t i = 0; i < verbs.size(); ++i) text += (i ? ", " : "") + verbs[i];
  if (!scope.catalogs.empty()) {
    text += scope.catalogs.size() == 1 ? ", only on catalog " : ", only on catalogs ";
    for (size_t i = 0; i < scope.catalogs.size(); ++i) text += (i ? ", " : "") + scope.catalogs[i];
  }
  return text;
}

[[noreturn]] void Deny(const Principal& who, const std::string& what) {
  if (who.scope) {
    throw dd::PermissionException("Hardcharger blocked this statement: " + what +
                                  ". This session logged in with scoped token " + who.scope->name +
                                  " of user " + who.user_name + ", which " +
                                  ScopeLimits(*who.scope));
  }
  if (!who.active_role_name.empty()) {
    throw dd::PermissionException("Hardcharger blocked this statement: " + what +
                                  ". This session acts with role " + who.active_role_name +
                                  " only (SET ROLE); RESET ROLE brings back all of user " +
                                  who.user_name + "'s roles (see SHOW GRANTS)");
  }
  throw dd::PermissionException("Hardcharger blocked this statement: " + what +
                                ". Access control on this server lets user " + who.user_name +
                                " do only what their roles are granted (see SHOW GRANTS)");
}

bool WritesFiles(const std::string& setting) {
  return setting == "log_query_path" || setting == "profiling_output" ||
         setting == "profile_output";
}

// Does SET (or RESET) of `name` in `scope` change the setting for the whole server?
// Explicitly GLOBAL does; so does a bare SET of a setting DuckDB keeps only globally
// (memory_limit, threads, extension settings, ...), which DuckDB applies globally.
bool ChangesWholeServer(dd::ClientContext& context, const dd::Identifier& name,
                        dd::SetScope scope) {
  if (scope == dd::SetScope::GLOBAL) return true;
  if (scope != dd::SetScope::AUTOMATIC) return false;
  try {
    if (auto option = dd::DBConfig::GetOptionByName(name)) {
      return dd::PhysicalSet::GetSettingScope(*option, scope) == dd::SetScope::GLOBAL;
    }
    // An extension's setting (TimeZone, Calendar, ...): its declared default scope.
    dd::ExtensionOption extension_option;
    if (dd::DBConfig::GetConfig(context).TryGetExtensionOption(name, extension_option)) {
      return extension_option.ResolveScope(name, scope) == dd::SetScope::GLOBAL;
    }
  } catch (...) {
    return true;  // DuckDB cannot tell either: treat it as global (fail closed)
  }
  // No such setting yet: DuckDB would autoload an extension that has one. That is the
  // administrator's to allow.
  return true;
}

// Walks one statement's bound plan and checks every access it makes.
//
// What the statement names itself is checked against the caller. What a view
// reads is checked against the view's owner (the caller needs SELECT on the
// view only), view inside view, each against its own owner. A view without a
// valid owner record reads with the caller's rights, in addition to the
// rights of any view it sits in.
class PlanChecker {
 public:
  using ViewColumns = std::unordered_map<const ViewMarker*, std::vector<std::string>>;

  PlanChecker(dd::ClientContext& context, const Snapshot& snapshot, const Principal& principal,
              const ViewColumns& view_columns)
      : context_(context),
        snapshot_(snapshot),
        principal_(principal),
        view_columns_(view_columns),
        rights_{Rights{principal, ""}} {}

  void Visit(dd::LogicalOperator& op,
             dd::optional_ptr<dd::TableCatalogEntry> dml_target = nullptr) {
    CheckSettingReads(op);
    switch (op.type) {
      // ---- reads ----------------------------------------------------------
      case OpType::LOGICAL_GET:
        VisitGet(op.Cast<dd::LogicalGet>(), dml_target);
        break;

      // ---- writes ---------------------------------------------------------
      case OpType::LOGICAL_INSERT: {
        auto& insert = op.Cast<dd::LogicalInsert>();
        Require(insert.table, Privilege::kInsert, {});
        if (insert.return_chunk) RequireAllColumns(insert.table);
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_UPDATE: {
        auto& update = op.Cast<dd::LogicalUpdate>();
        Require(update.table, Privilege::kUpdate, {});
        if (update.return_chunk) RequireAllColumns(update.table);
        VisitChildren(op, &update.table);
        return;
      }
      case OpType::LOGICAL_DELETE: {
        auto& del = op.Cast<dd::LogicalDelete>();
        Require(del.table, Privilege::kDelete, {});
        if (del.return_chunk) RequireAllColumns(del.table);
        VisitChildren(op, &del.table);
        return;
      }
      case OpType::LOGICAL_MERGE_INTO: {
        auto& merge = op.Cast<dd::LogicalMergeInto>();
        // MERGE may insert, update and delete: require all three.
        Require(merge.table, Privilege::kInsert, {});
        Require(merge.table, Privilege::kUpdate, {});
        Require(merge.table, Privilege::kDelete, {});
        if (merge.return_chunk) RequireAllColumns(merge.table);
        VisitChildren(op, &merge.table);
        return;
      }

      // ---- DDL ------------------------------------------------------------
      case OpType::LOGICAL_CREATE_TABLE: {
        auto& create = op.Cast<dd::LogicalCreateTable>();
        CheckCreate(*create.info->base, ObjectType::kTable, Privilege::kCreateTable);
        VisitChildren(op, nullptr);  // CREATE TABLE AS reads its source
        return;
      }
      case OpType::LOGICAL_CREATE_VIEW: {
        auto& create = op.Cast<dd::LogicalCreate>();
        const size_t before = ownership_.size();
        if (!CheckCreate(*create.info, ObjectType::kView, Privilege::kCreateView)) return;
        if (ownership_.size() > before)
          ownership_.back().definition_hash = DefinitionHashOf(*create.info);
        CheckViewQuery(create);
        return;
      }
      case OpType::LOGICAL_CREATE_SCHEMA: {
        auto& create = op.Cast<dd::LogicalCreate>();
        CheckCreate(*create.info, ObjectType::kSchema, Privilege::kCreateSchema);
        return;
      }
      case OpType::LOGICAL_CREATE_INDEX: {
        auto& index = op.Cast<dd::LogicalCreateIndex>();
        if (!IsTempCatalog(compat::Str(index.table.ParentCatalog().GetName()))) {
          Require(index.table, Privilege::kAlterTable, {});
        }
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_CREATE_SEQUENCE: {
        auto& create = op.Cast<dd::LogicalCreate>();
        if (principal_.catalog_rules) return RequireRuleWrite(CatalogOf(*create.info, create.info->temporary));
        if (create.info->temporary) return;  // session-local
        Deny(principal_, "creating sequences needs an administrator");
      }
      // Not even temporary ones: a session's macro or type is found before the
      // catalog's, also inside a view, and would change what the view computes.
      // (catalog_access principals read no view with another's rights.)
      case OpType::LOGICAL_CREATE_MACRO:
      case OpType::LOGICAL_CREATE_TYPE: {
        // Never for a non-administrator, temporary ones included: a macro's or type's
        // body runs unchecked when it is used (FROM m(), a column of the type), so it
        // would be a way around the plan check. PIVOT without an IN list is the one
        // exception: it creates its own temporary enum, which reads nothing.
        auto& create = op.Cast<dd::LogicalCreate>();
        if (create.info->temporary && op.type == OpType::LOGICAL_CREATE_TYPE &&
            PartsOf(*create.info).back().rfind("__pivot_enum_", 0) == 0) {
          return;
        }
        Deny(principal_, op.type == OpType::LOGICAL_CREATE_MACRO ? "creating macros needs an administrator"
                                                                 : "creating types needs an administrator");
      }
      case OpType::LOGICAL_DROP:
        CheckDrop(op);
        return;
      case OpType::LOGICAL_ALTER:
        CheckAlter(op);
        return;

      // ---- session statements --------------------------------------------
      case OpType::LOGICAL_SET: {
        const auto& set = op.Cast<dd::LogicalSet>();
        // A variable's value is computed by a query, and getvariable() hands it to any
        // view that asks, which a view's owner's rights wouldn't account for: not under
        // RBAC. A catalog_access principal reads no view with another's rights, so it may
        // set one; its query is checked below with the rest of the plan.
        if (set.scope == dd::SetScope::VARIABLE && !principal_.catalog_rules) {
          Deny(principal_, "SET VARIABLE is not available under access control");
        }
        if (BlockedSettings().count(Lower(compat::Str(set.name)))) {
          Deny(principal_, "changing " + compat::Str(set.name) + " is not allowed");
        }
        if (ChangesWholeServer(context_, set.name, set.scope)) {
          RequireSystem(SystemPrivilege::kManageConfig, "SET GLOBAL");
        }
        // Session settings that make DuckDB write files wherever they point (every
        // statement's text, or its profile): writing files, as COPY TO is.
        if (WritesFiles(Lower(compat::Str(set.name)))) {
          RequireSystem(SystemPrivilege::kWriteFilesystem, "SET " + compat::Str(set.name));
        }
        // USE c / SET schema / SET search_path: no catalog the user cannot see.
        const std::string setting = Lower(compat::Str(set.name));
        if ((setting == "schema" || setting == "search_path") && !set.value.IsNull()) {
          RequireVisibleSearchPath(set.value.ToString());
        }
        // SET VARIABLE's value is a query (SET VARIABLE v = (SELECT ... FROM c.t)), planned
        // as this operator's child: what it reads is checked like any query's.
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_RESET: {
        const auto& reset = op.Cast<dd::LogicalReset>();
        if (reset.scope != dd::SetScope::VARIABLE &&
            BlockedSettings().count(Lower(compat::Str(reset.name)))) {
          Deny(principal_, "changing " + compat::Str(reset.name) + " is not allowed");
        }
        if (reset.scope != dd::SetScope::VARIABLE &&
            ChangesWholeServer(context_, reset.name, reset.scope)) {
          RequireSystem(SystemPrivilege::kManageConfig, "RESET GLOBAL");
        }
        return;
      }

      // ---- a view's plan, marked when it was bound -------------------------
      case OpType::LOGICAL_EXTENSION_OPERATOR:
        if (auto* marker = dynamic_cast<ViewMarker*>(&op)) {
          VisitView(*marker);
          return;
        }
        Deny(principal_, "extension operators are not available under access control");
      case OpType::LOGICAL_TRANSACTION:
        return;

      // ---- system privileges ---------------------------------------------
      case OpType::LOGICAL_ATTACH:
      case OpType::LOGICAL_DETACH:
        RequireSystem(SystemPrivilege::kManageCatalogs, "ATTACH and DETACH");
        return;
      case OpType::LOGICAL_LOAD:
        RequireSystem(SystemPrivilege::kManageExtensions, "INSTALL and LOAD");
        return;
      case OpType::LOGICAL_CREATE_SECRET:
        RequireSystem(SystemPrivilege::kManageSecrets, "CREATE SECRET");
        return;
      case OpType::LOGICAL_COPY_TO_FILE:
        RequireSystem(SystemPrivilege::kWriteFilesystem, "COPY ... TO");
        VisitChildren(op, nullptr);  // and SELECT on what it copies
        return;
      case OpType::LOGICAL_EXPORT:
        RequireSystem(SystemPrivilege::kExportDatabase, "EXPORT DATABASE");
        VisitChildren(op, nullptr);
        return;

      // ---- plain relational operators: check what they read --------------
      case OpType::LOGICAL_PROJECTION:
      case OpType::LOGICAL_FILTER:
      case OpType::LOGICAL_AGGREGATE_AND_GROUP_BY:
      case OpType::LOGICAL_WINDOW:
      case OpType::LOGICAL_UNNEST:
      case OpType::LOGICAL_LIMIT:
      case OpType::LOGICAL_ORDER_BY:
      case OpType::LOGICAL_TOP_N:
      case OpType::LOGICAL_DISTINCT:
      case OpType::LOGICAL_SAMPLE:
      case OpType::LOGICAL_PIVOT:
      case OpType::LOGICAL_CHUNK_GET:
      case OpType::LOGICAL_DELIM_GET:
      case OpType::LOGICAL_EXPRESSION_GET:
      case OpType::LOGICAL_DUMMY_SCAN:
      case OpType::LOGICAL_EMPTY_RESULT:
      case OpType::LOGICAL_CTE_REF:
      case OpType::LOGICAL_JOIN:
      case OpType::LOGICAL_DELIM_JOIN:
      case OpType::LOGICAL_COMPARISON_JOIN:
      case OpType::LOGICAL_ANY_JOIN:
      case OpType::LOGICAL_CROSS_PRODUCT:
      case OpType::LOGICAL_POSITIONAL_JOIN:
      case OpType::LOGICAL_ASOF_JOIN:
      case OpType::LOGICAL_DEPENDENT_JOIN:
      case OpType::LOGICAL_UNION:
      case OpType::LOGICAL_EXCEPT:
      case OpType::LOGICAL_INTERSECT:
      case OpType::LOGICAL_RECURSIVE_CTE:
      case OpType::LOGICAL_MATERIALIZED_CTE:
      case OpType::LOGICAL_EXPLAIN:
      // A prepared statement's plan, and the re-planned statement an EXECUTE
      // carries after a rebind (without one, the check made when it was
      // prepared, at the same grants version, still stands).
      case OpType::LOGICAL_PREPARE:
      case OpType::LOGICAL_EXECUTE:
      case OpType::LOGICAL_SECURE_VIEW:
      case OpType::LOGICAL_TRIGGER:
        VisitChildren(op, dml_target);
        return;

      // ---- everything else is denied -------------------------------------
      default:
        Deny(principal_, Lower(dd::LogicalOperatorToString(op.type)) +
                             " is not available under access control");
    }
  }

  // current_setting('<name>') of a setting that holds a credential needs MANAGE
  // CONFIG. DuckDB reads the value when it binds the call (its argument may be any
  // constant expression), so the check reads the bound argument.
  void CheckSettingReads(dd::LogicalOperator& op) {
    if (OnlySuperusers()) return;
    dd::LogicalOperatorVisitor::EnumerateExpressions(op, [&](dd::unique_ptr<dd::Expression>* e) {
      if (e && *e) CheckSettingRead(**e);
    });
  }

  void CheckSettingRead(const dd::Expression& expr) {
    if (expr.GetExpressionClass() == dd::ExpressionClass::BOUND_FUNCTION) {
      CheckSequenceUse(expr.Cast<dd::BoundFunctionExpression>());
    }
    CheckCurrentSetting(expr);
  }

  // nextval / setval change a sequence and currval reads it: for a catalog_access
  // principal they need write (read) access to the sequence's catalog. DuckDB binds
  // the sequence when it binds the call (its name must be a constant).
  void CheckSequenceUse(const dd::BoundFunctionExpression& call) {
    if (!principal_.catalog_rules) return;
    const std::string name = Lower(compat::Str(call.Function().GetName()));
    if (name != "nextval" && name != "currval" && name != "setval") return;
    const auto& info = call.BindInfo();
    if (!info) return;  // a NULL sequence name: DuckDB returns NULL
    const auto& sequence = info->Cast<dd::NextvalBindData>().sequence;
    const std::string catalog = compat::Str(sequence.ParentCatalog().GetName());
    RequireVisible(catalog);
    const auto level = CatalogRuleLevel(*principal_.catalog_rules, catalog);
    const auto needed = name == "currval" ? CatalogAccessLevel::kRead : CatalogAccessLevel::kWrite;
    if (level == CatalogAccessLevel::kNone || (needed == CatalogAccessLevel::kWrite && level != needed)) {
      DenyByCatalogAccess(catalog, needed);
    }
  }

  void CheckCurrentSetting(const dd::Expression& expr) {
    if (expr.GetExpressionClass() == dd::ExpressionClass::BOUND_FUNCTION) {
      const auto& call = expr.Cast<dd::BoundFunctionExpression>();
      if (Lower(compat::Str(call.Function().GetName())) == "current_setting") {
        dd::Value name;
        const auto& arguments = call.GetChildren();
        const bool known =
            !arguments.empty() && arguments[0]->IsFoldable() &&
            dd::ExpressionExecutor::TryEvaluateScalar(context_, *arguments[0], name) &&
            !name.IsNull();
        // Fail closed: a setting that cannot be named might be any of them.
        const std::string setting = known ? name.ToString() : "(unknown)";
        if (!known || IsSensitiveSetting(setting)) {
          RequireSystem(SystemPrivilege::kManageConfig, "reading the setting " + setting);
        }
      }
    }
    dd::ExpressionIterator::EnumerateChildren(
        expr, [&](const dd::Expression& child) { CheckSettingRead(child); });
  }

  std::vector<PendingOwnership> TakeOwnership() { return std::move(ownership_); }
  // What the statement touches, for the policy service.
  boost::json::array TakeObjects() { return std::move(objects_); }
  std::vector<std::string> TakeAccesses() {
    return std::vector<std::string>(accesses_.begin(), accesses_.end());
  }

 private:
  void VisitChildren(dd::LogicalOperator& op, dd::optional_ptr<dd::TableCatalogEntry> dml_target) {
    for (auto& child : op.children) {
      if (child) Visit(*child, dml_target);
    }
  }

  void VisitGet(dd::LogicalGet& get, dd::optional_ptr<dd::TableCatalogEntry> dml_target) {
    auto table = get.GetTable();
    if (!table) {
      const std::string name = Lower(TableFunctionName(get));
      if (OnlySuperusers()) return;  // in a view an administrator owns
      if (ddb::IsFileReadingTableFunction(name)) {
        RequireSystem(SystemPrivilege::kReadFilesystem, "reading files and URLs");
        return;
      }
      if (name == "checkpoint" || name == "force_checkpoint") {
        RequireSystem(SystemPrivilege::kCheckpoint, "CHECKPOINT");
        return;
      }
      if (name == "duckdb_secrets") {
        RequireSystem(SystemPrivilege::kManageSecrets, "duckdb_secrets()");
        return;
      }
      // Only the ones that read no table and no file, the administrator with
      // catalog_access rules included: others (pragma_storage_info('c.t'), ...) read a
      // catalog around its rules.
      if (!AllowedTableFunctions().count(name)) {
        Deny(principal_, "the table function " + name + " is not available under access control");
      }
      if ((name == "pragma_table_info" || name == "pragma_show") && !get.parameters.empty()) {
        RequireVisibleTableArgument(get.parameters[0]);
      }
      return;
    }
    std::vector<std::string> columns;
    for (const auto& index : get.GetColumnIds()) {
      if (index.IsRowIdColumn() || index.IsVirtualColumn() || index.IsEmptyColumn()) continue;
      const auto& column = table->GetColumn(dd::LogicalIndex(index.GetPrimaryIndex()));
      columns.push_back(compat::Str(column.Name()));
    }
    // The row-locating scan under the statement's own UPDATE / DELETE target
    // needs no SELECT unless it reads columns (in WHERE or SET), as in Postgres.
    if (columns.empty() && dml_target && table.get() == dml_target.get()) return;
    Require(*table, Privilege::kSelect, columns);
  }

  ObjectPath PathOf(dd::CatalogEntry& entry) { return PathOfEntry(entry); }

  void Require(dd::CatalogEntry& entry, Privilege privilege, std::vector<std::string> columns) {
    RequireObject(PathOf(entry), ObjectTypeOf(entry), privilege, std::move(columns));
  }

  // A tenant's user sees only its tenant's catalogs: anything else is as if
  // it did not exist.
  // Any part of a search path entry ("c", "c.s", "s", quoted or not) that
  // names an attached catalog the user cannot see.
  void RequireVisibleSearchPath(const std::string& value) {
    std::string part;
    bool quoted = false;
    auto check = [&]() {
      if (part.empty()) return;
      auto db = dd::DatabaseManager::Get(context_).GetDatabase(context_, dd::Identifier(part));
      if (db) RequireVisible(compat::Str(db->GetName()));
      part.clear();
    };
    for (size_t i = 0; i < value.size(); ++i) {
      const char c = value[i];
      if (c == '"') {
        if (quoted && i + 1 < value.size() && value[i + 1] == '"') {
          part += '"';
          ++i;
        } else {
          quoted = !quoted;
        }
      } else if (!quoted && (c == '.' || c == ',')) {
        check();
      } else if (quoted || !std::isspace(static_cast<unsigned char>(c))) {
        part += c;
      }
    }
    check();
  }

  // The table a metadata function such as pragma_table_info('t') describes.
  void RequireVisibleTableArgument(const dd::Value& argument) {
    if (argument.IsNull()) return;
    std::vector<std::string> parts;
    for (const auto& part : dd::QualifiedName::ParseComponents(argument.ToString())) {
      parts.push_back(compat::Str(part));
    }
    if (parts.empty()) return;
    if (parts.size() >= 2) {
      // A leading part naming a catalog the user cannot see, whatever DuckDB
      // made of the rest.
      auto db = dd::DatabaseManager::Get(context_).GetDatabase(context_, dd::Identifier(parts[0]));
      if (db) RequireVisible(compat::Str(db->GetName()));
    }
    if (auto entry = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts)) {
      const ObjectPath path = PathOf(*entry);
      RequireVisible(path.catalog);
      // Describing a table lists its columns: only one the user may access (Warp SQL
      // §9.4), as its listings show it. Which columns, the plan rewrite narrows.
      if (!snapshot_.ObjectListed(principal_, path)) {
        if (principal_.catalog_rules) DenyByCatalogAccess(path.catalog, CatalogAccessLevel::kRead);
        Deny(principal_, "user " + principal_.user_name + " holds no privilege on " +
                             TypeWord(ObjectTypeOf(*entry)) + " " + path.ToString() +
                             ", so it cannot describe it");
      }
    }
  }

  void RequireVisible(const std::string& catalog) {
    if (!snapshot_.CatalogVisible(principal_, catalog)) {
      const bool hidden = std::any_of(principal_.hidden_catalogs.begin(), principal_.hidden_catalogs.end(),
                                      [&](const std::string& c) { return IdentifiersEqual(c, catalog); });
      if (principal_.catalog_rules && !hidden) DenyByCatalogAccess(catalog, CatalogAccessLevel::kRead);
      throw dd::CatalogException(HiddenObjectMessage(principal_.user_name));
    }
  }

  // For a catalog_access principal: the statement changes `catalog`, which its rules
  // must let it write. (A session-local object lives in temp, which the rules govern.)
  void RequireRuleWrite(const std::string& catalog) {
    if (CatalogRuleLevel(*principal_.catalog_rules, catalog) != CatalogAccessLevel::kWrite) {
      DenyByCatalogAccess(catalog, CatalogAccessLevel::kWrite);
    }
  }

  // The catalog a CREATE / DROP / ALTER names: temp for a temporary object, else the
  // one it names, else the session's default.
  template <class Info>
  std::string CatalogOf(const Info& info, bool temporary) {
    if (temporary) return TEMP_CATALOG;
    const auto parts = PartsOf(info);
    if (parts.size() >= 2) return parts[0];
    return compat::Str(dd::DatabaseManager::GetDefaultDatabase(context_));
  }

  [[noreturn]] void DenyByCatalogAccess(const std::string& catalog, CatalogAccessLevel needed) {
    std::string message = CatalogAccessDenied(catalog, needed, principal_.user_name);
    if (!creating_view_.empty()) message += ", which the query of view " + creating_view_ + " reads";
    throw dd::PermissionException(message);
  }

  // Every refusal goes through here (it hides the namespace's Deny inside the class), so
  // a refusal of a CREATE VIEW's query says that it is the view's query that reads it.
  [[noreturn]] void Deny(const Principal& who, const std::string& what) {
    if (creating_view_.empty()) ::hardcharger::authz::Deny(who, what);
    ::hardcharger::authz::Deny(who, "user " + who.user_name +
                                        " may create a view only over what it can SELECT itself, "
                                        "and the query of view " +
                                        creating_view_ + " reads more: " + what);
  }

  void RequireObject(const ObjectPath& path, ObjectType type, Privilege privilege,
                     std::vector<std::string> columns) {
    RequireVisible(path.catalog);
    if (IsTempCatalog(path.catalog)) {
      // A session's temporary table or view is found before the catalog's
      // objects, inside a view too: it could stand in for a table the view's
      // owner reads (a list of allowed rows, say). Only the caller's own
      // statement may read one.
      if (in_owned_view_) {
        Deny(principal_, "the view " + owned_view_ + " cannot read a temporary " + TypeWord(type) +
                             " of this session");
      }
      // catalog_access rules govern temp too: a read-only token
      // creates no temporary tables.
      if (!principal_.catalog_rules) return;
    }
    AccessRequest request{path, type, privilege, std::move(columns)};
    NoteAccess(PrivilegeName(privilege) + " on " + TypeWord(type) + " " + path.ToString(),
               request.columns);
    {
      std::string schema;
      for (const auto& part : path.schema_path) schema += (schema.empty() ? "" : ".") + part;
      boost::json::array columns;
      for (const auto& column : request.columns) columns.emplace_back(column);
      objects_.push_back(boost::json::object{{"catalog", path.catalog},
                                             {"schema", schema},
                                             {"name", path.object_name},
                                             {"type", TypeWord(type)},
                                             {"privilege", PrivilegeName(privilege)},
                                             {"columns", std::move(columns)},
                                             {"through_view", owned_view_}});
    }
    for (const auto& rights : rights_) {
      auto decision = snapshot_.Check(rights.principal, request);
      if (decision.allowed) continue;
      if (!rights.owner_of_view.empty()) {
        // Never name what the view reads: the caller may not know it.
        Deny(principal_, "the view " + rights.owner_of_view +
                             " cannot be read, because its owner lacks a privilege on an object "
                             "the view reads");
      }
      if (rights.principal.catalog_rules) {
        DenyByCatalogAccess(path.catalog, privilege == Privilege::kSelect ? CatalogAccessLevel::kRead
                                                                         : CatalogAccessLevel::kWrite);
      }
      std::string what = "user " + principal_.user_name + " has no " + PrivilegeName(privilege) +
                         " privilege on " + TypeWord(request.type) + " " + path.ToString();
      if (privilege == Privilege::kSelect && decision.reason.rfind("no SELECT on column", 0) == 0) {
        what =
            "user " + principal_.user_name + " has " + decision.reason + " of " + path.ToString();
      }
      if (!unowned_view_.empty()) {
        what += " (read through the view " + unowned_view_ +
                ", which has no owner, so it reads with your rights)";
      }
      Deny(principal_, what);
    }
  }

  // For the audit record; written before the check, which throws on a refusal.
  void NoteAccess(std::string what, const std::vector<std::string>& columns) {
    if (!columns.empty()) {
      what += " (";
      for (size_t i = 0; i < columns.size(); ++i) what += (i ? ", " : "") + columns[i];
      what += ")";
    }
    if (!owned_view_.empty()) what += " as the owner of view " + owned_view_;
    accesses_.insert(std::move(what));
  }

  // A system privilege, held by every principal whose rights apply here (the
  // caller, or the owner of the view this is read through).
  void RequireSystem(SystemPrivilege privilege, const std::string& what) {
    NoteAccess(SystemPrivilegeName(privilege), {});
    for (const auto& rights : rights_) {
      if (snapshot_.HasSystemPrivilege(rights.principal, privilege)) continue;
      if (!rights.owner_of_view.empty()) {
        Deny(principal_, "the view " + rights.owner_of_view +
                             " cannot be read, because its owner lacks a privilege on an object "
                             "the view reads");
      }
      if (rights.principal.rules_administrator) Deny(principal_, RulesAdministratorRefusal(what, privilege));
      Deny(principal_, what + " needs the " + SystemPrivilegeName(privilege) +
                           " privilege, which user " + principal_.user_name + " does not hold");
    }
  }

  bool OnlySuperusers() const {
    for (const auto& rights : rights_) {
      if (!rights.principal.superuser) return false;
    }
    return true;
  }

  // The caller needs SELECT on the view (on the columns the statement uses);
  // the view's body is then checked with its owner's rights.
  void VisitView(ViewMarker& view) {
    const std::string name = view.view.ToString();
    if (view.temporary) {
      if (in_owned_view_) {
        Deny(principal_,
             "the view " + owned_view_ + " cannot read a temporary view of this session");
      }
      VisitChildren(view, nullptr);  // the session's own view: the caller's rights
      return;
    }
    auto used = view_columns_.find(&view);
    RequireObject(view.view, ObjectType::kView, Privilege::kSelect,
                  used != view_columns_.end() ? used->second : view.column_names);

    const auto saved_rights = rights_;
    const auto saved_in_owned = in_owned_view_;
    const auto saved_owned = owned_view_;
    const auto saved_unowned = unowned_view_;
    const OwnerRecord* owner = snapshot_.FindOwner(view.view);
    if (owner && owner->type == ObjectType::kView && !owner->definition_hash.empty() &&
        owner->definition_hash == view.definition_hash) {
      rights_ = {Rights{OwnerPrincipal(*owner, name), name}};
      in_owned_view_ = true;
      owned_view_ = name;
      unowned_view_.clear();
    } else {
      // No owner on record, or the record is of an earlier view of this name:
      // the caller's own rights apply as well.
      bool has_caller = false;
      for (const auto& r : rights_) has_caller = has_caller || r.owner_of_view.empty();
      if (!has_caller) rights_.push_back(Rights{principal_, ""});
      unowned_view_ = name;
    }
    VisitChildren(view, nullptr);
    rights_ = saved_rights;
    in_owned_view_ = saved_in_owned;
    owned_view_ = saved_owned;
    unowned_view_ = saved_unowned;
  }

  Principal OwnerPrincipal(const OwnerRecord& owner, const std::string& view_name) {
    switch (owner.owner_kind) {
      case OwnerKind::kSystem:
        return Principal::Superuser("administrator");
      case OwnerKind::kUser: {
        const UserRecord* user = snapshot_.FindUserById(owner.owner_id);
        if (!user || !user->enabled) {
          Deny(principal_, "the view " + view_name +
                               " cannot be read, because its owner is disabled or was dropped");
        }
        return Principal::User(user->id, user->name);
      }
      case OwnerKind::kRole: {
        const RoleRecord* role = snapshot_.FindRoleById(owner.owner_id);
        if (!role) {
          Deny(principal_,
               "the view " + view_name + " cannot be read, because its owner was dropped");
        }
        return Principal::Role(role->id, role->name);
      }
    }
    Deny(principal_, "the view " + view_name + " has an owner record that is not understood");
  }

  void RequireAllColumns(dd::TableCatalogEntry& table) {
    std::vector<std::string> columns;
    for (const auto& name : table.GetColumns().GetColumnNames()) columns.push_back(name);
    Require(table, Privilege::kSelect, std::move(columns));
  }

  // CREATE needs the privilege on the container; CREATE OR REPLACE of an
  // existing object also needs the right to drop it; IF NOT EXISTS on an
  // existing object changes nothing and never takes its ownership.
  //
  // Returns false when the statement creates nothing (IF NOT EXISTS of an existing object).
  bool CheckCreate(dd::CreateInfo& info, ObjectType type, Privilege privilege) {
    auto parts = PartsOf(info);
    if (info.temporary || (!parts.empty() && IsTempCatalog(parts[0]))) {
      if (principal_.catalog_rules) RequireRuleWrite(TEMP_CATALOG);
      return true;
    }
    ObjectPath target;
    dd::optional_ptr<dd::CatalogEntry> existing;
    if (type == ObjectType::kSchema) {
      if (parts.empty()) Deny(principal_, "the new schema's name could not be determined");
      target = SchemaTarget(context_, parts);
      existing = LookUpSchema(context_, target);
    } else {
      if (parts.size() < 3) Deny(principal_, "the new object's schema could not be determined");
      target.catalog = parts[0];
      target.schema_path.assign(parts.begin() + 1, parts.end() - 1);
      target.object_name = parts.back();
      existing = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts);
    }
    if (existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT) return false;
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      CheckDropOf(*existing);
    }
    RequireVisible(target.catalog);
    AccessRequest request{target, type, privilege, {}};
    if (principal_.catalog_rules) RequireRuleWrite(target.catalog);
    if (!snapshot_.Check(principal_, request).allowed) {
      Deny(principal_, "user " + principal_.user_name + " has no " + PrivilegeName(privilege) +
                           " privilege on " +
                           (type == ObjectType::kSchema ? "catalog " + target.catalog
                                                        : "schema " + ContainerOf(target)));
    }
    ownership_.push_back({PendingOwnership::Kind::kCreate, target, type});
    return true;
  }

  // A view's creator may create it only over what it could SELECT itself: the view's
  // query is bound as DuckDB binds it when it creates the view (in the view's schema)
  // and checked as the creator's own SELECT of it would be (every table and view it
  // reads, the columns it reads of each, views read with their owners' rights, table
  // macros expanded). Without this, the creator could list the columns of the view (it
  // owns it), and so of tables it holds no privilege on. A view DuckDB would create
  // without binding it (WITH (defer_binding = true)) is bound here all the same: one whose
  // query names what doesn't exist yet fails as that SELECT would.
  void CheckViewQuery(dd::LogicalCreate& create) {
    auto& info = create.info->Cast<dd::CreateViewInfo>();
    if (!info.query) Deny(principal_, "the new view's query could not be read");
    // IF NOT EXISTS of an existing view (temporary ones included) creates nothing.
    if (info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT &&
        LookUp(context_, dd::CatalogType::TABLE_ENTRY, PartsOf(info))) {
      return;
    }
    if (!create.schema) Deny(principal_, "the new view's schema could not be determined");
    auto& schema = *create.schema;
    auto& catalog = schema.ParentCatalog();
    auto binder = dd::Binder::CreateBinder(context_);
    binder->SetCanContainNulls(true);
    // Binder::GetSearchPath(catalog, schema, true), as Binder::BindView sets it.
    dd::vector<dd::CatalogSearchEntry> search_path;
    search_path.emplace_back(catalog.GetName(), schema.name);
    search_path.emplace_back(catalog.GetName(), dd::Identifier(INVALID_SCHEMA), true);
    binder->EntryRetriever().SetSearchPath(std::move(search_path));
    auto copy = info.query->Copy();
    auto bound = binder->Bind(*copy);
    if (!bound.plan) Deny(principal_, "the new view's query could not be checked");

    ObjectPath view;
    const auto parts = PartsOf(info);
    if (info.temporary || parts.size() < 3) {
      view.catalog = compat::Str(catalog.GetName());
      view.schema_path = {compat::Str(schema.name)};
      view.object_name = compat::Str(info.GetViewName());
    } else {
      view.catalog = parts[0];
      view.schema_path.assign(parts.begin() + 1, parts.end() - 1);
      view.object_name = parts.back();
    }
    const auto view_columns = ViewColumnsUsed(*bound.plan);
    PlanChecker body(context_, snapshot_, principal_, view_columns);
    body.creating_view_ = view.ToString();
    body.Visit(*bound.plan);
    for (auto& access : body.accesses_) accesses_.insert(access);
    for (auto& object : body.objects_) objects_.push_back(std::move(object));
  }

  static std::string ContainerOf(const ObjectPath& object) {
    ObjectPath container = object;
    container.object_name.clear();
    return container.ToString();
  }

  // Dropping (or replacing) an object: its owner, or DROP TABLE / VIEW / SCHEMA.
  void CheckDropOf(dd::CatalogEntry& entry) {
    ObjectPath path = PathOf(entry);
    if (principal_.catalog_rules) {
      RequireVisible(path.catalog);
      RequireRuleWrite(path.catalog);
      // The object's owner record (if any) goes with it.
      if (!IsTempCatalog(path.catalog)) ownership_.push_back({PendingOwnership::Kind::kDrop, path, ObjectTypeOf(entry)});
      return;
    }
    if (IsTempCatalog(path.catalog)) return;
    RequireVisible(path.catalog);
    const ObjectType type = ObjectTypeOf(entry);
    const Privilege privilege = type == ObjectType::kView     ? Privilege::kDropView
                                : type == ObjectType::kSchema ? Privilege::kDropSchema
                                                              : Privilege::kDropTable;
    AccessRequest request{path, type, privilege, {}};
    if (!snapshot_.Check(principal_, request).allowed) {
      Deny(principal_, "user " + principal_.user_name + " may not drop " + TypeWord(type) + " " +
                           path.ToString() + " (only its owner, or a role granted " +
                           PrivilegeName(privilege) + ")");
    }
    ownership_.push_back({PendingOwnership::Kind::kDrop, path, type});
  }

  void CheckDrop(dd::LogicalOperator& op) {
    auto& info = *op.Cast<dd::LogicalDrop>().info;
    auto parts = PartsOf(info);
    if (!parts.empty() && IsTempCatalog(parts[0])) {
      if (principal_.catalog_rules) RequireRuleWrite(TEMP_CATALOG);
      return;
    }
    switch (info.type) {
      case dd::CatalogType::SCHEMA_ENTRY: {
        // Found the way DuckDB finds it: the named catalog, or the session's.
        auto entry = LookUpSchema(context_, SchemaTarget(context_, parts));
        if (!entry) return;  // DROP SCHEMA IF EXISTS of nothing; DuckDB reports a missing schema
        CheckDropOf(*entry);
        return;
      }
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY: {
        // The binder has already qualified the name of an existing object.
        auto entry = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts);
        if (!entry) return;  // DROP ... IF EXISTS of nothing; DuckDB reports a missing object
        CheckDropOf(*entry);
        return;
      }
      case dd::CatalogType::SECRET_ENTRY:
        RequireSystem(SystemPrivilege::kManageSecrets, "DROP SECRET");
        return;
      default:
        if (principal_.catalog_rules) return RequireRuleWrite(CatalogOf(info, false));
        Deny(principal_, "dropping this kind of object needs an administrator");
    }
  }

  void CheckAlter(dd::LogicalOperator& op) {
    auto& info = *op.Cast<dd::LogicalAlter>().info;
    auto parts = PartsOf(info);
    if (principal_.catalog_rules) {
      const std::string catalog = !parts.empty() && IsTempCatalog(parts[0]) ? TEMP_CATALOG : CatalogOf(info, false);
      RequireVisible(catalog);
      return RequireRuleWrite(catalog);
    }
    if (!parts.empty() && IsTempCatalog(parts[0])) return;
    const auto catalog_type = info.GetCatalogType();
    if (catalog_type != dd::CatalogType::TABLE_ENTRY &&
        catalog_type != dd::CatalogType::VIEW_ENTRY) {
      Deny(principal_, "altering this kind of object needs an administrator");
    }
    auto entry = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts);
    if (!entry) return;  // DuckDB reports the missing object
    ObjectPath path = PathOf(*entry);
    if (IsTempCatalog(path.catalog)) return;
    RequireVisible(path.catalog);
    const ObjectType type = ObjectTypeOf(*entry);
    if (info.type == dd::AlterType::ALTER_TABLE || info.type == dd::AlterType::ALTER_VIEW) {
      // A rename moves the object out from under its owner record: owners only.
      AccessRequest request{
          path,
          type,
          type == ObjectType::kView ? Privilege::kAlterView : Privilege::kAlterTable,
          {}};
      if (!snapshot_.Check(principal_, request).allowed) {
        Deny(principal_, "user " + principal_.user_name + " may not alter " + TypeWord(type) + " " +
                             path.ToString() + " (only its owner, or a role granted " +
                             PrivilegeName(request.privilege) + ")");
      }
      return;
    }
    Deny(principal_, "this ALTER needs an administrator");
  }

  // Whose rights a read must satisfy, every one of them: the caller's, or a
  // view owner's (owner_of_view names the view).
  struct Rights {
    Principal principal;
    std::string owner_of_view;
  };

  dd::ClientContext& context_;
  const Snapshot& snapshot_;
  const Principal& principal_;
  const ViewColumns& view_columns_;
  std::vector<Rights> rights_;
  bool in_owned_view_ = false;
  std::string owned_view_;    // the innermost view read with its owner's rights
  std::string unowned_view_;  // the innermost view read with the caller's rights
  std::string creating_view_;  // checking this new view's query (CheckViewQuery)
  std::vector<PendingOwnership> ownership_;
  std::set<std::string> accesses_;
  boost::json::array objects_;
};

}  // namespace

boost::json::object PolicyInput(const Snapshot& snapshot, const Principal& principal,
                                const std::string& action) {
  boost::json::array roles;
  for (const auto& id : snapshot.RolesOf(principal)) {
    if (const auto* role = snapshot.FindRoleById(id)) roles.emplace_back(role->name);
  }
  boost::json::array groups;
  for (const auto& group : principal.idp_groups) groups.emplace_back(group);
  const std::string tenant_id = snapshot.TenantOfPrincipal(principal);
  const auto* tenant = tenant_id.empty() ? nullptr : snapshot.FindTenantById(tenant_id);
  return {{"action", action},
          {"user", principal.user_name},
          {"roles", std::move(roles)},
          {"active_role", principal.active_role_name},
          {"tenant", tenant ? tenant->name : ""},
          {"scoped_token", principal.scope ? principal.scope->name : ""},
          {"identity_provider_groups", std::move(groups)}};
}

namespace {

// A session whose SET ROLE role was revoked acts with nothing until it picks
// another one: say so rather than refusing every object.
void RequireActiveRole(const Snapshot& snapshot, const Principal& principal) {
  if (principal.scope) {
    const auto now = std::chrono::duration_cast<std::chrono::seconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    if (now >= principal.scope->expires_at) {
      throw dd::PermissionException("Hardcharger blocked this statement: scoped token " +
                                    principal.scope->name + " of user " + principal.user_name +
                                    " has expired");
    }
  }
  if (snapshot.HoldsActiveRole(principal)) return;
  const RoleRecord* role = snapshot.FindRoleById(principal.active_role_id);
  throw dd::PermissionException("Hardcharger blocked this statement: user " + principal.user_name +
                                " no longer holds role " +
                                (role ? role->name : std::string("(dropped)")) +
                                ", which SET ROLE made active. Run SET ROLE with another role, "
                                "or RESET ROLE");
}

void PreOptimize(dd::OptimizerExtensionInput& input, dd::unique_ptr<dd::LogicalOperator>& plan) {
  auto& context = input.context;
  auto state = context.registered_state->Get<RbacSessionState>(kRbacSessionStateName);
  if (!state) return;  // a Hardcharger-internal connection
  auto engine = state->engine();
  if (!engine) {
    throw dd::PermissionException(
        "Hardcharger blocked this statement: access control is shutting down");
  }
  auto snapshot = engine->snapshot();
  if (state->unchecked() || !plan) {
    // Superusers and Hardcharger's own statements are not checked, but what they
    // create or drop still updates ownership.
    std::vector<PendingOwnership> ownership;
    if (plan && !state->bypassed()) {
      // Record superuser-created objects as system-owned so a stale record of a
      // user's earlier object of the same name can never apply to them.
      std::function<void(dd::LogicalOperator&)> collect = [&](dd::LogicalOperator& op) {
        if (op.type == OpType::LOGICAL_CREATE_TABLE) {
          auto parts = PartsOf(*op.Cast<dd::LogicalCreateTable>().info->base);
          if (parts.size() >= 3 && !IsTempCatalog(parts[0])) {
            ownership.push_back({PendingOwnership::Kind::kCreate,
                                 {parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()},
                                 ObjectType::kTable});
          }
        } else if (op.type == OpType::LOGICAL_CREATE_VIEW) {
          auto parts = PartsOf(*op.Cast<dd::LogicalCreate>().info);
          if (parts.size() >= 3 && !IsTempCatalog(parts[0])) {
            ownership.push_back({PendingOwnership::Kind::kCreate,
                                 {parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()},
                                 ObjectType::kView,
                                 DefinitionHashOf(*op.Cast<dd::LogicalCreate>().info)});
          }
        }
        for (auto& child : op.children) {
          if (child) collect(*child);
        }
      };
      collect(*plan);
    }
    RemoveViewMarkers(plan);
    state->NoteChecked(snapshot->version(), std::move(ownership),
                       plan && plan->type == OpType::LOGICAL_PREPARE);
    return;
  }
  const Principal principal = state->principal();
  if (!engine->IsActive(principal)) {
    throw dd::PermissionException("Hardcharger blocked this statement: user " +
                                  principal.user_name + " is disabled or was dropped");
  }
  RequireActiveRole(*snapshot, principal);
  const auto view_columns = ViewColumnsUsed(*plan);
  PlanChecker checker(context, *snapshot, principal, view_columns);
  checker.Visit(*plan);
  RemoveViewMarkers(plan);
  // The external policy service may refuse what passed.
  if (const auto* service = engine->policy_service()) {
    auto input = PolicyInput(*snapshot, principal, "statement");
    input["objects"] = checker.TakeObjects();
    switch (service->Decide(input)) {
      case PolicyService::Decision::kAllow:
        break;
      case PolicyService::Decision::kDeny:
        // Not a missing grant: say only who refused.
        throw dd::PermissionException(
            "Hardcharger blocked this statement: the access policy service denied it for "
            "user " +
            principal.user_name);
      case PolicyService::Decision::kNoAnswer:
        throw dd::PermissionException(
            "Hardcharger blocked this statement: Hardcharger could not get an answer from the "
            "access "
            "policy service, and refuses statements until it can");
    }
  }
  // Row and masking policies apply to what passed the check.
  ApplyPolicies(context, snapshot, principal, plan);
  state->NoteChecked(snapshot->version(), checker.TakeOwnership(),
                     plan->type == OpType::LOGICAL_PREPARE, checker.TakeAccesses());
}

// Check every statement as soon as it is bound. DuckDB prepares a statement with
// parameters that it plans again for each run without optimizing it, and
// duckdb_v2_statement_bind describes a statement's result columns from the bound plan
// alone, so neither reaches the pre-optimize check. Checking at bind time means a
// denied statement fails before its result columns are described; the plan that runs
// is checked again (and has the policies applied) before it is optimized.
void PostBind(dd::PlannerExtensionInput& input, dd::BoundStatement& statement) {
  auto state = input.context.registered_state->Get<RbacSessionState>(kRbacSessionStateName);
  if (!state || state->unchecked() || !statement.plan) return;
  auto engine = state->engine();
  if (!engine) {
    throw dd::PermissionException(
        "Hardcharger blocked this statement: access control is shutting down");
  }
  auto snapshot = engine->snapshot();
  const Principal principal = state->principal();
  if (!engine->IsActive(principal)) {
    throw dd::PermissionException("Hardcharger blocked this statement: user " +
                                  principal.user_name + " is disabled or was dropped");
  }
  RequireActiveRole(*snapshot, principal);
  const auto view_columns = ViewColumnsUsed(*statement.plan);
  PlanChecker checker(input.context, *snapshot, principal, view_columns);
  checker.Visit(*statement.plan);
  state->NoteBindChecked();
}

}  // namespace

// ---------------------------------------------------------------------------

RbacSessionState::RbacSessionState(std::shared_ptr<RbacEngine> engine, Principal principal)
    : engine_(engine), principal_(std::move(principal)) {}

RbacSessionState::BypassScope::BypassScope(dd::ClientContext& context)
    : state_(context.registered_state->Get<RbacSessionState>(kRbacSessionStateName)) {
  if (state_) ++state_->bypass_depth_;
}

RbacSessionState::BypassScope::~BypassScope() {
  if (state_) --state_->bypass_depth_;
}

void RbacSessionState::NoteChecked(int64_t snapshot_version,
                                   std::vector<PendingOwnership> ownership, bool prepare_only,
                                   std::vector<std::string> accesses) {
  std::lock_guard<std::mutex> lock(mutex_);
  last_accesses_ = std::move(accesses);
  ++checked_pending_;
  last_checked_version_ = snapshot_version;
  last_checked_persona_ = persona_version_.load();
  planned_prepare_only_ = prepare_only;
  for (auto& o : ownership) planned_ownership_.push_back(std::move(o));
}

std::vector<std::string> RbacSessionState::TakeAccesses() {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<std::string> taken;
  taken.swap(last_accesses_);
  return taken;
}

std::vector<PendingOwnership> RbacSessionState::TakePreparedOwnership() {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<PendingOwnership> taken;
  taken.swap(prepared_ownership_);
  return taken;
}

void RbacSessionState::SetActiveRole(std::string role_id, std::string role_name) {
  std::lock_guard<std::mutex> lock(principal_mutex_);
  principal_.active_role_id = std::move(role_id);
  principal_.active_role_name = std::move(role_name);
  ++persona_version_;
}

void RbacSessionState::NoteBindChecked() {
  std::lock_guard<std::mutex> lock(mutex_);
  bind_checked_pending_ = true;
}

dd::RebindQueryInfo RbacSessionState::OnFinalizePrepare(dd::ClientContext&,
                                                        dd::PreparedStatementData& prepared,
                                                        dd::PreparedStatementMode mode) {
  std::lock_guard<std::mutex> lock(mutex_);
  // A statement with parameters that DuckDB prepared without optimizing (it is
  // planned again with the values for each run). It may go ahead when the
  // bind-time check passed it, or when DuckDB could not plan it yet and so
  // describes no result columns; the run's plan is the one that is enforced.
  const bool bind_checked = bind_checked_pending_;
  bind_checked_pending_ = false;
  const bool unplanned =
      prepared.types.size() == 1 && prepared.types[0].id() == dd::LogicalTypeId::UNKNOWN;
  const bool replanned_on_run =
      prepared.statement_type != dd::StatementType::PREPARE_STATEMENT &&
      (!prepared.properties.bound_all_parameters || prepared.properties.always_require_rebind) &&
      (bind_checked || unplanned);
  // File the plan's ownership changes: applied at QueryEnd when it runs now,
  // or handed to Hardcharger to apply after a later successful execution.
  const bool prepare_only =
      mode == dd::PreparedStatementMode::PREPARE_ONLY || planned_prepare_only_;
  planned_prepare_only_ = false;
  auto& target = prepare_only ? prepared_ownership_ : pending_ownership_;
  if (prepare_only) prepared_ownership_.clear();
  for (auto& o : planned_ownership_) target.push_back(std::move(o));
  planned_ownership_.clear();
  if (checked_pending_ > 0) {
    --checked_pending_;
  } else if (!unchecked() && (prepared.statement_type == dd::StatementType::PREPARE_STATEMENT ||
                              replanned_on_run)) {
    // A statement prepared without optimizing (above): checked on a fresh bind
    // already, and not recorded as checked, so the run's re-plan is checked too.
    // DuckDB 2.x prepares a statement it must re-plan before every run (bound
    // parameters still open, or a catalog such as DuckLake that requires a
    // rebind) without optimizing it, so the check has not run yet. It runs on
    // that re-plan: the statement is not recorded as checked, so the execute
    // hook below forces the re-plan, and the check, before anything runs.
    return dd::RebindQueryInfo::DO_NOT_REBIND;
  } else if (!unchecked()) {
    // The plan never reached the check (the optimizer was skipped). Refuse it.
    pending_ownership_.clear();
    prepared_ownership_.clear();
    throw dd::PermissionException(
        "Hardcharger blocked this statement: its plan could not be checked against user " +
        principal_.user_name + "'s grants");
  }
  if (prepared_versions_.size() > 4096) prepared_versions_.clear();
  prepared_versions_[&prepared] = {last_checked_version_, last_checked_persona_};
  return dd::RebindQueryInfo::DO_NOT_REBIND;
}

dd::RebindQueryInfo RbacSessionState::RebindIfGrantsChanged(
    const dd::PreparedStatementData& prepared) {
  if (unchecked()) return dd::RebindQueryInfo::DO_NOT_REBIND;
  auto engine = engine_.lock();
  if (!engine) return dd::RebindQueryInfo::ATTEMPT_TO_REBIND;
  const int64_t current = engine->snapshot()->version();
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = prepared_versions_.find(&prepared);
  if (it != prepared_versions_.end() && it->second.first == current &&
      it->second.second == persona_version_.load()) {
    return dd::RebindQueryInfo::DO_NOT_REBIND;
  }
  // Grants changed since this statement was checked (or it was never seen):
  // re-plan it, which runs the check again.
  return dd::RebindQueryInfo::ATTEMPT_TO_REBIND;
}

dd::RebindQueryInfo RbacSessionState::OnRebindPreparedStatement(
    dd::ClientContext&, dd::BindPreparedStatementCallbackInfo& info, dd::RebindQueryInfo) {
  return RebindIfGrantsChanged(info.prepared_statement);
}

void RbacSessionState::QueryEnd(dd::ClientContext&, dd::optional_ptr<dd::ErrorData> error) {
  std::vector<PendingOwnership> pending;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    pending.swap(pending_ownership_);
    planned_ownership_.clear();
    checked_pending_ = 0;
    bind_checked_pending_ = false;
  }
  if (error || pending.empty()) return;
  if (auto engine = engine_.lock()) ApplyOwnership(*engine, principal_, pending);
}

bool IsAllowedTableFunction(const std::string& name_lower) {
  return AllowedTableFunctions().contains(name_lower);
}

void ApplyOwnership(RbacEngine& engine, const Principal& principal,
                    const std::vector<PendingOwnership>& changes) {
  for (const auto& p : changes) {
    // A catalog_access principal (rules only, or an administrator's token with rules) is
    // no store user or role: its rights are its token's rules, for that session only, so
    // nothing could read a view with them later. What it creates gets no owner (and loses
    // any record left by an earlier object of that name): a view it creates reads with
    // each reader's own rights, its creator's included, and fails when those don't reach
    // what the view reads.
    if (principal.catalog_rules && !principal.superuser) {
      (void)engine.ForgetOwner(p.object);
      continue;
    }
    if (p.kind == PendingOwnership::Kind::kCreate) {
      (void)engine.RecordOwner(p.object, p.type, principal, p.definition_hash);
    } else {
      (void)engine.ForgetOwner(p.object);
    }
  }
}

std::optional<SystemPrivilege> SystemPrivilegeForGatedCategory(const std::string& category) {
  auto starts = [&](const char* prefix) { return category.rfind(prefix, 0) == 0; };
  auto ends = [&](const std::string& suffix) {
    return category.size() >= suffix.size() &&
           category.compare(category.size() - suffix.size(), suffix.size(), suffix) == 0;
  };
  if (category == "ATTACH" || category == "DETACH") return SystemPrivilege::kManageCatalogs;
  if (category == "INSTALL extension" || category == "LOAD extension" ||
      category == "CREATE EXTENSION REPOSITORY" || category == "DROP EXTENSION REPOSITORY") {
    return SystemPrivilege::kManageExtensions;
  }
  if (category == "CREATE SECRET" || category == "DROP SECRET" || category == "duckdb_secrets()") {
    return SystemPrivilege::kManageSecrets;
  }
  if (starts("SET GLOBAL ") || starts("RESET GLOBAL ") || starts("reading the setting "))
    return SystemPrivilege::kManageConfig;
  if (category == "EXPORT DATABASE") return SystemPrivilege::kExportDatabase;
  if (category == "IMPORT DATABASE") return SystemPrivilege::kImportDatabase;
  if (category == "CHECKPOINT") return SystemPrivilege::kCheckpoint;
  if (starts("COPY TO")) return SystemPrivilege::kWriteFilesystem;
  if (starts("COPY FROM") || starts("reading files and URLs via FROM")) {
    return SystemPrivilege::kReadFilesystem;
  }
  // read_csv() (files and URLs), glob() (local filesystem), ...
  if (ends(" (files and URLs)") || ends(" (local filesystem)")) {
    const auto paren = category.find("()");
    if (paren != std::string::npos &&
        ddb::IsFileReadingTableFunction(Lower(category.substr(0, paren)))) {
      return SystemPrivilege::kReadFilesystem;
    }
  }
  return std::nullopt;
}

void RegisterRbacEnforcement(dd::DBConfig& config) {
  InstallViewHook();
  dd::OptimizerExtension extension;
  extension.pre_optimize_function = PreOptimize;
  dd::OptimizerExtension::Register(config, std::move(extension));
  dd::PlannerExtension planner_extension;
  planner_extension.post_bind_function = PostBind;
  dd::PlannerExtension::Register(config, std::move(planner_extension));
}

void AttachRbacSession(dd::ClientContext& context, std::shared_ptr<RbacEngine> engine,
                       Principal principal) {
  context.registered_state->Insert(
      kRbacSessionStateName,
      dd::make_shared_ptr<RbacSessionState>(std::move(engine), std::move(principal)));
}

}  // namespace hardcharger::authz
