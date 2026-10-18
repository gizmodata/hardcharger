#include "config.hpp"

#include <boost/json.hpp>
#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cstdlib>
#include <functional>
#include <map>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace hardcharger {

namespace {

enum class Kind { kString, kInt, kBool, kList };

struct Option {
  std::string name;  // the YAML key; the flag is --name-with-dashes, the env HARDCHARGER_NAME
  Kind kind;
  std::string help;
  std::function<void(Config&, const std::string&)> set;
  std::function<std::string(const Config&)> get;  // the value as a flag would spell it
};

std::string Flag(const std::string& name) {
  std::string flag = "--" + name;
  std::replace(flag.begin(), flag.end(), '_', '-');
  return flag;
}

std::string EnvName(const std::string& name) {
  std::string env = "HARDCHARGER_" + name;
  std::transform(env.begin(), env.end(), env.begin(), [](unsigned char c) { return std::toupper(c); });
  return env;
}

std::string Lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
  return text;
}

bool ParseBool(const std::string& name, const std::string& value) {
  const std::string v = Lower(value);
  if (v == "1" || v == "true" || v == "yes" || v == "on") return true;
  if (v == "0" || v == "false" || v == "no" || v == "off" || v.empty()) return false;
  throw std::invalid_argument(name + " must be true or false, not '" + value + "'");
}

int64_t ParseInt(const std::string& name, const std::string& value) {
  try {
    std::size_t used = 0;
    const int64_t parsed = std::stoll(value, &used);
    if (used == value.size()) return parsed;
  } catch (const std::exception&) {
  }
  throw std::invalid_argument(name + " must be a whole number, not '" + value + "'");
}

std::string JoinList(const std::vector<std::string>& items) {
  std::string out;
  for (const auto& item : items) out += (out.empty() ? "" : ",") + item;
  return out;
}

std::vector<std::string> SplitList(const std::string& value) {
  std::vector<std::string> out;
  std::stringstream stream(value);
  std::string item;
  while (std::getline(stream, item, ',')) {
    const auto start = item.find_first_not_of(" \t");
    const auto end = item.find_last_not_of(" \t");
    if (start != std::string::npos) out.push_back(item.substr(start, end - start + 1));
  }
  return out;
}

// clang-format off
// Each expands to the option's setter and its getter.
#define TEXT(field) [](Config& c, const std::string& v) { c.field = v; }, \
                    [](const Config& c) { return c.field; }
#define NUMBER(field) [](Config& c, const std::string& v) { c.field = ParseInt(#field, v); }, \
                      [](const Config& c) { return std::to_string(c.field); }
#define FLAG(field) [](Config& c, const std::string& v) { c.field = ParseBool(#field, v); }, \
                    [](const Config& c) { return std::string(c.field ? "true" : "false"); }
#define LIST(field) [](Config& c, const std::string& v) { c.field = SplitList(v); }, \
                    [](const Config& c) { return JoinList(c.field); }
// clang-format on

