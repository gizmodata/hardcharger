// A "readonly" principal's session (the sign-in token's role "readonly"), held to what
// DuckDB itself says each statement does, as GizmoSQL 1.x held it: a statement DuckDB
// prepares that modifies a database (the session's temp catalog included) or writes a
// file is refused before it runs, wherever the write sits (a data-modifying CTE such as
// WITH c AS (INSERT ... RETURNING *) SELECT ..., a COPY ... TO inside a CTE, EXPLAIN
// ANALYZE, a prepared statement). The statement-level allow-list in engine.cc decides
// first which kinds of statement a readonly session may run at all; this is the check of
// what they really do.
#include <duckdb.hpp>
#include <duckdb/common/exception.hpp>
#include <duckdb/execution/operator/helper/physical_reset.hpp>
#include <duckdb/execution/operator/helper/physical_set.hpp>
#include <duckdb/execution/operator/schema/physical_create_type.hpp>
#include <duckdb/execution/physical_operator.hpp>
#include <duckdb/execution/physical_plan_generator.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/client_context_state.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/prepared_statement_data.hpp>
#include <duckdb/main/setting_info.hpp>
#include <duckdb/parser/parser.hpp>
#include <duckdb/parser/parsed_data/create_type_info.hpp>
#include <duckdb/parser/statement/set_statement.hpp>

#include <algorithm>
#include <cctype>
#include <string>
#include <unordered_set>

#include "authz/authz.hpp"
#include "authz/duckdb_internal.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
using ddb::Str;
using OpType = dd::PhysicalOperatorType;

constexpr const char* kReadOnlyStateName = "hardcharger_read_only_session";

std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
  return s;
}

std::string RefusalMessage(const std::string& user) {
  return "User '" + user + "' has a readonly session and cannot run statements that modify state.";
}

// DuckDB expands a PIVOT whose values come from the data into CREATE TEMPORARY TYPE
// __pivot_enum_<uuid> AS ENUM (SELECT DISTINCT ...) and then the query; the enum is a
// write to the session's temp catalog that the user did not ask for (GizmoSQL 1.x lets it
// through the same way). Only a PIVOT can produce it: the statement-level check refuses
// a readonly session's own CREATE.
bool IsPivotEnumHelper(dd::PreparedStatementData& prepared) {
  if (prepared.statement_type != dd::StatementType::CREATE_STATEMENT || !prepared.physical_plan) return false;
  auto& root = prepared.physical_plan->Root();
  if (root.type != OpType::CREATE_TYPE) return false;
  const auto& info = *root.Cast<dd::PhysicalCreateType>().info;
  return info.temporary && Str(info.GetTypeName()).rfind("__pivot_enum_", 0) == 0;
}

// Operators that write outside the catalogs (files) or change the instance, which
// DuckDB's modified_databases does not list.
bool WritesOutsideCatalogs(OpType type) {
  switch (type) {
    case OpType::COPY_TO_FILE:
    case OpType::BATCH_COPY_TO_FILE:
    case OpType::COPY_DATABASE:
    case OpType::EXPORT:
    case OpType::CREATE_SECRET:
    case OpType::ATTACH:
    case OpType::DETACH:
    case OpType::LOAD:
    case OpType::VACUUM:
    case OpType::UPDATE_EXTENSIONS:
      return true;
    default:
      return false;
  }
}

bool PlanWritesOutsideCatalogs(const dd::PhysicalOperator& op) {
  if (WritesOutsideCatalogs(op.type)) return true;
  for (const auto& child : op.GetChildren()) {
    if (PlanWritesOutsideCatalogs(child.get())) return true;
  }
  return false;
}

class ReadOnlySessionState : public dd::ClientContextState {
 public:
  explicit ReadOnlySessionState(std::string user) : user_(std::move(user)) {}

  // DuckDB calls OnFinalizePrepare only when a registered state may ask for a rebind;
  // this one never does, it only refuses.
  bool CanRequestRebind() override { return true; }

  dd::RebindQueryInfo OnFinalizePrepare(dd::ClientContext&, dd::PreparedStatementData& prepared,
                                        dd::PreparedStatementMode) override {
    // EXPLAIN without ANALYZE only shows the plan: its own plan is one scan of the plan's
    // text. DuckDB lists the explained statement's writes, but none of them runs.
    if (prepared.statement_type == dd::StatementType::EXPLAIN_STATEMENT && prepared.physical_plan) {
      const auto& root = prepared.physical_plan->Root();
      if (root.type == OpType::COLUMN_DATA_SCAN && root.GetChildren().empty()) {
        return dd::RebindQueryInfo::DO_NOT_REBIND;
      }
    }
    auto modified = prepared.properties.modified_databases;
    if (IsPivotEnumHelper(prepared)) {
      for (auto it = modified.begin(); it != modified.end();) {
        it = Lower(Str(it->first)) == "temp" ? modified.erase(it) : std::next(it);
      }
    }
    if (!modified.empty() || (prepared.physical_plan && PlanWritesOutsideCatalogs(prepared.physical_plan->Root()))) {
      throw dd::PermissionException(RefusalMessage(user_));
    }
    return dd::RebindQueryInfo::DO_NOT_REBIND;
  }

