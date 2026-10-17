// hardcharger: the server. See `hardcharger --help` for configuration.

#include <csignal>
#include <cmath>
#include <fstream>
#include <iostream>
#include <thread>

#if defined(__APPLE__)
#include <mach/mach.h>
#endif
#include <unistd.h>

#include "auth.hpp"
#include "config.hpp"
#include "duckdb_sql.hpp"
#include "engine.hpp"
#include "http_client.hpp"
#include "jwks.hpp"
#include "license.hpp"
#include "oauth.hpp"
#include "logging.hpp"
#include "runtime.hpp"
#include "tokens.hpp"
#include "version.hpp"
#include "warpsql/server.hpp"

namespace hc = hardcharger;

namespace {

warpsql::ServerOptions ServerOptionsFrom(const hc::Config& config) {
  warpsql::ServerOptions options;
  options.host = config.host;
  options.port = static_cast<uint16_t>(config.port);
  options.health_port = static_cast<uint16_t>(config.health_port);
  if (!config.tls_cert.empty()) {
    options.tls = warpsql::TlsOptions{config.tls_cert, config.tls_key, config.mtls_ca_cert};
  }
  options.max_header_bytes = static_cast<std::size_t>(config.max_header_bytes);
  options.token_lifetime = std::chrono::seconds(config.token_lifetime_seconds);
  // 0 means "never": a session then lives until it is closed.
  options.session_idle_timeout = config.session_idle_timeout > 0
                                     ? std::chrono::seconds(config.session_idle_timeout)
                                     : std::chrono::seconds(std::chrono::hours(24 * 365 * 10));
  // The engine supplies each session's default timeout (query_timeout_seconds, which
  // SET [GLOBAL] gizmosql.query_timeout changes).
  options.default_statement_timeout = std::chrono::milliseconds(0);
  options.cors_allowed_origins = config.cors_origins;
  return options;
}

// Options that need a license feature: setting one without it stops the server rather
// than running without what the deployment asked for.
void CheckLicensedOptions(const hc::Loaded& loaded, const hc::License& license) {
  const auto& c = loaded.config;
  auto require = [&](bool used, const std::string& feature, const std::string& what) {
    if (used && !license.Has(feature)) throw std::runtime_error(hc::LicenseRequired(feature, what));
  };
  require(c.enable_metrics, "metrics", "Cannot enable metrics: enable_metrics");
  require(c.enable_instrumentation, "instrumentation", "Instrumentation (enable_instrumentation)");
  require(c.enable_catalog_logging, "instrumentation", "Catalog logging (--enable-catalog-logging)");
  require(c.capture_query_profile != "off", "instrumentation", "Query profile capture (capture_query_profile)");
  require(!c.instance_tag.empty(), "instrumentation", "The instance tag (instance_tag)");
  require(!c.oauth_client_id.empty(), "external_auth", "Server-side OAuth sign-in (oauth_client_id)");
  for (const char* option : {"max_concurrent_statements", "max_queued_statements", "max_queue_wait",
                             "admin_bypass_queue_default"}) {
    require(loaded.sources.contains(option), "statement_queue", std::string("The statement queue (") + option + ")");
  }
}

// The identity provider's keys and endpoints. Its JWKS verifies ID tokens when named
// (token_jwks_uri), or when tokens have an issuer and an audience but no configured key:
// then the issuer's OpenID configuration names it. Browser sign-in needs the provider's
// authorization and token endpoints, from the same document.
struct IdentityProvider {
  std::shared_ptr<hc::Jwks> jwks;
  hc::OidcEndpoints endpoints;
};

IdentityProvider DiscoverIdentityProvider(const hc::Config& c) {
  IdentityProvider provider;
  const bool oauth = !c.oauth_client_id.empty();
  const bool by_issuer =
      !c.token_allowed_issuer.empty() && !c.token_allowed_audience.empty() && c.token_signature_verify_cert_path.empty();
  if (oauth || (c.token_jwks_uri.empty() && by_issuer)) {
    try {
      provider.endpoints = hc::DiscoverOidc(c.token_allowed_issuer);
    } catch (const std::exception& e) {
      // Without OAuth this only costs JWKS sign-in, as in GizmoSQL 1.x; OAuth can't run.
      if (oauth) throw std::runtime_error(std::string("OAuth sign-in needs the issuer's OpenID configuration: ") + e.what());
      hc::Log(hc::LogLevel::kWarning, "OpenID discovery failed; identity provider tokens won't be accepted",
              {{"issuer", c.token_allowed_issuer}, {"error", e.what()}});
      return provider;
    }
  }
  const std::string uri = !c.token_jwks_uri.empty() ? c.token_jwks_uri : provider.endpoints.jwks_uri;
  if (!uri.empty() && (!c.token_jwks_uri.empty() || by_issuer || oauth)) {
    provider.jwks = std::make_shared<hc::Jwks>(uri);
    hc::Log(hc::LogLevel::kInfo, "identity provider tokens are verified with its JWKS", {{"jwks_uri", uri}});
  }
  return provider;
}

std::unique_ptr<hc::OAuthServer> MakeOAuthServer(const hc::Config& c, const IdentityProvider& provider,
                                                 const std::string& secret_key) {
  if (c.oauth_client_id.empty()) return nullptr;
  if (c.token_allowed_issuer.empty() || c.token_allowed_audience.empty()) {
    throw std::runtime_error("oauth_client_id needs token_allowed_issuer and token_allowed_audience");
  }
  if (provider.endpoints.authorization_endpoint.empty() || provider.endpoints.token_endpoint.empty()) {
    throw std::runtime_error("the issuer's OpenID configuration has no authorization_endpoint or token_endpoint");
  }
  const bool tls = !c.tls_cert.empty() && !c.oauth_disable_tls;
  if (c.oauth_disable_tls && !c.tls_cert.empty()) {
    hc::Log(hc::LogLevel::kWarning, "the OAuth server runs without TLS (oauth_disable_tls): only behind a TLS proxy");
  }
  const std::string base = !c.oauth_base_url.empty()
                               ? c.oauth_base_url
                               : std::string(tls ? "https" : "http") + "://localhost:" + std::to_string(c.oauth_port);
  hc::OAuthServer::Options options;
  options.host = c.host;
  options.port = static_cast<uint16_t>(c.oauth_port);
  options.client_id = c.oauth_client_id;
  options.client_secret = c.oauth_client_secret;
  options.scopes = c.oauth_scopes;
  options.redirect_uri = !c.oauth_redirect_uri.empty() ? c.oauth_redirect_uri : base + "/oauth/callback";
  options.secret_key = secret_key;
  options.instance_id = c.oauth_instance_id;
  options.authorized_emails = c.token_authorized_emails;
  if (tls) {
    options.tls_cert = c.tls_cert;
    options.tls_key = c.tls_key;
  }
  options.authorization_endpoint = provider.endpoints.authorization_endpoint;
  options.token_endpoint = provider.endpoints.token_endpoint;
  return std::make_unique<hc::OAuthServer>(std::move(options));
}

double ParseBytes(const std::string& text) {
  // DuckDB prints sizes such as "12.8 GiB" or "512.0 MiB".
  double number = 0;
  std::size_t used = 0;
  try {
    number = std::stod(text, &used);
  } catch (const std::exception&) {
    return NAN;
  }
  std::string unit = text.substr(used);
  unit.erase(0, unit.find_first_not_of(' '));
  static const std::map<std::string, double> kUnits = {
      {"", 1},      {"bytes", 1}, {"B", 1},       {"KiB", 1024.0},     {"MiB", 1048576.0}, {"GiB", 1073741824.0},
      {"TiB", 1099511627776.0},   {"KB", 1e3},  {"MB", 1e6},         {"GB", 1e9},        {"TB", 1e12}};
  auto it = kUnits.find(unit);
  return it == kUnits.end() ? NAN : number * it->second;
}

double ResidentBytes() {
#if defined(__APPLE__)
  mach_task_basic_info info{};
  mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
  if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO, reinterpret_cast<task_info_t>(&info), &count) == KERN_SUCCESS) {
    return static_cast<double>(info.resident_size);
  }
  return NAN;
