#include "engine.hpp"

#include <fstream>
#include <sstream>

#include "auth.hpp"
#include "duckdb_sql.hpp"
#include "logging.hpp"
#include "version.hpp"

namespace hardcharger {

namespace json = boost::json;
using warpsql::Error;
using warpsql::ErrorCode;

namespace {

warpduckdb::DuckDBBackend::Options EngineOptions(const Config& config) {
  warpduckdb::DuckDBBackend::Options options;
  options.database = config.database.empty() ? ":memory:" : config.database;
  options.read_only = config.read_only;
  if (!config.memory_limit.empty()) {
    options.instance_options.emplace_back("memory_limit", config.memory_limit);
  }
  if (config.allow_unsigned_extensions) {
    options.instance_options.emplace_back("allow_unsigned_extensions", "true");
  }
  if (!config.storage_version.empty()) {
    options.attach_options.emplace_back("STORAGE_VERSION", config.storage_version);
  }
  return options;
}

// Without access control, its statements get a clear error instead of DuckDB's syntax error.
void RefuseAccessControlStatement(std::string_view sql) {
  if (auto kind = authz::AccessControlStatementKind(sql)) {
    throw Error(ErrorCode::kSqlError,
                "Hardcharger access control is not enabled on this server, so " + *kind +
                    " is not available. (Server operators: set rbac_store_uri, with a GizmoData "
                    "Enterprise license that includes the rbac feature)");
  }
}

// Statements a "readonly" principal may run. A strict allow-list until the authorization
// module (with DuckDB's own read-only analysis) lands.
bool ReadOnlyStatement(DUCKDB_V2_STATEMENT_TYPE type) {
  return type == DUCKDB_V2_STATEMENT_TYPE_SELECT || type == DUCKDB_V2_STATEMENT_TYPE_EXPLAIN;
}


}  // namespace

Backend::Backend(const Config& config, const License& license,
                 std::shared_ptr<authz::AccessControl> access)
    : DuckDBBackend(EngineOptions(config)),
      config_(config),
      license_(license),
      access_(std::move(access)) {
  // Before anything else runs on the instance: from here on every plan of a bound
  // session is checked. (The backend's health connection and the init SQL below are
  // Hardcharger's own; they carry no principal, so the check leaves them alone.)
  if (access_) access_->InstallEnforcement(instance());
  RunInitSql(config);
}

void Backend::RunInitSql(const Config& config) {
  std::string sql = config.init_sql_commands;
  if (!config.init_sql_commands_file.empty()) {
    std::ifstream in(config.init_sql_commands_file);
    if (!in) {
      throw std::runtime_error("cannot read init_sql_commands_file " + config.init_sql_commands_file);
    }
    std::stringstream contents;
    contents << in.rdbuf();
    sql += "\n" + contents.str();
  }
  if (sql.find_first_not_of(" \t\r\n;") == std::string::npos) {
    return;
  }
  warpduckdb::Connection connection;
  warpduckdb::Call([&](auto* err) {
    return duckdb_v2_connection_create(instance(), connection.out(), err);
  });
  try {
    warpduckdb::RunSql(connection.get(), sql);
  } catch (const Error& error) {
    throw std::runtime_error(std::string("init SQL failed: ") + error.what());
  }
  Log(LogLevel::kInfo, "init SQL ran");
}

warpsql::ServerIdentity Backend::Identity() {
  return {"Hardcharger", std::string(kVersion) + " (DuckDB " + duckdb_version() + ")"};
}

json::object Backend::ServerInfo() {
  json::object info = DuckDBBackend::ServerInfo();
  info["edition"] = license_.Edition();
  info["access_control"] = access_ != nullptr;
  return info;
}

std::unique_ptr<warpsql::EngineSession> Backend::NewSession(const warpsql::Principal& principal) {
  return std::make_unique<Session>(instance(), principal, config_.print_queries, access_.get());
}

// User connections are created in exactly one place, the DuckDBSession constructor, and
// bound here right after it, before any statement runs on them.
Session::Session(duckdb_v2_instance_handle instance, warpsql::Principal principal,
                 bool print_queries, authz::AccessControl* access)
    : DuckDBSession(instance),
      principal_(std::move(principal)),
      print_queries_(print_queries),
      access_controlled_(access != nullptr) {
  if (access_controlled_) {
    guard_ = access->Bind(connection(), IdentityOf(principal_));
  }
}

Session::~Session() {
  // The guard may outlive this session briefly (the store's listener holds it while it
  // walks the sessions); it must not reach the connection once the base class closes it.
  if (guard_) guard_->Detach();
}

void Session::RequireLive() const {
  if (!access_controlled_) return;
  if (!guard_) {
    // Fail closed: under access control, a session without a guard runs nothing.
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this request: the session is not bound to access control",
                "42501");
  }
  guard_->RequireLive();
}

