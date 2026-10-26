// Instrumentation: the server records its own lifecycle, its sessions, their
// statements and every execution in SQL tables: instances, sessions, sql_statements,
// sql_executions, and the views session_activity, active_sessions, session_stats and
// execution_details.
//
// Two backends: by default the tables live in their own DuckDB file
// (instrumentation_db_path, next to the database), attached as _hardcharger_instr, for
// development and a single server; instrumentation_catalog names a PostgreSQL catalog the
// init SQL attaches instead, which several servers may share, recommended in production.
// Any other kind of catalog (DuckLake among them) is refused at startup.
//
// Recording never waits on the tables. Each event is queued (bounded: when the queue is
// full, new events are dropped and counted) and one background thread writes them in
// batches, one transaction each, on its own connection. Within a batch the events of one
// execution (or session) fold into one row: an execution that starts and ends inside it is
// one INSERT with its outcome, and the rows of earlier batches are updated with one
// statement per table. In the DuckDB file, which has no indexes, each such update reads
// only the row groups recent enough to hold its rows (a time bound on the start column);
// PostgreSQL gets native SQL that its primary keys serve. A batch that fails is retried in
// halves, so one bad record never loses the others. Clients can read the catalog (admins
// only) but never change it.
#pragma once

#include <duckdb_v2.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <variant>
#include <unordered_map>
#include <vector>

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
  // Live SQL Monitor identity; not additional columns in the history tables.
  std::string session_id, username, sql, query_tag;
  std::string query_profile;  // DuckDB's JSON profile, when captured
};

// A sessions row as it starts.
struct SessionRecord {
  std::string session_id;
  std::string username;
  std::string role;
  std::string auth_method;
  std::string connection_protocol;
  WallTime start;
};

// A sql_statements row.
struct StatementRecord {
  std::string statement_id;
  std::string session_id;
  std::string sql;
  std::string kind;  // statement, prepared
  bool accepted = true;
  std::string error;
  WallTime created;
  std::string query_tag;  // JSON, or empty
};

// How much the writer may hold and how long it may take to finish at shutdown.
struct InstrumentationLimits {
  std::size_t max_queued_bytes = 128u << 20;  // then new events are dropped
  std::size_t max_batch_events = 16384;       // events one transaction writes
  std::chrono::milliseconds drain_deadline = std::chrono::seconds(30);  // Stop() writes the backlog this long
};

class Instrumentation {
 public:
  Instrumentation(Runtime& runtime, duckdb_v2_instance_handle instance, std::string duckdb_version,
                  InstrumentationLimits limits = {});
  ~Instrumentation();

  // File mode: attaches the instrumentation file. Before the init SQL runs, so the init
  // SQL can't take the name.
  void Attach();
  // Creates the tables and views (also in a PostgreSQL catalog the init SQL attached;
  // another kind of catalog is refused), cleans up a previous unclean shutdown's rows
  // (file mode only: a shared catalog has live peers), records this instance and starts
  // the writer.
  void Start();
  // Stops taking events, writes the backlog (for at most the drain deadline; what is left
  // then is dropped and counted), closes this instance's open sessions and executions and
  // the instance row (graceful), and stops the writer.
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
  // Synchronous live progress, independent of the asynchronous history writer.
  void ExecutionProgress(const ExecutionRecord& record);
  std::vector<ExecutionRecord> MonitorRecords();

  // The same, from plain records (what the Session overloads build).
  void RecordSessionStarted(SessionRecord record);
  void RecordSessionEnded(const std::string& session_id, WallTime session_start, std::string_view reason);
  void RecordStatement(StatementRecord record);

  // Quotes `text` as a SQL string literal ('it''s').
  static std::string Literal(std::string_view text);

  // For tests: queues `sql` like any record's write; makes the writer wait `delay` before
  // each write (a slow catalog).
  void EnqueueForTesting(std::string sql) { Enqueue(Event{RawSql{std::move(sql)}}); }
  void SetWriteDelayForTesting(std::chrono::milliseconds delay) { write_delay_ms_ = delay.count(); }

 private:
  struct RawSql {
    std::string sql;
  };
  struct SessionChange {
    std::string session_id;
    WallTime session_start;
    std::optional<WallTime> stop;
    std::string stop_reason;
    std::optional<std::string> tag;  // set: the new tag (empty: none)
  };
  enum class Phase { kQueued, kStarted, kAdmitted, kFinished };
  struct ExecutionEvent {
    Phase phase;
    ExecutionRecord record;
  };
  struct Event {
    std::variant<RawSql, SessionRecord, SessionChange, StatementRecord, ExecutionEvent> what;
    std::size_t Bytes() const;
  };
  struct Batch;
  static std::string_view StatusOf(const ExecutionEvent& event);  // its row's status

  std::string Table(std::string_view name) const;  // "catalog"."schema"."name"
  std::string NativeTable(std::string_view name) const;  // PostgreSQL's own: "schema"."name"
  void Enqueue(Event event);
  void Run(const std::string& sql);
  void WriterLoop();
  // Writes `events` [first, last) in order, in one transaction; on failure, each half on its
  // own, unless the catalog itself is unreachable (then they are let go, counted).
  void Write(std::vector<Event>& events, std::size_t first, std::size_t last);
  bool TryWrite(std::vector<Event>& events, std::size_t first, std::size_t last, std::string& error);
  bool Reachable();  // the catalog answers (PostgreSQL); the file always
  std::vector<std::string> Render(const Batch& batch) const;  // what one batch runs, in order
  std::vector<std::string> FinalSweep() const;  // Stop()'s closing updates

  Runtime& runtime_;
  duckdb_v2_instance_handle instance_;
  const std::string duckdb_version_;
  const InstrumentationLimits limits_;
  const bool external_;  // a catalog the init SQL attached
  bool postgres_ = false;
  std::string catalog_;
  std::string file_;
  std::string schema_;
  warpduckdb::Connection connection_;

  std::mutex monitor_mutex_;
  std::unordered_map<std::string, ExecutionRecord> monitor_;
  std::size_t monitor_finished_bytes_ = 0;
  std::deque<std::string> monitor_finished_;  // bounded bridge until history is written

  std::mutex mutex_;
  std::condition_variable wake_;
  std::deque<Event> queue_;
  std::size_t queued_bytes_ = 0;
  bool stopping_ = false;
  bool stopped_ = false;
  std::chrono::steady_clock::time_point drain_until_;
  std::atomic<int64_t> write_delay_ms_{0};  // tests only
  std::thread writer_;
};

}  // namespace hardcharger
