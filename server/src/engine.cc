#include "engine.hpp"

#include <fstream>
#include <sstream>

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

// Statements a "readonly" principal may run. A strict allow-list until the authorization
// module (with DuckDB's own read-only analysis) lands.
bool ReadOnlyStatement(DUCKDB_V2_STATEMENT_TYPE type) {
  return type == DUCKDB_V2_STATEMENT_TYPE_SELECT || type == DUCKDB_V2_STATEMENT_TYPE_EXPLAIN;
}

std::string Abbreviate(std::string_view sql) {
  constexpr std::size_t kMax = 4000;
  return sql.size() <= kMax ? std::string(sql) : std::string(sql.substr(0, kMax)) + "…";
}

}  // namespace

Backend::Backend(const Config& config, const License& license)
    : DuckDBBackend(EngineOptions(config)), config_(config), license_(license) {
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
  return info;
}

std::unique_ptr<warpsql::EngineSession> Backend::NewSession(const warpsql::Principal& principal) {
  return std::make_unique<Session>(instance(), principal, config_.print_queries);
}

Session::Session(duckdb_v2_instance_handle instance, warpsql::Principal principal,
                 bool print_queries)
    : DuckDBSession(instance), principal_(std::move(principal)), print_queries_(print_queries) {}

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
    Log(LogLevel::kInfo, "statement", {{"user", principal_.name}, {"sql", Abbreviate(sql)}});
  }
  Authorize(sql);
  return DuckDBSession::Execute(sql, parameters);
}

std::unique_ptr<warpsql::PreparedStatement> Session::Prepare(std::string_view sql) {
  if (print_queries_) {
    Log(LogLevel::kInfo, "prepare", {{"user", principal_.name}, {"sql", Abbreviate(sql)}});
  }
  Authorize(sql);
  return DuckDBSession::Prepare(sql);
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
  return DuckDBSession::Ingest(target, std::move(rows));
}

}  // namespace hardcharger
