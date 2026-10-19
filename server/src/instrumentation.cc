#include "instrumentation.hpp"

#include <arrow/config.h>

#include <algorithm>
#include <filesystem>
#include <format>
#include <type_traits>
#include <unordered_map>

#include "duckdb_sql.hpp"
#include "engine.hpp"
#include "instance_info.hpp"
#include "logging.hpp"
#include "platform.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"
#include "version.hpp"

namespace hardcharger {

namespace {

// The DuckDB file's schema. No keys or indexes, by measurement: on a 5M-row table an ART
// primary key made batched inserts 2.5x slower and holds memory, while an update bounded
// by time (the writer's) reads only recent row groups and costs the same.
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
    statement_id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, sql_text VARCHAR NOT NULL, statement_kind VARCHAR,
    is_internal BOOLEAN NOT NULL, prepare_success BOOLEAN NOT NULL, prepare_error VARCHAR,
    created_time TIMESTAMP WITH TIME ZONE NOT NULL, query_tag JSON);
CREATE TABLE IF NOT EXISTS {P}.sql_executions (
    execution_id VARCHAR NOT NULL, statement_id VARCHAR NOT NULL, bind_parameters VARCHAR,
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
    statement_id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL REFERENCES {S}.sessions(session_id) ON DELETE CASCADE,
    sql_text VARCHAR NOT NULL, statement_kind VARCHAR, is_internal BOOLEAN NOT NULL, prepare_success BOOLEAN NOT NULL,
    prepare_error VARCHAR, created_time TIMESTAMP WITH TIME ZONE NOT NULL, query_tag JSON);
CREATE TABLE IF NOT EXISTS {S}.sql_executions (
    execution_id VARCHAR PRIMARY KEY,
    statement_id VARCHAR NOT NULL REFERENCES {S}.sql_statements(statement_id) ON DELETE CASCADE,
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

std::string Nullable(std::string_view text) { return text.empty() ? "NULL" : Instrumentation::Literal(text); }

std::string Json(std::string_view text) { return text.empty() ? "NULL" : Instrumentation::Literal(text) + "::JSON"; }

int64_t Micros(WallTime time) {
  return std::chrono::duration_cast<std::chrono::microseconds>(time.time_since_epoch()).count();
}

// date_diff('millisecond', from, to) as DuckDB counts it: millisecond boundaries crossed.
int64_t MillisBetween(WallTime from, WallTime to) {
  auto floor_ms = [](int64_t us) { return us >= 0 ? us / 1000 : -((-us + 999) / 1000); };
  return floor_ms(Micros(to)) - floor_ms(Micros(from));
}

// `text` as valid UTF-8 without NUL characters (both backends refuse those, and a 4000-byte
// cut of a statement can split a character): each bad byte becomes U+FFFD.
std::string Clean(std::string_view text) {
  std::string out;
  out.reserve(text.size());
  const auto* p = reinterpret_cast<const unsigned char*>(text.data());
  const std::size_t n = text.size();
  for (std::size_t i = 0; i < n;) {
    const unsigned char c = p[i];
    std::size_t length = 0;
    uint32_t code = 0;
    if (c >= 0x01 && c < 0x80) {
      out.push_back(static_cast<char>(c));
      i++;
      continue;
    }
    if (c >= 0xC2 && c <= 0xDF) {
      length = 2, code = c & 0x1F;
    } else if (c >= 0xE0 && c <= 0xEF) {
      length = 3, code = c & 0x0F;
    } else if (c >= 0xF0 && c <= 0xF4) {
      length = 4, code = c & 0x07;
    }
    bool ok = length > 0 && i + length <= n;
    for (std::size_t k = 1; ok && k < length; k++) {
      ok = (p[i + k] & 0xC0) == 0x80;
      code = (code << 6) | (p[i + k] & 0x3F);
    }
    // No overlong forms, surrogates or code points past U+10FFFF.
    ok = ok && !(length == 3 && (code < 0x800 || (code >= 0xD800 && code <= 0xDFFF))) &&
         !(length == 4 && (code < 0x10000 || code > 0x10FFFF));
    if (ok) {
      out.append(text.substr(i, length));
      i += length;
    } else {
      out.append("\xEF\xBF\xBD");
      i++;
    }
  }
  return out;
}

bool NeedsCleaning(std::string_view text) {
  for (unsigned char c : text) {
    if (c == 0 || c >= 0x80) return true;
  }
  return false;
}

// ---- A batch's rows travel as one JSON literal ----
//
// Both backends unpack it in a single scan: DuckDB with from_json(), PostgreSQL with
// json_to_recordset(). Measured, against the obvious VALUES list: DuckDB binds a VALUES
// list row by row (0.13 ms a row; from_json() takes 2.4 us), and PostgreSQL parses and
// plans one 10,000-row VALUES insert in ~100 ms twice (the postgres extension prepares
// each command, then runs it), where the JSON literal is one token.

enum class Kind { kText, kTime, kInt, kBool, kJson, kUuid };

struct Column {
  const char* name;
  Kind kind;
};

void AppendJsonString(std::string& out, std::string_view text) {
  out.push_back('"');
  for (char ch : text) {
    const auto c = static_cast<unsigned char>(ch);
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          std::format_to(std::back_inserter(out), "\\u{:04x}", c);
        } else {
          out.push_back(ch);
        }
    }
  }
  out.push_back('"');
}

