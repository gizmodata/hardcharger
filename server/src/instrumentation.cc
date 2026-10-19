#include "instrumentation.hpp"

#include <arrow/config.h>

#include <filesystem>
#include <format>

#include "duckdb_sql.hpp"
#include "engine.hpp"
#include "instance_info.hpp"
#include "logging.hpp"
#include "platform.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"
#include "tokens.hpp"
#include "version.hpp"

namespace hardcharger {

namespace {

// The instrumentation schema. No keys or CHECK
// constraints: DuckLake can't enforce them, and one schema serves every backend.
constexpr const char* kSchemaSql = R"SQL(
CREATE TABLE IF NOT EXISTS {P}.instances (
    instance_id UUID NOT NULL, cluster_id UUID, hardcharger_version VARCHAR NOT NULL,
    duckdb_version VARCHAR NOT NULL, arrow_version VARCHAR NOT NULL,
    hostname VARCHAR, hostname_arg VARCHAR, server_ip VARCHAR, port INTEGER, database_path VARCHAR,
    tls_enabled BOOLEAN NOT NULL, tls_cert_path VARCHAR, tls_key_path VARCHAR, mtls_required BOOLEAN NOT NULL,
    mtls_ca_cert_path VARCHAR, readonly BOOLEAN NOT NULL, os_platform VARCHAR, os_name VARCHAR,
    os_version VARCHAR, cpu_arch VARCHAR, cpu_model VARCHAR, cpu_count INTEGER, memory_total_bytes BIGINT,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL, stop_time TIMESTAMP WITH TIME ZONE, status VARCHAR NOT NULL,
    stop_reason VARCHAR, instance_tag JSON);
CREATE TABLE IF NOT EXISTS {P}.sessions (
    session_id VARCHAR NOT NULL, instance_id UUID NOT NULL, username VARCHAR NOT NULL, role VARCHAR NOT NULL,
    auth_method VARCHAR NOT NULL, peer VARCHAR NOT NULL, peer_identity VARCHAR, user_agent VARCHAR,
    connection_protocol VARCHAR NOT NULL, start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    stop_time TIMESTAMP WITH TIME ZONE, status VARCHAR NOT NULL, stop_reason VARCHAR, session_tag JSON);
CREATE TABLE IF NOT EXISTS {P}.sql_statements (
    statement_id UUID NOT NULL, session_id VARCHAR NOT NULL, sql_text VARCHAR NOT NULL, statement_kind VARCHAR,
    is_internal BOOLEAN NOT NULL, prepare_success BOOLEAN NOT NULL, prepare_error VARCHAR,
    created_time TIMESTAMP WITH TIME ZONE NOT NULL, query_tag JSON);
CREATE TABLE IF NOT EXISTS {P}.sql_executions (
    execution_id UUID NOT NULL, statement_id UUID NOT NULL, bind_parameters VARCHAR,
    execution_start_time TIMESTAMP WITH TIME ZONE NOT NULL, execution_end_time TIMESTAMP WITH TIME ZONE,
    enqueue_time TIMESTAMP WITH TIME ZONE, fetch_start_time TIMESTAMP WITH TIME ZONE,
    fetch_end_time TIMESTAMP WITH TIME ZONE, cursor_close_time TIMESTAMP WITH TIME ZONE, rows_fetched BIGINT,
    status VARCHAR NOT NULL, error_message VARCHAR, duration_ms BIGINT, total_duration_ms BIGINT,
    query_profile JSON, cancel_reason VARCHAR);
-- Tables made before cancel_reason existed get it.
ALTER TABLE {P}.sql_executions ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR;
CREATE OR REPLACE VIEW {P}.session_activity AS
SELECT i.instance_id, i.hardcharger_version, i.duckdb_version, i.arrow_version, i.hostname,
       i.hostname_arg, i.server_ip, i.port, i.database_path, i.start_time AS instance_start_time,
       i.stop_time AS instance_stop_time, i.status AS instance_status, i.stop_reason AS instance_stop_reason,
       i.instance_tag, s.session_id, s.username, s.role, s.auth_method, s.peer, s.start_time AS session_start_time,
       s.stop_time AS session_stop_time, s.status AS session_status, s.stop_reason AS session_stop_reason,
       s.session_tag, st.statement_id, st.sql_text, st.created_time AS statement_created_time, e.execution_id,
       e.bind_parameters, e.execution_start_time, e.execution_end_time, e.rows_fetched,
       e.status AS execution_status, e.error_message, e.duration_ms, e.query_profile, st.query_tag, i.cluster_id,
       e.fetch_start_time, e.fetch_end_time, e.cursor_close_time, e.total_duration_ms, e.cancel_reason
FROM {P}.instances i
LEFT JOIN {P}.sessions s ON i.instance_id = s.instance_id
LEFT JOIN {P}.sql_statements st ON s.session_id = st.session_id
LEFT JOIN {P}.sql_executions e ON st.statement_id = e.statement_id;
CREATE OR REPLACE VIEW {P}.active_sessions AS
SELECT s.session_id, s.instance_id, s.username, s.role, s.auth_method, s.peer, s.peer_identity, s.user_agent,
       s.connection_protocol, s.start_time, s.status, i.hostname, i.hostname_arg, i.server_ip, i.port,
       i.database_path, s.session_tag, i.instance_tag, EPOCH(now()) - EPOCH(s.start_time) AS session_duration_seconds,
       i.cluster_id
FROM {P}.sessions s JOIN {P}.instances i ON s.instance_id = i.instance_id
WHERE s.status = 'active' AND i.status = 'running';
CREATE OR REPLACE VIEW {P}.session_stats AS
SELECT s.session_id, s.instance_id, s.username, s.role, s.auth_method, s.peer, s.start_time, s.stop_time,
       s.status AS session_status, COUNT(DISTINCT st.statement_id) AS total_statements,
       COUNT(e.execution_id) AS total_executions,
       SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS successful_executions,
       SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END) AS failed_executions,
       SUM(CASE WHEN e.status = 'timeout' THEN 1 ELSE 0 END) AS timed_out_executions,
       SUM(CASE WHEN e.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_executions,
       SUM(e.rows_fetched) AS total_rows_fetched, AVG(e.duration_ms) AS avg_duration_ms,
       MAX(e.duration_ms) AS max_duration_ms, AVG(e.total_duration_ms) AS avg_total_duration_ms,
       MAX(e.total_duration_ms) AS max_total_duration_ms
FROM {P}.sessions s
LEFT JOIN {P}.sql_statements st ON s.session_id = st.session_id
LEFT JOIN {P}.sql_executions e ON st.statement_id = e.statement_id
GROUP BY ALL;
CREATE OR REPLACE VIEW {P}.execution_details AS
SELECT e.execution_id, e.statement_id, st.session_id, s.instance_id, st.sql_text, e.bind_parameters,
       e.execution_start_time, e.execution_end_time, e.enqueue_time,
       CASE WHEN e.enqueue_time IS NOT NULL
            THEN CAST(date_diff('millisecond', e.enqueue_time, e.execution_start_time) AS BIGINT) END AS queue_wait_ms,
       e.rows_fetched, e.status, e.error_message, e.duration_ms, e.query_profile, s.username, s.auth_method,
       s.peer, st.query_tag, s.session_tag, e.fetch_start_time, e.fetch_end_time, e.cursor_close_time,
       e.total_duration_ms, e.cancel_reason
FROM {P}.sql_executions e
JOIN {P}.sql_statements st ON e.statement_id = st.statement_id
JOIN {P}.sessions s ON st.session_id = s.session_id;
)SQL";

// The same schema in PostgreSQL's dialect, for a plain PostgreSQL catalog, created
// natively so it gets what PostgreSQL can enforce: keys, and foreign keys that cascade
// (deleting an instance removes its sessions, statements and executions).
constexpr const char* kPostgresSchemaSql = R"SQL(
CREATE SCHEMA IF NOT EXISTS {S};
CREATE TABLE IF NOT EXISTS {S}.instances (
    instance_id UUID PRIMARY KEY, cluster_id UUID, hardcharger_version VARCHAR NOT NULL,
    duckdb_version VARCHAR NOT NULL, arrow_version VARCHAR NOT NULL, hostname VARCHAR, hostname_arg VARCHAR,
    server_ip VARCHAR, port INTEGER, database_path VARCHAR, tls_enabled BOOLEAN NOT NULL, tls_cert_path VARCHAR,
    tls_key_path VARCHAR, mtls_required BOOLEAN NOT NULL, mtls_ca_cert_path VARCHAR, readonly BOOLEAN NOT NULL,
    os_platform VARCHAR, os_name VARCHAR, os_version VARCHAR, cpu_arch VARCHAR, cpu_model VARCHAR, cpu_count INTEGER,
    memory_total_bytes BIGINT, start_time TIMESTAMP WITH TIME ZONE NOT NULL, stop_time TIMESTAMP WITH TIME ZONE,
    status VARCHAR NOT NULL, stop_reason VARCHAR, instance_tag JSON);
CREATE TABLE IF NOT EXISTS {S}.sessions (
    session_id VARCHAR PRIMARY KEY, instance_id UUID NOT NULL REFERENCES {S}.instances(instance_id) ON DELETE CASCADE,
    username VARCHAR NOT NULL, role VARCHAR NOT NULL, auth_method VARCHAR NOT NULL, peer VARCHAR NOT NULL,
    peer_identity VARCHAR, user_agent VARCHAR, connection_protocol VARCHAR NOT NULL,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL, stop_time TIMESTAMP WITH TIME ZONE, status VARCHAR NOT NULL,
    stop_reason VARCHAR, session_tag JSON);
CREATE TABLE IF NOT EXISTS {S}.sql_statements (
    statement_id UUID PRIMARY KEY, session_id VARCHAR NOT NULL REFERENCES {S}.sessions(session_id) ON DELETE CASCADE,
    sql_text VARCHAR NOT NULL, statement_kind VARCHAR, is_internal BOOLEAN NOT NULL, prepare_success BOOLEAN NOT NULL,
    prepare_error VARCHAR, created_time TIMESTAMP WITH TIME ZONE NOT NULL, query_tag JSON);
CREATE TABLE IF NOT EXISTS {S}.sql_executions (
    execution_id UUID PRIMARY KEY,
    statement_id UUID NOT NULL REFERENCES {S}.sql_statements(statement_id) ON DELETE CASCADE,
    bind_parameters VARCHAR, execution_start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    execution_end_time TIMESTAMP WITH TIME ZONE, enqueue_time TIMESTAMP WITH TIME ZONE,
    fetch_start_time TIMESTAMP WITH TIME ZONE, fetch_end_time TIMESTAMP WITH TIME ZONE,
    cursor_close_time TIMESTAMP WITH TIME ZONE, rows_fetched BIGINT, status VARCHAR NOT NULL, error_message VARCHAR,
    duration_ms BIGINT, total_duration_ms BIGINT, query_profile JSON, cancel_reason VARCHAR);
CREATE INDEX IF NOT EXISTS sessions_instance_id ON {S}.sessions(instance_id);
CREATE INDEX IF NOT EXISTS sql_statements_session_id ON {S}.sql_statements(session_id);
CREATE INDEX IF NOT EXISTS sql_statements_created_time ON {S}.sql_statements(created_time);
CREATE INDEX IF NOT EXISTS sql_executions_statement_id ON {S}.sql_executions(statement_id);
CREATE INDEX IF NOT EXISTS sql_executions_start_time ON {S}.sql_executions(execution_start_time);
-- Tables made before cancel_reason existed get it.
ALTER TABLE {S}.sql_executions ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR;
CREATE OR REPLACE VIEW {S}.session_activity AS
SELECT i.instance_id, i.hardcharger_version, i.duckdb_version, i.arrow_version, i.hostname,
       i.hostname_arg, i.server_ip, i.port, i.database_path, i.start_time AS instance_start_time,
       i.stop_time AS instance_stop_time, i.status AS instance_status, i.stop_reason AS instance_stop_reason,
       i.instance_tag, s.session_id, s.username, s.role, s.auth_method, s.peer, s.start_time AS session_start_time,
       s.stop_time AS session_stop_time, s.status AS session_status, s.stop_reason AS session_stop_reason,
       s.session_tag, st.statement_id, st.sql_text, st.created_time AS statement_created_time, e.execution_id,
       e.bind_parameters, e.execution_start_time, e.execution_end_time, e.rows_fetched,
       e.status AS execution_status, e.error_message, e.duration_ms, e.query_profile, st.query_tag, i.cluster_id,
       e.fetch_start_time, e.fetch_end_time, e.cursor_close_time, e.total_duration_ms, e.cancel_reason
FROM {S}.instances i
LEFT JOIN {S}.sessions s ON i.instance_id = s.instance_id
LEFT JOIN {S}.sql_statements st ON s.session_id = st.session_id
LEFT JOIN {S}.sql_executions e ON st.statement_id = e.statement_id;
CREATE OR REPLACE VIEW {S}.active_sessions AS
SELECT s.session_id, s.instance_id, s.username, s.role, s.auth_method, s.peer, s.peer_identity, s.user_agent,
       s.connection_protocol, s.start_time, s.status, i.hostname, i.hostname_arg, i.server_ip, i.port,
       i.database_path, s.session_tag, i.instance_tag,
       EXTRACT(EPOCH FROM (now() - s.start_time)) AS session_duration_seconds, i.cluster_id
FROM {S}.sessions s JOIN {S}.instances i ON s.instance_id = i.instance_id
WHERE s.status = 'active' AND i.status = 'running';
CREATE OR REPLACE VIEW {S}.session_stats AS
SELECT s.session_id, s.instance_id, s.username, s.role, s.auth_method, s.peer, s.start_time, s.stop_time,
       s.status AS session_status, COUNT(DISTINCT st.statement_id) AS total_statements,
       COUNT(e.execution_id) AS total_executions,
       SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS successful_executions,
       SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END) AS failed_executions,
       SUM(CASE WHEN e.status = 'timeout' THEN 1 ELSE 0 END) AS timed_out_executions,
       SUM(CASE WHEN e.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_executions,
       SUM(e.rows_fetched) AS total_rows_fetched, AVG(e.duration_ms) AS avg_duration_ms,
       MAX(e.duration_ms) AS max_duration_ms, AVG(e.total_duration_ms) AS avg_total_duration_ms,
       MAX(e.total_duration_ms) AS max_total_duration_ms
FROM {S}.sessions s
LEFT JOIN {S}.sql_statements st ON s.session_id = st.session_id
LEFT JOIN {S}.sql_executions e ON st.statement_id = e.statement_id
GROUP BY s.session_id, s.instance_id, s.username, s.role, s.auth_method, s.peer, s.start_time, s.stop_time, s.status;
CREATE OR REPLACE VIEW {S}.execution_details AS
SELECT e.execution_id, e.statement_id, st.session_id, s.instance_id, st.sql_text, e.bind_parameters,
       e.execution_start_time, e.execution_end_time, e.enqueue_time,
       CASE WHEN e.enqueue_time IS NOT NULL
            THEN CAST(EXTRACT(EPOCH FROM (e.execution_start_time - e.enqueue_time)) * 1000 AS BIGINT) END AS queue_wait_ms,
       e.rows_fetched, e.status, e.error_message, e.duration_ms, e.query_profile, s.username, s.auth_method,
       s.peer, st.query_tag, s.session_tag, e.fetch_start_time, e.fetch_end_time, e.cursor_close_time,
       e.total_duration_ms, e.cancel_reason
FROM {S}.sql_executions e
JOIN {S}.sql_statements st ON e.statement_id = st.statement_id
JOIN {S}.sessions s ON st.session_id = s.session_id;
)SQL";

