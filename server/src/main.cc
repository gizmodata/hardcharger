// hardcharger: the server. See `hardcharger --help` for configuration.

#include <csignal>
#include <iostream>
#include <thread>

#include "auth.hpp"
#include "config.hpp"
#include "engine.hpp"
#include "license.hpp"
#include "logging.hpp"
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
  options.token_lifetime = std::chrono::seconds(config.token_lifetime_seconds);
  options.default_statement_timeout = std::chrono::seconds(config.query_timeout_seconds);
  options.cors_allowed_origins = config.cors_origins;
  return options;
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
    hc::ConfigureLogging(config.log_level, config.log_format);
    for (const auto& note : loaded.notes) {
      hc::Log(hc::LogLevel::kInfo, note);
    }
    const hc::License license =
        hc::LoadLicense(hc::ReadLicenseKey(config.license_key, config.license_key_file));

    std::string secret_key = config.secret_key;
    if (secret_key.empty()) {
      secret_key = hc::RandomSecretKey();
      hc::Log(hc::LogLevel::kWarning,
              "no secret_key: access tokens are valid on this process only, until it restarts");
    }
    const std::string instance_id = hc::RandomUuid();
    auto tokens = std::make_shared<hc::JwtTokenService>(
        secret_key, std::chrono::seconds(config.token_lifetime_seconds), instance_id,
        config.allow_cross_instance_tokens);
    auto backend = std::make_shared<hc::Backend>(config, license);
    auto authenticator = std::make_shared<hc::Authenticator>(config, license);

    warpsql::Server server(ServerOptionsFrom(config), backend, authenticator, tokens);
    server.Start();
    hc::Log(hc::LogLevel::kInfo, "Hardcharger is ready",
            {{"version", hc::kVersion},
             {"duckdb", backend->duckdb_version()},
             {"edition", license.Edition()},
             {"license_id", license.id},
             {"instance_id", instance_id},
             {"url", std::string(config.tls_cert.empty() ? "http" : "https") + "://" +
                         config.host + ":" + std::to_string(server.port()) + "/warp"},
             {"database", config.database}});

    // First signal: drain (finish running work, refuse new work). Second: stop now.
    std::thread([&server, &stop_signals, drain = config.drain_seconds] {
      int signal = 0;
      sigwait(&stop_signals, &signal);
      hc::Log(hc::LogLevel::kInfo, "draining; signal again to stop now",
              {{"drain_seconds", std::to_string(drain)}});
      server.Drain(std::chrono::seconds(drain));
      sigwait(&stop_signals, &signal);
      server.Stop();
    }).detach();

    server.Wait();
    hc::Log(hc::LogLevel::kInfo, "stopped");
  } catch (const std::exception& e) {
    hc::Log(hc::LogLevel::kError, e.what());
    return 1;
  }
  return 0;
}
