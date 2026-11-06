#include "chargewire_connection.hpp"

#include "plain_http_probe.hpp"
#include "password_prompt.hpp"
#include "platform.hpp"

#include <iostream>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <regex>
#include <utility>

#include <arrow/scalar.h>
#include <arrow/table.h>

#include "oauth_flow.hpp"
#include "output_renderer.hpp"
#include "sql_quoting.hpp"

namespace hardcharger::client {

namespace wc = chargewire::client;

namespace {

// Only interactive terminals get a transient progress line. Never mix it with data.
class QueryProgress {
 public:
  ~QueryProgress() { if (shown_) std::cerr << "\r\033[2K" << std::flush; }
  void Report(const wc::StatementDocument& doc) {
    std::lock_guard lock(mutex_);
    const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started_).count();
    if (seconds < 0.5) return;
    std::ostringstream line;
      double fraction = -1;
      if (const auto* p = doc.document.if_contains("progress"); p && p->is_object()) {
        if (const auto* f = p->as_object().if_contains("fraction"); f && f->is_number()) fraction = f->to_number<double>();
      }
      if (std::isfinite(fraction) && fraction >= 0 && fraction <= 1) {
        const int filled = static_cast<int>(fraction * 20);
        line << '[' << std::string(filled, '=') << std::string(20 - filled, ' ') << "] "
             << static_cast<int>(fraction * 100) << "% (estimated)";
      } else line << (doc.state == "queued" ? "Queued" : doc.state == "succeeded" ? "Receiving results" : "Running");
    line << " | " << std::fixed << std::setprecision(1) << seconds << "s";
    std::cerr << "\r\033[2K" << line.str() << std::flush;
    shown_ = true;
  }
 private:
  std::chrono::steady_clock::time_point started_ = std::chrono::steady_clock::now();
  std::mutex mutex_;
  bool shown_ = false;
};

// What a Chargewire reason means, for a person: why the server refused a token it issued
// (§4.2) or ended a session (§5.6).
std::string DescribeReason(const std::string& reason) {
  if (reason == "killed") return "an administrator ended it";
  if (reason == "credentials_changed") return "the user's password or credentials changed";
  if (reason == "principal_dropped") return "the user was dropped or disabled";
  if (reason == "token_revoked") return "its access token was revoked";
  if (reason == "token_expired") return "its access token expired";
  if (reason == "idle_in_transaction") return "it sat idle with a transaction open, which was rolled back";
  return reason;
}

}  // namespace

ChargewireConnection::ChargewireConnection(ChargewireConnection&& other) noexcept
    : client_(std::move(other.client_)),
      session_(std::move(other.session_)),
      server_url_(std::move(other.server_url_)),
      cancel_requested_(other.cancel_requested_.load()),
      transaction_maybe_open_(other.transaction_maybe_open_),
      session_state_changed_(other.session_state_changed_) {}

arrow::Status ChargewireConnection::ToStatus(const wc::Error& error) const {
  if (error.IsCancelled() || cancel_requested_.load(std::memory_order_relaxed)) {
    return arrow::Status::Cancelled("Query cancelled");
  }
  // The server refused the sign-in's token and said why: its reason, not its wording.
  if (error.code() == "invalid_token" && !error.reason().empty()) {
    return arrow::Status::IOError("The server no longer accepts this sign-in: " + DescribeReason(error.reason()) +
                                  " (" + error.reason() + "). Sign in again (.connect).");
  }
  return arrow::Status::IOError(error.what());
}

void ChargewireConnection::Disconnect() {
  // Serialized with the interrupt thread before the session goes.
  std::lock_guard cancel_lock(cancel_mutex_);
  if (session_) {
    session_->Close();  // best effort, bounded; the server reaps sessions it never hears from again
  }
  session_.reset();
  client_.reset();
  server_url_.clear();
}

void ChargewireConnection::SendCancelToServer() {
  std::lock_guard cancel_lock(cancel_mutex_);
  if (session_) session_->CancelRunning();
}

std::string ChargewireConnection::session_id() const { return session_ ? session_->id() : std::string(); }

