// The client's connection to a Hardcharger server (any Chargewire server, in fact), on the
// Chargewire client library. It takes GizmoSQL 1.x's FlightConnection's place, with the same
// calls, so the shell, the dot commands and the ported tests use it unchanged.
#pragma once

#include <atomic>
#include <cstdint>
#include <chrono>
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
  // a session. A server whose discovery document says "state": "stopped" (a stand-in for a
  // stopped server, Chargewire §11.2) is connected lazily: nothing is sent after discovery,
  // and the sign-in and the session happen at the first request that does work (IsLazy).
  arrow::Status Connect(const ClientConfig& config);
  // The CLI's own SET hardcharger.session_tag / query_tag from `config`, at connect. On a
  // lazily connected session they wait for the first request that does work and run just
  // before it. A refused SET is a warning; Reopen applies them again on any new session.
  void ApplyOwnTags(const ClientConfig& config);

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
  void RequestCancel() {
    cancel_requested_.store(true);
    resume_->stop.store(true);  // a wait on a resuming server ends within a second
  }
  /// Cancels the running statement through Chargewire's cancel (called from the interrupt
  /// thread while the main thread waits on the statement).
  void SendCancelToServer();

  bool IsConnected() const { return session_ != nullptr; }
  // Connected lazily to a stopped server and not signed in yet: nothing that does work has
  // been sent, so nothing has started the server. Checks, the prompt and completion leave
  // such a connection alone; the first statement (or metadata, an ingest) signs in.
  bool IsLazy() const { return session_ != nullptr && session_->id().empty(); }
  // From the discovery document at connect (no request): the server's version, and its state
  // ("stopped" from a stand-in for a stopped server; empty when it serves).
  const std::string& server_version() const { return server_version_; }
  const std::string& server_state() const { return server_state_; }
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
  // What a person reads for `error`. Before a lazily connected session has signed in, a
  // refusal is the sign-in's (or the session's), said as Connect would have said it.
  arrow::Status ToStatus(const chargewire::client::Error& error) const;
  // Before a request that does work: the tags ApplyOwnTags held back, which sign in and open
  // the session first. The error that stops the request (the sign-in's), or OK.
  arrow::Status BeforeWork();
  // Runs one of the CLI's own SETs on the session.
  void RunOwnSet(const std::string& name, const std::string& value);
  // After the server lost or ended the session (idle eviction, a restart, KILL SESSION):
  // opens a new one (signing in again first after session_ended), re-applies the tags,
  // and says so on stderr. Returns whether it did.
  bool Reopen(const chargewire::client::Error& error);
  // Whether the client can sign in again by itself (a password or a client certificate it was
  // given; a browser sign-in needs the person).
  bool CanSignIn() const;
  // Signs in again that way; throws the server's refusal.
  void SignIn();
  // SignIn() when it can; false when it can't or the server refused.
  bool SignInAgain();
  // Runs `call` on the session; when the server no longer has the session, reopens it and,
  // for session_not_found (the statement never ran), runs `call` once more, but only when the
  // old session is known to have held no transaction: a SQL BEGIN's earlier statements were
  // rolled back with it, and running the next one alone in autocommit would commit it
  // without them.
  template <class Call>
  auto WithSession(Call&& call) -> decltype(call());
  // After a statement: when its text could have opened or ended a transaction (BEGIN,
  // COMMIT, ...), reads the session's transaction state from the server.
  void NoteStatement(const std::string& sql);

  std::unique_ptr<chargewire::client::Client> client_;
  std::unique_ptr<chargewire::client::Session> session_;
  std::string server_url_;
  std::string server_version_;
  std::string server_state_;
  ClientConfig config_;  // what Connect was given, to sign in again
  std::optional<ClientConfig> deferred_;  // ConnectWhenNeeded's, until tried
  // Only cancellation and the connection's lifecycle take it; statements never do.
  std::mutex cancel_mutex_;
  std::atomic<bool> cancel_requested_{false};
  std::atomic<bool> executing_{false};
  // Whether the session may hold a transaction open: false only while the server's session
  // document last said "none" and no statement since could have changed it.
  bool transaction_maybe_open_ = false;
  // Whether the session ran USE, SET, RESET or PRAGMA: a new session starts without what
  // they set, so a statement isn't run again there.
  bool session_state_changed_ = false;
  // ApplyOwnTags held the tags back for a lazily connected session's first request.
  bool tags_pending_ = false;

  // What the client shows while it waits for a server that is resuming (Chargewire §11.2).
  // Shared with the client's callback, so it outlives a move of this connection.
  struct ResumeStatus {
    std::mutex mutex;
    bool quiet = false;
    bool terminal = false;  // stderr is a terminal: one line, rewritten as it counts down
    size_t line_length = 0;  // of the countdown line shown, 0: none
    int attempt_shown = 0;
    std::atomic<bool> stop{false};  // Ctrl-C: stop waiting
    // Called by the client on each wait; false stops it.
    bool Report(int attempt, std::chrono::milliseconds retry_in, std::chrono::milliseconds elapsed,
                const std::string& detail);
    // Clears the countdown line, if one is shown, once the request has its answer.
    void Done();
  };
  std::shared_ptr<ResumeStatus> resume_ = std::make_shared<ResumeStatus>();
};

// Whether `conn` is connected, connecting it first when ConnectWhenNeeded asked to; when it
// isn't, says why on stderr (the connection's error, or how to connect).
bool RequireConnection(ChargewireConnection& conn);

// Said after "Connected to ..." when the server is stopped (connected lazily).
inline constexpr const char* kStoppedServerNote =
    "The server is stopped; it starts when you run the first statement.";

// The hint for a session without a server.
inline constexpr const char* kNotConnectedHint =
    "Use '.connect HOST PORT USERNAME' (add --no-tls for a server without TLS) or '.connect URI' to connect.";

}  // namespace hardcharger::client
