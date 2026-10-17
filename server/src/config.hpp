// Hardcharger's configuration: every option has one canonical name, used as the YAML
// key, the command-line flag (--name-with-dashes) and the environment variable
// (HARDCHARGER_NAME). GizmoSQL 1.x's flags and environment names are read too, so a
// 1.x deployment (GizmoData Cloud sets them) runs unchanged.
//
// Precedence: flag, then environment, then the YAML file, then the default.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace hardcharger {

struct Config {
  // Listener
  std::string host = "0.0.0.0";
  int64_t port = 31337;
  int64_t health_port = 0;  // plaintext /health listener; 0 = none
  int64_t max_header_bytes = 16 * 1024;  // larger request headers get 431

  // TLS
  std::string tls_cert;   // PEM certificate chain; with tls_key, turns TLS on
  std::string tls_key;    // PEM private key
  std::string mtls_ca_cert;  // PEM CA; when set, clients must present a certificate

  // Database
  std::string backend = "duckdb";  // 1.x's --backend; DuckDB is the only one
  std::string database = ":memory:";
  bool read_only = false;
  std::string memory_limit;     // DuckDB memory_limit, e.g. "8GB"
  std::string storage_version;  // DuckDB storage version for new databases
  bool allow_unsigned_extensions = false;
  std::string init_sql_commands;       // run at startup, before the file
  std::string init_sql_commands_file;  // a SQL file run at startup
  int64_t result_batch_rows = 2048;    // rows per Arrow batch in results

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
  std::vector<std::string> token_authorized_emails = {"*"};  // globs, case-insensitive

  // Sessions
  int64_t max_sessions = 0;  // non-admin sessions at once; 0 = unlimited
  int64_t session_idle_timeout = 900;  // seconds; 0 = never
  bool reject_unknown_sessions = true;  // always true in Warp SQL; kept for 1.x configs

  // Statements
  int64_t query_timeout_seconds = 0;  // 0 = none
  bool print_queries = true;
  int64_t max_concurrent_statements = 0;  // 0 = no queue (Enterprise: statement_queue)
  int64_t max_queued_statements = -1;     // -1 = 8 x concurrency; 0 = unbounded
  int64_t max_queue_wait = -1;            // seconds; -1 = 300; 0 = forever
  bool admin_bypass_queue_default = true;

  // Guards
  bool block_sensitive_paths = true;
  std::vector<std::string> sensitive_paths;  // operator-protected paths, on top of the built-ins
  bool block_unredacted_secrets = true;

  // License (GizmoData Enterprise keys)
  std::string license_key;
  std::string license_key_file;

  // Identity of this deployment
  std::string cluster_id;  // a UUID shared by a cluster's replicas; empty if unset

  // Operations
  std::string log_level = "info";  // debug, info, warning, error
  std::string log_format = "text";  // text or json
  std::string log_file;  // empty: stderr; "-": stdout
  std::string query_log_level = "info";
  std::string auth_log_level = "info";
  std::string session_log_level = "info";
  bool graceful_shutdown = true;  // drain on the first stop signal
  int64_t shutdown_grace_period_seconds = 300;  // 0 = wait for running work forever
  std::string health_check_query = "SELECT 1";
  int64_t health_check_interval_seconds = 5;
  int64_t health_check_staleness_seconds = 0;  // 0 = 3 x interval
  std::vector<std::string> cors_origins;

  // Metrics (Enterprise: metrics)
  bool enable_metrics = false;
  int64_t metrics_port = 9091;  // 0 = no HTTP endpoint
  std::string metrics_bind_address = "0.0.0.0";

  std::string config_file;  // the YAML file the rest came from, if any
};

// What the command line asked for besides configuration.
enum class Action { kServe, kHelp, kVersion, kPrintDuckDBVersion };

struct Loaded {
  Config config;
  Action action = Action::kServe;
  std::vector<std::string> notes;  // e.g. legacy environment names in use
  // Where each option that was set came from: "flag", "env HARDCHARGER_X", "file", ...
  std::map<std::string, std::string> sources;
};

// Reads flags, environment and the YAML file named by --config / HARDCHARGER_CONFIG.
// Throws std::invalid_argument with a message for the user on any bad input.
Loaded LoadConfig(int argc, char** argv);

std::string Usage();

}  // namespace hardcharger