arrow::Status ChargewireConnection::Connect(const ClientConfig& config) {
  Disconnect();
  RegisterArrowExtensionTypes();  // before any result is read

  if (!config.mtls_cert.empty() && config.mtls_key.empty()) {
    return arrow::Status::Invalid("mTLS private key must be provided with mTLS certificate");
  }
  if (config.mtls_cert.empty() && !config.mtls_key.empty()) {
    return arrow::Status::Invalid("mTLS certificate must be provided with mTLS private key");
  }
  if (config.port <= 0 || config.port > 65535) {
    return arrow::Status::Invalid("invalid port " + std::to_string(config.port));
  }

  wc::ConnectOptions options;
  options.host = config.host;
  options.port = static_cast<uint16_t>(config.port);
  options.base_path = config.base_path;
  options.user_agent = "hardcharger-client";
  options.tls.enabled = config.use_tls;
  options.tls.root_certs_file = config.tls_roots;
  options.tls.skip_verify = config.tls_skip_verify;
  options.tls.cert_file = config.mtls_cert;
  options.tls.key_file = config.mtls_key;
  options.resume_timeout = std::chrono::seconds(std::max(0, config.resume_timeout));
  resume_->quiet = config.quiet;
  resume_->terminal = StderrIsTerminal();
  resume_->stop.store(false);
  options.on_resuming = [status = resume_](const wc::ResumeProgress& progress) {
    return status->Report(progress.attempt, progress.retry_in, progress.elapsed, progress.detail);
  };

  const std::string origin = std::string(config.use_tls ? "https://" : "http://") + config.host + ":" +
                             std::to_string(config.port);
  const bool auth_attempted = config.auth_type_external || config.auth_type_mtls || !config.username.empty();
  // Turns the library's errors into what a person at the prompt can act on.
  auto friendly = [&](const wc::Error& e) -> arrow::Status {
    // A TLS alert (or problem) refusing the client certificate (Chargewire §4.4): the server
    // is running, and said why.
    if (e.code() == "certificate_revoked") {
      return arrow::Status::IOError("The server at " + origin + " refused the client certificate: certificate revoked");
    }
    if (e.code() == "certificate_required") {
      return arrow::Status::IOError("The server at " + origin +
                                    " refused the connection: it requires a client certificate (--mtls-cert and "
                                    "--mtls-key)");
    }
    if (e.code() == "client_connection_failed") {
      const std::string what = e.what();
      if (what.find("could not resolve host") != std::string::npos) {
        return arrow::Status::IOError("Could not resolve host '" + config.host + "'");
      }
      // The reason first (refused, timed out, reset), then what to check.
      std::string reason = what;
      if (what.find("refused") != std::string::npos) {
        reason = "connection refused";
      } else if (what.find("timed out") != std::string::npos) {
        reason = "timed out";
      } else if (what.find("reset") != std::string::npos || what.find("end of stream") != std::string::npos ||
                 what.find("closed") != std::string::npos) {
        reason = "the server closed the connection";
      }
      std::string message = "Could not connect to " + origin + ": " + reason;
      if (reason == "connection refused" || reason == "timed out") {
        message += ". Is the server running" + std::string(config.port == 31337 ? "" : " on that port") + "?";
      } else if (!config.use_tls) {
        // Plain HTTP to a TLS server: it closes the connection without an HTTP answer.
        message += ". If the server uses TLS, drop --no-tls (or --tls=false, ?tls=false).";
      }
      return arrow::Status::IOError(message);
    }
    // A TLS handshake that met a plain-HTTP server: say so, and how to connect without TLS. Never
    // fall back to plaintext by itself.
    if (e.code() == "client_tls_failed" && config.use_tls && std::string(e.what()).find("TLS handshake") != std::string::npos &&
        AnswersTlsWithHttp(config.host, config.port)) {
      const bool v6 = config.host.find(':') != std::string::npos;
      return arrow::Status::IOError("The server at " + (v6 ? "[" + config.host + "]" : config.host) + ":" +
                                    std::to_string(config.port) +
                                    " speaks plain HTTP, not TLS. Add --no-tls (or --tls=false; in a "
                                    "hardcharger:// URI ?tls=false; after .connect HOST PORT USERNAME, --no-tls) "
                                    "to connect without encryption.");
    }
    // What the server said: its problem code, and its detail when it sent one. Nothing
    // else (the request, the credentials) goes into the message.
    const std::string refusal =
        e.code() + (e.detail().empty() || e.detail() == e.code() ? std::string() : " (" + e.detail() + ")");
    if (config.auth_type_mtls && e.status() == 401) {
      return arrow::Status::IOError("Certificate sign-in to " + origin + " failed: " + refusal);
    }
    if (e.code() == "invalid_credentials" || (e.status() == 401 && auth_attempted)) {
      return arrow::Status::IOError("Authentication failed for " + origin + ": " + refusal);
    }
    if (e.status() == 401) {
      return arrow::Status::IOError("Connection to " + origin + " failed: " + refusal +
                                    " — authentication required. Provide --username or set HARDCHARGER_USER "
                                    "(or HARDCHARGER_ADMIN_USERNAME)");
    }
    return arrow::Status::IOError(e.what());
  };

  std::unique_ptr<wc::Client> client;
  std::unique_ptr<wc::Session> session;
  try {
    client = std::make_unique<wc::Client>(options);
    // A Chargewire server answers GET /chargewire; anything else (a GizmoSQL 1.x gRPC port, a web
    // server) is refused here with a clear message.
    const wc::Discovery discovery = client->Discover();
    if (config.auth_type_external) {
      ARROW_RETURN_NOT_OK(SignInWithBrowser(*client, discovery));
    } else if (config.auth_type_mtls) {
      client->SignInWithClientCertificate();
    } else if (!config.access_token.empty()) {
      client->SetAccessToken(config.access_token);
    } else if (!config.username.empty()) {
      client->SignInWithPassword(config.username, config.password);
    }
    session = client->CreateSession();
  } catch (const wc::Error& e) {
    return friendly(e);
  }

  std::lock_guard cancel_lock(cancel_mutex_);
  server_url_ = wc::BaseUrl(options);
  config_ = config;
  config_.output_stream = &std::cout;
  client_ = std::move(client);
  session_ = std::move(session);
  transaction_maybe_open_ = false;
  session_state_changed_ = false;
  resume_->Done();
  return arrow::Status::OK();
}