const std::vector<Option>& Options() {
  static const std::vector<Option> options = {
      // Listener
      {"host", Kind::kString, "address to listen on",
       TEXT(host)},
      {"port", Kind::kInt, "port to listen on", NUMBER(port)},
      {"health_port", Kind::kInt,
       "plaintext port serving only /health (0: none)", NUMBER(health_port)},
      {"max_header_bytes", Kind::kInt,
       "largest request header block accepted, in bytes (0: 16384)", NUMBER(max_header_bytes)},
      {"max_result_partitions", Kind::kInt,
       "most partitions a result is split into for a client that reads them at once (1: never split)",
       NUMBER(max_result_partitions)},
      {"web_ui", Kind::kBool,
       "serve the web UI at /ui on this port (a build that embeds it)", FLAG(web_ui)},
      // TLS
      {"tls_cert", Kind::kString, "TLS certificate chain (PEM)",
       TEXT(tls_cert)},
      {"tls_key", Kind::kString, "TLS private key (PEM)",
       TEXT(tls_key)},
      {"mtls_ca_cert", Kind::kString, "CA (PEM) client certificates must chain to",
       TEXT(mtls_ca_cert)},
      {"mtls_require_client_cert", Kind::kBool,
       "refuse connections without a client certificate (false: they sign in another way)",
       FLAG(mtls_require_client_cert)},
      {"mtls_certificate_sign_in", Kind::kBool,
       "offer certificate sign-in (the client_certificate grant); off: certificates only admit connections",
       FLAG(mtls_certificate_sign_in)},
      {"mtls_crl_allow_missing_ca_crls", Kind::kBool,
       "accept a CA above the leaf that has no CRL in mtls_crl_file (default: refuse it)",
       FLAG(mtls_crl_allow_missing_ca_crls)},
      {"mtls_crl_file", Kind::kString,
       "certificate revocation lists (PEM) for client certificates; reloaded when the file changes",
       TEXT(mtls_crl_file)},
      {"mtls_identity_source", Kind::kString,
       "where certificate sign-in takes the identity from: san_email, san_uri or subject_cn",
       TEXT(mtls_identity_source)},
      {"mtls_admin_identities", Kind::kList,
       "client certificate identities that sign in as the administrator",
       LIST(mtls_admin_identities)},
      {"mtls_require_identity_match", Kind::kBool,
       "a password or token sign-in over a client certificate must be for its identity",
       FLAG(mtls_require_identity_match)},
      // Database
      {"database", Kind::kString, "DuckDB database file, or :memory:", TEXT(database)},
      {"read_only", Kind::kBool,
       "open the database read-only", FLAG(read_only)},
      {"memory_limit", Kind::kString, "DuckDB memory_limit, e.g. 8GB",
       TEXT(memory_limit)},
      {"storage_version", Kind::kString,
       "DuckDB storage version for new databases", TEXT(storage_version)},
      {"allow_unsigned_extensions", Kind::kBool,
       "let DuckDB load unsigned extensions", FLAG(allow_unsigned_extensions)},
      {"init_sql_commands", Kind::kString,
       "SQL run at startup (before the file)", TEXT(init_sql_commands)},
      {"init_sql_commands_file", Kind::kString, "SQL file run at startup",
       TEXT(init_sql_commands_file)},
      {"result_batch_rows", Kind::kInt, "most rows per Arrow batch in a result",
       NUMBER(result_batch_rows)},
      // Authentication
      {"admin_username", Kind::kString, "the administrator's user name", TEXT(admin_username)},
      {"admin_password", Kind::kString,
       "the administrator's password (prefer the environment variable)", TEXT(admin_password)},
      {"secret_key", Kind::kString,
       "key that signs access tokens; share it across replicas", TEXT(secret_key)},
      {"token_lifetime_seconds", Kind::kInt, "access token lifetime",
       NUMBER(token_lifetime_seconds)},
      {"allow_cross_instance_tokens", Kind::kBool,
       "accept tokens issued by other replicas", FLAG(allow_cross_instance_tokens)},
      {"token_signature_verify_cert_path", Kind::kString,
       "PEM key or certificate that verifies sign-in tokens", TEXT(token_signature_verify_cert_path)},
      {"token_allowed_issuer", Kind::kString, "required issuer of sign-in tokens", TEXT(token_allowed_issuer)},
      {"token_allowed_audience", Kind::kString,
       "required audience of sign-in tokens", TEXT(token_allowed_audience)},
      {"token_default_role", Kind::kString,
       "role for sign-in tokens without one", TEXT(token_default_role)},
      {"token_groups_claim", Kind::kString,
       "sign-in token claim listing the user's identity-provider groups",
       TEXT(token_groups_claim)},
      {"token_authorized_emails", Kind::kList,
       "users a sign-in token may name (globs, case-insensitive; *: anyone)",
       LIST(token_authorized_emails)},
      {"token_jwks_uri", Kind::kString,
       "JWKS of the identity provider whose ID tokens sign users in", TEXT(token_jwks_uri)},
      // Browser sign-in
      {"oauth_client_id", Kind::kString,
       "OAuth client id; set, the browser sign-in server runs", TEXT(oauth_client_id)},
      {"oauth_client_secret", Kind::kString, "OAuth client secret",
       TEXT(oauth_client_secret)},
      {"oauth_scopes", Kind::kString, "OAuth scopes requested",
       TEXT(oauth_scopes)},
      {"oauth_port", Kind::kInt, "port of the browser sign-in server",
       NUMBER(oauth_port)},
      {"oauth_base_url", Kind::kString,
       "public URL of the browser sign-in server (default http(s)://localhost:<oauth_port>)",
       TEXT(oauth_base_url)},
      {"oauth_redirect_uri", Kind::kString,
       "redirect URI registered with the provider (default <base url>/oauth/callback)",
       TEXT(oauth_redirect_uri)},
      {"oauth_instance_id", Kind::kString,
       "prefix of the OAuth state, for a shared callback proxy", TEXT(oauth_instance_id)},
      {"oauth_disable_tls", Kind::kBool,
       "serve browser sign-in over plain HTTP even with TLS on (behind a TLS proxy)",
       FLAG(oauth_disable_tls)},
      // Sessions
      {"max_sessions", Kind::kInt,
       "most non-admin sessions at once (0: unlimited)", NUMBER(max_sessions)},
      {"session_idle_timeout", Kind::kInt,
       "seconds an idle session lives (0: forever)", NUMBER(session_idle_timeout)},
      // Statements
      {"query_timeout_seconds", Kind::kInt, "default statement timeout (0: none)", NUMBER(query_timeout_seconds)},
      {"print_queries", Kind::kBool,
       "log every statement", FLAG(print_queries)},
      {"rbac_store_uri", Kind::kString,
       "turn on role-based access control, with users, roles and grants kept in "
       "this PostgreSQL database (postgresql://user:password@host/db or 'host=... dbname=...'); "
       "every replica of a cluster names the same one",
       TEXT(rbac_store_uri)},
      {"rbac_opa_url", Kind::kString,
       "also ask this Open Policy Agent decision URL about every store user's "
       "session and statement; only {\"result\": true} lets them run",
       TEXT(rbac_opa_url)},
      {"rbac_opa_timeout_ms", Kind::kInt,
       "how long to wait for the policy service (no answer refuses)", NUMBER(rbac_opa_timeout_ms)},
      {"audit_chain_key", Kind::kString,
       "key that seals access-control audit records into a tamper-evident chain "
       "(HMAC-SHA256; unset: SHA-256); --verify-audit-log FILE checks a JSON log",
       TEXT(audit_chain_key)},
      {"max_concurrent_statements", Kind::kInt,
       "statements that run at once; more wait in a queue (0: no queue)",
       NUMBER(max_concurrent_statements)},
      {"max_queued_statements", Kind::kInt,
       "statements that may wait (-1: 8 x concurrency; 0: unbounded)", NUMBER(max_queued_statements)},
      {"max_queue_wait", Kind::kInt,
       "seconds a statement may wait (-1: 300; 0: forever)", NUMBER(max_queue_wait)},
      {"admin_bypass_queue_default", Kind::kBool,
       "admin sessions skip the queue unless they SET hardcharger.bypass_queue = false",
       FLAG(admin_bypass_queue_default)},
      // Guards
      {"block_sensitive_paths", Kind::kBool,
       "refuse SQL naming credential and system files", FLAG(block_sensitive_paths)},
      {"sensitive_paths", Kind::kList,
       "more paths SQL may not name (comma-separated)", LIST(sensitive_paths)},
      {"block_unredacted_secrets", Kind::kBool,
       "refuse SET allow_unredacted_secrets, even for admins", FLAG(block_unredacted_secrets)},
      {"cluster_id", Kind::kString,
       "UUID shared by the replicas of one cluster", TEXT(cluster_id)},
      // Operations
      {"log_level", Kind::kString, "debug, info, warning or error",
       TEXT(log_level)},
      {"log_format", Kind::kString, "text or json", TEXT(log_format)},
      {"log_file", Kind::kString, "log destination (empty: stderr; -: stdout)",
       TEXT(log_file)},
      {"query_log_level", Kind::kString,
       "level of statement log lines", TEXT(query_log_level)},
      {"auth_log_level", Kind::kString, "level of sign-in log lines",
       TEXT(auth_log_level)},
      {"session_log_level", Kind::kString,
       "level of session log lines", TEXT(session_log_level)},
      {"graceful_shutdown", Kind::kBool,
       "on the first stop signal, let running work finish (false: stop at once)",
       FLAG(graceful_shutdown)},
      {"shutdown_grace_period_seconds", Kind::kInt,
       "how long a graceful shutdown waits for running work (0: forever)",
       NUMBER(shutdown_grace_period_seconds)},
      {"health_check_query", Kind::kString,
       "SQL the health check runs", TEXT(health_check_query)},
      {"health_check_interval_seconds", Kind::kInt,
       "seconds between health checks", NUMBER(health_check_interval_seconds)},
      {"health_check_staleness_seconds", Kind::kInt,
       "age at which the last check no longer counts (0: 3 x interval)",
       NUMBER(health_check_staleness_seconds)},
      {"cors_origins", Kind::kList, "browser origins allowed to call the server",
       LIST(cors_origins)},
      // Instrumentation
      {"enable_instrumentation", Kind::kBool,
       "record sessions, statements and executions in SQL tables", FLAG(enable_instrumentation)},
      {"instrumentation_db_path", Kind::kString,
       "DuckDB file for the instrumentation tables (default: next to the database)",
       TEXT(instrumentation_db_path)},
      {"instrumentation_catalog", Kind::kString,
       "an attached catalog (DuckLake, PostgreSQL) for them instead", TEXT(instrumentation_catalog)},
      {"instrumentation_schema", Kind::kString,
       "schema for them in that catalog", TEXT(instrumentation_schema)},
      // Catalog logging
      {"enable_catalog_logging", Kind::kBool,
       "also write every log line to a logs table", FLAG(enable_catalog_logging)},
      {"log_catalog_db_path", Kind::kString,
       "DuckDB file for the logs table (default: next to the database)", TEXT(log_catalog_db_path)},
      {"log_catalog", Kind::kString,
       "an attached catalog (DuckLake, PostgreSQL) for it instead", TEXT(log_catalog)},
      {"log_schema", Kind::kString, "schema for it in that catalog",
       TEXT(log_schema)},
      {"instance_tag", Kind::kString, "JSON object recorded with this instance",
       TEXT(instance_tag)},
      {"capture_query_profile", Kind::kString,
       "record DuckDB's query profile with each execution: off, standard or detailed",
       TEXT(capture_query_profile)},
      // Metrics
      {"enable_metrics", Kind::kBool,
       "collect runtime metrics and serve /metrics", FLAG(enable_metrics)},
      {"metrics_port", Kind::kInt, "port serving /metrics (0: none)",
       NUMBER(metrics_port)},
      {"metrics_bind_address", Kind::kString,
       "address /metrics listens on", TEXT(metrics_bind_address)},
  };
  return options;
}

