// Hardcharger's engine: Warp SQL's DuckDB backend, with Hardcharger's statement path
// around it (guards, the hardcharger.* settings, KILL SESSION, the statement queue,
// metrics and statement logging) and the server's SQL functions.
#pragma once

#include <atomic>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "authz/authz.hpp"
#include "config.hpp"
#include "duckdb_backend.hpp"
#include "duckdb_session.hpp"
#include "guards.hpp"
#include "health.hpp"
#include "catalog_log.hpp"
#include "instrumentation.hpp"
#include "logging.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"

namespace hardcharger {

// The catalog with the server's own read-only views (hardcharger_index_info, ...).
inline constexpr const char* kSystemCatalog = "_hardcharger_system";

class Backend : public warpduckdb::DuckDBBackend {
 public:
  // Opens the database, attaches the system catalog and runs the init SQL; throws (and
  // the server doesn't start) if any of it fails. With `access`, every session is under
  // access control.
  Backend(Runtime& runtime, std::shared_ptr<authz::AccessControl> access = nullptr);
  ~Backend() override;

  warpsql::ServerIdentity Identity() override;
  boost::json::object ServerInfo() override;
  void CheckHealth() override;

  // Starts the background health check; call once the server is about to serve.
  void StartHealthCheck();
  void StopHealthCheck();
  // Closes this instance's instrumentation records (sessions, instance) and stops it.
  void StopInstrumentation();

 protected:
  std::unique_ptr<warpsql::EngineSession> NewSession(const warpsql::Principal& principal,
                                                     const std::string& session_id) override;

 private:
  void AttachSystemCatalog();
  void RunInitSql();

  Runtime& runtime_;
  std::shared_ptr<authz::AccessControl> access_;
  // Shared with the instance's file system, which outlives this object's members.
  std::shared_ptr<SensitivePaths> sensitive_paths_;
  // The server's own files, which no SQL opens (guards.hpp): shared with the file
  // system guard in the instance.
  std::shared_ptr<PrivateFiles> private_files_;
  std::string system_catalog_dir_;
  std::unique_ptr<HealthChecker> health_;
  std::unique_ptr<Instrumentation> instrumentation_;
  std::unique_ptr<CatalogLog> catalog_log_;  // last: it takes the lines of everything above
};

// The ids one execution is logged and recorded in instrumentation under (Warp SQL ids,
// the ones clients hold). A submitted statement's statement and execution share its q_...
// id; the statements of a submitted script are its executions q_....1, q_....2, ... (by
// position in the script); a prepared statement's executions are each request's own q_...
struct ExecutionIds {
  std::string statement;  // its sql_statements row: the submitted q_..., or the prepared statement's p_...
  std::string execution;  // q_..., or q_....N
  bool prepared = false;  // `statement` is a prepared statement's
};

class Session : public warpduckdb::DuckDBSession {
 public:
  // With `access`, the session's connection is bound to `principal` before anything
  // runs on it; a session that cannot be bound is refused (the constructor throws).
  // `id` is the session's Warp SQL id (s_...), the one its client holds: the session's
  // only id, in HARDCHARGER_CURRENT_SESSION(), hardcharger_sessions(), KILL SESSION, the
  // logs and the instrumentation tables.
  Session(Runtime& runtime, const SensitivePaths& sensitive_paths, duckdb_v2_instance_handle instance,
          warpsql::Principal principal, std::string id, bool capped,
          authz::AccessControl* access);
  ~Session() override;

  warpsql::StatementOutcome Execute(std::string_view sql,
                                    const boost::json::value& parameters) override;
  std::unique_ptr<warpsql::PreparedStatement> Prepare(std::string_view sql) override;
  warpsql::StatementOutcome ExecutePrepared(warpsql::PreparedStatement& statement,
                                            const warpsql::Parameters& parameters) override;
  int64_t Ingest(const warpsql::IngestTarget& target,
                 std::shared_ptr<arrow::RecordBatchReader> rows) override;
  std::shared_ptr<arrow::RecordBatchReader> Metadata(warpsql::MetadataKind kind,
                                                     const warpsql::MetadataFilter& filter) override;
  warpsql::OptionErrors SetOptions(const boost::json::object& options) override;
  boost::json::object Options() override;
  void Commit() override;
  void Rollback() override;
  warpsql::TransactionState Transaction() override;
  std::optional<std::chrono::milliseconds> DefaultStatementTimeout() override;
  // Killed (KILL SESSION) or ended by access control: session_ended with its reason.
  void CheckEnded() override;
  // Also stops a statement waiting in the statement queue.
  void Interrupt() override;
  // Interrupt(), remembering why (Warp SQL's cancel_reason) for instrumentation and
  // hardcharger_active_connections().
  void Cancel(std::string_view reason) override;
  // The request's Warp-Query-Tag (Warp SQL §2.5), for the statement about to run.
  void SetQueryTag(const boost::json::object* tag) override;
  // The Warp SQL id the client holds for what is about to run (q_..., p_...): the id it
  // is logged and recorded in instrumentation under.
  void SetStatementId(std::string_view id) override;

  // ---- What the settings, SQL functions and KILL SESSION read ----
  const std::string& id() const { return id_; }
  const warpsql::Principal& principal() const { return principal_; }
  bool admin() const { return principal_.role == "admin"; }
  // An administrator whose token carries no catalog_access rules: the superuser, who
  // sees and manages every session. A restricted administrator (admin token with rules)
  // is not one, so it sees only its own session, like a non-administrator.
  bool full_admin() const { return admin() && (!guard_ || guard_->unrestricted()); }
  Runtime& runtime() const { return runtime_; }
  int64_t started_at_us() const { return started_at_us_; }
  bool running() const { return running_ > 0; }
  // The tenant of an access-control store user ("" for none).
  std::string tenant() const;