std::optional<arrow::Status> ChargewireConnection::ConnectDeferred() {
  if (!deferred_) return std::nullopt;
  const ClientConfig config = std::move(*deferred_);
  deferred_.reset();  // tried once
  arrow::Status status = Connect(config);
  if (!status.ok()) return status;
  // The tags, as main() sets them after a connection at startup.
  for (const auto& [name, value] : {std::pair{"session_tag", config.session_tag},
                                    std::pair{"query_tag", config.query_tag}}) {
    if (value.empty()) continue;
    auto result = ExecuteUpdate(std::string("SET hardcharger.") + name + " = " + QuoteSqlLiteral(value));
    if (!result.ok()) {
      std::cerr << "Warning: failed to set " << name << ": " << result.status().message() << std::endl;
    }
  }
  session_state_changed_ = false;  // its own tags: Reopen applies them again
  return status;
}

bool RequireConnection(ChargewireConnection& conn) {
  if (conn.IsConnected()) return true;
  if (auto status = conn.ConnectDeferred()) {
    if (status->ok()) return true;
    std::cerr << ErrorStyle() << "Error: " << status->message() << ErrorStyleReset() << std::endl;
    return false;
  }
  std::cerr << ErrorStyle() << "Error: not connected to a server. " << kNotConnectedHint << ErrorStyleReset()
            << std::endl;
  return false;
}

bool ChargewireConnection::CanSignIn() const {
  return client_ && (config_.auth_type_mtls || (!config_.auth_type_external && !config_.username.empty()));
}

void ChargewireConnection::SignIn() {
  if (config_.auth_type_mtls) {
    client_->SignInWithClientCertificate();
  } else {
    client_->SignInWithPassword(config_.username, config_.password);
  }
}

bool ChargewireConnection::SignInAgain() {
  if (!CanSignIn()) return false;
  try {
    SignIn();
    return true;
  } catch (const wc::Error&) {
    return false;  // the original refusal is reported
  }
}