// The reference's sections: each group starts at the named option and runs until the next
// group's first option, in Options()' order.
const std::vector<std::pair<std::string, std::string>>& Groups() {
  static const std::vector<std::pair<std::string, std::string>> groups = {
      {"host", "Listener"},
      {"tls_cert", "TLS"},
      {"database", "Database"},
      {"admin_username", "Authentication"},
      {"oauth_client_id", "Browser sign-in"},
      {"max_sessions", "Sessions"},
      {"query_timeout_seconds", "Statements"},
      {"rbac_store_uri", "Access control"},
      {"max_concurrent_statements", "Statement queue"},
      {"block_sensitive_paths", "Guards"},
      {"cluster_id", "Cluster"},
      {"log_level", "Operations"},
      {"enable_instrumentation", "Instrumentation"},
      {"enable_catalog_logging", "Catalog logging"},
      {"enable_metrics", "Metrics"},
  };
  return groups;
}

#undef TEXT
#undef NUMBER
#undef FLAG
#undef LIST

const Option* FindByFlag(const std::string& flag) {
  for (const auto& option : Options()) {
    if (Flag(option.name) == flag) return &option;
  }
  return nullptr;
}

std::optional<std::string> Env(const std::string& name) {
  const char* value = std::getenv(name.c_str());
  if (value == nullptr || *value == '\0') return std::nullopt;
  return std::string(value);
}