std::string Replace(std::string text, std::string_view from, std::string_view to) {
  for (auto at = text.find(from); at != std::string::npos; at = text.find(from, at + to.size())) {
    text.replace(at, from.size(), to);
  }
  return text;
}

std::string Quoted(std::string_view identifier) { return SqlIdentifier(identifier); }

std::string Time(WallTime time) { return SqlTimestamp(time); }

std::string Time(const std::optional<WallTime>& time) { return time ? Time(*time) : "NULL"; }

std::string Nullable(std::string_view text) { return text.empty() ? "NULL" : Instrumentation::Literal(text); }

std::string Json(std::string_view text) { return text.empty() ? "NULL" : Instrumentation::Literal(text) + "::JSON"; }

}  // namespace

std::string Instrumentation::Literal(std::string_view text) { return SqlLiteral(text); }

Instrumentation::Instrumentation(Runtime& runtime, duckdb_v2_instance_handle instance, std::string duckdb_version)
    : runtime_(runtime),
      instance_(instance),
      duckdb_version_(std::move(duckdb_version)),
      external_(!runtime.config.instrumentation_catalog.empty()),
      catalog_(external_ ? runtime.config.instrumentation_catalog : kInstrumentationCatalog),
      schema_(runtime.config.instrumentation_schema.empty() ? "main" : runtime.config.instrumentation_schema) {
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance_, connection_.out(), err); });
  // Times are written as UTC literals.
  warpduckdb::RunSql(connection_.get(), "SET TimeZone = 'UTC'");
}