bool ChargewireConnection::Reopen(const wc::Error& error) {
  const bool lost = error.code() == "session_not_found";
  const bool ended = error.code() == "session_ended";
  if ((!lost && !ended) || !client_) return false;
  try {
    if (ended && CanSignIn()) SignIn();
    std::unique_ptr<wc::Session> session;
    try {
      session = client_->CreateSession();
    } catch (const wc::Error& e) {
      // A restarted server that signs tokens with a new key (no fixed secret key) no longer
      // accepts this sign-in's token: sign in again with what the client has, then retry once.
      if (e.code() != "invalid_token" || !e.reason().empty() || !CanSignIn()) throw;
      SignIn();
      session = client_->CreateSession();
    }
    {
      std::lock_guard cancel_lock(cancel_mutex_);
      session_ = std::move(session);
    }
    transaction_maybe_open_ = false;
    session_state_changed_ = false;
  } catch (const wc::Error& e) {
    std::cerr << "Warning: the server "
              << (lost ? std::string("no longer has this session")
                       : "ended this session: " + DescribeReason(error.reason()) + " (" + error.reason() + ")")
              << ", and a new one could not be opened: " << ToStatus(e).message() << std::endl;
    return false;
  }
  std::cerr << "Warning: the server "
            << (lost ? std::string("no longer had this session (idle too long, or restarted)")
                     : "ended this session: " + DescribeReason(error.reason()) + " (" + error.reason() + ")")
            << "; a new session is open. Temporary tables and session settings are gone." << std::endl;
  for (const auto& [name, value] : {std::pair{"session_tag", config_.session_tag},
                                    std::pair{"query_tag", config_.query_tag}}) {
    if (value.empty()) continue;
    try {
      session_->Execute(std::string("SET hardcharger.") + name + " = " + QuoteSqlLiteral(value));
    } catch (const wc::Error&) {
    }
  }
  return true;
}

template <class Call>
auto ChargewireConnection::WithSession(Call&& call) -> decltype(call()) {
  struct ClearResumeLine {
    ResumeStatus& status;
    ~ClearResumeLine() { status.Done(); }
  } clear{*resume_};
  resume_->stop.store(cancel_requested_.load());
  try {
    try {
      return call();
    } catch (const wc::Error& e) {
      // The server refused the sign-in's token without a reason: it signs tokens with a key
      // this one wasn't signed with (a server restarted without a fixed secret key). The
      // request never ran: sign in again with what the client has and send it once more. A
      // session lost along with it is then handled below. A refusal with a reason (revoked,
      // credentials changed, ...) is the person's to act on, and is reported.
      if (e.code() != "invalid_token" || !e.reason().empty() || !SignInAgain()) throw;
      std::cerr << "Warning: the server no longer accepted this sign-in (restarted?); signed in again." << std::endl;
      return call();
    }
  } catch (const wc::Error& e) {
    const bool transaction_maybe_lost = transaction_maybe_open_;
    const bool state_lost = session_state_changed_;
    if (!Reopen(e)) throw;
    if (e.code() == "session_not_found") {
      // It never ran: the server had no session to run it in. Run it in the new one, unless
      // the old one may have held a transaction (rolled back with it: this statement would
      // commit alone, without the statements before it) or had USE or SET (gone with it: this
      // statement would run against another schema or with other settings).
      if (transaction_maybe_lost) {
        throw wc::Error(e.status(), e.code(),
                        "The statement did not run: the server no longer had this session, which may have held "
                        "a transaction open, and anything it had not committed was rolled back. It was not run "
                        "again; run the transaction again to run it in the new session.");
      }
      if (state_lost) {
        throw wc::Error(e.status(), e.code(),
                        "The statement did not run: the server no longer had this session, and what it was set to "
                        "with USE or SET is gone with it, so the statement could run against another schema or with "
                        "other settings. It was not run again; run USE or SET again, then the statement.");
      }
      return call();
    }
    // session_ended: the server stopped the statement with its session (a kill can come
    // while it runs), so it isn't run again. The server's advice (sign in again, open a new
    // session) is what Reopen just did: say what became of the statement instead.
    throw wc::Error(e.status(), e.code(),
                    "The statement did not complete: its session ended" +
                        (e.reason().empty() ? std::string() : " (" + e.reason() + ")") +
                        ". It was not run again; run it again to run it in the new session.");
  }
}

bool ChargewireConnection::ResumeStatus::Report(int attempt, std::chrono::milliseconds retry_in,
                                                std::chrono::milliseconds elapsed, const std::string& detail) {
  if (stop.load()) return false;
  std::lock_guard lock(mutex);
  if (quiet) return true;
  const auto seconds = [](std::chrono::milliseconds ms) { return static_cast<long long>((ms.count() + 999) / 1000); };
  const long long spent = elapsed.count() / 1000;
  char clock[32];
  std::snprintf(clock, sizeof clock, "%lld:%02lld", spent / 60, spent % 60);
  // The server's detail in parentheses, unless it only restates the sentence (the spec's
  // "the server is resuming; retry after 5 s"): a holder's own words are worth showing.
  static const std::regex kStock(R"(^\s*the server is resuming\b[^a-z]*(retry after \d+ s)?\.?\s*$)",
                                 std::regex::icase);
  const std::string aside = std::regex_match(detail, kStock) ? "" : " (" + detail + ")";
  if (terminal) {
    // One line, rewritten each second: the countdown, the time spent, and how to stop.
    // Overwritten with spaces, not an escape sequence, so any console shows it.
    const std::string line = "The server is resuming" + aside + ": retrying in " +
                             std::to_string(seconds(retry_in)) + " s, " + clock + " so far. Ctrl-C stops waiting.";
    std::cerr << '\r' << line << std::string(line_length > line.size() ? line_length - line.size() : 0, ' ')
              << std::flush;
    line_length = line.size();
  } else if (attempt != attempt_shown) {
    // A line per answer, not per second, in a log.
    std::cerr << "The server is resuming" << aside << "; retrying in " << seconds(retry_in) << " s (" << clock
              << " so far)." << std::endl;
  }
  attempt_shown = attempt;
  return true;
}

