#include "warp_connection.hpp"

#include "plain_http_probe.hpp"

#include <iostream>
#include <utility>

#include <arrow/scalar.h>
#include <arrow/table.h>

#include "oauth_flow.hpp"
#include "output_renderer.hpp"
#include "sql_quoting.hpp"

namespace hardcharger::client {

namespace wc = warpsql::client;

namespace {

// What a Warp SQL reason means, for a person: why the server refused a token it issued
// (§4.2) or ended a session (§5.6).
std::string DescribeReason(const std::string& reason) {
  if (reason == "killed") return "an administrator ended it";
  if (reason == "credentials_changed") return "the user's password or credentials changed";
  if (reason == "principal_dropped") return "the user was dropped or disabled";
  if (reason == "token_revoked") return "its access token was revoked";
  if (reason == "token_expired") return "its access token expired";
  return reason;
}

}  // namespace

WarpConnection::WarpConnection(WarpConnection&& other) noexcept
    : client_(std::move(other.client_)),
      session_(std::move(other.session_)),
      server_url_(std::move(other.server_url_)),
      cancel_requested_(other.cancel_requested_.load()) {}

arrow::Status WarpConnection::ToStatus(const wc::Error& error) const {
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

void WarpConnection::Disconnect() {
  // Serialized with the interrupt thread before the session goes.
  std::lock_guard cancel_lock(cancel_mutex_);
  if (session_) {
    session_->Close();  // best effort, bounded; the server reaps sessions it never hears from again
  }
  session_.reset();
  client_.reset();
  server_url_.clear();
}

void WarpConnection::SendCancelToServer() {
  std::lock_guard cancel_lock(cancel_mutex_);
  if (session_) session_->CancelRunning();
}

std::string WarpConnection::session_id() const { return session_ ? session_->id() : std::string(); }

arrow::Status WarpConnection::Connect(const ClientConfig& config) {
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

  const std::string origin = std::string(config.use_tls ? "https://" : "http://") + config.host + ":" +
                             std::to_string(config.port);
  const bool auth_attempted = config.auth_type_external || config.auth_type_mtls || !config.username.empty();
  // Turns the library's errors into what a person at the prompt can act on.
  auto friendly = [&](const wc::Error& e) -> arrow::Status {
    // A TLS alert (or problem) refusing the client certificate (Warp SQL §4.4): the server
    // is running, and said why.
    if (e.code() == "certificate_revoked") {
      return arrow::Status::IOError("The server at " + origin + " refused the client certificate: certificate revoked");
    }
    if (e.code() == "client_certificate_required") {
      return arrow::Status::IOError("The server at " + origin +
                                    " refused the connection: it requires a client certificate (--mtls-cert and "
                                    "--mtls-key)");
    }
    if (e.code() == "connection_failed") {
      const std::string what = e.what();
      if (what.find("could not resolve host") != std::string::npos) {
        return arrow::Status::IOError("Could not resolve host '" + config.host + "'");
      }
      return arrow::Status::IOError("Could not connect to " + origin + " — is the server running?" +
                                    (what.find("timed out") != std::string::npos ? " (timed out)" : ""));
    }
    // A TLS handshake that met a plain-HTTP server: say so, and how to connect without TLS. Never
    // fall back to plaintext by itself.
    if (e.code() == "tls_failed" && config.use_tls && std::string(e.what()).find("TLS handshake") != std::string::npos &&
        AnswersTlsWithHttp(config.host, config.port)) {
      const bool v6 = config.host.find(':') != std::string::npos;
      return arrow::Status::IOError("The server at " + (v6 ? "[" + config.host + "]" : config.host) + ":" +
                                    std::to_string(config.port) +
                                    " speaks plain HTTP, not TLS. Add --tls=false (or ?tls=false in a "
                                    "hardcharger:// URI) to connect without encryption.");
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
    // A Warp SQL server answers GET /warp; anything else (a GizmoSQL 1.x gRPC port, a web
    // server) is refused here with a clear message.
    client->Discover();
    if (config.auth_type_external) {
      std::string base = config.oauth_base_url;
      if (base.empty()) {
        base = std::string(config.use_tls ? "https://" : "http://") + config.host + ":" +
               std::to_string(config.oauth_port);
      }
      OAuthFlow oauth(options.tls);
      auto id_token = oauth.Authenticate(base);
      if (!id_token.ok()) return id_token.status();
      client->SignInWithToken(*id_token);
    } else if (config.auth_type_mtls) {
      client->SignInWithClientCertificate();
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
  return arrow::Status::OK();
}

bool WarpConnection::Reopen(const wc::Error& error) {
  const bool lost = error.code() == "session_not_found";
  const bool ended = error.code() == "session_ended";
  if ((!lost && !ended) || !client_) return false;
  try {
    if (ended && config_.auth_type_mtls) {
      client_->SignInWithClientCertificate();
    } else if (ended && !config_.auth_type_external && !config_.username.empty()) {
      client_->SignInWithPassword(config_.username, config_.password);
    }
    auto session = client_->CreateSession();
    {
      std::lock_guard cancel_lock(cancel_mutex_);
      session_ = std::move(session);
    }
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
auto WarpConnection::WithSession(Call&& call) -> decltype(call()) {
  try {
    return call();
  } catch (const wc::Error& e) {
    if (!Reopen(e)) throw;
    if (e.code() == "session_not_found") return call();  // it never ran: the server had no session to run it in
    // session_ended: the server stopped the statement with its session (a kill can come
    // while it runs), so it isn't run again. The server's advice (sign in again, open a new
    // session) is what Reopen just did: say what became of the statement instead.
    throw wc::Error(e.status(), e.code(),
                    "The statement did not complete: its session ended" +
                        (e.reason().empty() ? std::string() : " (" + e.reason() + ")") +
                        ". It was not run again; run it again to run it in the new session.");
  }
}

namespace {
struct Executing {
  std::atomic<bool>& flag;
  explicit Executing(std::atomic<bool>& f) : flag(f) { flag.store(true); }
  ~Executing() { flag.store(false); }
};
}  // namespace

arrow::Result<QueryResult> WarpConnection::ExecuteQuery(const std::string& sql, int64_t row_limit) {
  cancel_requested_.store(false);
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  Executing executing(executing_);
  try {
    wc::StatementOptions options;
    options.result_partitions = config_.result_partitions;  // in order: rows show as the server sent them
    auto result = WithSession([&] { return session_->Execute(sql, options); });
    QueryResult out;
    if (!result.HasRows()) {
      out.update = true;
      if (result.document) out.rows_affected = result.document->rows_affected;
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
    return out;
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<int64_t> WarpConnection::ExecuteUpdate(const std::string& sql) {
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
      return rows;
    }
    return result.document && result.document->rows_affected ? *result.document->rows_affected : 0;
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<std::shared_ptr<arrow::Table>> WarpConnection::GetTables(const std::string& catalog,
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

arrow::Result<std::shared_ptr<arrow::Table>> WarpConnection::GetDbSchemas(const std::string& catalog,
                                                                          const std::string& schema_pattern) {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->DbSchemas(catalog, schema_pattern); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<std::shared_ptr<arrow::Table>> WarpConnection::GetCatalogs() {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->Catalogs(); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Result<boost::json::object> WarpConnection::GetServerInfo() {
  if (!session_) return arrow::Status::Invalid("Not connected to a server");
  try {
    return WithSession([&] { return session_->ServerInfo(); });
  } catch (const wc::Error& e) {
    return ToStatus(e);
  }
}

arrow::Status WarpConnection::UploadLastResult(const std::shared_ptr<arrow::Table>& table,
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