Instrumentation::~Instrumentation() { Stop(); }

std::string Instrumentation::Table(std::string_view name) const {
  return Quoted(catalog_) + "." + Quoted(schema_) + "." + Quoted(name);
}

void Instrumentation::Attach() {
  if (external_) return;
  std::string path = runtime_.config.instrumentation_db_path;
  if (path.empty()) {
    // Next to the database file; in the working directory for an in-memory database.
    const auto& database = runtime_.config.database;
    const bool file = !database.empty() && database != ":memory:";
    path = (file ? std::filesystem::path(database).parent_path() / "hardcharger_instrumentation.db"
                 : std::filesystem::path("hardcharger_instrumentation.db"))
               .string();
  }
  file_ = path;
  // READ_WRITE: an instance opened read-only would otherwise open it read-only too.
  warpduckdb::RunSql(connection_.get(), "ATTACH " + Literal(path) + " AS " + Quoted(catalog_) + " (READ_WRITE)");
}

void Instrumentation::Start() {
  const std::string prefix = Quoted(catalog_) + "." + Quoted(schema_);
  const bool postgres =
      external_ && warpduckdb::QueryText(connection_.get(), "SELECT type FROM duckdb_databases() WHERE database_name = " +
                                                                Literal(catalog_)) == "postgres";
  if (postgres) {
    // Native DDL through the postgres extension, one command at a time (the schema text
    // has no ';' inside a statement); then DuckDB rereads the catalog.
    const std::string ddl = Replace(kPostgresSchemaSql, "{S}", Quoted(schema_));
    std::size_t start = 0;
    for (std::size_t end = ddl.find(';'); end != std::string::npos; start = end + 1, end = ddl.find(';', start)) {
      const std::string command = ddl.substr(start, end - start);
      if (command.find_first_not_of(" \t\r\n") == std::string::npos) continue;
      warpduckdb::RunSql(connection_.get(), "CALL postgres_execute(" + Literal(catalog_) + ", " + Literal(command) + ")");
    }
    warpduckdb::RunSql(connection_.get(), "CALL postgres_clear_cache()");
  } else {
    if (external_ && schema_ != "main") {
      warpduckdb::RunSql(connection_.get(), "CREATE SCHEMA IF NOT EXISTS " + prefix);
    }
    warpduckdb::RunSql(connection_.get(), Replace(kSchemaSql, "{P}", prefix));
  }
  // Hardcharger 0.3.0 kept session ids as UUIDs; a session's id is now its Warp SQL id
  // (s_...), which such a table can't hold. Its rows can't be carried over (the old ids
  // match no session anyone holds), so startup says what to do instead of failing on
  // every insert.
  for (const char* table : {"sessions", "sql_statements"}) {
    const std::string type = warpduckdb::QueryText(
        connection_.get(), "SELECT data_type FROM duckdb_columns() WHERE database_name = " + Literal(catalog_) +
                               " AND schema_name = " + Literal(schema_) + " AND table_name = " + Literal(table) +
                               " AND column_name = 'session_id'");
    if (type == "UUID") {
      throw std::runtime_error("instrumentation table " + Table(table) +
                               " is from Hardcharger 0.3.0, whose session ids were UUIDs; session ids are now the "
                               "Warp SQL session ids clients hold (s_...). Drop the instrumentation tables (or use a "
                               "new instrumentation database) and start the server again.");
    }
  }
  // Hardcharger 0.3.0 named sql_statements.statement_kind flight_method (GizmoSQL's Flight
  // SQL name); CREATE TABLE IF NOT EXISTS kept such a table, and every statement's insert
  // would fail. Startup says how to rename it instead.
  if (!warpduckdb::QueryText(connection_.get(),
                             "SELECT column_name FROM duckdb_columns() WHERE database_name = " + Literal(catalog_) +
                                 " AND schema_name = " + Literal(schema_) +
                                 " AND table_name = 'sql_statements' AND column_name = 'flight_method'")
           .empty()) {
    throw std::runtime_error("instrumentation table " + Table("sql_statements") +
                             " is from Hardcharger 0.3.0: its column flight_method is now statement_kind. Rename it "
                             "(ALTER TABLE ... RENAME COLUMN flight_method TO statement_kind), or drop the "
                             "instrumentation tables, and start the server again.");
  }
  // Rows a previous run left open (it was killed). Only in our own file: a shared
  // catalog's open rows may belong to live peers.
  if (!external_) {
    const std::string stale = "SELECT instance_id FROM " + Table("instances") + " WHERE status = 'running'";
    const std::string sessions = "SELECT session_id FROM " + Table("sessions") + " WHERE instance_id IN (" + stale + ")";
    warpduckdb::RunSql(connection_.get(),
                       "UPDATE " + Table("sql_executions") +
                           " SET execution_end_time = now(), status = 'error', error_message = 'Server shutdown "
                           "unexpectedly' WHERE status IN ('executing', 'queued') AND statement_id IN (SELECT "
                           "statement_id FROM " + Table("sql_statements") + " WHERE session_id IN (" + sessions + "));"
                           "UPDATE " + Table("sessions") +
                           " SET stop_time = COALESCE(stop_time, now()), status = 'closed', stop_reason = "
                           "COALESCE(stop_reason, 'unclean_shutdown') WHERE status = 'active' AND instance_id IN (" +
                           stale + ");"
                           "UPDATE " + Table("instances") +
                           " SET stop_time = now(), status = 'stopped', stop_reason = 'unclean_shutdown' WHERE "
                           "status = 'running';");
  }
  // What hardcharger_server_info() shows too (the backend collects it at startup; without
  // one, as in the unit tests, it is collected here, the same way).
  if (runtime_.instance_info.instance_id.empty()) {
    runtime_.instance_info =
        CollectInstanceInfo(runtime_.config, runtime_.instance_id, duckdb_version_, runtime_.started_at);
  }
  const InstanceInfo& i = runtime_.instance_info;
  Run("INSERT INTO " + Table("instances") +
      " (instance_id, cluster_id, hardcharger_version, duckdb_version, arrow_version, hostname, "
      "hostname_arg, server_ip, port, database_path, tls_enabled, tls_cert_path, tls_key_path, mtls_required, "
      "mtls_ca_cert_path, readonly, os_platform, os_name, os_version, cpu_arch, cpu_model, cpu_count, "
      "memory_total_bytes, start_time, status, instance_tag) VALUES (" +
      Literal(i.instance_id) + ", " + Nullable(i.cluster_id) + ", " + Literal(i.hardcharger_version) + ", " +
      Literal(i.duckdb_version) + ", " + Literal(i.arrow_version) + ", " + Nullable(i.hostname) + ", " +
      Nullable(i.hostname_arg) + ", NULL, " + std::to_string(i.port) + ", " + Nullable(i.database_path) + ", " +
      (i.tls_enabled ? "true" : "false") + ", " + Nullable(i.tls_cert_path) + ", " + Nullable(i.tls_key_path) + ", " +
      (i.mtls_required ? "true" : "false") + ", " + Nullable(i.mtls_ca_cert_path) + ", " +
      (i.readonly ? "true" : "false") + ", " + Nullable(i.os_platform) + ", " + Nullable(i.os_name) + ", " +
      Nullable(i.os_version) + ", " + Nullable(i.cpu_arch) + ", " + Nullable(i.cpu_model) + ", " +
      std::to_string(i.cpu_count) + ", " + std::to_string(i.memory_total_bytes) + ", " + Time(i.start_time) +
      ", 'running', " + Json(i.instance_tag) + ")");
  writer_ = std::thread([this] { WriterLoop(); });
  Log(LogLevel::kInfo, "instrumentation on", {{"catalog", catalog_}, {"schema", schema_}});
}

