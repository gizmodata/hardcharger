#include "authz/enforcement.hpp"

#include <algorithm>
#include <chrono>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/parser/constraints/check_constraint.hpp>
#include <duckdb/catalog/catalog_entry_retriever.hpp>
#include <duckdb/catalog/dependency_manager.hpp>
#include <duckdb/catalog/catalog_search_path.hpp>
#include <duckdb/catalog/catalog_entry/schema_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/trigger_catalog_entry.hpp>
#include <duckdb/execution/expression_executor.hpp>
#include <duckdb/function/scalar/sequence_utils.hpp>
#include <duckdb/execution/operator/helper/physical_set.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/main/prepared_statement_data.hpp>
#include <duckdb/optimizer/optimizer_extension.hpp>
#include <duckdb/catalog/catalog_entry/index_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/sequence_catalog_entry.hpp>
#include <duckdb/parser/parsed_data/comment_on_column_info.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/parsed_data/alter_info.hpp>
#include <duckdb/parser/parsed_data/alter_table_info.hpp>
#include <duckdb/parser/parsed_expression_iterator.hpp>
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
#include <duckdb/parser/parsed_data/create_trigger_info.hpp>
#include <duckdb/parser/parsed_data/create_type_info.hpp>
#include <duckdb/parser/parsed_data/extra_drop_info.hpp>
#include <duckdb/parser/tableref/basetableref.hpp>
#include <duckdb/planner/operator/logical_materialized_cte.hpp>
#include <duckdb/planner/operator/logical_trigger.hpp>
#include <duckdb/planner/operator/logical_delete.hpp>
#include <duckdb/planner/operator/logical_drop.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <duckdb/planner/operator/logical_insert.hpp>
#include <duckdb/planner/operator/logical_merge_into.hpp>
#include <duckdb/planner/operator/logical_reset.hpp>
#include <duckdb/planner/operator/logical_copy_to_file.hpp>
#include <duckdb/planner/operator/logical_export.hpp>
#include <duckdb/planner/operator/logical_set.hpp>
#include <duckdb/planner/operator/logical_update.hpp>
#include <duckdb/planner/operator/logical_vacuum.hpp>
#include <duckdb/planner/parsed_data/bound_create_table_info.hpp>
#include <duckdb/planner/planner_extension.hpp>
#include <functional>
#include <set>
#include <unordered_set>

#include "authz/duckdb_internal.hpp"
#include "authz/ducklake_calls.hpp"
#include "authz/macros.hpp"
#include "authz/name_scope.hpp"
#include "logging.hpp"
#include "authz/policies.hpp"
#include "authz/resolver.hpp"
#include "authz/storage_access.hpp"
#include "authz/triggers.hpp"
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
      "duckdb_dependencies", "duckdb_extensions", "duckdb_optimizers", "duckdb_triggers", "pragma_table_info",
      "pragma_show", "pragma_version", "pragma_platform", "pragma_database_size",
      "pragma_collations", "pragma_user_agent",
      // Bulk ingest reads the caller's own
      // upload through it; it reads nothing
      // else (the SDK refuses any other call).
      "chargewire_upload",
      // A prepare describes its result by binding it (never run); it converts
      // the prepared statement's own columns, nothing else (the SDK refuses
      // any other call).
      "chargewire_result_schema",
      // The server's own, which read only what the session may see (its own principal's
      // sessions, the settings rows that aren't admin-only, the server facts that aren't
      // the unrestricted administrator's, its own grants).
      "hardcharger_settings", "hardcharger_sessions", "hardcharger_metrics",
      "hardcharger_active_connections", "hardcharger_server_info",
      "hardcharger_live_queries", "hardcharger_monitor_info", "hardcharger_server_settings", "hardcharger_session_settings", "hardcharger_grants",
      "hardcharger_role_members", "hardcharger_roles", "hardcharger_users", "hardcharger_policies",
      "hardcharger_effective_privileges"};
  return kNames;
}

// Table functions no grant opens to users: they run SQL of their own (the gate refuses
// them by name), read arbitrary memory, change the engine, or show other sessions' data.
const std::unordered_set<std::string>& AdministratorOnlyTableFunctions() {
  static const std::unordered_set<std::string> kNames = {
      "query", "query_table", "json_execute_serialized_sql", "arrow_scan", "arrow_scan_dumb", "seq_scan",
      "checkpoint", "force_checkpoint", "duckdb_secrets", "which_secret", "duckdb_logs", "truncate_duckdb_logs",
      "enable_logging", "disable_logging", "enable_profiling", "disable_profiling", "duckdb_prepared_statements",
      "duckdb_temporary_files", "duckdb_memory", "duckdb_external_file_cache", "duckdb_external_resources",
      "duckdb_external_resource_types", "register_external_resource", "register_external_resource_type",
      "deregister_external_resource", "create_external_resource", "destroy_external_resource",
      "pragma_storage_info", "pragma_metadata_info", "duckdb_table_sample", "dbgen", "dsdgen",
      // DuckDB 2.0.1: every connection's query counters, and the session's SQL grammar (what
      // SET active_grammar_extensions changes, which BlockedSettings() refuses).
      "duckdb_live_query_metrics", "add_grammar_extension", "remove_grammar_extension"};
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

}  // namespace

std::optional<std::string> UngrantableTableFunction(const std::string& name_lower) {
  if (AllowedTableFunctions().count(name_lower)) {
    return "table function " + name_lower + " needs no grant: every user may call it";
  }
  if (ddb::IsFileReadingTableFunction(name_lower)) {
    return "table function " + name_lower +
           " reads files: the storage privileges decide who may call it (READ FILESYSTEM, READ OBJECT STORAGE, "
           "READ HTTP, or ON LOCATION), not EXECUTE";
  }
  if (name_lower.starts_with("hardcharger_") || name_lower.starts_with("chargewire_") ||
      AdministratorOnlyTableFunctions().count(name_lower)) {
    return "table function " + name_lower +
           " stays the administrator's: it runs SQL of its own, reaches memory or the engine's internals, or "
           "shows other sessions' data";
  }
  return std::nullopt;
}

