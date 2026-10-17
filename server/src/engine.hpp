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
  std::unique_ptr<warpsql::EngineSession> NewSession(const warpsql::Principal& principal) override;

 private:
  void AttachSystemCatalog();
  void RunInitSql();

  Runtime& runtime_;
  std::shared_ptr<authz::AccessControl> access_;
  SensitivePaths sensitive_paths_;
  std::string system_catalog_dir_;
  std::unique_ptr<HealthChecker> health_;
  std::unique_ptr<Instrumentation> instrumentation_;
  std::unique_ptr<CatalogLog> catalog_log_;  // last: it takes the lines of everything above
};

class Session : public warpduckdb::DuckDBSession {
 public:
  // With `access`, the session's connection is bound to `principal` before anything
  // runs on it; a session that cannot be bound is refused (the constructor throws).
  Session(Runtime& runtime, const SensitivePaths& sensitive_paths, duckdb_v2_instance_handle instance,
          warpsql::Principal principal, bool capped, authz::AccessControl* access);
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
  std::optional<std::chrono::milliseconds> DefaultStatementTimeout() override;
  // Also stops a statement waiting in the statement queue.
  void Interrupt() override;

  // ---- What the settings, SQL functions and KILL SESSION read ----
  const std::string& id() const { return id_; }
  const warpsql::Principal& principal() const { return principal_; }
  bool admin() const { return principal_.role == "admin"; }
  Runtime& runtime() const { return runtime_; }
  int64_t started_at_us() const { return started_at_us_; }
  bool running() const { return running_ > 0; }

  // Whether the session is inside a transaction it opened (BEGIN, or autocommit off),
  // and whether that transaction failed and waits for a ROLLBACK.
  enum class Transaction { kNone, kOpen, kAborted };
  Transaction transaction() const { return transaction_; }

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
  LogLevel QueryLogLevel() const;
  void LogQuery(LogLevel level, std::string_view message, const LogFields& fields) const;
  // A tracked execution ended (its Execution calls it).
  void FinishRunning();

 private:
  // Throws once the session was killed or, under access control, unless it is bound
  // and still live.
  void CheckAlive() const;
  // Runs `run` (DuckDB) under access control when the session has it.
  warpsql::StatementOutcome Guarded(std::string_view sql, bool has_parameters,
                                    const std::function<warpsql::StatementOutcome()>& run);
  // Refuses what the principal may not run; returns the statement's type.
  DUCKDB_V2_STATEMENT_TYPE Authorize(const Piece& piece);
  warpsql::StatementOutcome RunCommand(const Piece& piece);
  // Takes a queue slot (when the queue is on), runs `work`, and tracks its outcome
  // until its rows are read: running state, metrics, the slot.
  warpsql::StatementOutcome Track(StatementKind kind, std::string_view sql, const std::string& statement_id,
                                  const std::function<warpsql::StatementOutcome()>& work);
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
  std::atomic<Transaction> transaction_ = Transaction::kNone;
  bool manual_commit_ = false;  // the autocommit session option is off
  int profile_mode_ = 0;        // what the connection's profiler is set to
  void NoteTransaction(const Piece& piece);

  mutable std::mutex settings_mutex_;
  Settings settings_;
  // Instrumentation's statement id for each prepared statement of this session.
  std::map<const warpsql::PreparedStatement*, std::string> prepared_statement_ids_;
};

// The session whose statement this thread is running, for the SQL functions; null
// outside a client statement (init SQL, the health check).
Session* CurrentSession();

}  // namespace hardcharger