void Instrumentation::Stop() {
  {
    std::unique_lock lock(mutex_);
    if (stopped_ || !writer_.joinable()) {
      stopped_ = true;
      return;
    }
    stopped_ = true;  // later events are dropped
    const std::string me = Literal(runtime_.instance_id);
    const std::string sessions = "SELECT session_id FROM " + Table("sessions") + " WHERE instance_id = " + me;
    queue_.push_back("UPDATE " + Table("sql_executions") +
                     " SET execution_end_time = COALESCE(execution_end_time, now()), status = 'cancelled', "
                     "error_message = 'Server shut down' WHERE status IN ('executing', 'queued') AND statement_id IN "
                     "(SELECT statement_id FROM " + Table("sql_statements") + " WHERE session_id IN (" + sessions + "))");
    queue_.push_back("UPDATE " + Table("sessions") +
                     " SET stop_time = now(), status = 'closed', stop_reason = 'server_shutdown' WHERE instance_id = " +
                     me + " AND status = 'active'");
    queue_.push_back("UPDATE " + Table("instances") +
                     " SET stop_time = now(), status = 'stopped', stop_reason = 'graceful' WHERE instance_id = " + me);
    stopping_ = true;
  }
  wake_.notify_all();
  writer_.join();
}

void Instrumentation::Enqueue(std::string sql) {
  {
    std::lock_guard lock(mutex_);
    if (stopped_ || !writer_.joinable()) return;
    queue_.push_back(std::move(sql));
  }
  wake_.notify_one();
}