void ChargewireConnection::ResumeStatus::Done() {
  std::lock_guard lock(mutex);
  if (line_length > 0) std::cerr << '\r' << std::string(line_length, ' ') << '\r' << std::flush;
  line_length = 0;
  attempt_shown = 0;
}

namespace {
// What a statement's text can do to the session's transaction, by its words. A word in a
// string or a comment counts too: that only errs on the safe side.
struct TransactionWords {
  bool opens = false;  // BEGIN, START [TRANSACTION], autocommit settings, EXECUTE of a prepared one
  bool ends = false;   // COMMIT, ROLLBACK, END, ABORT
};
TransactionWords WordsOf(const std::string& sql) {
  static constexpr std::array<std::string_view, 4> kOpens = {"BEGIN", "START", "AUTOCOMMIT", "EXECUTE"};
  static constexpr std::array<std::string_view, 4> kEnds = {"COMMIT", "ROLLBACK", "END", "ABORT"};
  TransactionWords out;
  std::string word;
  auto check = [&] {
    if (std::find(kOpens.begin(), kOpens.end(), word) != kOpens.end()) out.opens = true;
    if (std::find(kEnds.begin(), kEnds.end(), word) != kEnds.end()) out.ends = true;
    word.clear();
  };
  for (const char c : sql) {
    if (std::isalnum(static_cast<unsigned char>(c)) || c == '_') {
      word.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    } else if (!word.empty()) {
      check();
    }
  }
  if (!word.empty()) check();
  return out;
}
}  // namespace

namespace {
// The statement's first word, upper-cased, after leading spaces and comments.
std::string FirstWord(const std::string& sql) {
  size_t i = 0;
  while (i < sql.size()) {
    if (std::isspace(static_cast<unsigned char>(sql[i]))) {
      ++i;
    } else if (sql.compare(i, 2, "--") == 0) {
      while (i < sql.size() && sql[i] != '\n') ++i;
    } else if (sql.compare(i, 2, "/*") == 0) {
      const size_t end = sql.find("*/", i + 2);
      i = end == std::string::npos ? sql.size() : end + 2;
    } else {
      break;
    }
  }
  std::string word;
  while (i < sql.size() && (std::isalnum(static_cast<unsigned char>(sql[i])) || sql[i] == '_')) {
    word.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(sql[i]))));
    ++i;
  }
  return word;
}
}  // namespace

void ChargewireConnection::NoteStatement(const std::string& sql) {
  if (!session_) return;
  // Where and how statements run (USE, SET, RESET, PRAGMA): a new session starts without it,
  // so a statement run again there could write to another schema's table or run with other
  // settings, with no error.
  const std::string first = FirstWord(sql);
  if (first == "USE" || first == "SET" || first == "RESET" || first == "PRAGMA") session_state_changed_ = true;
  const TransactionWords words = WordsOf(sql);
  if (words.opens) {
    // It may have opened a transaction. A read of the session right after it can't say
    // otherwise: servers before Chargewire 2.2.1 answer such a statement before they record
    // what it did, so the session can still say "none" right after a BEGIN. Only a later
    // statement that ends the transaction (below) clears this.
    transaction_maybe_open_ = true;
    return;
  }
  if (!words.ends) return;  // nothing that changes the transaction: what we knew stands
  try {
    // A read can only lag toward "open" (the transaction it ended), which errs on the safe
    // side; "none" is the truth.
    const auto doc = session_->Get();
    const auto* state = doc.if_contains("transaction");
    transaction_maybe_open_ = !(state && state->is_string() && state->as_string() == "none");
  } catch (const std::exception&) {
    transaction_maybe_open_ = true;  // can't tell: assume the worst
  }
}