namespace {

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

// A table's trigger by name: triggers hang off their tables, not their schema's sets.
dd::optional_ptr<dd::CatalogEntry> LookUpTrigger(dd::ClientContext& context, dd::TableCatalogEntry& table,
                                                 const std::string& name) {
  try {
    auto txn = table.ParentCatalog().GetCatalogTransaction(context);
    return table.GetTrigger(txn, dd::Identifier(name));
  } catch (...) {
    return nullptr;
  }
}

bool IsSequenceFunction(const std::string& name_lower) {
  return name_lower == "nextval" || name_lower == "currval" || name_lower == "setval";
}

// Every nextval / currval / setval call in a parsed expression (a column default):
// the function, and the sequence's name when it is a constant string (as DuckDB requires
// to bind the call), or nullopt.
void ForEachSequenceCall(dd::ParsedExpression& expression,
                         const std::function<void(const std::string&, const std::optional<std::string>&)>& call) {
  if (expression.GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
    auto& function = expression.Cast<dd::FunctionExpression>();
    const std::string name = Lower(compat::Str(function.FunctionName()));
    if (IsSequenceFunction(name)) {
      std::optional<std::string> sequence;
      for (const auto& argument : function.GetArguments()) {
        if (argument.HasName()) continue;
        const auto& first = argument.GetExpression();
        if (first.GetExpressionClass() == dd::ExpressionClass::CONSTANT) {
          const auto value = compat::ConstantValue(first.Cast<dd::ConstantExpression>());
          if (!value.IsNull()) sequence = value.ToString();
        }
        break;
      }
      call(name, sequence);
    }
  }
  dd::ParsedExpressionIterator::EnumerateChildren(expression, [&](dd::ParsedExpression& child) {
    ForEachSequenceCall(child, call);
  });
}

// A scan of `table` and nothing else: a LogicalGet of it, under projections of its
// columns only (what DuckDB plans under ANALYZE / VACUUM).
bool IsPlainScanOf(dd::LogicalOperator& op, dd::TableCatalogEntry& table) {
  if (op.type == dd::LogicalOperatorType::LOGICAL_GET) {
    return op.Cast<dd::LogicalGet>().GetTable().get() == &table && op.children.empty();
  }
  if (op.type != dd::LogicalOperatorType::LOGICAL_PROJECTION || op.children.size() != 1 || !op.children[0]) {
    return false;
  }
  for (const auto& expression : op.expressions) {
    if (!expression || expression->GetExpressionClass() != dd::ExpressionClass::BOUND_COLUMN_REF) return false;
  }
  return IsPlainScanOf(*op.children[0], table);
}

ObjectType ObjectTypeOf(const dd::CatalogEntry& entry) {
  if (entry.type == dd::CatalogType::VIEW_ENTRY) return ObjectType::kView;
  if (entry.type == dd::CatalogType::SCHEMA_ENTRY) return ObjectType::kSchema;
  if (entry.type == dd::CatalogType::TRIGGER_ENTRY) return ObjectType::kTrigger;
  return ObjectType::kTable;
}

const TriggerBodies kNoTriggerBodies;

std::string TypeWord(ObjectType type) {
  switch (type) {
    case ObjectType::kView:
      return "view";
    case ObjectType::kSchema:
      return "schema";
    case ObjectType::kTable:
      return "table";
    case ObjectType::kSequence:
      return "sequence";
    case ObjectType::kTrigger:
      return "trigger";
    case ObjectType::kTableFunction:
      return "table function";
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

// allow_extension_repositories decides whether a new extension repository, and with it a new
// signing key whose extensions run in the server, may be added under the engine repository policy: the unrestricted administrator's decision only,
// not MANAGE CONFIG's.
std::string AdminOnlyRepositorySetting(const std::string& verb) {
  return verb + " allow_extension_repositories decides whether new extension repositories, whose signing keys "
                "the server then trusts, may be added; only an administrator without catalog_access rules may "
                "change it";
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
// CREATE INDEX: an index's expressions are kept as written and bound again, under their
// table's name scope, when the index is first needed after a start (name_scope.hpp). Why the
// functions and types they name would then not be what they are in this session; "" if they are.
std::string IndexStoredNamesProblem(dd::ClientContext& context, dd::LogicalCreateIndex& index) {
  if (!index.info) return "";
  const ObjectPath table = PathOfEntry(index.table);
  std::vector<const dd::ParsedExpression*> expressions;
  for (const auto& expression : index.info->parsed_expressions) expressions.push_back(expression.get());
  for (const auto& expression : index.info->expressions) expressions.push_back(expression.get());
  return StoredNamesProblem(context, table.catalog, table.schema_path,
                            "an index on table " + table.ToString(), nullptr, expressions);
}

// ALTER TABLE ... ADD COLUMN (a generated one, or one with a CHECK) and ADD CONSTRAINT CHECK:
// the macros they call given their schema, as CREATE TABLE does (QualifyTableExpressions).
void QualifyAlterExpressions(dd::ClientContext& context, const std::string& catalog, dd::AlterInfo& info) {
  auto& table_info = info.Cast<dd::AlterTableInfo>();
  if (table_info.alter_table_type == dd::AlterTableType::ADD_COLUMN) {
    auto& column = table_info.Cast<dd::AddColumnInfo>().new_column;
    if (column.Generated()) {
      auto copy = column.GeneratedExpression().Copy();
      QualifyTableExpression(context, catalog, copy);
      column.SetGeneratedExpression(std::move(copy));
    }
  } else if (table_info.alter_table_type == dd::AlterTableType::ADD_CONSTRAINT) {
    auto& constraint = table_info.Cast<dd::AddConstraintInfo>().constraint;
    if (constraint && constraint->type == dd::ConstraintType::CHECK) {
      QualifyTableExpression(context, catalog, constraint->Cast<dd::CheckConstraint>().expression);
    }
  }
}

class PlanChecker {
 public:
  using ViewColumns = std::unordered_map<const ViewMarker*, std::vector<std::string>>;

  PlanChecker(dd::ClientContext& context, const Snapshot& snapshot, const Principal& principal,
              const ViewColumns& view_columns, const TriggerBodies& trigger_bodies)
      : context_(context),
        snapshot_(snapshot),
        principal_(principal),
        view_columns_(view_columns),
        trigger_bodies_(trigger_bodies),
        rights_{Rights{principal, ""}} {}

  // The carried columns (triggers.hpp) a base write's scan was not asked SELECT for,
  // because the rights it was checked with lack it: somebody must answer for them where
  // the base's rows are read (RequireCarriedRows). Shared by a checker and the checkers
  // it makes for trigger bodies. A base's scan is checked before its rows' readers (a
  // CTE's definition comes before its scope); `read` refuses a waiver that would come
  // after one all the same.
  struct CarriedRows {
    std::map<const dd::LogicalOperator*, std::vector<std::string>> waived;  // by the base's write
    std::set<const dd::LogicalOperator*> read;
  };
  std::shared_ptr<CarriedRows> carried_rows_ = std::make_shared<CarriedRows>();
  // The trigger body being checked, innermost (nullptr: the statement itself).
  const TriggerBody* current_body_ = nullptr;

  // Checking a trigger's body with its owner's rights: prefixed to every refusal.
  std::string trigger_context_;
  // Set by CheckAsDefiner for the one Visit of the body's root, so the root is checked
  // here rather than handed to yet another checker.
  bool trigger_body_entered_ = false;
  // The names in the trigger bodies a statement fires are held against the writer's
  // session (triggers.hpp, "Names in a body"). For an administrator's own statement, only in
  // the bodies of users' triggers (CheckDefinerBodies) and beneath them.
  bool check_trigger_names_ = true;

  void Visit(dd::LogicalOperator& op,
             dd::optional_ptr<dd::TableCatalogEntry> dml_target = nullptr) {
    // A trigger's body, bound into the statement that fires it (triggers.hpp): checked with
    // its owner's rights when a user owns the trigger, refused outright when its owner is
    // gone or the trigger can't be told, else with the writer's, as DuckDB runs it.
    struct RestoreBody {
      const TriggerBody*& slot;
      const TriggerBody* saved;
      ~RestoreBody() { slot = saved; }
    } restore_body{current_body_, current_body_};
    if (const TriggerBody* body = TriggerBodyAt(trigger_bodies_, op); body && !body->base) {
      if (!body->problem.empty()) Deny(principal_, body->problem);
      // Whoever made the trigger and whoever's rights its body runs with: every name in it
      // must be, in this session, what it is for the trigger's own table.
      if (check_trigger_names_ && !trigger_body_entered_) {
        const std::string names = TriggerBodyNameProblem(context_, trigger_bodies_, *body);
        if (!names.empty()) ::hardcharger::authz::Deny(principal_, names);
      }
      if (body->definer && !trigger_body_entered_) return CheckAsDefiner(*body, op);
      current_body_ = body;
      // A row trigger's body reads the rows its statement changes (OLD, NEW).
      if (body->reads_rows_of) {
        if (const TriggerBody* base = TriggerBodyAt(trigger_bodies_, *body->reads_rows_of)) RequireCarriedRows(*base);
      }
      dml_target = nullptr;
    }
    trigger_body_entered_ = false;
    // A table's own column defaults (id DEFAULT nextval('s')) are part of writing it:
    // INSERT (or UPDATE ... SET id = DEFAULT, MERGE) on the table covers the sequences
    // they advance, also where DuckDB copies a default into the plan (VALUES (DEFAULT)).
    // Who may write a default that names a sequence is checked where it is written
    // (CREATE TABLE, ALTER TABLE ... DEFAULT): USAGE on the sequence.
    const size_t defaults_before = default_sequences_.size();
    dd::optional_ptr<dd::TableCatalogEntry> written;
    if (op.type == OpType::LOGICAL_INSERT) written = &op.Cast<dd::LogicalInsert>().table;
    if (op.type == OpType::LOGICAL_UPDATE) written = &op.Cast<dd::LogicalUpdate>().table;
    if (op.type == OpType::LOGICAL_MERGE_INTO) written = &op.Cast<dd::LogicalMergeInto>().table;
    if (written) CollectDefaultSequences(*written);
    struct RestoreDefaults {
      std::vector<const dd::CatalogEntry*>& list;
      size_t size;
      ~RestoreDefaults() { list.resize(size); }
    } restore_defaults{default_sequences_, defaults_before};
    CheckSettingReads(op);
    switch (op.type) {
      // ---- reads ----------------------------------------------------------
      // A table function's input (range((SELECT ...)), unnest((SELECT list(v) FROM t))) is
      // planned as its child: what it reads, and the sequences it moves, are checked as any
      // query's.
      case OpType::LOGICAL_GET:
        VisitGet(op.Cast<dd::LogicalGet>(), dml_target);
        VisitChildren(op, nullptr);
        return;

      // ---- writes ---------------------------------------------------------
      case OpType::LOGICAL_INSERT: {
        auto& insert = op.Cast<dd::LogicalInsert>();
        Require(insert.table, Privilege::kInsert, {});
        // RETURNING hands rows back: SELECT on every column (not for the RETURNING * the
        // binder adds under a trigger for the transition tables, which the writer never sees).
        if (insert.return_chunk && !ReturningAddedForTriggers(trigger_bodies_, op)) RequireAllColumns(insert.table);
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_UPDATE: {
        auto& update = op.Cast<dd::LogicalUpdate>();
        Require(update.table, Privilege::kUpdate, {});
        if (update.return_chunk && !ReturningAddedForTriggers(trigger_bodies_, op)) RequireAllColumns(update.table);
        VisitChildren(op, &update.table);
        return;
      }
      case OpType::LOGICAL_DELETE: {
        auto& del = op.Cast<dd::LogicalDelete>();
        Require(del.table, Privilege::kDelete, {});
        if (del.return_chunk && !ReturningAddedForTriggers(trigger_bodies_, op)) RequireAllColumns(del.table);
        VisitChildren(op, &del.table);
        return;
      }
      case OpType::LOGICAL_MERGE_INTO: {
        auto& merge = op.Cast<dd::LogicalMergeInto>();
        // A merge (MERGE INTO, and INSERT ... ON CONFLICT, which DuckDB runs as one) needs
        // the privilege of each action it has, as in Postgres: ON CONFLICT DO NOTHING
        // inserts, DO UPDATE inserts and updates. The target columns it reads (the keys it
        // matches on, a condition) need SELECT, through the scan beneath.
        bool inserts = false, updates = false, deletes = false;
        for (const auto& [when, actions] : merge.actions) {
          for (const auto& action : actions) {
            switch (action ? action->action_type : dd::MergeActionType::MERGE_UPDATE) {
              case dd::MergeActionType::MERGE_INSERT:
                inserts = true;
                break;
              case dd::MergeActionType::MERGE_UPDATE:
                updates = true;
                // An update DuckDB runs as a delete and an insert is still an update.
                break;
              case dd::MergeActionType::MERGE_DELETE:
                deletes = true;
                break;
              case dd::MergeActionType::MERGE_DO_NOTHING:
              case dd::MergeActionType::MERGE_ERROR:
                break;
              default:  // an action this check does not know: everything
                inserts = updates = deletes = true;
                break;
            }
          }
        }
        // A merge that can change nothing still names its target as one to write.
        if (!inserts && !updates && !deletes) inserts = true;
        if (inserts) Require(merge.table, Privilege::kInsert, {});
        if (updates) Require(merge.table, Privilege::kUpdate, {});
        if (deletes) Require(merge.table, Privilege::kDelete, {});
        if (merge.return_chunk && !ReturningAddedForTriggers(trigger_bodies_, op)) RequireAllColumns(merge.table);
        VisitChildren(op, &merge.table);
        return;
      }

      // ---- DDL ------------------------------------------------------------
      case OpType::LOGICAL_CREATE_TABLE: {
        auto& create = op.Cast<dd::LogicalCreateTable>();
        // Temporary tables too: their inserts advance what their defaults name.
        if (!principal_.catalog_rules) {
          for (auto& column : create.info->Base().columns.Logical()) {
            if (!column.HasDefaultValue()) continue;
            auto copy = column.DefaultValue().Copy();
            CheckDefaultSequences(*copy);
          }
        }
        CheckCreate(*create.info->base, ObjectType::kTable, Privilege::kCreateTable);
        // The macros its CHECK constraints, generated columns and defaults call, given their
        // schema; then every name in them is what its table's name scope will find.
        if (const auto parts = PartsOf(*create.info->base); !parts.empty()) {
          QualifyTableExpressions(context_, parts[0], create.info->Base());
          if (parts.size() >= 3) {
            const ObjectPath table{parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()};
            if (auto problem = TableStoredNamesProblem(context_, table, create.info->Base()); !problem.empty()) {
              Deny(principal_, problem);
            }
          }
        }
        VisitChildren(op, nullptr);  // CREATE TABLE AS reads its source
        return;
      }
      case OpType::LOGICAL_CREATE_VIEW: {
        auto& create = op.Cast<dd::LogicalCreate>();
        const size_t before = ownership_.size();
        if (CheckCreate(*create.info, ObjectType::kView, Privilege::kCreateView)) {
          if (ownership_.size() > before) ownership_.back().definition_hash = DefinitionHashOf(*create.info);
          CheckViewQuery(create);  // the view's query, bound on its own
        }
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_CREATE_SCHEMA: {
        auto& create = op.Cast<dd::LogicalCreate>();
        CheckCreate(*create.info, ObjectType::kSchema, Privilege::kCreateSchema);
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_CREATE_INDEX: {
        auto& index = op.Cast<dd::LogicalCreateIndex>();
        if (!IsTempCatalog(compat::Str(index.table.ParentCatalog().GetName()))) {
          Require(index.table, Privilege::kAlterTable, {});
          if (index.info) QualifyIndexExpressions(context_, compat::Str(index.table.ParentCatalog().GetName()), *index.info);
          if (auto problem = IndexStoredNamesProblem(context_, index); !problem.empty()) Deny(principal_, problem);
        }
        VisitChildren(op, nullptr);
        return;
      }
      case OpType::LOGICAL_CREATE_SEQUENCE: {
        auto& create = op.Cast<dd::LogicalCreate>();
        if (principal_.catalog_rules) {
          RequireRuleWrite(CatalogOf(*create.info, create.info->temporary));
        } else if (!create.info->temporary) {  // a temporary one is session-local
          CheckCreateSequence(*create.info);
        }
        VisitChildren(op, nullptr);
        return;
      }
      // CREATE MACRO in a schema: the CREATE MACRO privilege (macros.hpp).
      case OpType::LOGICAL_CREATE_MACRO:
        CheckCreateMacro(*op.Cast<dd::LogicalCreate>().info);
        VisitChildren(op, nullptr);
        return;
      // CREATE TYPE in a schema: the CREATE TYPE privilege (and TypeNameRefusal).
      case OpType::LOGICAL_CREATE_TYPE:
        CheckCreateType(op);
        VisitChildren(op, nullptr);  // CREATE TYPE ... AS ENUM (SELECT ...) reads its query
        return;
      // CREATE TRIGGER on a table the user owns: the CREATE TRIGGER privilege (triggers.hpp).
      case OpType::LOGICAL_CREATE_TRIGGER:
        CheckCreateTrigger(op.Cast<dd::LogicalCreate>());
        VisitChildren(op, nullptr);
        return;
      case OpType::LOGICAL_DROP:
        CheckDrop(op);
        VisitChildren(op, nullptr);
        return;
      case OpType::LOGICAL_ALTER:
        CheckAlter(op);
        VisitChildren(op, nullptr);
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
        if (Lower(compat::Str(set.name)) == "allow_extension_repositories") {
          Deny(principal_, AdminOnlyRepositorySetting("SET"));
        }
        if (ChangesWholeServer(context_, set.name, set.scope)) {
          RequireSystem(SystemPrivilege::kManageConfig, "SET GLOBAL");
        }
        // Session settings that make DuckDB write files wherever they point (every
        // statement's text, or its profile): writing files, as COPY TO is.
        if (WritesFiles(Lower(compat::Str(set.name)))) {
          RequireStorage("SET " + compat::Str(set.name), StorageMode::kWrite,
                         set.value.IsNull() ? std::nullopt
                                            : std::optional<std::vector<std::string>>({set.value.ToString()}));
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
        if (Lower(compat::Str(reset.name)) == "allow_extension_repositories") {
          Deny(principal_, AdminOnlyRepositorySetting("RESET"));
        }
        if (reset.scope != dd::SetScope::VARIABLE &&
            ChangesWholeServer(context_, reset.name, reset.scope)) {
          RequireSystem(SystemPrivilege::kManageConfig, "RESET GLOBAL");
        }
        VisitChildren(op, nullptr);
        return;
      }

      // ---- a view's plan, marked when it was bound -------------------------
      case OpType::LOGICAL_EXTENSION_OPERATOR:
        if (auto* marker = dynamic_cast<ViewMarker*>(&op)) {
          VisitView(*marker);
          return;
        }
        // DuckLake's compaction and inlined-data flush, which ducklake_merge_adjacent_files,
        // ducklake_rewrite_data_files and ducklake_flush_inlined_data bind to: a catalog_access
        // principal's, when the gate checked such a call on the catalog it rewrites.
        if (!principal_.superuser &&
            op.Cast<dd::LogicalExtensionOperator>().GetExtensionName() == "ducklake") {
          CheckDuckLakeRewrite(op);
          return;
        }
        Deny(principal_, "extension operators are not available under access control");
      case OpType::LOGICAL_TRANSACTION:
        VisitChildren(op, nullptr);
        return;

      // ---- system privileges ---------------------------------------------
      case OpType::LOGICAL_ATTACH:
      case OpType::LOGICAL_DETACH:
        RequireSystem(SystemPrivilege::kManageCatalogs, "ATTACH and DETACH");
        VisitChildren(op, nullptr);
        return;
      case OpType::LOGICAL_LOAD:
        RequireSystem(SystemPrivilege::kManageExtensions, "INSTALL and LOAD");
        VisitChildren(op, nullptr);
        return;
      // ANALYZE and VACUUM of one table: MAINTAIN on it (its owner holds it). Of every
      // table at once: the administrator's (it would touch tables the user can't see).
      case OpType::LOGICAL_VACUUM: {
        auto& vacuum = op.Cast<dd::LogicalVacuum>();
        if (!vacuum.HasTable()) {
          Deny(principal_, "ANALYZE or VACUUM of every table needs an administrator; name a table you may maintain "
                           "(ANALYZE t, VACUUM t)");
        }
        Require(vacuum.GetTable(), Privilege::kMaintain, {});
        // DuckDB scans the table to gather its statistics, which reach no one: that scan is
        // the maintenance, not a read. Anything else under it is checked as usual.
        for (auto& child : op.children) {
          if (child && IsPlainScanOf(*child, vacuum.GetTable())) continue;
          if (child) Visit(*child, nullptr);
        }
        return;
      }
      // FORCE INSTALL of every installed extension that has a newer build.
      case OpType::LOGICAL_UPDATE_EXTENSIONS:
        RequireSystem(SystemPrivilege::kManageExtensions, "UPDATE EXTENSIONS");
        VisitChildren(op, nullptr);
        return;
      case OpType::LOGICAL_CREATE_SECRET:
        RequireSystem(SystemPrivilege::kManageSecrets, "CREATE SECRET");
        VisitChildren(op, nullptr);
        return;
      case OpType::LOGICAL_COPY_TO_FILE: {
        const auto& copy = op.Cast<dd::LogicalCopyToFile>();
        // A file name pattern can't step out of the directory COPY writes.
        std::vector<std::string> literals{copy.filename_pattern.SerializeBase()};
        for (const auto& segment : copy.filename_pattern.SerializeSegments()) literals.push_back(segment.data);
        for (const auto& literal : literals) {
          if (literal.find('/') != std::string::npos || literal.find('\\') != std::string::npos ||
              literal.find("..") != std::string::npos) {
            Deny(principal_, "COPY ... TO with a FILENAME_PATTERN that names a directory ('" + literal +
                                 "') is not available under access control");
          }
        }
        RequireStorage("COPY ... TO", StorageMode::kWrite, std::vector<std::string>{copy.file_path},
                       copy.partition_output || copy.per_thread_output || copy.rotate ||
                           copy.file_size_bytes.IsValid());
        VisitChildren(op, nullptr);  // and SELECT on what it copies
        return;
      }
      case OpType::LOGICAL_EXPORT: {
        RequireSystem(SystemPrivilege::kExportDatabase, "EXPORT DATABASE");
        const auto& exported = op.Cast<dd::LogicalExport>();
        RequireStorage("EXPORT DATABASE", StorageMode::kWrite,
                       exported.copy_info ? std::optional<std::vector<std::string>>({exported.copy_info->file_path})
                                          : std::nullopt,
                       /*directory=*/true);
        VisitChildren(op, nullptr);
        return;
      }

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
      case OpType::LOGICAL_EXPLAIN:
      // A prepared statement's plan, and the re-planned statement an EXECUTE
      // carries after a rebind (without one, the check made when it was
      // prepared, at the same grants version, still stands).
      case OpType::LOGICAL_PREPARE:
      case OpType::LOGICAL_EXECUTE:
      case OpType::LOGICAL_SECURE_VIEW:
        VisitChildren(op, dml_target);
        return;

      case OpType::LOGICAL_MATERIALIZED_CTE:
      case OpType::LOGICAL_TRIGGER:
        VisitChildren(op, dml_target);
        return;

      case OpType::LOGICAL_CTE_REF:
        // A CTE's rows were checked where its definition read them, with one exception:
        // the rows a write hands its triggers (a transition table), whose carried columns
        // the writer was not asked SELECT for. Somebody answers for those here.
        if (const TriggerBody* base = BaseReadBy(trigger_bodies_, op)) RequireCarriedRows(*base);
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
  //
  // For everyone else: nextval and currval need USAGE on the sequence (granted ON SEQUENCE,
  // ON SCHEMA or ON CATALOG, or owning its schema), except where the table being written
  // advances it through its own column default. setval resets a sequence, which dropping
  // and recreating it would do too: it needs DROP SEQUENCE on the sequence's schema.
  void CheckSequenceUse(const dd::BoundFunctionExpression& call) {
    const std::string name = Lower(compat::Str(call.Function().GetName()));
    if (!IsSequenceFunction(name)) return;
    const auto& info = call.BindInfo();
    if (!info) return;  // a NULL sequence name: DuckDB returns NULL
    auto& sequence = info->Cast<dd::NextvalBindData>().sequence;
    if (!principal_.catalog_rules) {
      const ObjectPath path = PathOf(sequence);
      if (IsTempCatalog(path.catalog)) return;  // the session's own
      if (name == "setval") return RequireSchemaPrivilege(path, Privilege::kDropSequence, "setval() of sequence");
      if (name == "nextval" && std::find(default_sequences_.begin(), default_sequences_.end(), &sequence) !=
                                   default_sequences_.end()) {
        return;
      }
      return RequireSequenceUsage(path);
    }
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
      // A DuckLake call: the catalog it names decides, wherever it sits. A store user's
      // administrator-only and unknown DuckLake functions are table functions like any
      // other (EXECUTE, below).
      if (!principal_.superuser) {
        if (const auto access = DuckLakeFunctionAccess(name);
            access && (principal_.catalog_rules ||
                       (*access != DuckLakeAccess::kAdministrator && *access != DuckLakeAccess::kUnknown))) {
          CheckDuckLakeCall(get, name, *access);
          return;
        }
      }
      if (OnlySuperusers()) return;  // in a view an administrator owns
      if (ddb::IsFileReadingTableFunction(name)) {
        RequireStorage(name + "()", StorageMode::kRead, PathsOf(get));
        return;
      }
      if (name == "checkpoint" || name == "force_checkpoint") {
        RequireSystem(SystemPrivilege::kCheckpoint, "CHECKPOINT");
        RequireDuckLakeCheckpoint(get, name);
        return;
      }
      if (name == "duckdb_secrets") {
        RequireSystem(SystemPrivilege::kManageSecrets, "duckdb_secrets()");
        return;
      }
      // Only the ones that read no table and no file, the administrator with
      // catalog_access rules included: others (pragma_storage_info('c.t'), ...) read a
      // catalog around its rules. Any other function needs EXECUTE on it (granted to
      // a role; a catalog_access principal holds no grants), and the call's path
      // arguments, as the grant declared them, go through the storage privileges.
      if (!AllowedTableFunctions().count(name)) {
        const auto declared = snapshot_.TableFunctionPathArguments(principal_, name);
        if (!declared) {
          Deny(principal_, "the table function " + name +
                               " is not available under access control (an administrator can GRANT EXECUTE ON "
                               "TABLE FUNCTION " + name + " to a role)");
        }
        std::vector<std::string> paths;
        for (const auto& argument : *declared) {
          std::optional<dd::Value> value;
          if (argument.position > 0) {
            if (get.parameters.size() < static_cast<size_t>(argument.position)) {
              Deny(principal_, name + "() was called without its path " + argument.Text() +
                                   ", which names the files it reads");
            }
            value = get.parameters[static_cast<size_t>(argument.position) - 1];
          } else {
            for (const auto& named : get.named_parameters) {
              if (IdentifiersEqual(compat::Str(named.first), argument.parameter)) value = named.second;
            }
            if (!value) continue;  // a named parameter the call left out names no file
          }
          auto some = PathsOfValue(*value);
          if (!some) {
            Deny(principal_, "the path " + argument.Text() + " of " + name +
                                 "() must be a string or a list of strings (the files it reads)");
          }
          paths.insert(paths.end(), some->begin(), some->end());
        }
        NoteAccess("EXECUTE " + name + "()", {});
        if (ddb::ReadsThroughItsOwnClient(name)) {
          // Whatever its declared path, it reads what its table's metadata names (storage_access.hpp).
          RequireStorage(name + "()", StorageMode::kRead, std::nullopt, false, true);
          return;
        }
        if (!paths.empty()) RequireStorage(name + "()", StorageMode::kRead, paths);
        return;
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
    if (dml_target && table.get() == dml_target.get()) {
      // The row-locating scan under the statement's own UPDATE / DELETE target
      // needs no SELECT unless it reads columns (in WHERE or SET), as in Postgres.
      if (columns.empty()) return;
      // On a table with triggers the binder makes that scan read every column, to hand the
      // triggers their rows (triggers.hpp, "Carried columns"). A writer who holds SELECT on
      // all of them is checked as ever, below. One who does not still needs SELECT on what
      // the statement itself reads; the carried columns it lacks go to nothing but the
      // write operator, and whoever reads the rows after it answers for them
      // (RequireCarriedRows).
      if (const TriggerBody* base = BaseScannedBy(trigger_bodies_, get)) {
        std::vector<std::string> lacking;
        for (const auto& column : base->carried_columns) {
          if (!HoldsSelect(PathOf(*table), ObjectTypeOf(*table), column)) lacking.push_back(column);
        }
        if (!lacking.empty()) {
          if (!base->read_columns.empty()) {
            // Say why when it is a key's, an index's or a CHECK's column the writer lacks.
            for (const auto& column : base->told_columns) {
              if (!HoldsSelect(PathOf(*table), ObjectTypeOf(*table), column)) {
                carried_note_ =
                    " (the table has triggers: a write to it that reads no column needs no SELECT, except on the "
                    "columns of its keys and indexes, and an UPDATE's on those of a CHECK over several columns or "
                    "one that calls more than DuckDB's own functions, whose errors can tell their values)";
                break;
              }
            }
            Require(*table, Privilege::kSelect, base->read_columns);
            carried_note_.clear();
          }
          // Never after a reader of these rows was let through (in every plan DuckDB
          // makes the scan is checked first): then the columns are the writer's to hold.
          if (carried_rows_->read.count(base->root)) Require(*table, Privilege::kSelect, lacking);
          carried_rows_->waived[base->root] = std::move(lacking);
          return;
        }
      } else if (BaseWriting(trigger_bodies_, (*table))) {
        // A table with triggers whose write's columns could not be told apart: every column
        // the scan reads is a read, as before. Say why.
        carried_note_ = (*table).HasGeneratedColumns()
                            ? " (the table has triggers and generated columns: a write to it computes the generated "
                              "columns from the rows it changes, which takes SELECT on the table)"
                            : " (the table has triggers, and this statement's plan is not one in which the columns "
                              "only the triggers need can be told apart from the ones it reads: that takes SELECT on "
                              "them)";
        Require(*table, Privilege::kSelect, columns);
        carried_note_.clear();
        return;
      }
    }
    // DuckLake's own read of a table it is compacting or flushing (CheckDuckLakeRewrite): part
    // of the maintenance MAINTAIN ON CATALOG allows, which hands back counts, not rows. Only a
    // table of the catalog being rewritten, and only under DuckLake's own operator.
    if (!ducklake_rewrite_catalogs_.empty() &&
        ducklake_rewrite_catalogs_.contains(FoldIdentifier(compat::Str(table->ParentCatalog().GetName())))) {
      return;
    }
    Require(*table, Privilege::kSelect, columns);
  }

  // Would Require(SELECT on `column`) pass, for every one whose rights are in force?
  bool HoldsSelect(const ObjectPath& path, ObjectType type, const std::string& column) {
    if (!snapshot_.CatalogVisible(principal_, path.catalog)) return false;
    if (IsTempCatalog(path.catalog)) {
      if (in_owned_view_) return false;
      if (!principal_.catalog_rules) return true;
    }
    const AccessRequest request{path, type, Privilege::kSelect, {column}};
    for (const auto& rights : rights_) {
      if (!snapshot_.Check(rights.principal, request).allowed) return false;
    }
    return true;
  }

  // The rows a base write hands its triggers are read here (a transition table, OLD and
  // NEW), by the trigger body being checked or by the statement itself. For the carried
  // columns the write's scan was let off, somebody answers (triggers.hpp, "Who answers
  // for a row reader"): the administrator who made the trigger (nothing is asked), the
  // trigger's owner (SELECT, with the owner's rights: the ones in force in its body), or
  // else the writer, who is the one that lacks them. The first two only for a trigger of
  // the written table itself. (Its body does what its maker wrote: the names in it were
  // held against this session where the body was entered.)
  void RequireCarriedRows(const TriggerBody& base) {
    carried_rows_->read.insert(base.root);
    auto waived = carried_rows_->waived.find(base.root);
    if (waived == carried_rows_->waived.end()) return;
    const TriggerBody* reader = current_body_;
    const bool of_this_table =
        reader && !reader->base && IdentifiersEqual(reader->table.catalog, base.table.catalog) &&
        reader->table.schema_path.size() == base.table.schema_path.size() &&
        std::equal(reader->table.schema_path.begin(), reader->table.schema_path.end(), base.table.schema_path.begin(),
                   [](const std::string& a, const std::string& b) { return IdentifiersEqual(a, b); }) &&
        IdentifiersEqual(reader->table.object_name, base.table.object_name);
    if (of_this_table && reader->administrators) return;  // an administrator's trigger vouches for these reads
    if (of_this_table && reader->definer) {
      carried_note_ = " (the trigger's body reads the rows the statement changes: that takes SELECT on them, the "
                      "writer's or else the trigger owner's)";
      RequireObject(base.table, ObjectType::kTable, Privilege::kSelect, waived->second);
      carried_note_.clear();
      return;
    }
    carried_note_ = reader && !reader->base
                        ? " (" + reader->describe +
                              " reads the rows the statement changes, and nobody is on record as having made it "
                              "under access control: that takes the writer's SELECT on them)"
                        : " (the statement reads the rows it changes)";
    RequireObject(base.table, ObjectType::kTable, Privilege::kSelect, waived->second);
    carried_note_.clear();
    // The rights in force here hold what the write's scan was let off for lacking: they
    // are not the writer's, then (no plan DuckDB makes reads a statement's rows in the
    // body of another table's trigger). Nobody answers for the rows.
    ::hardcharger::authz::Deny(principal_, "the rows the statement on table " + base.table.ToString() +
                                               " changes are read where nobody answers for them; ask an administrator");
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
      // Describing a table lists its columns: only one the user may access (Chargewire
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
    if (!trigger_context_.empty()) ::hardcharger::authz::Deny(who, trigger_context_ + what);
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
      what += carried_note_;
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
      if (rights.principal.catalog_access_token && !rights.principal.scope) {
        Deny(principal_, CatalogAccessTokenRefusal(what, privilege, principal_.user_name));
      }
      Deny(principal_, what + " needs the " + SystemPrivilegeName(privilege) +
                           " privilege, which user " + principal_.user_name + " does not hold");
    }
  }

  // The statement reads or writes `paths` (as DuckDB bound them): each principal whose rights
  // apply needs the storage privilege there, and none of them may reach DuckLake's storage
  // (storage_access.hpp).
  void RequireStorage(const std::string& what, StorageMode mode, std::optional<std::vector<std::string>> paths,
                      bool directory = false, bool own_client = false) {
    if (paths) {
      for (const auto& path : *paths) {
        auto parsed = ParseStoragePath(path, {}, mode == StorageMode::kRead);
        NoteAccess(SystemPrivilegeName(StoragePrivilege(parsed.ok() ? parsed->kind : StorageKind::kFilesystem, mode)),
                   {});
      }
    }
    std::vector<Principal> principals;
    for (const auto& rights : rights_) principals.push_back(rights.principal);
    if (!storage_) storage_.emplace(context_);
    if (auto refusal = storage_->Refusal(snapshot_, principals, {what, mode, std::move(paths), directory, own_client},
                                         principal_.user_name)) {
      throw dd::PermissionException(*refusal);
    }
  }

  // The paths a file-reading table function's first argument names (a string or a list of
  // them), as bound; nullopt when it is anything else.
  static std::optional<std::vector<std::string>> PathsOf(const dd::LogicalGet& get) {
    if (get.parameters.empty()) return std::nullopt;
    return PathsOfValue(get.parameters[0]);
  }

  // The paths a bound argument names: a string, or a list of strings; nullopt otherwise.
  static std::optional<std::vector<std::string>> PathsOfValue(const dd::Value& value) {
    if (value.IsNull()) return std::nullopt;
    if (value.type().id() == dd::LogicalTypeId::VARCHAR) return std::vector<std::string>{value.ToString()};
    if (value.type().id() == dd::LogicalTypeId::LIST) {
      std::vector<std::string> paths;
      for (const auto& child : dd::ListValue::GetChildren(value)) {
        if (child.IsNull() || child.type().id() != dd::LogicalTypeId::VARCHAR) return std::nullopt;
        paths.push_back(child.ToString());
      }
      return paths;
    }
    return std::nullopt;
  }

  // A catalog_access principal's DuckLake call, as bound: the pre-bind gate checked the
  // calls the statement names itself (authz/ducklake_calls.hpp); this refuses any other
  // (in a view or macro, or where the gate's walk does not reach) and checks the bound
  // catalog against the rules again.
  void CheckDuckLakeCall(dd::LogicalGet& get, const std::string& name, DuckLakeAccess access) {
    if (access == DuckLakeAccess::kAdministrator) throw dd::PermissionException(DuckLakeAdministratorRefusal(name));
    if (access == DuckLakeAccess::kUnknown) throw dd::PermissionException(DuckLakeUnknownRefusal(name));
    if (view_depth_ > 0 || !creating_view_.empty()) {
      throw dd::PermissionException(DuckLakeWrappedRefusal(name, "a view"));
    }
    const bool literal = !get.parameters.empty() && !get.parameters[0].IsNull() &&
                         get.parameters[0].type().id() == dd::LogicalTypeId::VARCHAR;
    if (!literal) throw dd::PermissionException(DuckLakeComputedCatalogRefusal(name));
    std::string catalog = get.parameters[0].ToString();
    if (auto db = dd::DatabaseManager::Get(context_).GetDatabase(context_, dd::Identifier(catalog))) {
      catalog = compat::Str(db->GetName());
    }
    RequireDuckLakeAccess(name, catalog, access);
    if (access == DuckLakeAccess::kWrite) {
      if (auto readonly = ReadOnlyRefusal(context_)) throw dd::PermissionException(*readonly);
    }
    auto state = context_.registered_state->Get<RbacSessionState>(kRbacSessionStateName);
    if (!state || !state->DuckLakeCallChecked(name, FoldIdentifier(catalog))) {
      throw dd::PermissionException(DuckLakeUncheckedRefusal(name));
    }
    NoteAccess("EXECUTE " + name + "() on catalog " + catalog, {});
  }

  // DuckLake's own rewrite of a table's files (compaction, or flushing inlined rows to
  // Parquet): it reads the table and writes new files into its catalog's storage, through a
  // COPY TO whose path DuckLake takes from the catalog (no option a user may set moves it).
  // Allowed for a catalog_access principal only when the statement itself called one of the
  // functions that plan it on that catalog (checked against the rules before binding), and
  // the principal may write the catalog; the table reads below are checked as any read.
  void CheckDuckLakeRewrite(dd::LogicalOperator& op) {
    static const char* kRewriters[] = {"ducklake_merge_adjacent_files", "ducklake_rewrite_data_files",
                                       "ducklake_flush_inlined_data"};
    if (view_depth_ > 0 || !creating_view_.empty()) {
      throw dd::PermissionException(DuckLakeWrappedRefusal("a DuckLake maintenance function", "a view"));
    }
    std::set<std::string> catalogs;
    std::function<void(dd::LogicalOperator&)> collect = [&](dd::LogicalOperator& node) {
      if (node.type == OpType::LOGICAL_GET) {
        if (auto table = node.Cast<dd::LogicalGet>().GetTable()) {
          catalogs.insert(compat::Str(table->ParentCatalog().GetName()));
          // The row and masking policies would be applied to DuckLake's own read of the
          // table (policies.hpp), and the rewrite would then replace the table's files with
          // the filtered or masked rows, for everyone: refused, as any write into a
          // table with policies is.
          const ObjectPath path = PathOf(*table);
          if (!snapshot_.RowPoliciesOn(path).empty() || !snapshot_.ApplicableMasks(principal_, path).empty()) {
            Deny(principal_, "DuckLake's rewrite of the files of " + path.ToString() +
                                 ", which has row or masking policies, is not available under access control yet");
          }
        }
      }
      for (auto& child : node.children) {
        if (child) collect(*child);
      }
    };
    collect(op);
    if (catalogs.empty()) Deny(principal_, "extension operators are not available under access control");
    auto state = context_.registered_state->Get<RbacSessionState>(kRbacSessionStateName);
    if (auto readonly = ReadOnlyRefusal(context_)) throw dd::PermissionException(*readonly);
    for (const auto& catalog : catalogs) {
      RequireDuckLakeAccess("DuckLake's rewrite of its files", catalog, DuckLakeAccess::kWrite);
      const bool called = state && std::any_of(std::begin(kRewriters), std::end(kRewriters), [&](const char* f) {
                            return state->DuckLakeCallChecked(f, FoldIdentifier(catalog));
                          });
      if (!called) {
        throw dd::PermissionException(
            "Hardcharger blocked DuckLake's rewrite of files of catalog " + catalog +
            ": it runs only for a ducklake_merge_adjacent_files(), ducklake_rewrite_data_files() or "
            "ducklake_flush_inlined_data() call that the statement makes itself (not inside a macro)");
      }
      NoteAccess("DuckLake rewrite of files of catalog " + catalog, {});
    }
    for (auto& child : op.children) {
      if (!child) continue;
      // DuckLake's own write into its catalog's storage: what it copies is checked below. Its
      // scans of the catalog's tables are the maintenance itself (VisitGet): no SELECT on them.
      if (child->type == OpType::LOGICAL_COPY_TO_FILE) {
        const auto saved = ducklake_rewrite_catalogs_;
        for (const auto& catalog : catalogs) ducklake_rewrite_catalogs_.insert(FoldIdentifier(catalog));
        VisitChildren(*child, nullptr);
        ducklake_rewrite_catalogs_ = saved;
      } else {
        Visit(*child, nullptr);
      }
    }
  }

  // A DuckLake function's access to its catalog: the server's own catalogs (and, for a
  // store user, any it cannot see) read as missing. A catalog_access principal's rules
  // decide; a store user needs EXECUTE on the function and SELECT or MAINTAIN ON CATALOG.
  // `function`: the ducklake_* function (its EXECUTE is checked too), or what else runs DuckLake's
  // maintenance, named for the refusal ("CHECKPOINT", "DuckLake's rewrite of its files").
  void RequireDuckLakeAccess(const std::string& function, const std::string& catalog, DuckLakeAccess access) {
    const auto needed = access == DuckLakeAccess::kWrite ? CatalogAccessLevel::kWrite : CatalogAccessLevel::kRead;
    const bool servers = std::any_of(principal_.hidden_catalogs.begin(), principal_.hidden_catalogs.end(),
                                     [&](const std::string& c) { return IdentifiersEqual(c, catalog); });
    if (servers || !principal_.catalog_rules) RequireVisible(catalog);
    if (!principal_.catalog_rules) {
      if (function.starts_with("ducklake_") && !snapshot_.TableFunctionPathArguments(principal_, function)) {
        throw dd::PermissionException(DuckLakeExecuteRefusal(function));
      }
      if (!snapshot_.HoldsOnCatalog(principal_, DuckLakeCatalogPrivilege(access), catalog)) {
        throw dd::PermissionException(DuckLakeCatalogPrivilegeRefusal(
            function.starts_with("ducklake_") ? function + "()" : function, catalog, access, principal_.user_name));
      }
      return;
    }
    const auto level = snapshot_.CatalogVisible(principal_, catalog) ? CatalogRuleLevel(*principal_.catalog_rules, catalog)
                                                                     : CatalogAccessLevel::kNone;
    if (level == CatalogAccessLevel::kNone || (needed == CatalogAccessLevel::kWrite && level != needed)) {
      DenyByCatalogAccess(catalog, needed);
    }
  }

  // CHECKPOINT of a DuckLake catalog runs all of DuckLake's maintenance on it (flush, expire,
  // compact, rewrite, clean up), on a connection of its own, outside the checks on this
  // statement: it needs what those functions need. A catalog_access principal: write access to
  // the catalog. A store user: MAINTAIN ON CATALOG, beside the CHECKPOINT privilege. Other
  // catalogs' checkpoints are unchanged (the CHECKPOINT privilege alone).
  void RequireDuckLakeCheckpoint(dd::LogicalGet& get, const std::string& name) {
    if (principal_.superuser) return;
    std::string catalog;
    if (get.parameters.empty()) {
      catalog = compat::Str(dd::DatabaseManager::GetDefaultDatabase(context_));
    } else if (!get.parameters[0].IsNull() && get.parameters[0].type().id() == dd::LogicalTypeId::VARCHAR) {
      catalog = get.parameters[0].ToString();
    } else {
      Deny(principal_, name + "() must name its database as a string");
    }
    auto db = dd::DatabaseManager::Get(context_).GetDatabase(context_, dd::Identifier(catalog));
    if (!db || compat::Str(db->GetCatalog().GetCatalogType()) != "ducklake") return;
    catalog = compat::Str(db->GetName());
    RequireDuckLakeAccess("CHECKPOINT", catalog, DuckLakeAccess::kWrite);
    if (auto readonly = ReadOnlyRefusal(context_)) throw dd::PermissionException(*readonly);
    NoteAccess("CHECKPOINT of DuckLake catalog " + catalog, {});
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
    ++view_depth_;
    struct Leave {
      int& depth;
      ~Leave() { --depth; }
    } leave{view_depth_};
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
      if (type != ObjectType::kSchema) {
        CheckDependents({*existing}, false, Describe(*existing), "replace");
      }
    }
    RequireVisible(target.catalog);
    AccessRequest request{target, type, privilege, {}};
    if (principal_.catalog_rules) RequireRuleWrite(target.catalog);
    if (!snapshot_.Check(principal_, request).allowed) {
      // A nested schema is created in its parent: CREATE SCHEMA on the parent (ON SCHEMA
      // or ON CATALOG), or owning the parent.
      std::string where;
      if (type != ObjectType::kSchema) {
        where = "schema " + ContainerOf(target);
      } else if (target.schema_path.size() > 1) {
        ObjectPath parent = target;
        parent.schema_path.pop_back();
        where = "schema " + parent.ToString() + " (its parent)";
      } else {
        where = "catalog " + target.catalog;
      }
      Deny(principal_, "user " + principal_.user_name + " has no " + PrivilegeName(privilege) +
                           " privilege on " + where);
    }
    if (type == ObjectType::kTable || type == ObjectType::kView) {
      if (auto refusal = TriggerNameRefusal(context_, target)) Deny(principal_, *refusal);
    }
    ownership_.push_back({PendingOwnership::Kind::kCreate, target, type, {}, false, {}, !existing});
    return true;
  }

  // CREATE [OR REPLACE] MACRO by a user other than the administrator. Temporary macros
  // never: a session's macro is found before every other function, also inside a view
  // that reads with its owner's rights, so it would change what that view computes.
  // Otherwise the CREATE MACRO privilege on the macro's schema (granted ON SCHEMA or ON
  // CATALOG, or as the schema's owner), DROP MACRO as well to replace one, and a name
  // that changes nothing else's meaning (MacroNameRefusal). Macros have no owner: what
  // they read is checked, as part of each statement that calls them, with its rights.
  void CheckCreateMacro(dd::CreateInfo& info) {
    const auto parts = PartsOf(info);
    if (info.temporary || (!parts.empty() && IsTempCatalog(parts[0]))) {
      Deny(principal_,
           "creating temporary macros needs an administrator: a session's macro is found before every other "
           "function, also inside views that read with their owners' rights; create it in a schema instead");
    }
    // A catalog_access principal holds no grants, CREATE MACRO among them.
    if (principal_.catalog_rules) {
      Deny(principal_, "creating macros needs an administrator without catalog_access rules, or a user whose "
                       "roles hold CREATE MACRO on the schema");
    }
    if (parts.size() < 3) Deny(principal_, "the new macro's schema could not be determined");
    ObjectPath macro;
    macro.catalog = parts[0];
    macro.schema_path.assign(parts.begin() + 1, parts.end() - 1);
    macro.object_name = parts.back();
    auto existing = LookUp(context_, info.type, parts);
    if (existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT) return;
    RequireMacroPrivilege(macro, Privilege::kCreateMacro, "create");
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      RequireMacroPrivilege(macro, Privilege::kDropMacro, "replace");
      // Replacement drops the old entry first. Report recorded blockers with the
      // same visibility rules as DROP before checking names the replacement could shadow.
      CheckDependents({*existing}, false, Describe(*existing), "replace");
    }
    if (auto refusal = MacroNameRefusal(context_, snapshot_, macro)) Deny(principal_, *refusal);
  }

  // CREATE [OR REPLACE] TYPE by a user other than the administrator: the CREATE TYPE
  // privilege on the type's schema (granted ON SCHEMA or ON CATALOG, or as the schema's
  // owner), DROP TYPE as well to replace one, and a name that changes nothing else's
  // meaning (TypeNameRefusal: no built-in or extension type's name, none a view, macro,
  // trigger, policy, column default or check constraint names). Temporary types never: a
  // session's type is found before every other type, also inside views that read with
  // their owners' rights. Types have no owner: a type is a definition, it reads nothing.
  void CheckCreateType(dd::LogicalOperator& op) {
    auto& create = op.Cast<dd::LogicalCreate>();
    auto& info = *create.info;
    const auto parts = PartsOf(info);
    if (info.temporary || (!parts.empty() && IsTempCatalog(parts[0]))) {
      // PIVOT without an IN list creates its own temporary enum of the ON column's values:
      // not refused as a temporary type (its name shadows nothing), but its query is checked
      // as the user's own SELECT (by the caller, which visits this operator's children). A
      // statement the user writes cannot use that name (CheckBeforeBind).
      if (!parts.empty() && parts.back().rfind("__pivot_enum_", 0) == 0) return;
      Deny(principal_,
           "creating temporary types needs an administrator: a session's type is found before every other type, "
           "also inside views that read with their owners' rights; create it in a schema instead");
    }
    if (principal_.catalog_rules) {
      Deny(principal_, "creating types needs an administrator without catalog_access rules, or a user whose "
                       "roles hold CREATE TYPE on the schema");
    }
    if (parts.size() < 3) Deny(principal_, "the new type's schema could not be determined");
    ObjectPath type;
    type.catalog = parts[0];
    type.schema_path.assign(parts.begin() + 1, parts.end() - 1);
    type.object_name = parts.back();
    auto existing = LookUp(context_, dd::CatalogType::TYPE_ENTRY, parts);
    if (existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT) return;
    RequireSchemaPrivilege(type, Privilege::kCreateType, "create type");
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      RequireSchemaPrivilege(type, Privilege::kDropType, "replace type");
      if (auto refusal = TypeNameRefusal(context_, snapshot_, type, "replace")) Deny(principal_, *refusal);
      CheckDependents({*existing}, false, Describe(*existing), "replace");
    } else if (auto refusal = TypeNameRefusal(context_, snapshot_, type, "create")) {
      Deny(principal_, *refusal);
    }
  }

  // CREATE [OR REPLACE] TRIGGER by a user other than the administrator: the trigger's
  // table must be the user's own (its body runs inside everyone's writes of the table),
  // and CREATE TRIGGER on the table's schema (granted ON SCHEMA or ON CATALOG, or as the
  // schema's owner); replacing a trigger needs the right to drop it. The user becomes the
  // trigger's owner, and its body runs with the owner's rights (triggers.hpp). What the
  // body calls was checked by the gate as the creator could run it; what it reads and
  // writes is checked, as the owner, each time it fires.
  void CheckCreateTrigger(dd::LogicalCreate& create) {
    auto& info = create.info->Cast<dd::CreateTriggerInfo>();
    const auto parts = PartsOf(info);
    if (info.temporary || (!parts.empty() && IsTempCatalog(parts[0]))) {
      // On a session's own temporary table: session-local, like the table.
      if (principal_.catalog_rules) RequireRuleWrite(TEMP_CATALOG);
      return;
    }
    if (principal_.catalog_rules) {
      Deny(principal_, "creating triggers needs an administrator without catalog_access rules, or a user whose "
                       "roles hold CREATE TRIGGER on the schema");
    }
    if (parts.size() < 3 || !info.base_table) Deny(principal_, "the new trigger's table could not be determined");
    ObjectPath trigger;
    trigger.catalog = parts[0];
    trigger.schema_path.assign(parts.begin() + 1, parts.end() - 1);
    trigger.object_name = parts.back();
    // The binder put the trigger in its table's schema.
    std::vector<std::string> table_parts(parts.begin(), parts.end() - 1);
    table_parts.push_back(compat::Str(info.base_table->GetQualifiedName().Name()));
    auto table = LookUp(context_, dd::CatalogType::TABLE_ENTRY, table_parts);
    if (!table || table->type != dd::CatalogType::TABLE_ENTRY) {
      Deny(principal_, "the table of trigger " + QuoteIdentifierIfNeeded(trigger.object_name) + " could not be found");
    }
    const ObjectPath table_path = PathOf(*table);
    auto existing = LookUpTrigger(context_, table->Cast<dd::TableCatalogEntry>(), trigger.object_name);
    if (existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT) return;
    RequireVisible(table_path.catalog);
    const OwnerRecord* owner = snapshot_.FindOwner(table_path);
    if (!owner || owner->type != ObjectType::kTable || !snapshot_.IsOwner(principal_, *owner)) {
      Deny(principal_, "user " + principal_.user_name + " may not create a trigger on table " + table_path.ToString() +
                           ": a trigger's body runs inside every write of its table, so only the table's owner may "
                           "(with CREATE TRIGGER on the schema or its catalog, or owning the schema)");
    }
    RequireSchemaPrivilege(trigger, Privilege::kCreateTrigger, "create trigger");
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      RequireTriggerDrop(table_path, trigger.object_name, "replace");
    }
    // The trigger is stored with the names in its action given their schema, as this session
    // resolves them, and its owner record is of that definition.
    if (auto problem = QualifyTriggerNames(context_, table_path, info); !problem.empty()) Deny(principal_, problem);
    // And the functions and types it calls are what its table's name scope will find.
    if (auto problem = TriggerStoredNamesProblem(context_, table_path, info); !problem.empty()) Deny(principal_, problem);
    ownership_.push_back({PendingOwnership::Kind::kCreate, TriggerOwnerPath(table_path, trigger.object_name),
                          ObjectType::kTrigger, TriggerDefinitionHash(info)});
  }

  // Dropping (or replacing) a trigger: its owner, or DROP TRIGGER on its schema.
  void RequireTriggerDrop(const ObjectPath& table, const std::string& trigger_name, const std::string& verb) {
    const ObjectPath trigger = TriggerOwnerPath(table, trigger_name);
    RequireVisible(trigger.catalog);
    if (principal_.catalog_rules) return RequireRuleWrite(trigger.catalog);
    if (const OwnerRecord* owner = snapshot_.FindTriggerOwner(trigger)) {
      if (snapshot_.IsOwner(principal_, *owner)) return;
    }
    ObjectPath schema = table;
    schema.object_name.clear();
    if (!snapshot_.Check(principal_, AccessRequest{schema, ObjectType::kSchema, Privilege::kDropTrigger, {}}).allowed) {
      Deny(principal_, "user " + principal_.user_name + " may not " + verb + " trigger " +
                           QuoteIdentifierIfNeeded(trigger_name) + " on table " + table.ToString() +
                           " (only its owner, or a role granted DROP TRIGGER on schema " + schema.ToString() +
                           " or its catalog, or owning the schema)");
    }
  }

  // A trigger body a user owns: a checker of its own, with the owner's rights, visits it
  // (entering the body's root operator itself, which this checker then skips).
  void CheckAsDefiner(const TriggerBody& body, dd::LogicalOperator& plan) {
    const auto view_columns = ViewColumnsUsed(plan);
    PlanChecker checker(context_, snapshot_, *body.definer, view_columns, trigger_bodies_);
    checker.trigger_context_ = body.describe + " cannot run, because its owner " + body.definer_name +
                               " may not do what its body does: ";
    checker.trigger_body_entered_ = true;
    checker.check_trigger_names_ = check_trigger_names_;
    checker.carried_rows_ = carried_rows_;
    checker.Visit(plan);
    for (auto& access : checker.accesses_) accesses_.insert(access);
    for (auto& object : checker.objects_) objects_.push_back(std::move(object));
  }

  // CREATE MACRO and DROP MACRO are held on the macro's schema: granted ON SCHEMA or ON
  // CATALOG, or as the schema's owner (asked of the schema itself, so that owning a table
  // of the same name grants nothing). A catalog_access principal (which creates none)
  // drops one with write access to the catalog, as before.
  void RequireMacroPrivilege(const ObjectPath& macro, Privilege privilege, const std::string& verb) {
    RequireSchemaPrivilege(macro, privilege, verb + " macro");
  }

  // A privilege held on an object's schema (macros, sequences: objects without owner
  // records), asked of the schema itself.
  void RequireSchemaPrivilege(const ObjectPath& object, Privilege privilege, const std::string& action) {
    RequireVisible(object.catalog);
    if (principal_.catalog_rules) return RequireRuleWrite(object.catalog);
    ObjectPath schema = object;
    schema.object_name.clear();
    if (!snapshot_.Check(principal_, AccessRequest{schema, ObjectType::kSchema, privilege, {}}).allowed) {
      Deny(principal_, "user " + principal_.user_name + " may not " + action + " " + object.ToString() +
                           " (it needs " + PrivilegeName(privilege) + " on schema " + schema.ToString() +
                           " or its catalog, or to own the schema)");
    }
  }

  // CREATE [OR REPLACE] SEQUENCE in a schema: CREATE SEQUENCE there, DROP SEQUENCE as
  // well to replace one, and a name no table default, view or macro calls nextval() /
  // currval() / setval() by (it would hand out that sequence's numbers instead).
  void CheckCreateSequence(dd::CreateInfo& info) {
    const auto parts = PartsOf(info);
    if (parts.size() < 3) Deny(principal_, "the new sequence's schema could not be determined");
    ObjectPath sequence;
    sequence.catalog = parts[0];
    sequence.schema_path.assign(parts.begin() + 1, parts.end() - 1);
    sequence.object_name = parts.back();
    auto existing = LookUp(context_, dd::CatalogType::SEQUENCE_ENTRY, parts);
    if (existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT) return;
    RequireSchemaPrivilege(sequence, Privilege::kCreateSequence, "create sequence");
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      RequireSchemaPrivilege(sequence, Privilege::kDropSequence, "replace sequence");
    }
    if (auto refusal = SequenceNameRefusal(context_, sequence)) Deny(principal_, *refusal);
    if (existing && info.on_conflict == dd::OnCreateConflict::REPLACE_ON_CONFLICT) {
      CheckDependents({*existing}, false, Describe(*existing), "replace");
    }
  }

  // The sequences an INSERT, UPDATE or MERGE target's own column defaults advance, as
  // DuckDB would find them (the name as written, or in the table's schema or catalog).
  // DuckDB moves a default's bound expression into the plan, so they are read from the
  // table's definition; a name that can't be read covers nothing (fail closed).
  void CollectDefaultSequences(dd::TableCatalogEntry& table) {
    const std::string catalog = compat::Str(table.ParentCatalog().GetName());
    const std::string schema = compat::Str(table.ParentSchema().name);
    for (const auto& column : table.GetColumns().Logical()) {
      if (!column.HasDefaultValue()) continue;
      auto copy = column.DefaultValue().Copy();
      ForEachSequenceCall(*copy, [&](const std::string& function, const std::optional<std::string>& name) {
        if (function != "nextval" || !name) return;
        std::vector<std::string> parts;
        try {
          for (const auto& part : dd::QualifiedName::ParseComponents(*name)) parts.push_back(compat::Str(part));
        } catch (...) {
          return;
        }
        std::vector<std::vector<std::string>> candidates{parts};
        if (parts.size() == 1) {
          candidates.push_back({catalog, schema, parts[0]});
          candidates.push_back({catalog, DEFAULT_SCHEMA, parts[0]});
        } else if (parts.size() == 2) {
          candidates.push_back({catalog, parts[0], parts[1]});
        }
        for (const auto& candidate : candidates) {
          if (auto entry = LookUp(context_, dd::CatalogType::SEQUENCE_ENTRY, candidate)) {
            default_sequences_.push_back(entry.get());
          }
        }
      });
    }
  }

  // USAGE on a sequence (nextval, currval), with the rights the read is checked with: the
  // caller's, or a view's owner's inside a view.
  void RequireSequenceUsage(const ObjectPath& sequence) {
    RequireVisible(sequence.catalog);
    AccessRequest request{sequence, ObjectType::kSequence, Privilege::kUsage, {}};
    NoteAccess("USAGE on sequence " + sequence.ToString(), {});
    for (const auto& rights : rights_) {
      if (snapshot_.Check(rights.principal, request).allowed) continue;
      if (!rights.owner_of_view.empty()) {
        Deny(principal_, "the view " + rights.owner_of_view +
                             " cannot be read, because its owner lacks a privilege on an object the view reads");
      }
      Deny(principal_, "user " + principal_.user_name + " has no USAGE privilege on sequence " + sequence.ToString());
    }
  }

  // A column default written by CREATE TABLE or ALTER TABLE: each sequence it names needs
  // USAGE, so that a table can't be made to advance a sequence its creator may not use.
  void CheckDefaultSequences(dd::ParsedExpression& expression) {
    ForEachSequenceCall(expression, [&](const std::string& function, const std::optional<std::string>& name) {
      if (!name) Deny(principal_, "a column default must name its sequence by a constant: " + function + "('name')");
      std::vector<std::string> parts;
      try {
        for (const auto& part : dd::QualifiedName::ParseComponents(*name)) parts.push_back(compat::Str(part));
      } catch (...) {
        Deny(principal_, "the sequence name in a column default could not be read: " + *name);
      }
      auto entry = LookUp(context_, dd::CatalogType::SEQUENCE_ENTRY, parts);
      if (!entry) return;  // DuckDB reports the missing sequence
      const ObjectPath path = PathOf(*entry);
      if (IsTempCatalog(path.catalog)) return;
      if (function == "setval") Deny(principal_, "a column default may not call setval()");
      RequireSequenceUsage(path);
    });
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
    // And the view's name scope (name_scope.hpp): what its functions and types will be for
    // every reader. Bound that way here, so a name the scope does not find is said now.
    if (!info.temporary && !IsTempCatalog(compat::Str(catalog.GetName()))) {
      binder->EntryRetriever().SetNameScope(NameScopeIn(schema, "view", compat::Str(info.GetViewName())));
      ObjectPath in = PathOfEntry(schema);
      const std::string what = "view " + in.ToString() + "." + QuoteIdentifierIfNeeded(compat::Str(info.GetViewName()));
      if (auto problem = StoredNamesProblem(context_, in.catalog, in.schema_path, what, info.query->node.get(), {});
          !problem.empty()) {
        Deny(principal_, problem);
      }
    }
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
    PlanChecker body(context_, snapshot_, principal_, view_columns, kNoTriggerBodies);
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

  // The table named `table_name` in the very schema `entry` lives in (an index and a trigger
  // live in their table's schema, nested ones included): found in that schema object itself,
  // never by a name path, so a top-level schema named like a nested one's last part is never
  // taken for it.
  dd::optional_ptr<dd::CatalogEntry> TableInSchemaOf(dd::CatalogEntry& entry, const std::string& table_name) {
    if (table_name.empty()) return nullptr;
    try {
      auto& schema = entry.ParentSchema();
      auto transaction = schema.ParentCatalog().GetCatalogTransaction(context_);
      auto table = schema.GetEntry(transaction, dd::CatalogType::TABLE_ENTRY, dd::Identifier(table_name));
      if (!table || table->type != dd::CatalogType::TABLE_ENTRY) return nullptr;
      return table;
    } catch (const std::exception&) {
      return nullptr;
    }
  }

  // The table a trigger is on (DuckDB puts a trigger in its table's schema).
  dd::optional_ptr<dd::CatalogEntry> TriggerTable(dd::CatalogEntry& trigger) {
    auto& entry = trigger.Cast<dd::TriggerCatalogEntry>();
    if (!entry.base_table) return nullptr;
    auto parts = NameParts(entry.base_table->GetQualifiedName());
    if (parts.empty()) return nullptr;
    return TableInSchemaOf(trigger, parts.back());
  }

  // The table an index is on (an index lives in its table's schema).
  dd::optional_ptr<dd::CatalogEntry> IndexTable(dd::CatalogEntry& entry) {
    return TableInSchemaOf(entry, compat::Str(entry.Cast<dd::IndexCatalogEntry>().GetTableName()));
  }

  // What the principal sees of a catalog entry, as the metadata views list it (Chargewire
  // §9.4): a table or view (an index or trigger with its table) when it holds a privilege on
  // it, a sequence when it may use or drop it, a type or macro with its schema. A refusal
  // names only what this says the principal sees.
  bool EntryListed(dd::CatalogEntry& entry, const ObjectPath& path) {
    if (principal_.superuser) return true;
    switch (entry.type) {
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY:
        return snapshot_.ObjectListed(principal_, path);
      case dd::CatalogType::INDEX_ENTRY: {
        auto table = IndexTable(entry);
        return table && snapshot_.ObjectListed(principal_, PathOf(*table));
      }
      case dd::CatalogType::TRIGGER_ENTRY: {
        auto table = TriggerTable(entry);
        return table && snapshot_.ObjectListed(principal_, PathOf(*table));
      }
      case dd::CatalogType::SEQUENCE_ENTRY:
        return snapshot_.SequenceListed(principal_, path);
      case dd::CatalogType::SCHEMA_ENTRY:
        return snapshot_.SchemaListed(principal_, path);
      case dd::CatalogType::TYPE_ENTRY:
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY: {
        ObjectPath schema = path;
        schema.object_name.clear();
        return snapshot_.SchemaListed(principal_, schema);
      }
      default:
        return false;
    }
  }

  static std::string EntryWord(const dd::CatalogEntry& entry) {
    switch (entry.type) {
      case dd::CatalogType::VIEW_ENTRY: return "view";
      case dd::CatalogType::INDEX_ENTRY: return "index";
      case dd::CatalogType::TRIGGER_ENTRY: return "trigger";
      case dd::CatalogType::SEQUENCE_ENTRY: return "sequence";
      case dd::CatalogType::SCHEMA_ENTRY: return "schema";
      case dd::CatalogType::TYPE_ENTRY: return "type";
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY: return "macro";
      default: return "table";
    }
  }

  // An entry as a message names it: "table c.s.t", "trigger g on table c.s.t".
  std::string Describe(dd::CatalogEntry& entry) {
    if (entry.type == dd::CatalogType::TRIGGER_ENTRY) {
      auto table = TriggerTable(entry);
      return "trigger " + QuoteIdentifierIfNeeded(compat::Str(entry.name)) +
             (table ? " on table " + PathOf(*table).ToString() : std::string());
    }
    return EntryWord(entry) + " " + PathOf(entry).ToString();
  }

  // Whether the principal may drop the entry, without saying why: for one it cannot see.
  bool MayDropQuietly(dd::CatalogEntry& entry, const ObjectPath& path) {
    if (principal_.superuser) return true;
    if (IsTempCatalog(path.catalog)) return true;
    if (!snapshot_.CatalogVisible(principal_, path.catalog)) return false;
    if (principal_.catalog_rules) {
      return CatalogRuleLevel(*principal_.catalog_rules, path.catalog) == CatalogAccessLevel::kWrite;
    }
    auto on_schema = [&](Privilege privilege) {
      ObjectPath schema = path;
      schema.object_name.clear();
      return snapshot_.Check(principal_, AccessRequest{schema, ObjectType::kSchema, privilege, {}}).allowed;
    };
    switch (entry.type) {
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY:
      case dd::CatalogType::SCHEMA_ENTRY: {
        const ObjectType type = ObjectTypeOf(entry);
        const Privilege privilege = type == ObjectType::kView     ? Privilege::kDropView
                                    : type == ObjectType::kSchema ? Privilege::kDropSchema
                                                                  : Privilege::kDropTable;
        return snapshot_.Check(principal_, AccessRequest{path, type, privilege, {}}).allowed;
      }
      case dd::CatalogType::TRIGGER_ENTRY: {
        auto table = TriggerTable(entry);
        if (!table) return false;
        const ObjectPath table_path = PathOf(*table);
        if (const OwnerRecord* owner =
                snapshot_.FindTriggerOwner(TriggerOwnerPath(table_path, compat::Str(entry.name)))) {
          if (snapshot_.IsOwner(principal_, *owner)) return true;
        }
        ObjectPath schema = table_path;
        schema.object_name.clear();
        return snapshot_.Check(principal_, AccessRequest{schema, ObjectType::kSchema, Privilege::kDropTrigger, {}})
            .allowed;
      }
      // DROP INDEX: the administrator's (a catalog_access principal's with write, above).
      case dd::CatalogType::INDEX_ENTRY: return false;
      case dd::CatalogType::SEQUENCE_ENTRY: return on_schema(Privilege::kDropSequence);
      case dd::CatalogType::TYPE_ENTRY: return on_schema(Privilege::kDropType);
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY: return on_schema(Privilege::kDropMacro);
      default: return false;
    }
  }

  // Dropping an entry the statement takes with what it names (a cascade's dependent, a
  // sequence a dropped table owns, a nested schema): the right to drop it, as if named.
  // One the principal cannot see is refused without its name (`what`: the statement, said
  // from what the principal named).
  void RequireDropOfTaken(dd::CatalogEntry& entry, const std::string& what) {
    const ObjectPath path = PathOf(entry);
    if (!EntryListed(entry, path)) {
      if (!MayDropQuietly(entry, path)) {
        Deny(principal_, what + " would also drop objects that depend on it, which user " + principal_.user_name +
                             " may not drop; ask an administrator");
      }
      if (entry.type == dd::CatalogType::SEQUENCE_ENTRY) {
        ownership_.push_back({PendingOwnership::Kind::kDrop, path, ObjectType::kSequence});
      } else if (entry.type == dd::CatalogType::TRIGGER_ENTRY) {
        if (auto table = TriggerTable(entry)) {
          ownership_.push_back({PendingOwnership::Kind::kDrop,
                                TriggerOwnerPath(PathOf(*table), compat::Str(entry.name)), ObjectType::kTrigger});
        }
      } else if (entry.type == dd::CatalogType::TABLE_ENTRY || entry.type == dd::CatalogType::VIEW_ENTRY ||
                 entry.type == dd::CatalogType::SCHEMA_ENTRY) {
        if (!IsTempCatalog(path.catalog)) ownership_.push_back({PendingOwnership::Kind::kDrop, path, ObjectTypeOf(entry)});
      }
      return;
    }
    switch (entry.type) {
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY:
      case dd::CatalogType::SCHEMA_ENTRY:
        CheckDropOf(entry);
        break;
      case dd::CatalogType::INDEX_ENTRY:
        // Taken without its table (a macro its expression calls is dropped): as DROP INDEX, the
        // administrator's, or a catalog_access principal's with write access to the catalog.
        if (IsTempCatalog(path.catalog)) break;
        if (principal_.catalog_rules) {
          RequireRuleWrite(path.catalog);
          break;
        }
        Deny(principal_, what + " would also drop " + Describe(entry) +
                             ", which only an administrator may drop (as DROP INDEX)");
      case dd::CatalogType::TRIGGER_ENTRY: {
        auto table = TriggerTable(entry);
        if (!table) Deny(principal_, what + " would also drop " + Describe(entry) + ", whose table was not found");
        const ObjectPath table_path = PathOf(*table);
        if (IsTempCatalog(table_path.catalog)) break;
        RequireTriggerDrop(table_path, compat::Str(entry.name), "drop");
        ownership_.push_back({PendingOwnership::Kind::kDrop, TriggerOwnerPath(table_path, compat::Str(entry.name)),
                              ObjectType::kTrigger});
        break;
      }
      case dd::CatalogType::SEQUENCE_ENTRY:
        RequireSchemaPrivilege(path, Privilege::kDropSequence, "drop sequence");
        ownership_.push_back({PendingOwnership::Kind::kDrop, path, ObjectType::kSequence});
        break;
      case dd::CatalogType::TYPE_ENTRY:
        RequireSchemaPrivilege(path, Privilege::kDropType, "drop type");
        break;
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY:
        RequireMacroPrivilege(path, Privilege::kDropMacro, "drop");
        break;
      default:
        Deny(principal_, what + " would also drop " + Describe(entry) +
                             ", which only an administrator may drop that way; drop it first");
    }
  }

  // DuckDB's refusal of a DROP (or of a CREATE OR REPLACE, which drops the old entry first)
  // that something depends on names every dependent, also ones the principal cannot see.
  // This one says that other objects depend on it and names only those the principal sees.
  [[noreturn]] void RefuseDependents(const std::string& verb, const std::string& what,
                                     const std::vector<dd::CatalogEntry*>& dependents, bool cascade = false) {
    std::set<std::string> named;
    for (auto* dependent : dependents) {
      if (!EntryListed(*dependent, PathOf(*dependent))) continue;
      named.insert(Describe(*dependent));
    }
    std::string seen;
    for (const auto& name : named) seen += (seen.empty() ? "" : ", ") + name;
    throw dd::DependencyException("Cannot " + verb + " " + what + ": other objects depend on it" +
                                  (seen.empty() ? std::string() : " (" + seen + ")") + ". Drop them first" +
                                  (verb == "drop" && !cascade ? ", or use DROP ... CASCADE" : ""));
  }

  // What a DROP (or a CREATE OR REPLACE) takes with what it names, or is blocked by, in any
  // schema, as DuckDB's DependencyManager::DropObject decides it, applied again to each entry
  // it takes:
  // - a dependent (a table whose column has a user type, recorded again since DuckDB 2.0.1;
  //   whose default calls a sequence; a trigger on a table or writing to it; for a sequence,
  //   the table that owns it) is taken with CASCADE; without, it blocks the drop if it is a
  //   blocking or an owning one, and goes with it otherwise (a trigger or index with its table);
  // - what the entry owns (a sequence OWNED BY a dropped table) is taken, with or without
  //   CASCADE, and its own dependents follow the same rules;
  // - a table that references a dropped table by foreign key is never taken: DuckDB refuses.
  // A blocked drop is refused naming only the dependents the principal sees; each object
  // taken needs the right to drop it, as if named (with CASCADE, everything taken; without,
  // what a dropped object owns). `targets`: what the statement names (a schema with the
  // schemas nested in it); `what`: the target as the refusal says it; `verb`: "drop" or
  // "replace".
  void CheckDependents(const std::vector<dd::reference<dd::CatalogEntry>>& targets, bool cascade,
                       const std::string& what, const std::string& verb = "drop") {
    if (targets.empty() || principal_.superuser) return;
    auto& catalog = targets.front().get().ParentCatalog();
    auto manager = catalog.GetDependencyManager();
    if (!manager) return;  // a catalog without DuckDB's dependency records cascades nothing through them
    enum class Reason { kNamed, kDependent, kOwned };
    std::unordered_map<const dd::CatalogEntry*, Reason> taken_by;
    std::vector<dd::CatalogEntry*> order;
    std::unordered_set<const dd::SchemaCatalogEntry*> schemas;
    for (auto& target : targets) {
      taken_by.emplace(&target.get(), Reason::kNamed);
      if (target.get().type == dd::CatalogType::SCHEMA_ENTRY) {
        schemas.insert(&target.get().Cast<dd::SchemaCatalogEntry>());
      }
    }
    struct Edge {
      dd::CatalogEntry* object;
      dd::CatalogEntry* dependent;
      bool blocking;
      bool owned_by;  // `dependent` owns `object` (ALTER SEQUENCE object OWNED BY dependent)
    };
    std::vector<Edge> edges;
    try {
      manager->Scan(context_, [&](dd::CatalogEntry& object, dd::CatalogEntry& dependent,
                                  const dd::DependencyDependentFlags& flags) {
        edges.push_back({&object, &dependent, flags.IsBlocking(), flags.IsOwnedBy()});
      });
    } catch (const std::exception& e) {
      Log(LogLevel::kWarning, "access control could not list what depends on an object",
          {{"user", principal_.user_name}, {"object", what}, {"error", e.what()}});
      throw dd::CatalogException("Hardcharger could not list what depends on " + what +
                                 ", so the statement was not run (the server log says why)");
    }
    auto in_dropped_schema = [&](const dd::CatalogEntry& entry) {
      if (schemas.empty() || entry.type == dd::CatalogType::SCHEMA_ENTRY) return false;
      try {
        return schemas.contains(&entry.ParentSchema());
      } catch (const std::exception&) {
        return false;
      }
    };
    auto taken = [&](const dd::CatalogEntry* entry) {
      return taken_by.contains(entry) || in_dropped_schema(*entry);
    };
    auto take = [&](dd::CatalogEntry* entry, Reason reason) {
      taken_by.emplace(entry, reason);
      order.push_back(entry);
    };
    std::vector<dd::CatalogEntry*> blockers;
    for (bool grew = true; grew;) {
      grew = false;
      for (auto& edge : edges) {
        if (edge.owned_by && taken(edge.dependent) && !taken(edge.object)) {
          take(edge.object, Reason::kOwned);
          grew = true;
        }
        if (!taken(edge.object) || taken(edge.dependent)) continue;
        const bool foreign_key = edge.blocking && edge.object->type == dd::CatalogType::TABLE_ENTRY &&
                                 edge.dependent->type == dd::CatalogType::TABLE_ENTRY;
        if (foreign_key || (!cascade && (edge.blocking || edge.owned_by))) {
          blockers.push_back(edge.dependent);
          continue;
        }
        take(edge.dependent, Reason::kDependent);
        grew = true;
      }
    }
    std::vector<dd::CatalogEntry*> blocking;
    for (auto* blocker : blockers) {
      if (!taken(blocker)) blocking.push_back(blocker);
    }
    if (!blocking.empty()) RefuseDependents(verb, what, blocking, cascade);
    // What the principal sees first, by name, so that a refusal names the first of them;
    // then what it does not see, which no refusal names.
    std::vector<std::pair<std::string, dd::CatalogEntry*>> seen;
    std::vector<dd::CatalogEntry*> unseen;
    for (auto* entry : order) {
      // An index or trigger whose own table the statement takes goes with it, which the drop
      // of the table covers. Anything else taken, also an index or trigger whose table stays
      // (taken because a macro its expression calls, or a table its body writes, is dropped),
      // needs the right to drop it.
      if (entry->type == dd::CatalogType::INDEX_ENTRY) {
        auto table = IndexTable(*entry);
        if (table && taken(table.get())) continue;
      }
      if (entry->type == dd::CatalogType::TRIGGER_ENTRY) {
        auto table = TriggerTable(*entry);
        if (table && taken(table.get())) continue;
      }
      const ObjectPath path = PathOf(*entry);
      if (EntryListed(*entry, path)) {
        seen.emplace_back(Describe(*entry), entry);
      } else {
        unseen.push_back(entry);
      }
    }
    std::sort(seen.begin(), seen.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
    const std::string statement = (verb == "drop" ? std::string(cascade ? "DROP ... CASCADE of " : "DROP of ")
                                                  : std::string("CREATE OR REPLACE of ")) + what;
    for (auto& [name, entry] : seen) RequireDropOfTaken(*entry, statement);
    for (auto* entry : unseen) RequireDropOfTaken(*entry, statement);
  }

  // DROP SCHEMA without CASCADE: DuckDB refuses a schema with anything in it and names it all,
  // also what the principal cannot see. Refused here the same way, naming what it sees.
  void CheckSchemaEmpty(dd::SchemaCatalogEntry& schema, const std::string& what) {
    if (principal_.superuser) return;
    std::vector<dd::CatalogEntry*> contents;
    try {
      auto transaction = schema.ParentCatalog().GetCatalogTransaction(context_);
      for (auto type : {dd::CatalogType::TABLE_ENTRY, dd::CatalogType::SEQUENCE_ENTRY, dd::CatalogType::TYPE_ENTRY,
                        dd::CatalogType::MACRO_ENTRY, dd::CatalogType::TABLE_MACRO_ENTRY,
                        dd::CatalogType::INDEX_ENTRY}) {
        schema.Scan(transaction, type, [&](dd::CatalogEntry& entry) {
          if (!entry.internal) contents.push_back(&entry);
        });
      }
      schema.ScanSchemaTree(transaction, [&](dd::SchemaCatalogEntry& found) {
        if (&found != &schema) contents.push_back(&found);
      });
    } catch (const std::exception& e) {
      Log(LogLevel::kWarning, "access control could not list what is in a schema",
          {{"user", principal_.user_name}, {"object", what}, {"error", e.what()}});
      throw dd::CatalogException("Hardcharger could not list what is in " + what +
                                 ", so the statement was not run (the server log says why)");
    }
    if (!contents.empty()) RefuseDependents("drop", what, contents);
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
        // DROP SCHEMA ... CASCADE drops the schemas nested in it too (DuckDB refuses the
        // drop without CASCADE while there are any): each one needs its own drop right.
        if (info.cascade) {
          auto& schema = entry->Cast<dd::SchemaCatalogEntry>();
          std::vector<dd::reference<dd::SchemaCatalogEntry>> children;
          bool listed = false;
          try {
            schema.ScanSchemaTree(schema.ParentCatalog().GetCatalogTransaction(context_),
                                  [&](dd::SchemaCatalogEntry& found) {
                                    if (&found != &schema) children.push_back(found);
                                  });
            listed = true;
          } catch (const std::exception&) {
            listed = false;
          }
          if (!listed) {
            Deny(principal_, "the schemas nested in " + PathOf(*entry).ToString() +
                                 " could not be listed, so DROP SCHEMA ... CASCADE can't check them; ask an administrator");
          }
          const std::string what = "schema " + PathOf(*entry).ToString();
          for (auto& child : children) RequireDropOfTaken(child.get(), "DROP SCHEMA ... CASCADE of " + what);
          std::vector<dd::reference<dd::CatalogEntry>> targets{*entry};
          for (auto& child : children) targets.emplace_back(child.get());
          CheckDependents(targets, true, what);
        } else {
          CheckSchemaEmpty(entry->Cast<dd::SchemaCatalogEntry>(), "schema " + PathOf(*entry).ToString());
        }
        return;
      }
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY: {
        // The binder has already qualified the name of an existing object.
        auto entry = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts);
        if (!entry) return;  // DROP ... IF EXISTS of nothing; DuckDB reports a missing object
        CheckDropOf(*entry);
        CheckDependents({*entry}, info.cascade, Describe(*entry));
        return;
      }
      case dd::CatalogType::SEQUENCE_ENTRY: {
        auto entry = LookUp(context_, info.type, parts);
        if (!entry) return;  // DROP SEQUENCE IF EXISTS of nothing; DuckDB reports a missing sequence
        ObjectPath sequence = PathOf(*entry);
        if (IsTempCatalog(sequence.catalog)) return;
        RequireSchemaPrivilege(sequence, Privilege::kDropSequence, "drop sequence");
        ownership_.push_back({PendingOwnership::Kind::kDrop, sequence, ObjectType::kSequence});
        CheckDependents({*entry}, info.cascade, Describe(*entry));
        return;
      }
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY: {
        auto entry = LookUp(context_, info.type, parts);
        if (!entry) return;  // DROP MACRO IF EXISTS of nothing; DuckDB reports a missing macro
        ObjectPath macro = PathOf(*entry);
        if (IsTempCatalog(macro.catalog)) return;
        RequireMacroPrivilege(macro, Privilege::kDropMacro, "drop");
        CheckDependents({*entry}, info.cascade, Describe(*entry));
        return;
      }
      case dd::CatalogType::TYPE_ENTRY: {
        auto entry = LookUp(context_, info.type, parts);
        if (!entry) return;  // DROP TYPE IF EXISTS of nothing; DuckDB reports a missing type
        ObjectPath type = PathOf(*entry);
        if (IsTempCatalog(type.catalog)) return;
        RequireSchemaPrivilege(type, Privilege::kDropType, "drop type");
        // Also with CASCADE: what names the type keeps naming it, and would mean something
        // else or nothing.
        if (auto refusal = TypeNameRefusal(context_, snapshot_, type, "drop")) Deny(principal_, *refusal);
        CheckDependents({*entry}, info.cascade, Describe(*entry));
        return;
      }
      case dd::CatalogType::TRIGGER_ENTRY: {
        // DROP TRIGGER t ON s.x: the binder resolved the table and put the trigger's name in
        // its schema; the trigger is found on the table.
        if (!info.extra_drop_info) Deny(principal_, "DROP TRIGGER names no table");
        auto& extra = info.extra_drop_info->Cast<dd::ExtraDropTriggerInfo>();
        if (!extra.base_table) Deny(principal_, "DROP TRIGGER names no table");
        auto table = LookUp(context_, dd::CatalogType::TABLE_ENTRY,
                            NameParts(extra.base_table->Cast<dd::BaseTableRef>().GetQualifiedName()));
        if (!table || table->type != dd::CatalogType::TABLE_ENTRY) return;  // DuckDB reports the missing table
        if (parts.empty()) Deny(principal_, "the trigger's name could not be determined");
        auto entry = LookUpTrigger(context_, table->Cast<dd::TableCatalogEntry>(), parts.back());
        if (!entry) return;  // DROP TRIGGER IF EXISTS of nothing; DuckDB reports a missing trigger
        const ObjectPath table_path = PathOf(*table);
        if (IsTempCatalog(table_path.catalog)) return;
        RequireTriggerDrop(table_path, compat::Str(entry->name), "drop");
        ownership_.push_back({PendingOwnership::Kind::kDrop, TriggerOwnerPath(table_path, compat::Str(entry->name)),
                              ObjectType::kTrigger});
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
    // A default that names a sequence (a temporary table's too): USAGE on it. (DuckDB
    // binds ADD COLUMN ... DEFAULT nextval() with a NULL default and fills the column with
    // an UPDATE, which is checked when it runs; SET DEFAULT is checked here.)
    if (info.type == dd::AlterType::ALTER_TABLE && !parts.empty()) QualifyAlterExpressions(context_, parts[0], info);
    if (info.type == dd::AlterType::ALTER_TABLE) {
      auto& table_info = info.Cast<dd::AlterTableInfo>();
      if (table_info.alter_table_type == dd::AlterTableType::ADD_COLUMN) {
        auto& column = table_info.Cast<dd::AddColumnInfo>().new_column;
        if (column.HasDefaultValue()) {
          auto copy = column.DefaultValue().Copy();
          CheckDefaultSequences(*copy);
        }
      } else if (table_info.alter_table_type == dd::AlterTableType::SET_DEFAULT) {
        auto& expression = table_info.Cast<dd::SetDefaultInfo>().expression;
        if (expression) CheckDefaultSequences(*expression);
      }
    }
    if (!parts.empty() && IsTempCatalog(parts[0])) return;
    if (info.type == dd::AlterType::SET_COMMENT || info.type == dd::AlterType::SET_COLUMN_COMMENT) {
      return CheckComment(info, parts);
    }
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
      std::string renamed;
      if (info.type == dd::AlterType::ALTER_TABLE &&
          info.Cast<dd::AlterTableInfo>().alter_table_type == dd::AlterTableType::RENAME_TABLE) {
        renamed = compat::Str(info.Cast<dd::RenameTableInfo>().new_table_name);
      } else if (info.type == dd::AlterType::ALTER_VIEW &&
                 info.Cast<dd::AlterViewInfo>().alter_view_type == dd::AlterViewType::RENAME_VIEW) {
        renamed = compat::Str(info.Cast<dd::RenameViewInfo>().new_view_name);
      }
      if (!renamed.empty() && !IdentifiersEqual(renamed, path.object_name)) {
        ObjectPath destination = path;
        destination.object_name = renamed;
        if (auto refusal = TriggerNameRefusal(context_, destination)) Deny(principal_, *refusal);
      }
      return;
    }
    Deny(principal_, "this ALTER needs an administrator");
  }

  // COMMENT ON: what may change the object may describe it. A table, a column of one or
  // an index on one: ALTER TABLE (its owner holds it); a view or a column of one: ALTER
  // VIEW; a sequence or a macro (objects without owners): DROP SEQUENCE / DROP MACRO on
  // its schema. Types stay the administrator's, as creating them does.
  void CheckComment(dd::AlterInfo& info, const std::vector<std::string>& parts) {
    dd::CatalogType type = dd::CatalogType::INVALID;
    if (info.type == dd::AlterType::SET_COMMENT) {
      type = info.Cast<dd::SetCommentInfo>().entry_catalog_type;
    } else {
      type = dd::CatalogType::TABLE_ENTRY;  // a column of a table or view, found below
    }
    const auto require_alter = [&](dd::CatalogEntry& entry, const std::string& what) {
      ObjectPath path = PathOf(entry);
      if (IsTempCatalog(path.catalog)) return;
      RequireVisible(path.catalog);
      const ObjectType object_type = ObjectTypeOf(entry);
      const Privilege privilege = object_type == ObjectType::kView ? Privilege::kAlterView : Privilege::kAlterTable;
      if (!snapshot_.Check(principal_, AccessRequest{path, object_type, privilege, {}}).allowed) {
        Deny(principal_, "user " + principal_.user_name + " may not comment on " + what + " (only the " +
                             TypeWord(object_type) + "'s owner, or a role granted " + PrivilegeName(privilege) +
                             " on " + path.ToString() + ")");
      }
    };
    switch (type) {
      case dd::CatalogType::TABLE_ENTRY:
      case dd::CatalogType::VIEW_ENTRY: {
        auto entry = LookUp(context_, dd::CatalogType::TABLE_ENTRY, parts);
        if (!entry) return;  // DuckDB reports the missing object
        const std::string what = info.type == dd::AlterType::SET_COLUMN_COMMENT
                                     ? "a column of " + PathOf(*entry).ToString()
                                     : TypeWord(ObjectTypeOf(*entry)) + " " + PathOf(*entry).ToString();
        return require_alter(*entry, what);
      }
      case dd::CatalogType::INDEX_ENTRY: {
        auto entry = LookUp(context_, dd::CatalogType::INDEX_ENTRY, parts);
        if (!entry) return;
        // In the index's own schema (nested ones included), not by its last name part.
        auto table = IndexTable(*entry);
        if (!table) Deny(principal_, "the table of index " + PathOf(*entry).ToString() + " could not be found");
        return require_alter(*table, "index " + PathOf(*entry).ToString());
      }
      case dd::CatalogType::SEQUENCE_ENTRY: {
        auto entry = LookUp(context_, dd::CatalogType::SEQUENCE_ENTRY, parts);
        if (!entry) return;
        const ObjectPath path = PathOf(*entry);
        if (IsTempCatalog(path.catalog)) return;
        return RequireSchemaPrivilege(path, Privilege::kDropSequence, "comment on sequence");
      }
      case dd::CatalogType::MACRO_ENTRY:
      case dd::CatalogType::TABLE_MACRO_ENTRY: {
        auto entry = LookUp(context_, type, parts);
        if (!entry) return;
        const ObjectPath path = PathOf(*entry);
        if (IsTempCatalog(path.catalog)) return;
        return RequireSchemaPrivilege(path, Privilege::kDropMacro, "comment on macro");
      }
      case dd::CatalogType::TYPE_ENTRY: {
        auto entry = LookUp(context_, type, parts);
        if (!entry) return;
        const ObjectPath path = PathOf(*entry);
        if (IsTempCatalog(path.catalog)) return;
        return RequireSchemaPrivilege(path, Privilege::kDropType, "comment on type");
      }
      default:
        Deny(principal_, "commenting on this kind of object needs an administrator");
    }
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
  const TriggerBodies& trigger_bodies_;
  std::vector<Rights> rights_;
  int view_depth_ = 0;  // inside a view's body
  // Under DuckLake's own compaction or flush: the catalogs it rewrites (folded).
  std::set<std::string> ducklake_rewrite_catalogs_;
  // The sequences the column defaults of the table being written advance (Visit).
  std::vector<const dd::CatalogEntry*> default_sequences_;
  bool in_owned_view_ = false;
  std::string owned_view_;    // the innermost view read with its owner's rights
  std::string unowned_view_;  // the innermost view read with the caller's rights
  std::string creating_view_;  // checking this new view's query (CheckViewQuery)
  std::string carried_note_;   // added to a refusal of RequireCarriedRows
  std::vector<PendingOwnership> ownership_;
  std::set<std::string> accesses_;
  boost::json::array objects_;
  std::optional<StorageAccess> storage_;  // made at the first path the statement reaches
};

// A superuser's statement is not checked, but the bodies of users' triggers it fires are,
// with their owners' rights (nested ones included: the checker handles those it meets).
void CheckDefinerBodies(dd::ClientContext& context, const Snapshot& snapshot, dd::LogicalOperator& op,
                        const TriggerBodies& bodies) {
  if (const TriggerBody* body = TriggerBodyAt(bodies, op)) {
    if (!body->problem.empty()) ::hardcharger::authz::Deny(Principal::Superuser("administrator"), body->problem);
    if (body->definer) {
      // The administrator's session must not decide what a user's trigger names either: the
      // body runs with its owner's rights, and would read the administrator's temporary
      // objects (or WITH) into the owner's tables. The same for the bodies beneath it.
      const std::string names = TriggerBodyNameProblem(context, bodies, *body);
      if (!names.empty()) ::hardcharger::authz::Deny(Principal::Superuser("administrator"), names);
      const auto view_columns = ViewColumnsUsed(op);
      PlanChecker checker(context, snapshot, *body->definer, view_columns, bodies);
      checker.trigger_context_ = body->describe + " cannot run, because its owner " + body->definer_name +
                                 " may not do what its body does: ";
      checker.trigger_body_entered_ = true;
      checker.check_trigger_names_ = true;
      checker.Visit(op);
      return;
    }
  }
  for (auto& child : op.children) {
    if (child) CheckDefinerBodies(context, snapshot, *child, bodies);
  }
}

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
  if (principal.asks_system_admin()) throw dd::PermissionException(SystemAdminLostMessage(principal.user_name));
  const RoleRecord* role = snapshot.FindRoleById(principal.active_role_id);
  throw dd::PermissionException("Hardcharger blocked this statement: user " + principal.user_name +
                                " no longer holds role " +
                                (role ? role->name : std::string("(dropped)")) +
                                ", which SET ROLE made active. Run SET ROLE with another role, "
                                "or RESET ROLE");
}

void CollectRename(dd::ClientContext& context, dd::LogicalOperator& op,
                   std::vector<PendingOwnership>& changes) {
  if (op.type == OpType::LOGICAL_ALTER) {
    auto& info = *op.Cast<dd::LogicalAlter>().info;
    std::string name;
    if (info.type == dd::AlterType::ALTER_TABLE &&
        info.Cast<dd::AlterTableInfo>().alter_table_type == dd::AlterTableType::RENAME_TABLE) {
      name = compat::Str(info.Cast<dd::RenameTableInfo>().new_table_name);
    } else if (info.type == dd::AlterType::ALTER_VIEW &&
               info.Cast<dd::AlterViewInfo>().alter_view_type == dd::AlterViewType::RENAME_VIEW) {
      name = compat::Str(info.Cast<dd::RenameViewInfo>().new_view_name);
    }
    if (!name.empty()) {
      auto entry = LookUp(context, dd::CatalogType::TABLE_ENTRY, PartsOf(info));
      if (entry) {
        auto path = PathOfEntry(*entry);
        if (!IsTempCatalog(path.catalog)) {
          auto destination = path;
          destination.object_name = name;
          // An occupied destination makes the rename fail; never overwrite its
          // permissions while preparing that failure. Case-only renames keep keys.
          if (IdentifiersEqual(path.object_name, name)) return;
          auto destination_parts = PartsOf(info);
          destination_parts.back() = name;
          if (LookUp(context, dd::CatalogType::TABLE_ENTRY, destination_parts)) return;
          changes.push_back({PendingOwnership::Kind::kRename, path,
                             entry->type == dd::CatalogType::VIEW_ENTRY ? ObjectType::kView : ObjectType::kTable,
                             {}, false, destination});
        }
      }
    }
  }
  for (auto& child : op.children) if (child) CollectRename(context, *child, changes);
}

// CREATE TRIGGER outside any session (the init SQL): its action's names written in full.
// Never a reason for the statement to fail: what can't be resolved stays as written, and
// is held against each writer's session when the trigger fires.
void QualifyUncheckedTriggers(dd::ClientContext& context, dd::LogicalOperator& op) {
  try {
    if (op.type == OpType::LOGICAL_CREATE_TABLE) {
      auto& create = op.Cast<dd::LogicalCreateTable>();
      if (const auto parts = PartsOf(*create.info->base); !parts.empty()) {
        QualifyTableExpressions(context, parts[0], create.info->Base());
      }
    } else if (op.type == OpType::LOGICAL_ALTER) {
      auto& info = *op.Cast<dd::LogicalAlter>().info;
      const auto parts = PartsOf(info);
      if (info.type == dd::AlterType::ALTER_TABLE && !parts.empty()) QualifyAlterExpressions(context, parts[0], info);
    } else if (op.type == OpType::LOGICAL_CREATE_INDEX) {
      auto& index = op.Cast<dd::LogicalCreateIndex>();
      if (index.info) QualifyIndexExpressions(context, compat::Str(index.table.ParentCatalog().GetName()), *index.info);
    }
  } catch (const std::exception&) {
  }
  if (op.type == OpType::LOGICAL_CREATE_TRIGGER) {
    try {
      auto& info = op.Cast<dd::LogicalCreate>().info->Cast<dd::CreateTriggerInfo>();
      const auto parts = PartsOf(info);
      if (parts.size() >= 3 && !IsTempCatalog(parts[0]) && !info.temporary && info.base_table) {
        const ObjectPath table{parts[0], {parts.begin() + 1, parts.end() - 1},
                               compat::Str(info.base_table->GetQualifiedName().Name())};
        QualifyTriggerNames(context, table, info);
      }
    } catch (const std::exception&) {
    }
  }
  for (auto& child : op.children) {
    if (child) QualifyUncheckedTriggers(context, *child);
  }
}

void PreOptimize(dd::OptimizerExtensionInput& input, dd::unique_ptr<dd::LogicalOperator>& plan) {
  auto& context = input.context;
  auto state = context.registered_state->Get<RbacSessionState>(kRbacSessionStateName);
  if (!state) {
    // A Hardcharger-internal connection: nothing to check. The init SQL's DuckDB statements
    // run on one: a trigger they create is stored with its names in full, as a session's is.
    if (plan) QualifyUncheckedTriggers(context, *plan);
    return;
  }
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
      // An index that could not be bound to one macro beyond doubt is nobody's to read or write
      // through, an administrator's statement included (triggers.hpp, IndexExpressionProblem).
      if (auto problem = StatementIndexProblem(context, *plan); !problem.empty()) {
        ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
      }
      std::function<void(dd::LogicalOperator&)> collect = [&](dd::LogicalOperator& op) {
        if (op.type == OpType::LOGICAL_ALTER) {
          auto& info = *op.Cast<dd::LogicalAlter>().info;
          const auto parts = PartsOf(info);
          if (info.type == dd::AlterType::ALTER_TABLE && !parts.empty()) QualifyAlterExpressions(context, parts[0], info);
        }
        if (op.type == OpType::LOGICAL_CREATE_INDEX) {
          auto& index = op.Cast<dd::LogicalCreateIndex>();
          if (!IsTempCatalog(compat::Str(index.table.ParentCatalog().GetName()))) {
            if (index.info) QualifyIndexExpressions(context, compat::Str(index.table.ParentCatalog().GetName()), *index.info);
            if (auto problem = IndexStoredNamesProblem(context, index); !problem.empty()) {
              ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
            }
          }
        }
        if (op.type == OpType::LOGICAL_CREATE_TABLE) {
          auto parts = PartsOf(*op.Cast<dd::LogicalCreateTable>().info->base);
          auto& info = *op.Cast<dd::LogicalCreateTable>().info->base;
          if (!parts.empty()) QualifyTableExpressions(context, parts[0], op.Cast<dd::LogicalCreateTable>().info->Base());
          if (parts.size() >= 3) {
            const ObjectPath table{parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()};
            if (auto problem = TableStoredNamesProblem(context, table, op.Cast<dd::LogicalCreateTable>().info->Base());
                !problem.empty()) {
              ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
            }
          }
          auto existing = LookUp(context, dd::CatalogType::TABLE_ENTRY, parts);
          if (parts.size() >= 3 && !IsTempCatalog(parts[0]) &&
              !(existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT)) {
            ownership.push_back({PendingOwnership::Kind::kCreate,
                                 {parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()},
                                 ObjectType::kTable, {}, false, {}, !existing});
          }
        } else if (op.type == OpType::LOGICAL_CREATE_VIEW) {
          auto parts = PartsOf(*op.Cast<dd::LogicalCreate>().info);
          auto& info = *op.Cast<dd::LogicalCreate>().info;
          auto existing = LookUp(context, dd::CatalogType::TABLE_ENTRY, parts);
          if (parts.size() >= 3 && !IsTempCatalog(parts[0]) &&
              !(existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT)) {
            // An administrator's view too finds its functions and types under its name scope when
            // it is read: say now what its query names that the scope would not find.
            const ObjectPath view{parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()};
            auto& view_info = info.Cast<dd::CreateViewInfo>();
            if (!view_info.temporary && view_info.query && view_info.query->node) {
              if (auto problem = StoredNamesProblem(context, view.catalog, view.schema_path, "view " + view.ToString(),
                                                    view_info.query->node.get(), {});
                  !problem.empty()) {
                ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
              }
            }
            ownership.push_back({PendingOwnership::Kind::kCreate,
                                 {parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()},
                                 ObjectType::kView, DefinitionHashOf(info), false, {}, !existing});
          }
        } else if (op.type == OpType::LOGICAL_CREATE_SCHEMA) {
          auto& info = *op.Cast<dd::LogicalCreate>().info;
          auto target = SchemaTarget(context, PartsOf(info));
          auto existing = LookUpSchema(context, target);
          if (!IsTempCatalog(target.catalog) &&
              !(existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT)) {
            ownership.push_back({PendingOwnership::Kind::kCreate, target, ObjectType::kSchema,
                                 {}, false, {}, !existing});
          }
        } else if (op.type == OpType::LOGICAL_DROP) {
          auto& info = *op.Cast<dd::LogicalDrop>().info;
          auto parts = PartsOf(info);
          if (info.type == dd::CatalogType::SCHEMA_ENTRY) {
            auto path = SchemaTarget(context, parts);
            if (!path.catalog.empty() && !IsTempCatalog(path.catalog))
              ownership.push_back({PendingOwnership::Kind::kDrop, path, ObjectType::kSchema});
          } else if (info.type == dd::CatalogType::TABLE_ENTRY || info.type == dd::CatalogType::VIEW_ENTRY ||
                     info.type == dd::CatalogType::SEQUENCE_ENTRY) {
            auto entry = LookUp(context, info.type, parts);
            if (entry && !IsTempCatalog(PathOfEntry(*entry).catalog)) {
              ownership.push_back({PendingOwnership::Kind::kDrop, PathOfEntry(*entry),
                                   info.type == dd::CatalogType::SEQUENCE_ENTRY ? ObjectType::kSequence :
                                   info.type == dd::CatalogType::VIEW_ENTRY ? ObjectType::kView : ObjectType::kTable});
            } else if (!entry && parts.size() >= 3 && !IsTempCatalog(parts[0])) {
              // An administrator can repair legacy stale rows by repeating a fully
              // qualified DROP ... IF EXISTS; no phantom object needs to be created.
              ownership.push_back({PendingOwnership::Kind::kDrop,
                                   {parts[0], {parts.begin() + 1, parts.end() - 1}, parts.back()},
                                   info.type == dd::CatalogType::SEQUENCE_ENTRY ? ObjectType::kSequence :
                                   info.type == dd::CatalogType::VIEW_ENTRY ? ObjectType::kView : ObjectType::kTable});
            }
          }
        } else if (op.type == OpType::LOGICAL_CREATE_TRIGGER) {
          // An administrator's trigger (replacing a user's too) runs with the writer's rights.
          auto& info = op.Cast<dd::LogicalCreate>().info->Cast<dd::CreateTriggerInfo>();
          auto parts = PartsOf(info);
          if (parts.size() >= 3 && !IsTempCatalog(parts[0]) && !info.temporary && info.base_table) {
            const ObjectPath table{parts[0], {parts.begin() + 1, parts.end() - 1},
                                   compat::Str(info.base_table->GetQualifiedName().Name())};
            // IF NOT EXISTS over a trigger that is there changes nothing: DuckDB keeps it, and
            // its owner record (a user's trigger stays the user's, run with the user's rights).
            dd::optional_ptr<dd::CatalogEntry> existing;
            std::vector<std::string> table_parts(parts.begin(), parts.end() - 1);
            table_parts.push_back(table.object_name);
            if (auto entry = LookUp(context, dd::CatalogType::TABLE_ENTRY, table_parts);
                entry && entry->type == dd::CatalogType::TABLE_ENTRY) {
              existing = LookUpTrigger(context, entry->Cast<dd::TableCatalogEntry>(), parts.back());
            }
            if (!(existing && info.on_conflict == dd::OnCreateConflict::IGNORE_ON_CONFLICT)) {
              // Stored with its names given their schema, as a user's is.
              if (auto problem = QualifyTriggerNames(context, table, info); !problem.empty()) {
                ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
              }
              if (auto problem = TriggerStoredNamesProblem(context, table, info); !problem.empty()) {
                ::hardcharger::authz::Deny(Principal::Superuser("administrator"), problem);
              }
              ownership.push_back({PendingOwnership::Kind::kCreate, TriggerOwnerPath(table, parts.back()),
                                   ObjectType::kTrigger, TriggerDefinitionHash(info)});
            }
          }
        }
        for (auto& child : op.children) {
          if (child) collect(*child);
        }
      };
      collect(*plan);
      CollectRename(context, *plan, ownership);
    }
    // A user's trigger fired by this statement runs with the user's rights, not the
    // administrator's: its body is checked, and its policies applied, as the owner.
    if (plan && !state->bypassed()) {
      const auto bodies = FindTriggerBodies(context, *snapshot, *plan);
      if (HasDefinerBody(bodies)) {
        CheckDefinerBodies(context, *snapshot, *plan, bodies);
        RemoveViewMarkers(plan);
        ApplyPolicies(context, snapshot, Principal::Superuser("administrator"), plan,
                      FindTriggerBodies(context, *snapshot, *plan, state->definer_exemptions_unverified()));
        state->NoteChecked(snapshot->version(), std::move(ownership), plan->type == OpType::LOGICAL_PREPARE);
        return;
      }
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
  const auto trigger_bodies = FindTriggerBodies(context, *snapshot, *plan);
  PlanChecker checker(context, *snapshot, principal, view_columns, trigger_bodies);
  checker.Visit(*plan);
  // The indexes of what it reads and writes are bound (again, after a start) while it runs.
  if (auto problem = StatementIndexProblem(context, *plan); !problem.empty()) {
    ::hardcharger::authz::Deny(principal, problem);
  }
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
  // Row and masking policies apply to what passed the check (a user's trigger body: the
  // owner's policies).
  // The audit record names each policy exemption that let the statement through.
  std::set<std::string> exemptions;
  ApplyPolicies(context, snapshot, principal, plan,
                FindTriggerBodies(context, *snapshot, *plan, state->definer_exemptions_unverified()), false, &exemptions);
  auto ownership = checker.TakeOwnership();
  CollectRename(context, *plan, ownership);
  auto accesses = checker.TakeAccesses();
  accesses.insert(accesses.end(), exemptions.begin(), exemptions.end());
  state->NoteChecked(snapshot->version(), std::move(ownership),
                     plan->type == OpType::LOGICAL_PREPARE, std::move(accesses));
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
  const auto trigger_bodies = FindTriggerBodies(input.context, *snapshot, *statement.plan);
  PlanChecker checker(input.context, *snapshot, principal, view_columns, trigger_bodies);
  checker.Visit(*statement.plan);
  if (auto problem = StatementIndexProblem(input.context, *statement.plan); !problem.empty()) {
    ::hardcharger::authz::Deny(principal, problem);
  }
  state->NoteBindChecked();
}

}  // namespace

// ---------------------------------------------------------------------------

std::string SystemAdminLostMessage(const std::string& user_name) {
  return "Hardcharger blocked this statement: user " + user_name + " no longer holds " + kSystemAdminName +
         ", which SET ROLE made active. Run RESET ROLE to go on as " + user_name;
}

RbacSessionState::RbacSessionState(std::shared_ptr<RbacEngine> engine, Principal principal)
    : engine_(engine), administrator_(principal.superuser), principal_(std::move(principal)) {}

std::string RbacSessionState::SystemAdminRefusal() const {
  std::string user_name, unverified;
  {
    MutexLock lock(principal_mutex_);
    user_name = principal_.user_name;
    unverified = system_admin_unverified_;
  }
  if (unverified.empty()) return SystemAdminLostMessage(user_name);
  return "Hardcharger blocked this statement: user " + user_name + " acts as System Admin here (SET ROLE " +
         kSystemAdminName + "), which is asked of the access control store before every statement, and the store did "
         "not answer (" + unverified + "). Run RESET ROLE to go on as " + user_name;
}

Principal RbacSessionState::principal() const {
  Principal user;
  bool unverified = false;
  {
    MutexLock lock(principal_mutex_);
    user = principal_;
    unverified = !system_admin_unverified_.empty();
    user.policy_exemptions_unverified = !policy_exemptions_unverified_.empty();
  }
  if (administrator_ || !user.asks_system_admin()) return user;
  // The store did not answer when this request asked it: nobody acts as System Admin on
  // what this server last heard.
  if (unverified) return user;
  // Never for a session its sign-in limits (the guard refuses those at SET ROLE too).
  if (user.scope || user.catalog_rules) return user;
  auto engine = engine_.lock();
  if (!engine || !engine->snapshot()->HoldsSystemAdmin(user)) return user;
  // The administrator's shape exactly (no user id: what it creates is the system's, what
  // it grants stands as the administrator's), naming the user it is.
  Principal acting = Principal::Superuser(user.user_name);
  acting.acting_user_id = user.user_id;
  acting.active_role_id = user.active_role_id;
  acting.active_role_name = user.active_role_name;
  acting.idp_issuer = user.idp_issuer;
  acting.idp_groups = user.idp_groups;
  return acting;
}

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
  MutexLock lock(mutex_);
  last_accesses_ = std::move(accesses);
  ++checked_pending_;
  last_checked_version_ = snapshot_version;
  last_checked_persona_ = persona_version_.load();
  planned_prepare_only_ = prepare_only;
  for (auto& o : ownership) planned_ownership_.push_back(std::move(o));
}

std::vector<std::string> RbacSessionState::TakeAccesses() {
  MutexLock lock(mutex_);
  std::vector<std::string> taken;
  taken.swap(last_accesses_);
  return taken;
}

std::vector<PendingOwnership> RbacSessionState::TakePreparedOwnership() {
  MutexLock lock(mutex_);
  std::vector<PendingOwnership> taken;
  taken.swap(prepared_ownership_);
  return taken;
}

void RbacSessionState::SetActiveRole(std::string role_id, std::string role_name) {
  MutexLock lock(principal_mutex_);
  principal_.active_role_id = std::move(role_id);
  principal_.active_role_name = std::move(role_name);
  ++persona_version_;
}

void RbacSessionState::NoteBindChecked() {
  MutexLock lock(mutex_);
  bind_checked_pending_ = true;
}

dd::RebindQueryInfo RbacSessionState::OnFinalizePrepare(dd::ClientContext&,
                                                        dd::PreparedStatementData& prepared,
                                                        dd::PreparedStatementMode mode) {
  bool runs_now = false;
  dd::RebindQueryInfo rebind;
  std::vector<PendingOwnership> to_record;
  {
    MutexLock lock(mutex_);
    rebind = FinalizePrepareLocked(prepared, mode, runs_now);
    if (runs_now) {
      for (const auto& o : pending_ownership_) {
        if (o.kind != PendingOwnership::Kind::kDrop && !o.recorded) to_record.push_back(o);
      }
    }
  }
  if (to_record.empty()) return rebind;
  // The statement runs now: its creations get their owners first, so none can exist
  // without one (QueryEnd puts the records back if it fails).
  auto engine = engine_.lock();
  if (!engine) {
    throw dd::PermissionException("Hardcharger blocked this statement: access control is shutting down");
  }
  std::vector<RecordedOwnership> undo;
  auto status = RecordOwnership(to_record, undo);
  MutexLock lock(mutex_);
  for (auto& u : undo) recorded_ownership_.push_back(std::move(u));
  if (!status.ok()) {
    pending_ownership_.clear();
    throw dd::IOException(
        "Hardcharger did not run this statement: the owner of what it creates could not be recorded (" +
        status.message() + ")");
  }
  for (auto& o : pending_ownership_) {
    for (const auto& r : to_record) {
      if (r.recorded && o.kind == r.kind && o.object.ToString() == r.object.ToString()) o.recorded = true;
    }
  }
  return rebind;
}

dd::RebindQueryInfo RbacSessionState::FinalizePrepareLocked(dd::PreparedStatementData& prepared,
                                                            dd::PreparedStatementMode mode, bool& runs_now) {
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
  runs_now = !prepare_only;
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
    const auto user_name = session_principal().user_name;
    Log(LogLevel::kWarning, "a prepared plan reached no access-control check",
        {{"user", user_name},
         {"statement_type", dd::StatementTypeToString(prepared.statement_type)},
         {"mode", mode == dd::PreparedStatementMode::PREPARE_ONLY ? "prepare_only" : "prepare_and_execute"},
         {"bound_all_parameters", prepared.properties.bound_all_parameters ? "true" : "false"},
         {"always_require_rebind", prepared.properties.always_require_rebind ? "true" : "false"},
         {"bind_checked", bind_checked ? "true" : "false"},
         {"unplanned", unplanned ? "true" : "false"},
         {"planned_prepare_only", prepare_only ? "true" : "false"},
         {"has_physical_plan", prepared.physical_plan ? "true" : "false"}});
    pending_ownership_.clear();
    prepared_ownership_.clear();
    throw dd::PermissionException(
        "Hardcharger blocked this statement: access control did not see its plan (it was not planned for this run), "
        "so it was not run; this is not about user " + user_name + "'s grants. Run it again, and if it "
        "keeps happening, report it with the server log");
  }
  return dd::RebindQueryInfo::DO_NOT_REBIND;
}

dd::RebindQueryInfo RbacSessionState::RebindIfGrantsChanged(
    const dd::PreparedStatementData&) {
  if (bypassed() || administrator_) return dd::RebindQueryInfo::DO_NOT_REBIND;
  // Every run is planned and checked again: a prepared statement's address can be reused
  // by another statement, so it cannot identify a previously checked plan. Store users
  // also re-plan while acting as System Admin, so SET/RESET ROLE and policy exemptions
  // never reuse a plan carrying a different persona's filters or masks.
  return dd::RebindQueryInfo::ATTEMPT_TO_REBIND;
}

dd::RebindQueryInfo RbacSessionState::OnRebindPreparedStatement(
    dd::ClientContext&, dd::BindPreparedStatementCallbackInfo& info, dd::RebindQueryInfo) {
  return RebindIfGrantsChanged(info.prepared_statement);
}

void RbacSessionState::QueryEnd(dd::ClientContext& context, dd::optional_ptr<dd::ErrorData> error) {
  std::vector<PendingOwnership> pending;
  std::vector<RecordedOwnership> recorded;
  {
    MutexLock lock(mutex_);
    pending.swap(pending_ownership_);
    recorded.swap(recorded_ownership_);
    planned_ownership_.clear();
    checked_pending_ = 0;
    bind_checked_pending_ = false;
  }
  auto engine = engine_.lock();
  if (!engine) return;
  if (error) {
    UndoRecordedOwners(*engine, recorded);
    return;
  }
  CompleteOwnership(context, pending, recorded);
}

namespace {
struct MetadataUpdate {
  explicit MetadataUpdate(std::atomic<bool>& flag) : flag(flag) { flag = true; }
  ~MetadataUpdate() { flag = false; }
  std::atomic<bool>& flag;
};
}  // namespace

arrow::Status RbacSessionState::RecordOwnership(std::vector<PendingOwnership>& changes,
                                                std::vector<RecordedOwnership>& undo) {
  auto engine = engine_.lock();
  if (!engine) return arrow::Status::Invalid("access control is shutting down");
  MetadataUpdate updating(updating_metadata_);
  return RecordCreatedOwners(*engine, principal(), changes, undo);
}

void RbacSessionState::CompleteOwnership(dd::ClientContext& context,
                                         const std::vector<PendingOwnership>& pending,
                                         const std::vector<RecordedOwnership>& recorded) {
  auto engine = engine_.lock();
  if (!engine) return;
  MetadataUpdate updating(updating_metadata_);
  if (context.transaction.HasActiveTransaction()) {
    for (const auto& change : pending) {
      if (change.kind == PendingOwnership::Kind::kRename) {
        transaction_ownership_.push_back({PendingOwnership::Kind::kDrop, change.object, change.type});
        transaction_ownership_.push_back({PendingOwnership::Kind::kCreate, change.destination,
                                          change.type, {}, true});
      } else {
        transaction_ownership_.push_back(change);
      }
    }
    for (const auto& record : recorded) transaction_recorded_.push_back(record);
  } else if (!pending.empty()) {
    ApplyOwnership(*engine, principal(), pending);
  }
}

void RbacSessionState::TransactionCommit(dd::MetaTransaction&, dd::ClientContext&) {
  MetadataUpdate updating(updating_metadata_);
  if (auto engine = engine_.lock(); engine && !transaction_ownership_.empty())
    ApplyOwnership(*engine, principal(), transaction_ownership_);
  transaction_ownership_.clear();
  transaction_recorded_.clear();
}

void RbacSessionState::TransactionRollback(dd::MetaTransaction&, dd::ClientContext&) {
  MetadataUpdate updating(updating_metadata_);
  if (auto engine = engine_.lock()) {
    UndoRecordedOwners(*engine, transaction_recorded_);
  }
  transaction_ownership_.clear();
  transaction_recorded_.clear();
}

bool IsAllowedTableFunction(const std::string& name_lower) {
  return AllowedTableFunctions().contains(name_lower);
}

void ApplyOwnership(RbacEngine& engine, const Principal& principal,
                    const std::vector<PendingOwnership>& changes) {
  for (size_t index = 0; index < changes.size(); ++index) {
    const auto& p = changes[index];
    if (p.recorded && p.kind == PendingOwnership::Kind::kCreate) continue;
    // CREATE OR REPLACE drops and creates the same object: the creation's record, written
    // before the statement ran, is the one that stands.
    const bool replaced = p.kind == PendingOwnership::Kind::kDrop &&
                          std::any_of(changes.begin() + static_cast<std::ptrdiff_t>(index + 1), changes.end(), [&](const PendingOwnership& c) {
                            return c.kind == PendingOwnership::Kind::kCreate && c.recorded &&
                                   OwnerKey(c.type, c.object) == OwnerKey(p.type, p.object);
                          });
    if (replaced) continue;
    // A catalog_access principal (rules only, or an administrator's token with rules) is
    // no store user or role: its rights are its token's rules, for that session only, so
    // nothing could read a view with them later. What it creates gets no owner (and loses
    // any record left by an earlier object of that name): a view it creates reads with
    // each reader's own rights, its creator's included, and fails when those don't reach
    // what the view reads.
    arrow::Status status;
    if (p.kind == PendingOwnership::Kind::kRename || p.kind == PendingOwnership::Kind::kDrop) {
      status = engine.ForgetObject(p.object, p.type);
    } else if (principal.catalog_rules && !principal.superuser) {
      status = engine.ForgetOwner(p.object, p.type);
    } else {
      status = engine.RecordOwner(p.object, p.type, principal, p.definition_hash);
    }
    if (!status.ok()) {
      Log(LogLevel::kWarning, "an object's access-control records could not be updated",
          {{"object", p.object.ToString()},
           {"change", p.kind == PendingOwnership::Kind::kDrop ? "drop" :
                      p.kind == PendingOwnership::Kind::kRename ? "rename" : "create"},
           {"error", status.message()}});
    }
  }
}

arrow::Status RecordCreatedOwners(RbacEngine& engine, const Principal& principal,
                                  std::vector<PendingOwnership>& changes,
                                  std::vector<RecordedOwnership>& undo) {
  const std::size_t first = undo.size();
  for (auto& p : changes) {
    if (p.kind == PendingOwnership::Kind::kDrop || p.recorded) continue;
    if (p.kind == PendingOwnership::Kind::kRename) {
      ObjectAccessRecords previous_access;
      auto status = engine.CopyObjectAccess(p.object, p.type, p.destination, &previous_access);
      if (!status.ok()) {
        std::vector<RecordedOwnership> mine(undo.begin() + static_cast<std::ptrdiff_t>(first), undo.end());
        UndoRecordedOwners(engine, mine);
        undo.resize(first);
        return status;
      }
      p.recorded = true;
      undo.push_back({p.destination, p.type, std::nullopt, std::move(previous_access)});
      continue;
    }
    bool seen = false;  // the same object twice in one statement (a re-plan)
    for (std::size_t i = first; i < undo.size(); ++i) seen = seen || undo[i].object.ToString() == p.object.ToString();
    if (seen) {
      p.recorded = true;
      continue;
    }
    // A new name is a new object, including after an earlier drop whose store write
    // failed or before upgrading from a server that left stale grants. Fail closed:
    // without a successful cleanup, no new object is created under those grants.
    std::optional<ObjectAccessRecords> previous_access;
    if (p.fresh) {
      previous_access.emplace();
      auto status = engine.ForgetObject(p.object, p.type, &*previous_access);
      if (!status.ok()) {
        std::vector<RecordedOwnership> mine(undo.begin() + static_cast<std::ptrdiff_t>(first), undo.end());
        UndoRecordedOwners(engine, mine);
        undo.resize(first);
        return status;
      }
    }
    const auto snapshot = engine.snapshot();
    std::optional<OwnerRecord> previous;
    if (const OwnerRecord* record = p.type == ObjectType::kTrigger ? snapshot->FindTriggerOwner(p.object)
                                                                 : snapshot->FindOwner(p.object)) {
      previous = *record;
    }
    // See ApplyOwnership: a catalog_access principal's creations get no owner.
    const bool rules_only = principal.catalog_rules && !principal.superuser;
    undo.push_back({p.object, p.type, std::move(previous), std::move(previous_access)});
    auto status = rules_only ? engine.ForgetOwner(p.object, p.type)
                             : engine.RecordOwner(p.object, p.type, principal, p.definition_hash);
    if (!status.ok()) {
      std::vector<RecordedOwnership> mine(undo.begin() + static_cast<std::ptrdiff_t>(first), undo.end());
      UndoRecordedOwners(engine, mine);
      undo.resize(first);
      return status;
    }
    p.recorded = true;
  }
  return arrow::Status::OK();
}

void UndoRecordedOwners(RbacEngine& engine, const std::vector<RecordedOwnership>& undo) {
  for (auto it = undo.rbegin(); it != undo.rend(); ++it) {
    auto status = it->previous_access ? engine.RestoreObjectAccess(it->object, it->type, *it->previous_access)
                  : it->previous ? engine.PutOwner(*it->previous) : engine.ForgetOwner(it->object, it->type);
    if (!status.ok()) {
      Log(LogLevel::kWarning, "the owner record of an object a failed statement would have created could not be put back",
          {{"object", it->object.ToString()}, {"error", status.message()}});
    }
  }
}

std::optional<SystemPrivilege> SystemPrivilegeForGatedCategory(const std::string& category) {
  auto starts = [&](const char* prefix) { return category.rfind(prefix, 0) == 0; };
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
  // Reading and writing files, URLs and object storage is checked by path, not by category
  // (storage_access.hpp).
  return std::nullopt;
}

void RegisterRbacEnforcement(dd::DBConfig& config) {
  InstallViewHook();
  InstallNameScopeHook();
  dd::OptimizerExtension extension;
  extension.pre_optimize_function = PreOptimize;
  dd::OptimizerExtension::Register(config, std::move(extension));
  dd::PlannerExtension planner_extension;
  planner_extension.post_bind_function = PostBind;
  dd::PlannerExtension::Register(config, std::move(planner_extension));
}

dd::shared_ptr<RbacSessionState> AttachRbacSession(dd::ClientContext& context, std::shared_ptr<RbacEngine> engine,
                                                   Principal principal) {
  auto state = dd::make_shared_ptr<RbacSessionState>(std::move(engine), std::move(principal));
  context.registered_state->Insert(kRbacSessionStateName, state);
  return state;
}

}  // namespace hardcharger::authz