void Instrumentation::Run(const std::string& sql) {
  // In a catalog several servers share (DuckLake), two writers' commits can conflict:
  // such a write is tried again, a few times, before it is given up.
  constexpr int kAttempts = 6;
  for (int attempt = 1;; attempt++) {
    try {
      warpduckdb::RunSql(connection_.get(), sql);
      return;
    } catch (const std::exception& e) {
      const std::string error = e.what();
      // DuckLake reports a lost commit race as a conflict, or as "Failed to commit".
      const bool conflict = error.find("onflict") != std::string::npos ||
                            error.find("Failed to commit") != std::string::npos;
      if (conflict && attempt < kAttempts) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100 * attempt));
        continue;
      }
      // One failed write must not stop the others.
      Log(LogLevel::kWarning, "instrumentation write failed", {{"error", error}});
      return;
    }
  }
}

void Instrumentation::WriterLoop() {
  std::unique_lock lock(mutex_);
  for (;;) {
    wake_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
    if (queue_.empty() && stopping_) return;
    std::string sql = std::move(queue_.front());
    queue_.pop_front();
    lock.unlock();
    Run(sql);
    lock.lock();
  }
}

void Instrumentation::SessionStarted(const Session& session) {
  const auto& p = session.principal();
  Enqueue("INSERT INTO " + Table("sessions") +
          " (session_id, instance_id, username, role, auth_method, peer, connection_protocol, start_time, status, "
          "session_tag) VALUES (" + Literal(session.id()) + ", " + Literal(runtime_.instance_id) + ", " +
          Literal(p.name) + ", " + Literal(p.role.empty() ? "user" : p.role) + ", " +
          Literal(p.auth_method.empty() ? "token" : p.auth_method) + ", '', " +
          Literal(runtime_.config.tls_cert.empty() ? "http" : "https") + ", " +
          Time(WallTime(std::chrono::microseconds(session.started_at_us()))) + ", 'active', NULL)");
}