// YAML values as strings, keyed by canonical name; lists become comma-joined.
std::map<std::string, std::string> ReadYaml(const std::string& path) {
  YAML::Node root;
  try {
    root = YAML::LoadFile(path);
  } catch (const YAML::Exception& e) {
    throw std::invalid_argument("cannot read config file " + path + ": " + e.what());
  }
  std::map<std::string, std::string> values;
  if (!root || root.IsNull()) return values;
  if (!root.IsMap()) throw std::invalid_argument("config file " + path + " must be a mapping");
  for (const auto& item : root) {
    const auto key = item.first.as<std::string>();
    const auto known = std::find_if(Options().begin(), Options().end(),
                                    [&](const Option& o) { return o.name == key; });
    if (known == Options().end()) {
      throw std::invalid_argument("config file " + path + ": unknown option '" + key + "'");
    }
    if (item.second.IsSequence()) {
      std::string joined;
      for (const auto& element : item.second) {
        joined += (joined.empty() ? "" : ",") + element.as<std::string>();
      }
      values[key] = joined;
    } else if (item.second.IsScalar()) {
      values[key] = item.second.as<std::string>();
    } else if (!item.second.IsNull()) {
      throw std::invalid_argument("config file " + path + ": '" + key + "' must be a value");
    }
  }
  return values;
}