namespace {
struct Executing {
  std::atomic<bool>& flag;
  explicit Executing(std::atomic<bool>& f) : flag(f) { flag.store(true); }
  ~Executing() { flag.store(false); }
};
}  // namespace

arrow::Result<QueryResult> ChargewireConnection::ExecuteQuery(const std::string& sql, int64_t row_limit) {
  cancel_requested_.store(false);
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  Executing executing(executing_);
  try {
    QueryProgress progress;
    wc::StatementOptions options;
    if (!config_.quiet && IsOutputTerminal() && IsErrorTerminal())
      options.on_progress = [&](const auto& doc) { progress.Report(doc); };
    options.result_partitions = config_.result_partitions;  // in order: rows show as the server sent them
    auto result = WithSession([&] { return session_->Execute(sql, options); });
    QueryResult out;
    if (!result.HasRows()) {
      out.update = true;
      if (result.document) out.rows_affected = result.document->rows_affected;
      NoteStatement(sql);
      return out;
    }
    bool more = false;
    out.table = result.reader->ToTable(row_limit, &more);
    out.total_rows = out.table->num_rows();
    if (more) {
      // Stopped early: count the rest on the server (as 1.x did when Flight had no total).
      out.total_rows = -1;
      try {
        auto count = session_->Execute("SELECT COUNT(*) FROM (" + sql + ") AS __count__");
        if (count.HasRows()) {
          auto table = count.reader->ToTable();
          if (table->num_rows() > 0 && table->num_columns() > 0) {
            auto scalar = table->column(0)->GetScalar(0);
            if (scalar.ok() && (*scalar)->is_valid) out.total_rows = std::stoll((*scalar)->ToString());
          }
        }
      } catch (const wc::Error&) {
        // Not countable (not a query): the total stays unknown.
      } catch (const std::exception&) {
      }
    }
    // Only once the result is read: a request on the session while its result streams would
    // abandon the stream.
    NoteStatement(sql);
    return out;
  } catch (const wc::Error& e) {
    NoteStatement(sql);  // a failed COMMIT can still have ended the transaction
    return ToStatus(e);
  }
}

arrow::Result<int64_t> ChargewireConnection::ExecuteUpdate(const std::string& sql) {
  cancel_requested_.store(false);
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  Executing executing(executing_);
  try {
    auto result = WithSession([&] { return session_->Execute(sql); });
    if (result.HasRows()) {
      // Read to the end: a statement that changes data and returns rows runs either way,
      // and reading it keeps the connection for the next statement.
      int64_t rows = 0;
      while (auto batch = result.reader->Next()) rows += batch->num_rows();
      NoteStatement(sql);  // after the read, never while the result streams
      return rows;
    }
    NoteStatement(sql);
    return result.document && result.document->rows_affected ? *result.document->rows_affected : 0;
  } catch (const wc::Error& e) {
    NoteStatement(sql);
    return ToStatus(e);
  }
}

arrow::Result<std::shared_ptr<arrow::Table>> ChargewireConnection::GetTables(const std::string& catalog,
                                                                       const std::string& schema_pattern,
                                                                       const std::string& table_pattern,
                                                                       bool include_schema) {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    wc::TablesRequest request;
    request.catalog = catalog;
    request.db_schema_filter_pattern = schema_pattern;
    request.table_name_filter_pattern = table_pattern;
    request.include_schema = include_schema;
    return WithSession([&] { return session_->Tables(request); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<std::shared_ptr<arrow::Table>> ChargewireConnection::GetDbSchemas(const std::string& catalog,
                                                                          const std::string& schema_pattern) {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->DbSchemas(catalog, schema_pattern); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<std::shared_ptr<arrow::Table>> ChargewireConnection::GetCatalogs() {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->Catalogs(); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<boost::json::object> ChargewireConnection::GetServerInfo() {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->ServerInfo(); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Status ChargewireConnection::UploadLastResult(const std::shared_ptr<arrow::Table>& table,
                                               const std::string& table_name) {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  if (!table || table->num_rows() == 0) return arrow::Status::Invalid("No result to upload");
  try {
    arrow::TableBatchReader reader(*table);
    wc::IngestOptions options;
    options.mode = "replace";
    options.temporary = true;
    options.parts = 1;  // one stream: the table keeps the result's row order
    session_->Ingest(table_name, reader, options);
    return arrow::Status::OK();
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

}  // namespace hardcharger::client