void Instrumentation::SessionEnded(const Session& session, std::string_view reason) {
  Enqueue("UPDATE " + Table("sessions") + " SET stop_time = now(), status = 'closed', stop_reason = " +
          Literal(reason) + " WHERE session_id = " + Literal(session.id()) + " AND status = 'active'");
}

void Instrumentation::SessionTagChanged(const Session& session, const std::string& tag) {
  Enqueue("UPDATE " + Table("sessions") + " SET session_tag = " + Json(tag) + " WHERE session_id = " +
          Literal(session.id()));
}

std::string Instrumentation::StatementCreated(const Session& session, std::string_view sql, std::string_view kind,
                                              bool accepted, std::string_view error) {
  std::string id = RandomUuid();
  Enqueue("INSERT INTO " + Table("sql_statements") +
          " (statement_id, session_id, sql_text, statement_kind, is_internal, prepare_success, prepare_error, "
          "created_time, query_tag) VALUES (" + Literal(id) + ", " + Literal(session.id()) + ", " + Literal(sql) + ", " +
          Literal(kind) + ", false, " + (accepted ? "true" : "false") + ", " + Nullable(error) + ", " +
          Time(std::chrono::system_clock::now()) + ", " + Json(session.settings().query_tag) + ")");
  return id;
}

void Instrumentation::ExecutionStarted(const ExecutionRecord& r) {
  Enqueue("INSERT INTO " + Table("sql_executions") +
          " (execution_id, statement_id, execution_start_time, enqueue_time, status) VALUES (" +
          Literal(r.execution_id) + ", " + Literal(r.statement_id) + ", " + Time(r.start) + ", " + Time(r.enqueue_time) +
          ", 'executing')");
}