bool IsUuid(const std::string& text) {
  static const std::regex kUuid("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
  return std::regex_match(text, kUuid);
}

bool IsLevel(const std::string& level) {
  const std::string v = Lower(level);
  return v == "debug" || v == "info" || v == "warning" || v == "warn" || v == "error";
}

void Validate(Loaded& loaded) {
  Config& c = loaded.config;
  if (c.admin_password.empty()) {
    throw std::invalid_argument("an administrator password is required: set HARDCHARGER_ADMIN_PASSWORD");
  }
  if (c.tls_cert.empty() != c.tls_key.empty()) {
    throw std::invalid_argument("tls_cert and tls_key go together");
  }
  if (!c.mtls_ca_cert.empty() && c.tls_cert.empty()) {
    throw std::invalid_argument("mtls_ca_cert needs tls_cert and tls_key");
  }
  if (c.mtls_ca_cert.empty()) {
    for (const auto& [name, set] :
         {std::pair{"mtls_crl_file", !c.mtls_crl_file.empty()},
          {"mtls_admin_identities", !c.mtls_admin_identities.empty()},
          {"mtls_require_identity_match", c.mtls_require_identity_match},
          {"mtls_certificate_sign_in", c.mtls_certificate_sign_in},
          {"mtls_crl_allow_missing_ca_crls", c.mtls_crl_allow_missing_ca_crls},
          {"mtls_require_client_cert", !c.mtls_require_client_cert}}) {
      if (set) throw std::invalid_argument(std::string(name) + " needs mtls_ca_cert (mutual TLS)");
    }
  }
  if (!c.mtls_admin_identities.empty() && !c.mtls_certificate_sign_in) {
    throw std::invalid_argument("mtls_admin_identities needs mtls_certificate_sign_in");
  }
  if (c.mtls_identity_source != "san_email" && c.mtls_identity_source != "san_uri" &&
      c.mtls_identity_source != "subject_cn") {
    throw std::invalid_argument("mtls_identity_source must be san_email, san_uri or subject_cn, not '" +
                                c.mtls_identity_source + "'");
  }
  if (c.port < 0 || c.port > 65535 || c.health_port < 0 || c.health_port > 65535 ||
      c.metrics_port < 0 || c.metrics_port > 65535) {
    throw std::invalid_argument("ports must be between 0 and 65535");
  }
  if (c.log_format != "text" && c.log_format != "json") {
    throw std::invalid_argument("log_format must be text or json");
  }
  if (c.rbac_store_uri == "memory") {
    throw std::invalid_argument("rbac_store_uri must be a PostgreSQL connection string");
  }
  if (!c.rbac_opa_url.empty() && c.rbac_store_uri.empty()) {
    throw std::invalid_argument("rbac_opa_url needs rbac_store_uri (access control)");
  }
  if (c.rbac_opa_timeout_ms < 0) {
    throw std::invalid_argument("rbac_opa_timeout_ms must not be negative");
  }
  if (c.rbac_opa_timeout_ms == 0) {
    c.rbac_opa_timeout_ms = 2000;  // 0: the default
  }
  for (const auto& [name, level] : {std::pair{"log_level", c.log_level},
                                    {"query_log_level", c.query_log_level},
                                    {"auth_log_level", c.auth_log_level},
                                    {"session_log_level", c.session_log_level}}) {
    if (!IsLevel(level)) throw std::invalid_argument(std::string(name) + " must be debug, info, warning or error");
  }
  // memory_limit reaches DuckDB as a setting value, never as SQL, but anything beyond a
  // size or percentage is refused all the same.
  static const std::regex kMemoryLimit("^[0-9]+(\\.[0-9]+)?\\s*[a-zA-Z%]*$");
  if (!c.memory_limit.empty() && !std::regex_match(c.memory_limit, kMemoryLimit)) {
    throw std::invalid_argument("Invalid memory_limit '" + c.memory_limit +
                                "': use a size such as 8GB or 512MiB, or a percentage such as 75%");
  }
  if (!c.cluster_id.empty() && !IsUuid(c.cluster_id)) {
    throw std::invalid_argument("cluster_id must be a UUID, not '" + c.cluster_id + "'");
  }
  if (c.max_header_bytes == 0) c.max_header_bytes = 16 * 1024;
  if (c.max_header_bytes < 1024) throw std::invalid_argument("max_header_bytes must be at least 1024");
  if (c.max_result_partitions < 1 || c.max_result_partitions > 256) {
    throw std::invalid_argument("max_result_partitions must be from 1 to 256");
  }
  if (c.result_batch_rows < 1) throw std::invalid_argument("result_batch_rows must be positive");
  if (c.max_sessions < 0 || c.session_idle_timeout < 0 || c.shutdown_grace_period_seconds < 0 ||
      c.max_concurrent_statements < 0) {
    throw std::invalid_argument(
        "max_sessions, session_idle_timeout, shutdown_grace_period_seconds and "
        "max_concurrent_statements cannot be negative");
  }
  if (c.health_check_interval_seconds < 1) {
    throw std::invalid_argument("health_check_interval_seconds must be at least 1");
  }
  c.health_check_staleness_seconds = EffectiveHealthCheckStaleness(c);
  if (c.capture_query_profile != "off" && c.capture_query_profile != "standard" &&
      c.capture_query_profile != "detailed") {
    throw std::invalid_argument("capture_query_profile must be off, standard or detailed, not '" +
                                c.capture_query_profile + "'");
  }
  if (!c.instance_tag.empty()) {
    boost::system::error_code ec;
    const auto tag = boost::json::parse(c.instance_tag, ec);
    if (ec || !tag.is_object()) {
      throw std::invalid_argument("Invalid JSON for instance_tag: it must be a JSON object, not '" + c.instance_tag + "'");
    }
  }
}

}  // namespace

