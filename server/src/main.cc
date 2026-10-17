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
#include "license.hpp"
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
  for (const char* option : {"max_concurrent_statements", "max_queued_statements", "max_queue_wait",
                             "admin_bypass_queue_default"}) {
    require(loaded.sources.contains(option), "statement_queue", std::string("The statement queue (") + option + ")");
  }
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

void InstallCollectors(hc::Runtime& runtime, hc::Backend& backend) {
  auto connection = std::make_shared<warpduckdb::Connection>();
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(backend.instance(), connection->out(), err); });
  auto mutex = std::make_shared<std::mutex>();
  runtime.metrics.AddCollector([&runtime, &backend, connection, mutex](std::vector<hc::Sample>& out) {
    const auto now = std::chrono::system_clock::now();
    out.push_back({"gizmosql_build_info",
                   {{"version", std::string("v") + hc::kVersion}, {"edition", runtime.license.Edition()},
                    {"duckdb_version", backend.duckdb_version()}, {"server", "hardcharger"}},
                   1});
    out.push_back({"gizmosql_up_seconds", {}, std::chrono::duration<double>(now - runtime.started_at).count()});
    int64_t running = 0;
    runtime.sessions.ForEach([&](hc::Session& s) { running += s.running() ? 1 : 0; });
    out.push_back({"gizmosql_sessions", {{"state", "open"}}, static_cast<double>(runtime.sessions.size())});
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
    auto authenticator = std::make_shared<hc::Authenticator>(config, license);
    backend->StartHealthCheck();

    std::unique_ptr<hc::MetricsEndpoint> metrics_endpoint;
    if (config.enable_metrics) {
      InstallCollectors(runtime, *backend);
      if (config.metrics_port != 0) {
        metrics_endpoint = std::make_unique<hc::MetricsEndpoint>(runtime.metrics, config.metrics_bind_address,
                                                                 static_cast<uint16_t>(config.metrics_port));
        metrics_endpoint->Start();
      }
    }

    warpsql::Server server(ServerOptionsFrom(config), backend, authenticator, tokens);
    server.Start();
    hc::Log(hc::LogLevel::kInfo, "Hardcharger is ready",
            {{"version", hc::kVersion},
             {"duckdb", backend->duckdb_version()},
             {"edition", license.Edition()},
             {"license_id", license.id},
             {"licensee", license.licensee},
             {"url", std::string(config.tls_cert.empty() ? "http" : "https") + "://" +
                         config.host + ":" + std::to_string(server.port()) + "/warp"},
             {"metrics_port", metrics_endpoint ? std::to_string(metrics_endpoint->port()) : ""},
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
    backend->StopHealthCheck();
    hc::Log(hc::LogLevel::kInfo, "stopped");
  } catch (const std::exception& e) {
    hc::Log(hc::LogLevel::kError, e.what());
    return 1;
  }
  return 0;
}
