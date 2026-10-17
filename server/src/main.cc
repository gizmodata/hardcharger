// hardcharger: the server. See `hardcharger --help` for configuration.

#include <csignal>
#include <fstream>
#include <iostream>
#include <thread>

#include "audit.hpp"
#include "auth.hpp"
#include "authz/authz.hpp"
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

// A URL for the log: never its credentials (scheme://user:password@host/...).
std::string RedactUrl(const std::string& url) {
  const auto scheme_end = url.find("://");
  const auto at = url.find('@');
  if (scheme_end == std::string::npos || at == std::string::npos ||
      at > url.find('/', scheme_end + 3)) {
    return url;
  }
  return url.substr(0, scheme_end + 3) + "***@" + url.substr(at + 1);
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
      std::cout << "Hardcharger " << hc::kVersion
                << (hc::TrustsTestLicenses() ? " (test-license build: not for production)" : "")
                << "\n";
      return 0;
    case hc::Action::kPrintDuckDBVersion: {
      duckdb_v2_str version{};
      duckdb_v2_library_version(&version, nullptr);
      std::cout << std::string(version.ptr, version.len) << "\n";
      return 0;
    }
    case hc::Action::kVerifyAuditLog: {
      std::ifstream in(loaded.audit_log);
      if (!in) {
        std::cerr << "cannot read " << loaded.audit_log << "\n";
        return 1;
      }
      const auto report =
          hc::VerifyAuditLog(in, loaded.config.audit_chain_key, loaded.audit_log_partial);
      for (const auto& chain : report.chains) std::cout << "chain " << chain << "\n";
      for (const auto& problem : report.problems) std::cout << "PROBLEM: " << problem << "\n";
      if (report.records == 0) std::cout << "PROBLEM: no access-control audit records found\n";
      std::cout << report.records << " audit records, " << (report.ok() ? "intact" : "NOT intact")
                << "\n";
      return report.ok() ? 0 : 1;
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
    if (hc::TrustsTestLicenses()) {
      hc::Log(hc::LogLevel::kWarning,
              "this is a test-license build: it trusts license keys anyone can sign, so it "
              "must never serve real data");
    }
    const hc::License license =
        hc::LoadLicense(hc::ReadLicenseKey(config.license_key, config.license_key_file));

    // Role-based access control: the store is opened (and must be reachable) before
    // anything else, and refuses to start without the license feature.
    std::shared_ptr<hc::authz::AccessControl> access;
    if (!config.rbac_store_uri.empty()) {
      if (!license.Has("rbac")) {
        throw std::runtime_error(
            hc::LicenseRequired("rbac", "Role-based access control (rbac_store_uri)"));
      }
      hc::authz::Options options;
      options.store_uri = config.rbac_store_uri;
      options.administrator_name = config.username;
      options.policy_service_url = config.rbac_opa_url;
      options.policy_service_timeout = std::chrono::milliseconds(config.rbac_opa_timeout_ms);
      // Access-control audit records form a hash chain from here on.
      hc::StartAuditChain(config.audit_chain_key);
      access = std::make_shared<hc::authz::AccessControl>(options);
      hc::Log(hc::LogLevel::kInfo,
              "role-based access control is on: users, roles and grants come from the store; "
              "sign-in token roles other than admin, and catalog_access claims, grant nothing",
              {{"store", access->store_description()}});
      if (!config.rbac_opa_url.empty()) {
        hc::Log(hc::LogLevel::kInfo,
                "access control also asks the policy service (statements are refused when it "
                "does not answer)",
                {{"url", RedactUrl(config.rbac_opa_url)}});
      }
    }

    std::string secret_key = config.secret_key;
    if (secret_key.empty()) {
      secret_key = hc::RandomSecretKey();
      hc::Log(hc::LogLevel::kWarning,
              "no secret_key: access tokens are valid on this process only, until it restarts");
    }
    const std::string instance_id = hc::RandomUuid();
    auto tokens = std::make_shared<hc::JwtTokenService>(
        secret_key, std::chrono::seconds(config.token_lifetime_seconds), instance_id,
        config.allow_cross_instance_tokens, access);
    if (access) {
      // CREATE SCOPED TOKEN signs with the server's key.
      access->SetScopedTokenSigner(
          [tokens](const std::string& user, const std::string& scope_id, int64_t expires_at) {
            return tokens->IssueScoped(user, scope_id, expires_at);
          });
    }
    auto backend = std::make_shared<hc::Backend>(config, license, access);
    auto authenticator = std::make_shared<hc::Authenticator>(
        config, license, access,
        [tokens](std::string_view token) { return tokens->Validate(token); });

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