// Appends the timestamp as SqlTimestamp spells it inside its quotes
// (2026-10-19 12:00:00.000000+00), without strftime: a batch formats hundreds of thousands.
void AppendTimeText(std::string& out, WallTime time) {
  const int64_t us = Micros(time);
  const int64_t seconds = us >= 0 ? us / 1000000 : -((-us + 999999) / 1000000);
  const int64_t fraction = us - seconds * 1000000;
  const auto day = std::chrono::floor<std::chrono::days>(std::chrono::sys_seconds(std::chrono::seconds(seconds)));
  const std::chrono::year_month_day date(day);
  const int64_t of_day = seconds - static_cast<int64_t>(day.time_since_epoch().count()) * 86400;
  std::format_to(std::back_inserter(out), "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:06}+00", static_cast<int>(date.year()),
                 static_cast<unsigned>(date.month()), static_cast<unsigned>(date.day()), of_day / 3600,
                 of_day / 60 % 60, of_day % 60, fraction);
}

// One JSON object per row, keyed c0, c1, ... in column order; a NULL is left out.
class JsonRows {
 public:
  JsonRows() : out_("[") {}
  void Begin() {
    if (rows_++ > 0) out_.push_back(',');
    out_.push_back('{');
    column_ = 0;
    first_ = true;
  }
  void End() { out_.push_back('}'); }
  void Null() { column_++; }
  void Str(std::string_view text) {
    Key();
    AppendJsonString(out_, NeedsCleaning(text) ? std::string_view(cleaned_ = Clean(text)) : text);
  }
  void StrOrNull(std::string_view text) { text.empty() ? Null() : Str(text); }
  void Time(WallTime time) {
    Key();
    out_.push_back('"');
    AppendTimeText(out_, time);
    out_.push_back('"');
  }
  void Time(const std::optional<WallTime>& time) { time ? Time(*time) : Null(); }
  void Int(std::optional<int64_t> value) {
    if (!value) return Null();
    Key();
    std::format_to(std::back_inserter(out_), "{}", *value);
  }
  void Bool(bool value) {
    Key();
    out_ += value ? "true" : "false";
  }
  std::string Finish() { return std::move(out_) + "]"; }

 private:
  void Key() {
    if (!first_) out_.push_back(',');
    first_ = false;
    std::format_to(std::back_inserter(out_), "\"c{}\":", column_++);
  }
  std::string out_;
  std::string cleaned_;
  std::size_t rows_ = 0;
  std::size_t column_ = 0;
  bool first_ = true;
};

// A PostgreSQL dollar-quoted string: no escapes are processed inside, whatever the server's
// standard_conforming_strings, and the tag is one the text doesn't contain.
std::string DollarQuoted(const std::string& text) {
  std::string tag = "$hc$";
  for (int i = 0; text.find(tag) != std::string::npos; i++) tag = "$hc" + std::to_string(i) + "$";
  return tag + text + tag;
}

// The rows as a relation with the columns' names and types:
//   (SELECT r.c0 AS name, r.c1::TIMESTAMPTZ AS ..., ... FROM (SELECT unnest(from_json(...)) AS r))
// in DuckDB, and in PostgreSQL
//   (SELECT r.c0 AS name, r.c1 AS ..., ... FROM json_to_recordset($hc$...$hc$) AS r (c0 varchar, ...))
std::string Relation(bool postgres, const std::vector<Column>& columns, JsonRows& rows) {
  std::string select, structure, types;
  for (std::size_t i = 0; i < columns.size(); i++) {
    const Kind kind = columns[i].kind;
    const char* sep = i ? ", " : "";
    if (postgres) {
      const char* type = kind == Kind::kTime ? "timestamptz" : kind == Kind::kInt ? "bigint"
                                                             : kind == Kind::kBool ? "boolean"
                                                                                   : "varchar";
      const char* cast = kind == Kind::kJson ? "::json" : kind == Kind::kUuid ? "::uuid" : "";
      std::format_to(std::back_inserter(select), "{}r.c{}{} AS {}", sep, i, cast, columns[i].name);
      std::format_to(std::back_inserter(types), "{}c{} {}", sep, i, type);
    } else {
      const char* type = kind == Kind::kInt ? "BIGINT" : kind == Kind::kBool ? "BOOLEAN" : "VARCHAR";
      const char* cast = kind == Kind::kTime ? "::TIMESTAMPTZ" : kind == Kind::kJson ? "::JSON"
                                                               : kind == Kind::kUuid ? "::UUID"
                                                                                     : "";
      std::format_to(std::back_inserter(select), "{}r.c{}{} AS {}", sep, i, cast, columns[i].name);
      std::format_to(std::back_inserter(structure), "{}\"c{}\":\"{}\"", i ? "," : "", i, type);
    }
  }
  if (postgres) {
    return "(SELECT " + select + " FROM json_to_recordset(" + DollarQuoted(rows.Finish()) + ") AS r (" + types + "))";
  }
  return "(SELECT " + select + " FROM (SELECT unnest(from_json(" + Instrumentation::Literal(rows.Finish()) + ", " +
         Instrumentation::Literal("[{" + structure + "}]") + ")) AS r))";
}