Loaded LoadConfig(int argc, char** argv) {
  Loaded loaded;
  std::map<std::string, std::string> flags;
  for (int i = 1; i < argc; i++) {
    std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") { loaded.action = Action::kHelp; return loaded; }
    if (arg == "--version") { loaded.action = Action::kVersion; return loaded; }
    if (arg == "--print-duckdb-version") { loaded.action = Action::kPrintDuckDBVersion; return loaded; }
    if (arg == "--print-options-json") { loaded.action = Action::kPrintOptionsJson; return loaded; }
    // A self-check of outbound HTTPS (the trust store a container really has).
    if (arg == "--verify-tls" || arg.starts_with("--verify-tls=")) {
      loaded.action = Action::kVerifyTls;
      if (arg != "--verify-tls") {
        loaded.verify_tls_url = arg.substr(13);
      } else if (i + 1 < argc) {
        loaded.verify_tls_url = argv[i + 1];
      }
      if (loaded.verify_tls_url.empty()) throw std::invalid_argument("--verify-tls needs a URL");
      return loaded;
    }
    std::optional<std::string> inline_value;
    if (const auto eq = arg.find('='); eq != std::string::npos && arg.starts_with("--")) {
      inline_value = arg.substr(eq + 1);
      arg = arg.substr(0, eq);
    }
    if (arg == "--verify-audit-log") {
      if (!inline_value && i + 1 >= argc) throw std::invalid_argument("--verify-audit-log needs a file");
      loaded.action = Action::kVerifyAuditLog;
      loaded.audit_log = inline_value ? *inline_value : argv[++i];
      continue;
    }
    if (arg == "--verify-audit-log-partial") {
      loaded.audit_log_partial = true;
      continue;
    }
    if (arg == "--config") {
      if (!inline_value && i + 1 >= argc) throw std::invalid_argument("--config needs a file");
      loaded.config.config_file = inline_value ? *inline_value : argv[++i];
      continue;
    }
    const Option* option = FindByFlag(arg);
    if (option == nullptr) throw std::invalid_argument("unknown option " + arg);
    if (inline_value) {
      flags[option->name] = *inline_value;
    } else if (option->kind == Kind::kBool &&
               (i + 1 >= argc || std::string(argv[i + 1]).rfind("-", 0) == 0)) {
      flags[option->name] = "true";  // a bare boolean flag
    } else {
      if (i + 1 >= argc) throw std::invalid_argument(arg + " needs a value");
      flags[option->name] = argv[++i];
    }
  }

  if (loaded.config.config_file.empty()) {
    loaded.config.config_file = Env("HARDCHARGER_CONFIG").value_or("");
  }
  const auto yaml = loaded.config.config_file.empty()
                        ? std::map<std::string, std::string>{}
                        : ReadYaml(loaded.config.config_file);

  for (const auto& option : Options()) {
    std::optional<std::string> value;
    std::string source;
    if (auto flag = flags.find(option.name); flag != flags.end()) {
      value = flag->second;
      source = "flag";
      if (option.name == "admin_password") {
        loaded.notes.push_back("--admin-password is visible in process listings; prefer HARDCHARGER_ADMIN_PASSWORD");
      }
    } else if (auto env = Env(EnvName(option.name))) {
      value = env;
      source = "env " + EnvName(option.name);
    } else if (auto from_file = yaml.find(option.name); from_file != yaml.end()) {
      value = from_file->second;
      source = "file";
    }
    if (value) {
      option.set(loaded.config, *value);
      loaded.sources[option.name] = source;
    }
  }

  if (loaded.action == Action::kVerifyAuditLog) {
    return loaded;  // checks a log file; nothing to serve
  }
  Validate(loaded);
  return loaded;
}