void Instrumentation::ExecutionQueued(const ExecutionRecord& r) {
  Enqueue("INSERT INTO " + Table("sql_executions") +
          " (execution_id, statement_id, execution_start_time, enqueue_time, status) VALUES (" +
          Literal(r.execution_id) + ", " + Literal(r.statement_id) + ", " + Time(r.start) + ", " + Time(r.enqueue_time) +
          ", 'queued')");
}

void Instrumentation::ExecutionAdmitted(const ExecutionRecord& r) {
  Enqueue("UPDATE " + Table("sql_executions") + " SET status = 'executing', execution_start_time = " + Time(r.start) +
          " WHERE execution_id = " + Literal(r.execution_id) + " AND status = 'queued'");
}

void Instrumentation::ExecutionFinished(const ExecutionRecord& r) {
  // Durations from the stored times themselves, so they always agree with them.
  Enqueue("UPDATE " + Table("sql_executions") + " SET execution_end_time = " + Time(r.end) +
          ", fetch_start_time = " + Time(r.fetch_start) + ", fetch_end_time = " + Time(r.fetch_end) +
          ", cursor_close_time = " + Time(r.cursor_close) + ", rows_fetched = " + std::to_string(r.rows) +
          ", status = " + Literal(r.status) + ", error_message = " + Nullable(r.error) +
          ", cancel_reason = " + Nullable(r.cancel_reason) +
          // Only when captured: through the postgres extension a NULL arrives as text, which
          // PostgreSQL won't store in a json column.
          (r.query_profile.empty() ? std::string() : ", query_profile = " + Json(r.query_profile)) +
          ", duration_ms = date_diff('millisecond', execution_start_time, " + Time(r.end) + ")" +
          ", total_duration_ms = date_diff('millisecond', execution_start_time, " + Time(r.fetch_end ? r.fetch_end : r.end) +
          ") WHERE execution_id = " + Literal(r.execution_id));
}

}  // namespace hardcharger