#else
  std::ifstream statm("/proc/self/statm");
  long pages = 0, resident = 0;
  if (statm >> pages >> resident) return static_cast<double>(resident) * static_cast<double>(sysconf(_SC_PAGESIZE));
  return NAN;
#endif
}

// Whether the last run of this database's server ended without a clean stop: a marker
// beside the database file says "running" while a server runs and "clean" after it
// stops. Only the process that can write the database (DuckDB lets one) keeps it; a
// read-only or in-memory server reports NaN.
class ExitMarker {
 public:
  explicit ExitMarker(const hc::Config& config) {
    if (config.read_only || config.database.empty() || config.database == ":memory:") return;
    path_ = config.database + ".gizmosql-metrics-state";
    std::ifstream in(path_);
    std::string line;
    bool was_running = false;
    while (std::getline(in, line)) {
      if (line == "state=running") was_running = true;
      if (line.starts_with("unclean_exits=")) unclean_exits_ = std::atoll(line.c_str() + 14);
    }
    if (was_running) unclean_exits_++;
    last_unclean_ = was_running ? 1.0 : 0.0;
    Write("running");
  }
  void Clean() {
    if (!path_.empty()) Write("clean");
  }
  double last_unclean() const { return last_unclean_; }
  double unclean_exits() const { return path_.empty() ? NAN : static_cast<double>(unclean_exits_); }