std::string Usage() {
  std::ostringstream out;
  out << "usage: hardcharger-server [--config FILE] [options]\n\n"
      << "Every option can also be set as an environment variable (HARDCHARGER_<NAME>) or\n"
      << "as a key in the YAML config file. Precedence: flag, environment, file, default.\n\n";
  for (const auto& option : Options()) {
    out << "  " << Flag(option.name) << "\n      " << option.help << "  [" << EnvName(option.name) << "]\n";
  }
  out << "\n  --verify-tls URL  (fetch URL over HTTPS with the system's CA bundle, then exit)\n"
      << "  --verify-audit-log FILE [--verify-audit-log-partial]\n"
      << "      check the access-control audit records in a JSON log and exit (0: intact)\n"
      << "  --print-options-json  (every option, with its flag, variable, YAML key and default, as JSON)\n"
      << "  --version, --print-duckdb-version, --help\n";
  return out.str();
}

std::string OptionsJson() {
  static const std::map<Kind, std::string> kTypes = {
      {Kind::kString, "string"}, {Kind::kInt, "integer"}, {Kind::kBool, "boolean"}, {Kind::kList, "list"}};
  const Config defaults;
  boost::json::array options;
  std::string group;
  for (const auto& option : Options()) {
    for (const auto& [first, name] : Groups()) {
      if (first == option.name) group = name;
    }
    options.push_back(boost::json::object{
        {"name", option.name},
        {"group", group},
        {"flag", Flag(option.name)},
        {"env", EnvName(option.name)},
        {"yaml_key", option.name},
        {"type", kTypes.at(option.kind)},
        {"default", option.get(defaults)},
        {"description", option.help},
    });
  }
  boost::json::object out{
      {"config_file", boost::json::object{{"flag", "--config"}, {"env", "HARDCHARGER_CONFIG"}}},
      {"precedence", boost::json::array{"flag", "env", "file", "default"}},
      {"options", options},
  };
  return boost::json::serialize(out) + "\n";
}

}  // namespace hardcharger