std::string ColumnNames(const std::vector<Column>& columns) {
  std::string names;
  for (std::size_t i = 0; i < columns.size(); i++) names += (i ? ", " : "") + std::string(columns[i].name);
  return names;
}

}  // namespace

// What one transaction writes: each session, statement and execution once, in the state
// of its last event in the batch. Rows first written by an earlier batch are updates.
struct Instrumentation::Batch {
  struct Session {
    SessionRecord row;
    std::optional<WallTime> stop;
    std::string stop_reason;
    std::string tag;
  };
  std::vector<Session> new_sessions;
  std::unordered_map<std::string, std::size_t> new_session_at;
  std::vector<SessionChange> session_changes;
  std::unordered_map<std::string, std::size_t> session_change_at;
  std::vector<StatementRecord> statements;
  std::vector<ExecutionEvent> new_executions;
  std::unordered_map<std::string, std::size_t> new_execution_at;
  std::vector<ExecutionEvent> execution_updates;
  std::unordered_map<std::string, std::size_t> execution_update_at;
  std::size_t events = 0;

  void Add(const Event& event) {
    events++;
    std::visit(
        [&](const auto& what) {
          using T = std::decay_t<decltype(what)>;
          if constexpr (std::is_same_v<T, SessionRecord>) {
            new_session_at[what.session_id] = new_sessions.size();
            new_sessions.push_back({what, std::nullopt, "", ""});
          } else if constexpr (std::is_same_v<T, SessionChange>) {
            if (auto it = new_session_at.find(what.session_id); it != new_session_at.end()) {
              Session& s = new_sessions[it->second];
              if (what.stop && !s.stop) {
                s.stop = what.stop;
                s.stop_reason = what.stop_reason;
              }
              if (what.tag) s.tag = *what.tag;
              return;
            }
            auto [it, fresh] = session_change_at.try_emplace(what.session_id, session_changes.size());
            if (fresh) {
              session_changes.push_back(what);
              return;
            }
            SessionChange& c = session_changes[it->second];
            if (what.stop && !c.stop) {
              c.stop = what.stop;
              c.stop_reason = what.stop_reason;
            }
            if (what.tag) c.tag = what.tag;
          } else if constexpr (std::is_same_v<T, StatementRecord>) {
            statements.push_back(what);
          } else if constexpr (std::is_same_v<T, ExecutionEvent>) {
            const std::string& id = what.record.execution_id;
            if (what.phase == Phase::kQueued || what.phase == Phase::kStarted) {
              new_execution_at[id] = new_executions.size();
              new_executions.push_back(what);
            } else if (auto it = new_execution_at.find(id); it != new_execution_at.end()) {
              new_executions[it->second] = what;  // its row, in its latest state
            } else if (auto [at, fresh] = execution_update_at.try_emplace(id, execution_updates.size()); fresh) {
              execution_updates.push_back(what);
            } else {
              execution_updates[at->second] = what;
            }
          }
        },
        event.what);
  }
};

std::string_view Instrumentation::StatusOf(const ExecutionEvent& e) {
  switch (e.phase) {
    case Phase::kQueued: return "queued";
    case Phase::kFinished: return e.record.status;
    default: return "executing";
  }
}

std::size_t Instrumentation::Event::Bytes() const {
  std::size_t bytes = sizeof(Event);
  std::visit(
      [&](const auto& what) {
        using T = std::decay_t<decltype(what)>;
        if constexpr (std::is_same_v<T, RawSql>) {
          bytes += what.sql.capacity();
        } else if constexpr (std::is_same_v<T, SessionRecord>) {
          bytes += what.session_id.capacity() + what.username.capacity() + what.role.capacity() +
                   what.auth_method.capacity() + what.connection_protocol.capacity();
        } else if constexpr (std::is_same_v<T, SessionChange>) {
          bytes += what.session_id.capacity() + what.stop_reason.capacity() + (what.tag ? what.tag->capacity() : 0);
        } else if constexpr (std::is_same_v<T, StatementRecord>) {
          bytes += what.statement_id.capacity() + what.session_id.capacity() + what.sql.capacity() +
                   what.kind.capacity() + what.error.capacity() + what.query_tag.capacity();
        } else {
          const ExecutionRecord& r = what.record;
          bytes += r.execution_id.capacity() + r.statement_id.capacity() + r.status.capacity() + r.error.capacity() +
                   r.cancel_reason.capacity() + r.query_profile.capacity();
        }
      },
      what);
  return bytes;
}