 private:
  void Write(const char* state) {
    std::ofstream out(path_, std::ios::trunc);
    out << "state=" << state << "\nunclean_exits=" << unclean_exits_ << "\n";
  }
  std::string path_;
  int64_t unclean_exits_ = 0;
  double last_unclean_ = NAN;
};

void InstallCollectors(hc::Runtime& runtime, hc::Backend& backend, const ExitMarker& marker) {
  auto connection = std::make_shared<warpduckdb::Connection>();
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(backend.instance(), connection->out(), err); });
  auto mutex = std::make_shared<std::mutex>();
  runtime.metrics.AddCollector([&runtime, &backend, &marker, connection, mutex](std::vector<hc::Sample>& out) {
    const auto now = std::chrono::system_clock::now();
    out.push_back({"gizmosql_build_info",
                   {{"version", std::string("v") + hc::kVersion}, {"edition", runtime.license.Edition()},
                    {"duckdb_version", backend.duckdb_version()}, {"server", "hardcharger"}},
                   1});
    out.push_back({"gizmosql_up_seconds", {}, std::chrono::duration<double>(now - runtime.started_at).count()});
    int64_t running = 0, idle = 0, in_transaction = 0, aborted = 0;
    runtime.sessions.ForEach([&](hc::Session& s) {
      if (s.killed()) return;
      if (s.running()) {
        running++;
      } else if (s.transaction() == hc::Session::Transaction::kOpen) {
        in_transaction++;
      } else if (s.transaction() == hc::Session::Transaction::kAborted) {
        aborted++;
      } else {
        idle++;
      }
    });
    out.push_back({"gizmosql_sessions", {{"state", "active"}}, static_cast<double>(running)});
    out.push_back({"gizmosql_sessions", {{"state", "idle"}}, static_cast<double>(idle)});
    out.push_back({"gizmosql_sessions", {{"state", "idle_in_transaction"}}, static_cast<double>(in_transaction)});
    out.push_back({"gizmosql_sessions", {{"state", "idle_in_transaction_aborted"}}, static_cast<double>(aborted)});
    out.push_back({"gizmosql_session_limit", {}, static_cast<double>(runtime.config.max_sessions)});
    out.push_back({"gizmosql_statements_active", {}, static_cast<double>(running)});
    out.push_back({"gizmosql_concurrency_limit", {}, static_cast<double>(runtime.admission.Limit())});
    out.push_back({"gizmosql_queue_depth", {{"state", "active"}}, static_cast<double>(runtime.admission.ActiveCount())});
    out.push_back({"gizmosql_queue_depth", {{"state", "queued"}}, static_cast<double>(runtime.admission.QueuedCount())});
    out.push_back({"gizmosql_draining", {}, runtime.draining ? 1.0 : 0.0});
    out.push_back({"gizmosql_health_check_status", {}, runtime.health && runtime.health->Healthy() ? 1.0 : 0.0});
    out.push_back({"gizmosql_license_expiry_seconds", {},
                   runtime.license.enterprise ? std::chrono::duration<double>(runtime.license.expires - now).count() : NAN});
    out.push_back({"process_resident_memory_bytes", {}, ResidentBytes()});
    out.push_back({"gizmosql_last_exit_unclean", {}, marker.last_unclean()});
    out.push_back({"gizmosql_unclean_exits_total", {}, marker.unclean_exits()});
    bool ok = true;
    double limit = NAN;
    double usage = NAN;
    try {
      std::lock_guard lock(*mutex);
      limit = ParseBytes(warpduckdb::QueryText(connection->get(), "SELECT current_setting('memory_limit')"));
      usage = std::stod(warpduckdb::QueryText(
          connection->get(), "SELECT coalesce(sum(memory_usage_bytes), 0)::DOUBLE FROM duckdb_memory()"));
    } catch (const std::exception&) {
      ok = false;
    }
    out.push_back({"gizmosql_duckdb_memory_limit_bytes", {}, limit});
    out.push_back({"gizmosql_duckdb_memory_usage_bytes", {}, usage});
    out.push_back({"gizmosql_metrics_collection_success", {}, ok ? 1.0 : 0.0});
    out.push_back({"gizmosql_metrics_last_collection_success_seconds", {},
                   ok ? std::chrono::duration<double>(now.time_since_epoch()).count() : NAN});
  });
}

}  // namespace

