// Hardcharger's configuration: every option has one canonical name, used as the YAML
// key, the command-line flag (--name-with-dashes) and the environment variable
// (HARDCHARGER_NAME). GizmoSQL 1.x's environment names are read too, so a 1.x
// deployment (GizmoData Cloud sets them) runs unchanged.
//
// Precedence: flag, then environment, then the YAML file, then the default.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace hardcharger {

struct Config {
  // Listener
  std::string host = "0.0.0.0";
  int64_t port = 31337;
  int64_t health_port = 0;  // plaintext /health listener; 0 = none

  // TLS
  std::string tls_cert;   // PEM certificate chain; with tls_key, turns TLS on
  std::string tls_key;    // PEM private key
  std::string mtls_ca_cert;  // PEM CA; when set, clients must present a certificate

  // Database
  std::string database = ":memory:";
  bool read_only = false;
  std::string memory_limit;     // DuckDB memory_limit, e.g. "8GB"
  std::string storage_version;  // DuckDB storage version for new databases
  bool allow_unsigned_extensions = false;
  std::string init_sql_commands;       // run at startup, before the file
  std::string init_sql_commands_file;  // a SQL file run at startup

  // Authentication
  std::string username = "hardcharger";
  std::string password;      // required
  std::string secret_key;    // signs access tokens; random (per process) if unset
  int64_t token_lifetime_seconds = 86400;
  bool allow_cross_instance_tokens = false;  // tokens from other replicas of this cluster
  std::string token_signature_verify_cert_path;  // PEM key/cert for signed sign-in tokens
  std::string token_allowed_issuer;
  std::string token_allowed_audience;
  std::string token_default_role;  // for sign-in tokens without a role claim

  // Statements
  int64_t query_timeout_seconds = 0;  // 0 = none
  bool print_queries = true;

  // License (GizmoData Enterprise keys)
  std::string license_key;
  std::string license_key_file;

  // Operations
  std::string log_level = "info";  // debug, info, warning, error
  std::string log_format = "text";  // text or json
  int64_t drain_seconds = 30;
  std::vector<std::string> cors_origins;

  std::string config_file;  // the YAML file the rest came from, if any
};

// What the command line asked for besides configuration.
enum class Action { kServe, kHelp, kVersion, kPrintDuckDBVersion };

struct Loaded {
  Config config;
  Action action = Action::kServe;
  std::vector<std::string> notes;  // e.g. legacy environment names in use
};

// Reads flags, environment and the YAML file named by --config / HARDCHARGER_CONFIG.
// Throws std::invalid_argument with a message for the user on any bad input.
Loaded LoadConfig(int argc, char** argv);

std::string Usage();

}  // namespace hardcharger
