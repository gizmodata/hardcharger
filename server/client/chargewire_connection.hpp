// The client's connection to a Hardcharger server (any Chargewire server, in fact), on the
// Chargewire client library. It takes GizmoSQL 1.x's FlightConnection's place, with the same
// calls, so the shell, the dot commands and the ported tests use it unchanged.
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include <arrow/result.h>
#include <arrow/status.h>
#include <arrow/table.h>
#include <boost/json.hpp>

#include "client_config.hpp"
#include "chargewire/client.hpp"

namespace hardcharger::client {

struct QueryResult {
  std::shared_ptr<arrow::Table> table;  // rows; null when `update`
  int64_t total_rows = 0;               // -1 if unknown
  bool update = false;                  // the statement returned no rows (DML, DDL, SET, ...)
  std::optional<int64_t> rows_affected;  // an update's count, when the engine counts one
};

class ChargewireConnection {
 public:
  ChargewireConnection() = default;
  ~ChargewireConnection() { Disconnect(); }
  ChargewireConnection(ChargewireConnection&& other) noexcept;
  ChargewireConnection& operator=(ChargewireConnection&&) = delete;
  ChargewireConnection(const ChargewireConnection&) = delete;
  ChargewireConnection& operator=(const ChargewireConnection&) = delete;

  // Signs in (password, or the server's browser sign-in for auth_type_external) and opens
  // a session.
  arrow::Status Connect(const ClientConfig& config);

  // Runs a statement. Rows come back as a table (at most `row_limit` when > 0, with the
  // full count in total_rows, -1 if it can't be counted); a statement without rows comes
  // back as an update.
  arrow::Result<QueryResult> ExecuteQuery(const std::string& sql, int64_t row_limit = 0);
  // Runs a statement for its effect; returns the rows it changed (0 when not counted).
  arrow::Result<int64_t> ExecuteUpdate(const std::string& sql);

  arrow::Result<std::shared_ptr<arrow::Table>> GetTables(const std::string& catalog = "",
                                                         const std::string& schema_pattern = "",
                                                         const std::string& table_pattern = "",
                                                         bool include_schema = false);
  arrow::Result<std::shared_ptr<arrow::Table>> GetDbSchemas(const std::string& catalog = "",
                                                            const std::string& schema_pattern = "");
  arrow::Result<std::shared_ptr<arrow::Table>> GetCatalogs();
  // The server-info document (Chargewire §10: server_name, server_version, engine, ...).
  arrow::Result<boost::json::object> GetServerInfo();

  /// Upload an Arrow Table to the server as a temporary table via bulk ingest
  /// (mode replace, so repeated uploads overwrite the previous one).
  arrow::Status UploadLastResult(const std::shared_ptr<arrow::Table>& table,
                                 const std::string& table_name = "_last_result");

  void Disconnect();

  /// Request cancellation (async-signal-safe: only sets an atomic flag).
  void RequestCancel() { cancel_requested_.store(true); }
  /// Cancels the running statement through Chargewire's cancel (called from the interrupt
  /// thread while the main thread waits on the statement).
  void SendCancelToServer();

  bool IsConnected() const { return session_ != nullptr; }
  // A script run without connection settings: Connect(config) is tried once, when something
  // first needs the server (RequireConnection), so a failure says why.
  void ConnectWhenNeeded(const ClientConfig& config) { deferred_ = config; }
  // Connects now if ConnectWhenNeeded asked to and it hasn't been tried; the status of that
  // attempt, or nullopt when there was nothing to try.
  std::optional<arrow::Status> ConnectDeferred();
  // Whether a statement is running now (for the interrupt thread).
  bool IsExecuting() const { return executing_.load(); }
  // The server's base URL (http(s)://host:port/chargewire), once connected.
  const std::string& server_url() const { return server_url_; }
  // The Chargewire session id.
  std::string session_id() const;

 private:
  arrow::Status ToStatus(const chargewire::client::Error& error) const;
  // After the server lost or ended the session (idle eviction, a restart, KILL SESSION):
  // opens a new one (signing in again first after session_ended), re-applies the tags,
  // and says so on stderr. Returns whether it did.
  bool Reopen(const chargewire::client::Error& error);
  // Runs `call` on the session; when the server no longer has the session, reopens it and,
  // for session_not_found (the statement never ran), runs `call` once more.
  template <class Call>
  auto WithSession(Call&& call) -> decltype(call());

  std::unique_ptr<chargewire::client::Client> client_;
  std::unique_ptr<chargewire::client::Session> session_;
  std::string server_url_;
  ClientConfig config_;  // what Connect was given, to sign in again
  std::optional<ClientConfig> deferred_;  // ConnectWhenNeeded's, until tried
  // Only cancellation and the connection's lifecycle take it; statements never do.
  std::mutex cancel_mutex_;
  std::atomic<bool> cancel_requested_{false};
  std::atomic<bool> executing_{false};
};

// Whether `conn` is connected, connecting it first when ConnectWhenNeeded asked to; when it
// isn't, says why on stderr (the connection's error, or how to connect).
bool RequireConnection(ChargewireConnection& conn);

// The hint for a session without a server.
inline constexpr const char* kNotConnectedHint =
    "Use '.connect HOST PORT USERNAME' (add --no-tls for a server without TLS) or '.connect URI' to connect.";

}  // namespace hardcharger::client