 private:
  const std::string user_;
};

// Settings a readonly session may not change even for itself: they reach the host or
// the instance (files, extensions, secrets, resources), switch the SQL grammar, or make
// DuckDB write files (the profiler's output).
bool IsGuardedSetting(const std::string& name) {
  static const std::unordered_set<std::string> kNames = {
      // The instance and the host (guards.cc and gate.cc keep the same lists for users).
      "memory_limit", "max_memory", "threads", "worker_threads", "external_threads", "temp_directory",
      "max_temp_directory_size", "enable_external_access", "allow_unsigned_extensions",
      "allow_community_extensions", "allow_extensions_metadata_query", "autoinstall_known_extensions",
      "autoload_known_extensions", "custom_extension_repository", "autoinstall_extension_repository",
      "extension_directory", "enable_external_file_cache", "secret_directory", "allow_persistent_secrets",
      "disabled_filesystems", "allowed_directories", "allowed_paths", "home_directory", "lock_configuration",
      "access_mode", "checkpoint_threshold", "wal_autocheckpoint", "storage_compatibility_version",
      "default_block_size", "allow_extension_repositories", "extension_repository_directory",
      "extension_directories", "redact_http_logs", "allow_unredacted_secrets",
      // The SQL grammar the server's checks read.
      "current_dialect", "active_grammar_extensions", "enable_legacy_parser",
      // Files DuckDB writes, and the profiling the server sets up for instrumentation.
      "enable_profiling", "profiling_output", "profile_output", "profiling_mode", "custom_profiling_settings",
      "log_query_path", "enable_http_logging", "http_logging_output", "enable_logging", "logging_storage"};
  return kNames.contains(name);
}

// Whether a plain SET / RESET (no GLOBAL or SESSION) of `name` stays in the session.
// DuckDB decides by the setting: SET default_order = 'desc' changes it for every
// session, SET TimeZone only for this one. Unknown: no.
bool AutomaticScopeIsSession(dd::ClientContext& context, const std::string& name) {
  const dd::Identifier id(name);
  if (auto option = dd::DBConfig::GetOptionByName(id)) {
    switch (option->scope) {
      case dd::SettingScopeTarget::LOCAL_ONLY:
      case dd::SettingScopeTarget::LOCAL_DEFAULT:
        return true;
      case dd::SettingScopeTarget::GLOBAL_ONLY:
      case dd::SettingScopeTarget::GLOBAL_DEFAULT:
        return false;
      default:
        // A setting kept only per session (search_path, schema, enable_progress_bar):
        // it has no instance-wide value to change.
        return option->set_local != nullptr && option->set_global == nullptr;
    }
  }
  dd::ExtensionOption extension;
  if (dd::DBConfig::GetConfig(context).TryGetExtensionOption(id, extension)) {
    try {
      const auto scope = extension.ResolveScope(id, dd::SetScope::AUTOMATIC);
      return scope == dd::SetScope::SESSION || scope == dd::SetScope::LOCAL;
    } catch (const std::exception&) {
      return false;
    }
  }
  return false;
}

}  // namespace

void BindReadOnlySession(duckdb_v2_connection_handle connection, const std::string& user_name) {
  auto& context = *dd::capiv2::Convert(connection)->context;
  context.registered_state->Insert(kReadOnlyStateName, dd::make_shared_ptr<ReadOnlySessionState>(user_name));
}

bool ReadOnlySessionMaySet(duckdb_v2_connection_handle connection, std::string_view sql) {
  dd::vector<dd::unique_ptr<dd::SQLStatement>> statements;
  try {
    auto parser = ddb::StandaloneParser();
    parser.ParseQuery(std::string(sql));
    statements = std::move(parser.statements);
  } catch (const std::exception&) {
    return false;
  }
  if (statements.size() != 1 || statements[0]->type != dd::StatementType::SET_STATEMENT) return false;
  auto& set = statements[0]->Cast<dd::SetStatement>();
  const std::string name = Lower(Str(set.name));
  if (set.scope != dd::SetScope::VARIABLE && IsGuardedSetting(name)) return false;
  switch (set.scope) {
    case dd::SetScope::VARIABLE:  // SET VARIABLE / RESET VARIABLE: the session's own variables
      return true;
    case dd::SetScope::SESSION:
    case dd::SetScope::LOCAL:
      return true;
    case dd::SetScope::AUTOMATIC:
      return AutomaticScopeIsSession(*dd::capiv2::Convert(connection)->context, name);
    default:  // GLOBAL
      return false;
  }
}

}  // namespace hardcharger::authz
