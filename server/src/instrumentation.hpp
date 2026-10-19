// Instrumentation: the server records its own lifecycle, its sessions, their
// statements and every execution in SQL tables: instances, sessions, sql_statements,
// sql_executions, and the views session_activity, active_sessions, session_stats and
// execution_details.
//
// By default the tables live in their own DuckDB file (instrumentation_db_path, next to
// the database), attached as _hardcharger_instr. instrumentation_catalog names a catalog
// the init SQL attaches instead (DuckLake or PostgreSQL), which several servers may share.
//
// Writes go through one background thread in order, on their own connection, so a
// statement never waits for its own record; a write that fails is logged and the others
// carry on. Clients can read the catalog (admins only) but never change it.
#pragma once

#include <duckdb_v2.h>

#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include "duckdb_v2.hpp"

namespace hardcharger {

class Session;
struct Config;
struct Runtime;

// The default name of the instrumentation catalog.
inline constexpr const char* kInstrumentationCatalog = "_hardcharger_instr";

using WallTime = std::chrono::system_clock::time_point;

// What one execution of a statement records.
struct ExecutionRecord {
  std::string execution_id;  // Warp SQL statement id of the request that ran it (q_..., q_....N in a script)
  std::string statement_id;  // its sql_statements row (q_..., or a prepared statement's p_...)
  std::optional<WallTime> enqueue_time;  // when it joined the statement queue, if it did
  WallTime start;
  std::optional<WallTime> end;          // the engine produced its outcome
  std::optional<WallTime> fetch_start;  // first batch read
  std::optional<WallTime> fetch_end;    // last batch read
  std::optional<WallTime> cursor_close;  // the result was let go
  int64_t rows = 0;
  std::string status = "executing";  // success, error, cancelled
  std::string error;
  // A cancelled execution's Warp SQL cancel_reason (client, timeout, killed, cancelled,
  // session_closed, server_shutdown), when the server knows it.
  std::string cancel_reason;
  std::string query_profile;  // DuckDB's JSON profile, when captured
};

class Instrumentation {
 public:
  Instrumentation(Runtime& runtime, duckdb_v2_instance_handle instance, std::string duckdb_version);
  ~Instrumentation();

  // File mode: attaches the instrumentation file. Before the init SQL runs, so the init
  // SQL can't take the name.
  void Attach();
  // Creates the tables and views (also in a catalog the init SQL attached), cleans up a
  // previous unclean shutdown's rows (file mode only: a shared catalog has live peers),
  // records this instance and starts the writer.
  void Start();
  // Closes this instance's open sessions and executions and the instance row (graceful),
  // writes everything queued and stops the writer. Later events are dropped.
  void Stop();

  const std::string& catalog() const { return catalog_; }
  // The file Attach() attached, as it spelled it (empty: an external catalog).
  const std::string& file() const { return file_; }
  const std::string& schema() const { return schema_; }

  void SessionStarted(const Session& session);
  void SessionEnded(const Session& session, std::string_view reason);
  void SessionTagChanged(const Session& session, const std::string& tag);
  // Records a statement under `id`, its Warp SQL id (q_..., or a prepared statement's
  // p_...): its text, how it arrived (`kind` 'statement' or 'prepared', the Warp SQL
  // endpoint), whether it was accepted.
  void StatementCreated(const Session& session, const std::string& id, std::string_view sql, std::string_view kind,
                        bool accepted, std::string_view error);
  void ExecutionStarted(const ExecutionRecord& record);
  // A statement waiting in the statement queue ('queued'), and its leaving it.
  void ExecutionQueued(const ExecutionRecord& record);
  void ExecutionAdmitted(const ExecutionRecord& record);
  void ExecutionFinished(const ExecutionRecord& record);

  // Quotes `text` as a SQL string literal ('it''s').
  static std::string Literal(std::string_view text);

  // For tests: queues `sql` like any record's write.
  void EnqueueForTesting(std::string sql) { Enqueue(std::move(sql)); }

 private:
  std::string Table(std::string_view name) const;  // "catalog"."schema"."name"
  void Enqueue(std::string sql);
  void Run(const std::string& sql);
  void WriterLoop();

  Runtime& runtime_;
  duckdb_v2_instance_handle instance_;
  const std::string duckdb_version_;
  const bool external_;  // a catalog the init SQL attached
  std::string catalog_;
  std::string file_;
  std::string schema_;
  warpduckdb::Connection connection_;

  std::mutex mutex_;
  std::condition_variable wake_;
  std::deque<std::string> queue_;
  bool stopping_ = false;
  bool stopped_ = false;
  std::thread writer_;
};

}  // namespace hardcharger