int main(int argc, char** argv) {
  warpsql::InitializeProcess();  // first: before anything uses OpenSSL
  hc::Loaded loaded;
  try {
    loaded = hc::LoadConfig(argc, argv);
  } catch (const std::exception& e) {
    std::cerr << "hardcharger: " << e.what() << "\n\n" << hc::Usage();
    return 2;
  }
  switch (loaded.action) {
    case hc::Action::kHelp:
      std::cout << hc::Usage();
      return 0;
    case hc::Action::kVersion:
      std::cout << "Hardcharger " << hc::kVersion << "\n";
      return 0;
    case hc::Action::kPrintDuckDBVersion: {
      duckdb_v2_str version{};
      duckdb_v2_library_version(&version, nullptr);
      std::cout << std::string(version.ptr, version.len) << "\n";
      return 0;
    }
    case hc::Action::kVerifyTls: {
      const auto bundle = hc::FindSystemCaCertFile();
      std::cout << "verify-tls: CA bundle " << bundle.value_or("(none found; set SSL_CERT_FILE)") << "\n";
      try {
        const hc::HttpResponse response = hc::HttpGet(loaded.verify_tls_url);
        std::cout << "verify-tls: OK " << loaded.verify_tls_url << " -> HTTP " << response.status << "\n";
        return 0;
      } catch (const std::exception& e) {
        std::cout << "verify-tls: FAILED " << e.what() << "\n";
        return 1;
      }
    }
    case hc::Action::kServe:
      break;
  }
  const hc::Config& config = loaded.config;

  // Block the stop signals before any thread starts, so only sigwait() below sees them.
  sigset_t stop_signals;
  sigemptyset(&stop_signals);
  sigaddset(&stop_signals, SIGINT);
  sigaddset(&stop_signals, SIGTERM);
  pthread_sigmask(SIG_BLOCK, &stop_signals, nullptr);

  try {
    hc::ConfigureLogging(config.log_level, config.log_format, config.log_file);
    const std::string instance_id = hc::RandomUuid();
    hc::LogFields identity = {{"instance_id", instance_id}};
    if (!config.cluster_id.empty()) identity.emplace_back("cluster_id", config.cluster_id);
    hc::SetDefaultLogFields(identity);
    for (const auto& note : loaded.notes) {
      hc::Log(hc::LogLevel::kInfo, note);
    }
    const hc::License license =
        hc::LoadLicense(hc::ReadLicenseKey(config.license_key, config.license_key_file,
                                           loaded.sources.contains("license_key")));
    CheckLicensedOptions(loaded, license);

    std::string secret_key = config.secret_key;
    if (secret_key.empty()) {
      secret_key = hc::RandomSecretKey();
      hc::Log(hc::LogLevel::kWarning,
              "no secret_key: access tokens are valid on this process only, until it restarts");
    }
    hc::Runtime runtime(config, license, instance_id);
    if (config.enable_metrics) runtime.metrics.Enable();
    auto tokens = std::make_shared<hc::JwtTokenService>(
        secret_key, std::chrono::seconds(config.token_lifetime_seconds), instance_id,
        config.allow_cross_instance_tokens, hc::ParseLogLevel(config.auth_log_level));
    auto backend = std::make_shared<hc::Backend>(runtime);
    const IdentityProvider provider = DiscoverIdentityProvider(config);
    auto authenticator = std::make_shared<hc::Authenticator>(config, license, provider.jwks);
    auto oauth = MakeOAuthServer(config, provider, secret_key);
    backend->StartHealthCheck();

    std::unique_ptr<hc::MetricsEndpoint> metrics_endpoint;
    std::unique_ptr<ExitMarker> exit_marker;
    if (config.enable_metrics) {
      exit_marker = std::make_unique<ExitMarker>(config);
      InstallCollectors(runtime, *backend, *exit_marker);
      if (config.metrics_port != 0) {
        metrics_endpoint = std::make_unique<hc::MetricsEndpoint>(runtime.metrics, config.metrics_bind_address,
                                                                 static_cast<uint16_t>(config.metrics_port));
        metrics_endpoint->Start();
      }
    }

    warpsql::Server server(ServerOptionsFrom(config), backend, authenticator, tokens);
    server.Start();
    if (oauth) oauth->Start();
    hc::Log(hc::LogLevel::kInfo, "Hardcharger is ready",
            {{"version", hc::kVersion},
             {"duckdb", backend->duckdb_version()},
             {"edition", license.Edition()},
             {"license_id", license.id},
             {"licensee", license.licensee},
             {"url", std::string(config.tls_cert.empty() ? "http" : "https") + "://" +
                         config.host + ":" + std::to_string(server.port()) + "/warp"},
             {"metrics_port", metrics_endpoint ? std::to_string(metrics_endpoint->port()) : ""},
             {"oauth_port", oauth ? std::to_string(oauth->port()) : ""},
             {"database", config.database}});

    // First signal: with graceful_shutdown, drain (finish running work, refuse new work)
    // for up to shutdown_grace_period_seconds, both live-adjustable with SET GLOBAL;
    // otherwise stop at once. A second signal always stops at once.
    std::atomic<bool> exiting = false;
    std::thread watcher;
    std::thread signals([&] {
      int signal = 0;
      sigwait(&stop_signals, &signal);
      if (exiting) return;
      if (!runtime.graceful_shutdown) {
        hc::Log(hc::LogLevel::kInfo, "stopping (graceful_shutdown is off)");
        server.Stop();
        return;
      }
      runtime.draining = true;
      hc::Log(hc::LogLevel::kInfo, "draining; signal again to stop now",
              {{"shutdown_grace_period_seconds", std::to_string(runtime.shutdown_grace_period_seconds.load())}});
      server.Drain(std::chrono::hours(24 * 365));
      const auto started = std::chrono::steady_clock::now();
      watcher = std::thread([&] {
        while (!exiting) {
          const int64_t grace = runtime.shutdown_grace_period_seconds.load();
          if (grace > 0 && std::chrono::steady_clock::now() - started >= std::chrono::seconds(grace)) {
            hc::Log(hc::LogLevel::kInfo, "grace period over; stopping");
            server.Stop();
            return;
          }
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
      });
      sigwait(&stop_signals, &signal);
      if (exiting) return;
      hc::Log(hc::LogLevel::kInfo, "stopping now");
      server.Stop();
    });

    server.Wait();
    // Wake the signal thread (it may still wait for a signal) so nothing outlives the server.
    exiting = true;
    kill(getpid(), SIGTERM);
    signals.join();
    if (watcher.joinable()) watcher.join();
    if (metrics_endpoint) metrics_endpoint->Stop();
    if (oauth) oauth->Stop();
    if (exit_marker) exit_marker->Clean();
    backend->StopHealthCheck();
    backend->StopInstrumentation();
    hc::Log(hc::LogLevel::kInfo, "stopped");
  } catch (const std::exception& e) {
    hc::Log(hc::LogLevel::kError, e.what());
    return 1;
  }
  return 0;
}
