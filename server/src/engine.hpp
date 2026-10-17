// Hardcharger's engine: Warp SQL's DuckDB backend, with Hardcharger's statement path
// around it (guards, the gizmosql.* settings, KILL SESSION, the statement queue,
// metrics and statement logging) and the server's SQL functions.
#pragma once

#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "config.hpp"
#include "duckdb_backend.hpp"
#include "duckdb_session.hpp"
#include "guards.hpp"
#include "health.hpp"
#include "license.hpp"
#include "logging.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"

namespace hardcharger {

// The catalog with the server's own read-only views (gizmosql_index_info, ...).
inline constexpr const char* kSystemCatalog = "_gizmosql_system";

class Backend : public warpduckdb::DuckDBBackend {
 public:
  // Opens the database, attaches the system catalog and runs the init SQL; throws (and
  // the server doesn't start) if any of it fails.
  Backend(Runtime& runtime);
  ~Backend() override;

  warpsql::ServerIdentity Identity() override;
  boost::json::object ServerInfo() override;
  void CheckHealth() override;

  // Starts the background health check; call once the server is about to serve.
  void StartHealthCheck();
  void StopHealthCheck();

 protected:
  std::unique_ptr<warpsql::EngineSession> NewSession(const warpsql::Principal& principal) override;

 private:
  void AttachSystemCatalog();
  void RunInitSql();

  Runtime& runtime_;
  SensitivePaths sensitive_paths_;
  std::string system_catalog_dir_;
  std::unique_ptr<HealthChecker> health_;
};

class Session : public warpduckdb::DuckDBSession {
 public:
  Session(Runtime& runtime, const SensitivePaths& sensitive_paths, duckdb_v2_instance_handle instance,
          warpsql::Principal principal, bool capped);
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

  // Ends the session from another thread: its running or queued statement stops and
  // its next request fails with session_ended.
  void Kill();
  bool killed() const { return killed_; }

  // Settings chosen with SET [SESSION] gizmosql.*; unset ones follow the server's.
  struct Settings {
    std::optional<int64_t> query_timeout;
    std::optional<LogLevel> query_log_level;
    std::optional<bool> bypass_queue;
    std::optional<int64_t> max_queue_wait;
    std::string session_tag;
    std::string query_tag;
  };
  Settings settings() const;
  void UpdateSettings(const std::function<void(Settings&)>& update);
  LogLevel QueryLogLevel() const;

 private:
  void CheckAlive() const;
  // Refuses what the principal may not run; returns the statement's type.
  DUCKDB_V2_STATEMENT_TYPE Authorize(const Piece& piece);
  warpsql::StatementOutcome RunCommand(const Piece& piece);
  // Takes a queue slot (when the queue is on), runs `work`, and tracks its outcome
  // until its rows are read: running state, metrics, the slot.
  warpsql::StatementOutcome Track(StatementKind kind, std::string_view sql,
                                  const std::function<warpsql::StatementOutcome()>& work);

  Runtime& runtime_;
  const SensitivePaths& sensitive_paths_;
  const warpsql::Principal principal_;
  const std::string id_;
  const int64_t started_at_us_;
  std::atomic<int> running_ = 0;
  std::atomic<bool> killed_ = false;
  std::atomic<bool> interrupted_ = false;  // the current statement was cancelled

  mutable std::mutex settings_mutex_;
  Settings settings_;
};

// The session whose statement this thread is running, for the SQL functions; null
// outside a client statement (init SQL, the health check).
Session* CurrentSession();

}  // namespace hardcharger