std::string Instrumentation::Literal(std::string_view text) { return SqlLiteral(text); }

Instrumentation::Instrumentation(Runtime& runtime, duckdb_v2_instance_handle instance, std::string duckdb_version,
                                 InstrumentationLimits limits)
    : runtime_(runtime),
      instance_(instance),
      duckdb_version_(std::move(duckdb_version)),
      limits_(limits),
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

std::string Instrumentation::NativeTable(std::string_view name) const { return Quoted(schema_) + "." + Quoted(name); }

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
  if (external_) {
    // The two backends: the DuckDB file (no instrumentation_catalog) and PostgreSQL.
    const std::string type = warpduckdb::QueryText(
        connection_.get(), "SELECT type FROM duckdb_databases() WHERE database_name = " + Literal(catalog_));
    if (type == "ducklake") {
      throw std::runtime_error(
          "Hardcharger can't keep instrumentation in catalog '" + catalog_ +
          "': it is a DuckLake catalog, which instrumentation doesn't support. Use PostgreSQL (recommended in "
          "production: attach it in the init SQL and name it in instrumentation_catalog), or leave "
          "instrumentation_catalog unset for the DuckDB file next to the database (instrumentation_db_path).");
    }
    if (type != "postgres") {
      throw std::runtime_error(
          "Hardcharger can't keep instrumentation in catalog '" + catalog_ + "': " +
          (type.empty() ? std::string("no catalog has that name (the init SQL must attach it)")
                        : "it is a " + type + " catalog") +
          ". instrumentation_catalog names a PostgreSQL catalog (recommended in production); leave it unset "
          "for the DuckDB file next to the database (instrumentation_db_path).");
    }
    postgres_ = true;
  }
  if (postgres_) {
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
  // Earlier Hardcharger builds kept statement and execution ids as UUIDs of the server's
  // own; they are now the Warp SQL ids clients hold (q_..., p_...), which such a table
  // can't hold. As above, startup says what to do rather than failing every insert. (The
  // tables that named statement_kind flight_method had UUID statement ids too.)
  for (const auto& [table, column] : {std::pair{"sql_statements", "statement_id"},
                                      std::pair{"sql_executions", "execution_id"},
                                      std::pair{"sql_executions", "statement_id"}}) {
    const std::string type = warpduckdb::QueryText(
        connection_.get(), "SELECT data_type FROM duckdb_columns() WHERE database_name = " + Literal(catalog_) +
                               " AND schema_name = " + Literal(schema_) + " AND table_name = " + Literal(table) +
                               " AND column_name = " + Literal(column));
    if (type == "UUID") {
      throw std::runtime_error("Hardcharger can't use instrumentation table " + Table(table) + ": its " + column +
                               " is a UUID, from an earlier Hardcharger. Statement and execution ids are now the "
                               "Warp SQL ids clients hold (q_... for a statement, p_... for a prepared "
                               "statement). Drop the instrumentation tables, or rename them to keep their rows "
                               "(ALTER TABLE ... RENAME TO ...), or use a new instrumentation database, and start "
                               "the server again.");
    }
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
  Log(LogLevel::kInfo, "instrumentation on",
      {{"catalog", catalog_}, {"schema", schema_}, {"backend", postgres_ ? "postgresql" : "duckdb"}});
}

void Instrumentation::Stop() {
  {
    std::unique_lock lock(mutex_);
    if (stopped_ || !writer_.joinable()) {
      stopped_ = true;
      return;
    }
    stopped_ = true;  // later events are dropped
    stopping_ = true;
    drain_until_ = std::chrono::steady_clock::now() + limits_.drain_deadline;
  }
  wake_.notify_all();
  writer_.join();
  const InstrumentationStats& stats = runtime_.instrumentation_stats;
  const LogFields fields = {{"written", std::to_string(stats.written.load())},
                            {"dropped", std::to_string(stats.dropped.load())},
                            {"failed", std::to_string(stats.failed.load())}};
  Log(stats.dropped.load() > 0 || stats.failed.load() > 0 ? LogLevel::kWarning : LogLevel::kInfo,
      "instrumentation stopped", fields);
}

void Instrumentation::Enqueue(Event event) {
  const std::size_t bytes = event.Bytes();
  {
    std::lock_guard lock(mutex_);
    if (stopped_ || !writer_.joinable()) return;
    if (queued_bytes_ + bytes > limits_.max_queued_bytes) {
      // Never block the statement on its record: count it and carry on.
      runtime_.instrumentation_stats.dropped++;
      return;
    }
    queued_bytes_ += bytes;
    queue_.push_back(std::move(event));
    runtime_.instrumentation_stats.queued++;
  }
  wake_.notify_one();
}

void Instrumentation::Run(const std::string& sql) {
  try {
    warpduckdb::RunSql(connection_.get(), sql);
  } catch (const std::exception& e) {
    // One failed write must not stop the others.
    Log(LogLevel::kWarning, "instrumentation write failed", {{"error", LoggableError("", e.what())}});
  }
}

void Instrumentation::WriterLoop() {
  InstrumentationStats& stats = runtime_.instrumentation_stats;
  uint64_t reported_drops = 0;
  std::optional<std::chrono::steady_clock::time_point> reported_at;  // the last drop report
  std::unique_lock lock(mutex_);
  for (;;) {
    wake_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
    if (queue_.empty() && stopping_) break;
    if (stopping_ && std::chrono::steady_clock::now() >= drain_until_) {
      // Past the drain deadline: what is left is dropped, and said so.
      const auto left = queue_.size();
      queue_.clear();
      queued_bytes_ = 0;
      stats.queued -= static_cast<int64_t>(left);
      stats.dropped += left;
      lock.unlock();
      Log(LogLevel::kWarning, "instrumentation backlog not written by the shutdown deadline: records dropped",
          {{"dropped", std::to_string(left)},
           {"deadline_seconds", std::to_string(limits_.drain_deadline.count() / 1000.0)}});
      lock.lock();
      break;
    }
    if (const auto delay = write_delay_ms_.load(); delay > 0) {  // tests: a slow catalog
      lock.unlock();
      std::this_thread::sleep_for(std::chrono::milliseconds(delay));
      lock.lock();
    }
    // A batch: everything queued, up to the cap; a raw statement (tests) runs alone.
    std::vector<Event> batch;
    const bool raw = std::holds_alternative<RawSql>(queue_.front().what);
    while (!queue_.empty() && batch.size() < limits_.max_batch_events &&
           (batch.empty() || (!raw && !std::holds_alternative<RawSql>(queue_.front().what)))) {
      queued_bytes_ -= queue_.front().Bytes();
      batch.push_back(std::move(queue_.front()));
      queue_.pop_front();
    }
    stats.queued -= static_cast<int64_t>(batch.size());
    lock.unlock();
    if (raw) {
      Run(std::get<RawSql>(batch.front().what).sql);
      stats.written++;
    } else {
      Write(batch, 0, batch.size());
    }
    // At most once a minute: a catalog that stays slow must not flood the output.
    const uint64_t dropped = stats.dropped.load();
    if (dropped > reported_drops &&
        (!reported_at || std::chrono::steady_clock::now() - *reported_at >= std::chrono::minutes(1))) {
      reported_at = std::chrono::steady_clock::now();
      Log(LogLevel::kWarning, "instrumentation queue full: records dropped",
          {{"dropped_total", std::to_string(dropped)},
           {"queue_limit_bytes", std::to_string(limits_.max_queued_bytes)}});
      reported_drops = dropped;
    }
    lock.lock();
  }
  lock.unlock();
  for (const std::string& sql : FinalSweep()) Run(sql);
}

bool Instrumentation::TryWrite(std::vector<Event>& events, std::size_t first, std::size_t last, std::string& error) {
  Batch batch;
  for (std::size_t i = first; i < last; i++) batch.Add(events[i]);
  try {
    const std::vector<std::string> statements = Render(batch);
    warpduckdb::RunSql(connection_.get(), "BEGIN TRANSACTION");
    try {
      for (const std::string& sql : statements) warpduckdb::RunSql(connection_.get(), sql);
      warpduckdb::RunSql(connection_.get(), "COMMIT");
    } catch (...) {
      try {
        warpduckdb::RunSql(connection_.get(), "ROLLBACK");
      } catch (const std::exception&) {
        // A failed COMMIT has ended the transaction already.
      }
      throw;
    }
    runtime_.instrumentation_stats.written += last - first;
    return true;
  } catch (const std::exception& e) {
    // As it may be logged: a connection error can quote the connection string.
    error = LoggableError("", e.what());
    return false;
  }
}

bool Instrumentation::Reachable() {
  if (!postgres_) return true;  // the file is local: a failure is the record's
  try {
    warpduckdb::RunSql(connection_.get(),
                       "CALL postgres_execute(" + Literal(catalog_) + ", 'SELECT 1', prepare = FALSE)");
    return true;
  } catch (const std::exception&) {
    return false;
  }
}

void Instrumentation::Write(std::vector<Event>& events, std::size_t first, std::size_t last) {
  std::string error;
  if (TryWrite(events, first, last, error)) return;
  if (!Reachable()) {
    // The catalog itself is failing (PostgreSQL down or unreachable), not one record:
    // splitting the batch would only fail every piece. Wait for it a few seconds (not
    // past a stop), try the batch again, and otherwise let it go, counted and said once.
    for (int attempt = 0; attempt < 3; attempt++) {
      {
        std::unique_lock lock(mutex_);
        if (wake_.wait_for(lock, std::chrono::seconds(1), [this] { return stopping_; })) break;
      }
      if (Reachable()) {
        if (TryWrite(events, first, last, error)) return;
        break;  // reachable again: the failure is in the batch, so split it below
      }
    }
    if (!Reachable()) {
      runtime_.instrumentation_stats.failed += last - first;
      Log(LogLevel::kWarning, "instrumentation catalog unavailable: records not written",
          {{"records", std::to_string(last - first)}, {"catalog", catalog_}, {"error", error}});
      return;
    }
  }
  if (last - first > 1) {
    // One bad record must not lose the batch: each half on its own, in order.
    const std::size_t middle = first + (last - first) / 2;
    Write(events, first, middle);
    Write(events, middle, last);
    return;
  }
  runtime_.instrumentation_stats.failed++;
  Log(LogLevel::kWarning, "instrumentation write failed", {{"error", error}});
}

std::vector<std::string> Instrumentation::Render(const Batch& b) const {
  const bool pg = postgres_;
  auto table = [&](std::string_view name) { return pg ? NativeTable(name) : Table(name); };
  std::vector<std::string> out;
  // PostgreSQL keeps foreign keys: a row whose parent was never recorded (dropped from a
  // full queue) is left out rather than failing the batch, and a row already there is kept.
  auto parent = [&](std::string_view parent_table, std::string_view key) -> std::string {
    if (!pg) return "";
    return std::format(" WHERE EXISTS (SELECT 1 FROM {} p WHERE p.{} = v.{}) ON CONFLICT DO NOTHING",
                       table(parent_table), key, key);
  };
  if (!b.new_sessions.empty()) {
    static const std::vector<Column> columns = {
        {"session_id", Kind::kText},  {"instance_id", Kind::kUuid}, {"username", Kind::kText},
        {"role", Kind::kText},        {"auth_method", Kind::kText}, {"peer", Kind::kText},
        {"connection_protocol", Kind::kText}, {"start_time", Kind::kTime}, {"status", Kind::kText},
        {"stop_time", Kind::kTime},   {"stop_reason", Kind::kText}, {"session_tag", Kind::kJson}};
    JsonRows rows;
    for (const auto& s : b.new_sessions) {
      rows.Begin();
      rows.Str(s.row.session_id), rows.Str(runtime_.instance_id), rows.Str(s.row.username), rows.Str(s.row.role);
      rows.Str(s.row.auth_method), rows.Str(""), rows.Str(s.row.connection_protocol), rows.Time(s.row.start);
      rows.Str(s.stop ? "closed" : "active"), rows.Time(s.stop), rows.StrOrNull(s.stop_reason);
      rows.StrOrNull(s.tag);
      rows.End();
    }
    out.push_back("INSERT INTO " + table("sessions") + " (" + ColumnNames(columns) + ") SELECT * FROM " +
                  Relation(pg, columns, rows) + " AS v" + (pg ? " ON CONFLICT DO NOTHING" : ""));
  }
  if (!b.statements.empty()) {
    static const std::vector<Column> columns = {
        {"statement_id", Kind::kText},   {"session_id", Kind::kText},      {"sql_text", Kind::kText},
        {"statement_kind", Kind::kText}, {"is_internal", Kind::kBool},     {"prepare_success", Kind::kBool},
        {"prepare_error", Kind::kText},  {"created_time", Kind::kTime},    {"query_tag", Kind::kJson}};
    JsonRows rows;
    for (const auto& s : b.statements) {
      rows.Begin();
      rows.Str(s.statement_id), rows.Str(s.session_id), rows.Str(s.sql), rows.StrOrNull(s.kind), rows.Bool(false);
      rows.Bool(s.accepted), rows.StrOrNull(s.error), rows.Time(s.created), rows.StrOrNull(s.query_tag);
      rows.End();
    }
    out.push_back("INSERT INTO " + table("sql_statements") + " (" + ColumnNames(columns) + ") SELECT * FROM " +
                  Relation(pg, columns, rows) + " AS v" + parent("sessions", "session_id"));
  }
  // An execution's row as of its latest event: in flight (queued, executing) only its times
  // so far, finished everything, with durations as date_diff('millisecond') counts them.
  static const std::vector<Column> execution_columns = {
      {"execution_id", Kind::kText},     {"statement_id", Kind::kText},      {"execution_start_time", Kind::kTime},
      {"execution_end_time", Kind::kTime}, {"enqueue_time", Kind::kTime},    {"fetch_start_time", Kind::kTime},
      {"fetch_end_time", Kind::kTime},   {"cursor_close_time", Kind::kTime}, {"rows_fetched", Kind::kInt},
      {"status", Kind::kText},           {"error_message", Kind::kText},     {"duration_ms", Kind::kInt},
      {"total_duration_ms", Kind::kInt}, {"query_profile", Kind::kJson},     {"cancel_reason", Kind::kText}};
  auto execution_row = [](JsonRows& rows, const ExecutionEvent& e) {
    const ExecutionRecord& r = e.record;
    const bool finished = e.phase == Phase::kFinished;
    const auto end = finished ? r.end : std::nullopt;
    const auto total_end = finished ? (r.fetch_end ? r.fetch_end : r.end) : std::nullopt;
    rows.Begin();
    rows.Str(r.execution_id), rows.Str(r.statement_id), rows.Time(r.start), rows.Time(end);
    rows.Time(r.enqueue_time), rows.Time(finished ? r.fetch_start : std::nullopt);
    rows.Time(finished ? r.fetch_end : std::nullopt), rows.Time(finished ? r.cursor_close : std::nullopt);
    rows.Int(finished ? std::optional<int64_t>(r.rows) : std::nullopt);
    rows.Str(StatusOf(e)), rows.StrOrNull(finished ? r.error : "");
    rows.Int(end ? std::optional<int64_t>(MillisBetween(r.start, *end)) : std::nullopt);
    rows.Int(total_end ? std::optional<int64_t>(MillisBetween(r.start, *total_end)) : std::nullopt);
    rows.StrOrNull(finished ? r.query_profile : ""), rows.StrOrNull(finished ? r.cancel_reason : "");
    rows.End();
  };
  if (!b.new_executions.empty()) {
    JsonRows rows;
    for (const auto& e : b.new_executions) execution_row(rows, e);
    out.push_back("INSERT INTO " + table("sql_executions") + " (" + ColumnNames(execution_columns) +
                  ") SELECT * FROM " + Relation(pg, execution_columns, rows) + " AS v" +
                  parent("sql_statements", "statement_id"));
  }
  if (!b.session_changes.empty()) {
    static const std::vector<Column> columns = {{"session_id", Kind::kText},
                                                {"stop_time", Kind::kTime},
                                                {"stop_reason", Kind::kText},
                                                {"tag_set", Kind::kBool},
                                                {"session_tag", Kind::kJson}};
    JsonRows rows;
    WallTime oldest = WallTime::max();
    for (const auto& c : b.session_changes) {
      rows.Begin();
      rows.Str(c.session_id), rows.Time(c.stop), rows.StrOrNull(c.stop_reason), rows.Bool(c.tag.has_value());
      rows.StrOrNull(c.tag ? *c.tag : "");
      rows.End();
      oldest = std::min(oldest, c.session_start);
    }
    // The DuckDB file has no index: the bound on start_time lets the scan skip every row
    // group older than the oldest session this batch changes. PostgreSQL has its key.
    out.push_back("UPDATE " + table("sessions") +
                  " AS s SET stop_time = CASE WHEN v.stop_time IS NOT NULL AND s.status = 'active' THEN v.stop_time "
                  "ELSE s.stop_time END, stop_reason = CASE WHEN v.stop_time IS NOT NULL AND s.status = 'active' THEN "
                  "v.stop_reason ELSE s.stop_reason END, status = CASE WHEN v.stop_time IS NOT NULL THEN 'closed' "
                  "ELSE s.status END, session_tag = CASE WHEN v.tag_set THEN v.session_tag ELSE s.session_tag END "
                  "FROM " + Relation(pg, columns, rows) + " AS v WHERE s.session_id = v.session_id" +
                  (pg ? "" : " AND s.start_time >= " + Time(oldest)));
  }
  if (!b.execution_updates.empty()) {
    JsonRows rows;
    WallTime oldest = WallTime::max();
    for (const auto& e : b.execution_updates) {
      execution_row(rows, e);
      // Its row's start: the enqueue time while it was queued, then the admission time.
      oldest = std::min(oldest, e.record.enqueue_time ? std::min(*e.record.enqueue_time, e.record.start)
                                                      : e.record.start);
    }
    // As above: in the file, only row groups at least as recent as the oldest execution
    // updated here are read.
    out.push_back("UPDATE " + table("sql_executions") +
                  " AS e SET execution_start_time = v.execution_start_time, execution_end_time = "
                  "v.execution_end_time, enqueue_time = v.enqueue_time, fetch_start_time = v.fetch_start_time, "
                  "fetch_end_time = v.fetch_end_time, cursor_close_time = v.cursor_close_time, rows_fetched = "
                  "v.rows_fetched, status = v.status, error_message = v.error_message, duration_ms = v.duration_ms, "
                  "total_duration_ms = v.total_duration_ms, query_profile = COALESCE(v.query_profile, "
                  "e.query_profile), cancel_reason = v.cancel_reason FROM " +
                  Relation(pg, execution_columns, rows) + " AS v WHERE e.execution_id = v.execution_id" +
                  (pg ? "" : " AND e.execution_start_time >= " + Time(oldest)));
  }
  if (pg && !out.empty()) {
    // One round trip: the commands go as one simple query (prepare=FALSE: the extension
    // would otherwise send each twice, to prepare it and then to run it), in this
    // transaction's connection.
    std::string commands;
    for (const auto& command : out) commands += (commands.empty() ? "" : ";\n") + command;
    return {"CALL postgres_execute(" + Literal(catalog_) + ", " + Literal(commands) + ", prepare = FALSE)"};
  }
  return out;
}

std::vector<std::string> Instrumentation::FinalSweep() const {
  // This instance's rows are no older than its start (a second's margin for clocks).
  const std::string since = Time(runtime_.started_at - std::chrono::seconds(1));
  auto table = [&](std::string_view name) { return postgres_ ? NativeTable(name) : Table(name); };
  const std::string me = Literal(runtime_.instance_id);
  const std::string sessions = "SELECT session_id FROM " + table("sessions") + " WHERE instance_id = " + me +
                               " AND start_time >= " + since;
  std::vector<std::string> sql = {
      "UPDATE " + table("sql_executions") +
          " SET execution_end_time = COALESCE(execution_end_time, now()), status = 'cancelled', error_message = "
          "'Server shut down' WHERE status IN ('executing', 'queued') AND execution_start_time >= " + since +
          " AND statement_id IN (SELECT statement_id FROM " + table("sql_statements") + " WHERE created_time >= " +
          since + " AND session_id IN (" + sessions + "))",
      "UPDATE " + table("sessions") +
          " SET stop_time = now(), status = 'closed', stop_reason = 'server_shutdown' WHERE instance_id = " + me +
          " AND status = 'active' AND start_time >= " + since,
      "UPDATE " + table("instances") +
          " SET stop_time = now(), status = 'stopped', stop_reason = 'graceful' WHERE instance_id = " + me};
  if (postgres_) {
    for (auto& command : sql) {
      command = "CALL postgres_execute(" + Literal(catalog_) + ", " + Literal(command) + ", prepare = FALSE)";
    }
  }
  return sql;
}

void Instrumentation::SessionStarted(const Session& session) {
  const auto& p = session.principal();
  RecordSessionStarted({.session_id = session.id(),
                        .username = p.name,
                        .role = p.role.empty() ? "user" : p.role,
                        .auth_method = p.auth_method.empty() ? "token" : p.auth_method,
                        .connection_protocol = runtime_.config.tls_cert.empty() ? "http" : "https",
                        .start = WallTime(std::chrono::microseconds(session.started_at_us()))});
}

void Instrumentation::RecordSessionStarted(SessionRecord record) { Enqueue(Event{std::move(record)}); }

void Instrumentation::SessionEnded(const Session& session, std::string_view reason) {
  RecordSessionEnded(session.id(), WallTime(std::chrono::microseconds(session.started_at_us())), reason);
}

void Instrumentation::RecordSessionEnded(const std::string& session_id, WallTime session_start,
                                         std::string_view reason) {
  Enqueue(Event{SessionChange{.session_id = session_id,
                              .session_start = session_start,
                              .stop = std::chrono::system_clock::now(),
                              .stop_reason = std::string(reason),
                              .tag = std::nullopt}});
}

void Instrumentation::SessionTagChanged(const Session& session, const std::string& tag) {
  Enqueue(Event{SessionChange{.session_id = session.id(),
                              .session_start = WallTime(std::chrono::microseconds(session.started_at_us())),
                              .stop = std::nullopt,
                              .stop_reason = "",
                              .tag = tag}});
}

void Instrumentation::StatementCreated(const Session& session, const std::string& id, std::string_view sql,
                                       std::string_view kind, bool accepted, std::string_view error) {
  RecordStatement({.statement_id = id,
                   .session_id = session.id(),
                   .sql = std::string(sql),
                   .kind = std::string(kind),
                   .accepted = accepted,
                   .error = std::string(error),
                   .created = std::chrono::system_clock::now(),
                   .query_tag = session.StatementQueryTag()});
}

void Instrumentation::RecordStatement(StatementRecord record) { Enqueue(Event{std::move(record)}); }

void Instrumentation::ExecutionStarted(const ExecutionRecord& r) { Enqueue(Event{ExecutionEvent{Phase::kStarted, r}}); }

void Instrumentation::ExecutionQueued(const ExecutionRecord& r) { Enqueue(Event{ExecutionEvent{Phase::kQueued, r}}); }

void Instrumentation::ExecutionAdmitted(const ExecutionRecord& r) {
  Enqueue(Event{ExecutionEvent{Phase::kAdmitted, r}});
}

void Instrumentation::ExecutionFinished(const ExecutionRecord& r) {
  Enqueue(Event{ExecutionEvent{Phase::kFinished, r}});
}

}  // namespace hardcharger