  // What the session is doing, for hardcharger_active_connections() (safe from any thread).
  struct Activity {
    std::string statement;            // what it runs now, as the logs show it; empty: nothing
    int64_t statement_started_us = 0;  // when it left the statement queue (since the epoch)
    int64_t queued_since_us = 0;       // waiting in the statement queue since; 0: not waiting
    bool ingesting = false;            // a bulk ingest is running
    std::string cancel_reason;         // why its latest statement was cancelled, until the next
    std::string session_tag_option;    // the Warp SQL session_tag option, as JSON text
  };
  Activity activity() const;
  // Why the current statement was cancelled (Cancel()), or empty.
  std::string cancel_reason() const;

  // Whether the session is inside a transaction (BEGIN, or autocommit off), and whether
  // that transaction failed and waits for a ROLLBACK: what Transaction() last said, for the
  // metrics (safe from any thread).
  warpsql::TransactionState transaction() const { return transaction_; }

  // Ends the session from another thread: its running or queued statement stops and
  // its next request fails with session_ended.
  void Kill();
  bool killed() const { return killed_; }

  // Settings chosen with SET [SESSION] hardcharger.*; unset ones follow the server's.
  struct Settings {
    std::optional<int64_t> query_timeout;
    std::optional<LogLevel> query_log_level;
    std::optional<bool> bypass_queue;
    std::optional<int64_t> max_queue_wait;
    std::optional<int> capture_query_profile;  // 0 off, 1 standard, 2 detailed
    std::string session_tag;
    std::string query_tag;
  };
  Settings settings() const;
  void UpdateSettings(const std::function<void(Settings&)>& update);
  // The query tag recorded with the statement starting now: the request's Warp-Query-Tag,
  // else SET hardcharger.query_tag. On the session's engine thread only.
  std::string StatementQueryTag() const;
  LogLevel QueryLogLevel() const;
  void LogQuery(LogLevel level, std::string_view message, const LogFields& fields) const;
  // A tracked execution ended (its Execution calls it).
  void FinishRunning();

 private:
  // Throws once the session was killed or, under access control, unless it is bound
  // and still live.
  void CheckAlive() const;
  std::string request_query_tag_;  // SetQueryTag()'s, as JSON text; engine thread only
  std::string request_statement_id_;  // SetStatementId()'s; engine thread only
  // The id of what runs now: the client's (SetStatementId()), or, for work that reached
  // the engine without one, a new id of the server's own (x_...).
  std::string RequestStatementId() const;
  // Runs `run` (DuckDB) under access control when the session has it.
  warpsql::StatementOutcome Guarded(std::string_view sql, bool has_parameters,
                                    const std::function<warpsql::StatementOutcome()>& run);
  // Refuses what the principal may not run; returns the statement's type.
  DUCKDB_V2_STATEMENT_TYPE Authorize(const Piece& piece);
  warpsql::StatementOutcome RunCommand(const Piece& piece);
  // Takes a queue slot (when the queue is on), runs `work`, and tracks its outcome
  // until its rows are read: running state, metrics, the slot.
  // `shown`: the statement as hardcharger_active_connections() shows it (default: `sql`
  // as the logs show it).
  // `ids`: what it is recorded and logged under.
  warpsql::StatementOutcome Track(StatementKind kind, std::string_view sql, const ExecutionIds& ids,
                                  const std::function<warpsql::StatementOutcome()>& work,
                                  std::string_view shown = {});
  // A new statement starts: the previous one's cancellation is over.
  void BeginStatement();
  // A catalog the server owns (instrumentation, logs) and what it is called in messages.
  struct ManagedCatalog {
    std::string name;
    std::string label;
  };
  std::vector<ManagedCatalog> ManagedCatalogs() const;
  void GuardManagedCatalog(const Piece& piece, DUCKDB_V2_STATEMENT_TYPE type, const ManagedCatalog& managed) const;
  // Turns DuckDB's profiler on or off for the next execution; returns where it will write
  // the profile (empty: none).
  std::string PrepareProfile(const std::string& execution_id);

  Runtime& runtime_;
  const SensitivePaths& sensitive_paths_;
  const warpsql::Principal principal_;
  const bool access_controlled_;  // bound to access control (RBAC, or catalog_access rules only)
  const bool rbac_;               // ... with a store: users, roles, grants, access-control statements
  std::shared_ptr<authz::SessionGuard> guard_;  // set exactly when access_controlled_
  const std::string id_;
  const int64_t started_at_us_;
  std::atomic<int> running_ = 0;
  std::atomic<bool> killed_ = false;
  std::atomic<bool> interrupted_ = false;  // the current statement was cancelled
  std::atomic<warpsql::TransactionState> transaction_ = warpsql::TransactionState::kNone;
  int profile_mode_ = 0;        // what the connection's profiler is set to

  mutable std::mutex settings_mutex_;
  Settings settings_;
  mutable std::mutex activity_mutex_;
  Activity activity_;
  // Each prepared statement's Warp SQL id (p_...), its id in the logs and instrumentation.
  std::map<const warpsql::PreparedStatement*, std::string> prepared_statement_ids_;
  // Each prepared statement's text, as the logs show it (for its executions' activity).
  std::map<const warpsql::PreparedStatement*, std::string> prepared_sql_;
};

// The session whose statement this thread is running, for the SQL functions; null
// outside a client statement (init SQL, the health check).
Session* CurrentSession();

}  // namespace hardcharger