void Session::Authorize(std::string_view sql) {
  if (principal_.role != "readonly") {
    return;
  }
  for (const auto& statement : warpduckdb::Parse(connection(), sql)) {
    DUCKDB_V2_STATEMENT_TYPE type = DUCKDB_V2_STATEMENT_TYPE_INVALID;
    duckdb_v2_sql_statement_get_type(statement.get(), &type, nullptr);
    if (!ReadOnlyStatement(type)) {
      throw Error(ErrorCode::kPermissionDenied,
                  "User '" + principal_.name +
                      "' has a readonly session and cannot run statements that modify state.",
                  "42501");
    }
  }
}

warpsql::StatementOutcome Session::Execute(std::string_view sql, const json::value& parameters) {
  if (print_queries_) {
    Log(LogLevel::kInfo, "statement", {{"user", principal_.name}, {"sql", LoggableSql(sql)}});
  }
  RequireLive();
  if (!access_controlled_) {
    RefuseAccessControlStatement(sql);
    Authorize(sql);
    return DuckDBSession::Execute(sql, parameters);
  }
  return guard_->Execute(sql, !parameters.is_null(), [&] {
    Authorize(sql);
    return DuckDBSession::Execute(sql, parameters);
  });
}

std::unique_ptr<warpsql::PreparedStatement> Session::Prepare(std::string_view sql) {
  if (print_queries_) {
    Log(LogLevel::kInfo, "prepare", {{"user", principal_.name}, {"sql", LoggableSql(sql)}});
  }
  RequireLive();
  if (!access_controlled_) {
    RefuseAccessControlStatement(sql);
    Authorize(sql);
    return DuckDBSession::Prepare(sql);
  }
  return guard_->Prepare(sql, [&] {
    Authorize(sql);
    return DuckDBSession::Prepare(sql);
  });
}

warpsql::StatementOutcome Session::ExecutePrepared(warpsql::PreparedStatement& statement,
                                                   const warpsql::Parameters& parameters) {
  RequireLive();
  if (!access_controlled_) return DuckDBSession::ExecutePrepared(statement, parameters);
  const bool has_parameters = parameters.rows != nullptr || !parameters.json.is_null();
  return guard_->ExecutePrepared(statement, has_parameters, [&](warpsql::PreparedStatement& inner) {
    return DuckDBSession::ExecutePrepared(inner, parameters);
  });
}

int64_t Session::Ingest(const warpsql::IngestTarget& target,
                        std::shared_ptr<arrow::RecordBatchReader> rows) {
  if (principal_.role == "readonly") {
    throw Error(ErrorCode::kPermissionDenied,
                "User '" + principal_.name + "' has a readonly session and cannot ingest data.",
                "42501");
  }
  if (print_queries_) {
    Log(LogLevel::kInfo, "ingest", {{"user", principal_.name}, {"table", target.table}});
  }
  RequireLive();
  if (!access_controlled_) return DuckDBSession::Ingest(target, std::move(rows));
  // One INSERT or CREATE TABLE AS on this session's connection, checked like any other.
  return guard_->Ingest(target, [&] { return DuckDBSession::Ingest(target, std::move(rows)); });
}

std::shared_ptr<arrow::RecordBatchReader> Session::Metadata(warpsql::MetadataKind kind,
                                                            const warpsql::MetadataFilter& filter) {
  RequireLive();
  if (!access_controlled_) return DuckDBSession::Metadata(kind, filter);
  return guard_->Metadata(kind, filter, [&](const warpsql::MetadataFilter& narrowed) {
    return DuckDBSession::Metadata(kind, narrowed);
  });
}

warpsql::OptionErrors Session::SetOptions(const json::object& options) {
  RequireLive();
  return DuckDBSession::SetOptions(options);
}

json::object Session::Options() {
  RequireLive();
  return DuckDBSession::Options();
}

void Session::Commit() {
  RequireLive();
  DuckDBSession::Commit();
}

void Session::Rollback() {
  RequireLive();
  DuckDBSession::Rollback();
}

}  // namespace hardcharger
