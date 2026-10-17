#include "config.hpp"

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
  std::string name;  // canonical: YAML key; flag --name-with-dashes; env HARDCHARGER_NAME
  Kind kind;
  std::vector<std::string> legacy_env;    // GizmoSQL 1.x names, read after HARDCHARGER_*
  std::vector<std::string> legacy_flags;  // GizmoSQL 1.x flags, e.g. --readonly, -O
  std::string help;
  std::function<void(Config&, const std::string&)> set;
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
#define TEXT(field) [](Config& c, const std::string& v) { c.field = v; }
#define NUMBER(field) [](Config& c, const std::string& v) { c.field = ParseInt(#field, v); }
#define FLAG(field) [](Config& c, const std::string& v) { c.field = ParseBool(#field, v); }
#define LIST(field) [](Config& c, const std::string& v) { c.field = SplitList(v); }
// clang-format on

const std::vector<Option>& Options() {
  static const std::vector<Option> options = {
      // Listener
      {"host", Kind::kString, {"GIZMOSQL_HOSTNAME"}, {"--hostname", "-H"}, "address to listen on",
       TEXT(host)},
      {"port", Kind::kInt, {"GIZMOSQL_PORT"}, {"-R"}, "port to listen on", NUMBER(port)},
      {"health_port", Kind::kInt, {"HEALTH_PORT", "GIZMOSQL_HEALTH_PORT"}, {},
       "plaintext port serving only /health (0: none)", NUMBER(health_port)},
      {"max_header_bytes", Kind::kInt, {"GIZMOSQL_MAX_METADATA_SIZE"}, {"--max-metadata-size"},
       "largest request header block accepted, in bytes (0: 16384)", NUMBER(max_header_bytes)},
      // TLS
      {"tls_cert", Kind::kString, {"TLS_CERT", "GIZMOSQL_TLS_CERT"}, {}, "TLS certificate chain (PEM)",
       TEXT(tls_cert)},
      {"tls_key", Kind::kString, {"TLS_KEY", "GIZMOSQL_TLS_KEY"}, {}, "TLS private key (PEM)",
       TEXT(tls_key)},
      {"mtls_ca_cert", Kind::kString, {"MTLS_CA_CERT_FILENAME", "GIZMOSQL_MTLS_CA_CERT_FILENAME"},
       {"--mtls-ca-cert-filename", "-M"}, "CA (PEM) client certificates must chain to",
       TEXT(mtls_ca_cert)},
      // Database
      {"backend", Kind::kString, {"DATABASE_BACKEND", "GIZMOSQL_BACKEND"}, {"-B"},
       "database engine (duckdb, the only one)", TEXT(backend)},
      {"database", Kind::kString, {"DATABASE_FILENAME", "GIZMOSQL_DATABASE_FILENAME"},
       {"--database-filename", "-D"}, "DuckDB database file, or :memory:", TEXT(database)},
      {"read_only", Kind::kBool, {"READONLY", "GIZMOSQL_READONLY"}, {"--readonly", "-O"},
       "open the database read-only", FLAG(read_only)},
      {"memory_limit", Kind::kString, {"GIZMOSQL_MEMORY_LIMIT"}, {}, "DuckDB memory_limit, e.g. 8GB",
       TEXT(memory_limit)},
      {"storage_version", Kind::kString, {"GIZMOSQL_STORAGE_VERSION"}, {},
       "DuckDB storage version for new databases", TEXT(storage_version)},
      {"allow_unsigned_extensions", Kind::kBool, {"GIZMOSQL_ALLOW_UNSIGNED_EXTENSIONS"}, {},
       "let DuckDB load unsigned extensions", FLAG(allow_unsigned_extensions)},
      {"init_sql_commands", Kind::kString, {"INIT_SQL_COMMANDS", "GIZMOSQL_INIT_SQL_COMMANDS"}, {"-I"},
       "SQL run at startup (before the file)", TEXT(init_sql_commands)},
      {"init_sql_commands_file", Kind::kString,
       {"INIT_SQL_COMMANDS_FILE", "GIZMOSQL_INIT_SQL_COMMANDS_FILE"}, {"-F"}, "SQL file run at startup",
       TEXT(init_sql_commands_file)},
      {"result_batch_rows", Kind::kInt, {}, {}, "most rows per Arrow batch in a result",
       NUMBER(result_batch_rows)},
      // Authentication
      {"username", Kind::kString, {"GIZMOSQL_USERNAME"}, {"-U"}, "the administrator's user name",
       TEXT(username)},
      {"password", Kind::kString, {"GIZMOSQL_PASSWORD"}, {"-P"},
       "the administrator's password (prefer the environment variable)", TEXT(password)},
      {"secret_key", Kind::kString, {"SECRET_KEY", "GIZMOSQL_SECRET_KEY"}, {"-S"},
       "key that signs access tokens; share it across replicas", TEXT(secret_key)},
      {"token_lifetime_seconds", Kind::kInt, {}, {}, "access token lifetime",
       NUMBER(token_lifetime_seconds)},
      {"allow_cross_instance_tokens", Kind::kBool, {"GIZMOSQL_ALLOW_CROSS_INSTANCE_TOKENS"}, {},
       "accept tokens issued by other replicas", FLAG(allow_cross_instance_tokens)},
      {"token_signature_verify_cert_path", Kind::kString,
       {"TOKEN_SIGNATURE_VERIFY_CERT_PATH", "GIZMOSQL_TOKEN_SIGNATURE_VERIFY_CERT_PATH"}, {},
       "PEM key or certificate that verifies sign-in tokens", TEXT(token_signature_verify_cert_path)},
      {"token_allowed_issuer", Kind::kString, {"TOKEN_ALLOWED_ISSUER", "GIZMOSQL_TOKEN_ALLOWED_ISSUER"},
       {}, "required issuer of sign-in tokens", TEXT(token_allowed_issuer)},
      {"token_allowed_audience", Kind::kString,
       {"TOKEN_ALLOWED_AUDIENCE", "GIZMOSQL_TOKEN_ALLOWED_AUDIENCE"}, {},
       "required audience of sign-in tokens", TEXT(token_allowed_audience)},
      {"token_default_role", Kind::kString, {"GIZMOSQL_TOKEN_DEFAULT_ROLE"}, {},
       "role for sign-in tokens without one", TEXT(token_default_role)},
      {"token_authorized_emails", Kind::kList, {"GIZMOSQL_TOKEN_AUTHORIZED_EMAILS"}, {},
       "users a sign-in token may name (globs, case-insensitive; *: anyone)",
       LIST(token_authorized_emails)},
      // Sessions
      {"max_sessions", Kind::kInt, {"GIZMOSQL_MAX_SESSIONS"}, {},
       "most non-admin sessions at once (0: unlimited)", NUMBER(max_sessions)},
      {"session_idle_timeout", Kind::kInt, {"GIZMOSQL_SESSION_IDLE_TIMEOUT"}, {},
       "seconds an idle session lives (0: forever)", NUMBER(session_idle_timeout)},
      {"reject_unknown_sessions", Kind::kBool, {"GIZMOSQL_REJECT_UNKNOWN_SESSIONS"}, {},
       "always on: unknown sessions are refused (accepted for GizmoSQL 1.x configs)",
       FLAG(reject_unknown_sessions)},
      // Statements
      {"query_timeout_seconds", Kind::kInt, {"QUERY_TIMEOUT", "GIZMOSQL_QUERY_TIMEOUT"},
       {"--query-timeout"}, "default statement timeout (0: none)", NUMBER(query_timeout_seconds)},
      {"print_queries", Kind::kBool, {"PRINT_QUERIES", "GIZMOSQL_PRINT_QUERIES"}, {"-Q"},
       "log every statement", FLAG(print_queries)},
      {"max_concurrent_statements", Kind::kInt, {"GIZMOSQL_MAX_CONCURRENT_STATEMENTS"}, {},
       "statements that run at once; more wait in a queue (0: no queue)",
       NUMBER(max_concurrent_statements)},
      {"max_queued_statements", Kind::kInt, {"GIZMOSQL_MAX_QUEUED_STATEMENTS"}, {},
       "statements that may wait (-1: 8 x concurrency; 0: unbounded)", NUMBER(max_queued_statements)},
      {"max_queue_wait", Kind::kInt, {"GIZMOSQL_MAX_QUEUE_WAIT"}, {},
       "seconds a statement may wait (-1: 300; 0: forever)", NUMBER(max_queue_wait)},
      {"admin_bypass_queue_default", Kind::kBool, {"GIZMOSQL_ADMIN_BYPASS_QUEUE_DEFAULT"}, {},
       "admin sessions skip the queue unless they SET gizmosql.bypass_queue = false",
       FLAG(admin_bypass_queue_default)},
      // Guards
      {"block_sensitive_paths", Kind::kBool, {"GIZMOSQL_BLOCK_SENSITIVE_PATHS"}, {},
       "refuse SQL naming credential and system files", FLAG(block_sensitive_paths)},
      {"sensitive_paths", Kind::kList, {"GIZMOSQL_SENSITIVE_PATHS"}, {},
       "more paths SQL may not name (comma-separated)", LIST(sensitive_paths)},
      {"block_unredacted_secrets", Kind::kBool, {"GIZMOSQL_BLOCK_UNREDACTED_SECRETS"}, {},
       "refuse SET allow_unredacted_secrets, even for admins", FLAG(block_unredacted_secrets)},
      // License
      {"license_key", Kind::kString, {"GIZMOSQL_LICENSE_KEY"}, {}, "GizmoData license key (JWT)",
       TEXT(license_key)},
      {"license_key_file", Kind::kString, {"GIZMOSQL_LICENSE_KEY_FILE"}, {"-L"},
       "file holding the GizmoData license key", TEXT(license_key_file)},
      {"cluster_id", Kind::kString, {"GIZMOSQL_CLUSTER_ID"}, {},
       "UUID shared by the replicas of one cluster", TEXT(cluster_id)},
      // Operations
      {"log_level", Kind::kString, {"GIZMOSQL_LOG_LEVEL"}, {}, "debug, info, warning or error",
       TEXT(log_level)},
      {"log_format", Kind::kString, {"GIZMOSQL_LOG_FORMAT"}, {}, "text or json", TEXT(log_format)},
      {"log_file", Kind::kString, {"GIZMOSQL_LOG_FILE"}, {}, "log destination (empty: stderr; -: stdout)",
       TEXT(log_file)},
      {"query_log_level", Kind::kString, {"GIZMOSQL_QUERY_LOG_LEVEL"}, {},
       "level of statement log lines", TEXT(query_log_level)},
      {"auth_log_level", Kind::kString, {"GIZMOSQL_AUTH_LOG_LEVEL"}, {}, "level of sign-in log lines",
       TEXT(auth_log_level)},
      {"session_log_level", Kind::kString, {"GIZMOSQL_SESSION_LOG_LEVEL"}, {},
       "level of session log lines", TEXT(session_log_level)},
      {"graceful_shutdown", Kind::kBool, {"GIZMOSQL_GRACEFUL_SHUTDOWN"}, {},
       "on the first stop signal, let running work finish (false: stop at once)",
       FLAG(graceful_shutdown)},
      {"shutdown_grace_period_seconds", Kind::kInt, {"GIZMOSQL_SHUTDOWN_GRACE_PERIOD_SECONDS"}, {},
       "how long a graceful shutdown waits for running work (0: forever)",
       NUMBER(shutdown_grace_period_seconds)},
      {"health_check_query", Kind::kString, {"GIZMOSQL_HEALTH_CHECK_QUERY"}, {},
       "SQL the health check runs", TEXT(health_check_query)},
      {"health_check_interval_seconds", Kind::kInt, {"GIZMOSQL_HEALTH_CHECK_INTERVAL_SECONDS"}, {},
       "seconds between health checks", NUMBER(health_check_interval_seconds)},
      {"health_check_staleness_seconds", Kind::kInt, {"GIZMOSQL_HEALTH_CHECK_STALENESS_SECONDS"}, {},
       "age at which the last check no longer counts (0: 3 x interval)",
       NUMBER(health_check_staleness_seconds)},
      {"cors_origins", Kind::kList, {}, {}, "browser origins allowed to call the server",
       LIST(cors_origins)},
      // Metrics
      {"enable_metrics", Kind::kBool, {"GIZMOSQL_ENABLE_METRICS"}, {},
       "collect runtime metrics and serve /metrics (Enterprise)", FLAG(enable_metrics)},
      {"metrics_port", Kind::kInt, {"GIZMOSQL_METRICS_PORT"}, {}, "port serving /metrics (0: none)",
       NUMBER(metrics_port)},
      {"metrics_bind_address", Kind::kString, {"GIZMOSQL_METRICS_BIND_ADDRESS"}, {},
       "address /metrics listens on", TEXT(metrics_bind_address)},
  };
  return options;
}

#undef TEXT
#undef NUMBER
#undef FLAG
#undef LIST

// GizmoSQL 1.x options this version does not implement. Setting one (to anything but
// its "off" value) stops the server: silently ignoring it would run a deployment
// without something it asked for (instrumentation, OAuth sign-in, telemetry, ...).
struct Unsupported {
  std::string flag;
  std::vector<std::string> env;
  bool boolean;  // "false"/"0"/"" count as unset
};

const std::vector<Unsupported>& UnsupportedOptions() {
  static const std::vector<Unsupported> options = {
      {"--enable-instrumentation", {"GIZMOSQL_ENABLE_INSTRUMENTATION"}, true},
      {"--instrumentation-db-path", {"GIZMOSQL_INSTRUMENTATION_DB_PATH"}, false},
      {"--instrumentation-catalog", {"GIZMOSQL_INSTRUMENTATION_CATALOG"}, false},
      {"--instrumentation-schema", {"GIZMOSQL_INSTRUMENTATION_SCHEMA"}, false},
      {"--instance-tag", {"GIZMOSQL_INSTANCE_TAG"}, false},
      {"--capture-query-profile", {"GIZMOSQL_CAPTURE_QUERY_PROFILE"}, false},
      {"--enable-catalog-logging", {"GIZMOSQL_ENABLE_CATALOG_LOGGING"}, true},
      {"--log-catalog", {"GIZMOSQL_LOG_CATALOG"}, false},
      {"--log-schema", {"GIZMOSQL_LOG_SCHEMA"}, false},
      {"--log-catalog-db-path", {"GIZMOSQL_LOG_DB_PATH"}, false},
      {"--token-jwks-uri", {"GIZMOSQL_TOKEN_JWKS_URI"}, false},
      {"--oauth-client-id", {"GIZMOSQL_OAUTH_CLIENT_ID"}, false},
      {"--oauth-client-secret", {"GIZMOSQL_OAUTH_CLIENT_SECRET"}, false},
      {"--otel-enabled", {"GIZMOSQL_OTEL_ENABLED"}, true},
      {"--verify-tls", {}, false},
  };
  return options;
}

const Option* FindByFlag(const std::string& flag) {
  for (const auto& option : Options()) {
    if (Flag(option.name) == flag) return &option;
    for (const auto& legacy : option.legacy_flags) {
      if (legacy == flag) return &option;
    }
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
  if (c.password.empty()) {
    throw std::invalid_argument("a password is required: set HARDCHARGER_PASSWORD");
  }
  if (Lower(c.backend) != "duckdb") {
    throw std::invalid_argument("backend '" + c.backend + "' is not supported: Hardcharger runs DuckDB");
  }
  // GizmoSQL's start script turned TLS on only when TLS_ENABLED was set; its TLS_CERT and
  // TLS_KEY alone did nothing, so they must not turn TLS on here either.
  for (const char* field : {"tls_cert", "tls_key"}) {
    auto source = loaded.sources.find(field);
    if (source != loaded.sources.end() && source->second.starts_with("env TLS_") &&
        !ParseBool("TLS_ENABLED", Env("TLS_ENABLED").value_or("false"))) {
      (std::string(field) == "tls_cert" ? c.tls_cert : c.tls_key).clear();
      loaded.sources.erase(source);
    }
  }
  if (c.tls_cert.empty() != c.tls_key.empty()) {
    throw std::invalid_argument("tls_cert and tls_key go together");
  }
  if (c.port < 0 || c.port > 65535 || c.health_port < 0 || c.health_port > 65535 ||
      c.metrics_port < 0 || c.metrics_port > 65535) {
    throw std::invalid_argument("ports must be between 0 and 65535");
  }
  if (c.log_format != "text" && c.log_format != "json") {
    throw std::invalid_argument("log_format must be text or json");
  }
  for (const auto& [name, level] : {std::pair{"log_level", c.log_level},
                                    {"query_log_level", c.query_log_level},
                                    {"auth_log_level", c.auth_log_level},
                                    {"session_log_level", c.session_log_level}}) {
    if (!IsLevel(level)) throw std::invalid_argument(std::string(name) + " must be debug, info, warning or error");
  }
  // memory_limit reaches DuckDB as a setting value, never as SQL, but GizmoSQL 1.x
  // refused anything beyond a size or percentage, and so does Hardcharger.
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
  if (c.health_check_staleness_seconds == 0) {
    c.health_check_staleness_seconds = 3 * c.health_check_interval_seconds;
  }
  if (!c.reject_unknown_sessions) {
    throw std::invalid_argument(
        "reject_unknown_sessions cannot be turned off: Warp SQL never recreates a session "
        "implicitly; clients open a new one");
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
    std::optional<std::string> inline_value;
    if (const auto eq = arg.find('='); eq != std::string::npos && arg.starts_with("--")) {
      inline_value = arg.substr(eq + 1);
      arg = arg.substr(0, eq);
    }
    if (arg == "--config") {
      if (!inline_value && i + 1 >= argc) throw std::invalid_argument("--config needs a file");
      loaded.config.config_file = inline_value ? *inline_value : argv[++i];
      continue;
    }
    // GizmoSQL 1.x's --tls takes the certificate and the key.
    if (arg == "--tls" || arg == "-T") {
      if (i + 2 >= argc) throw std::invalid_argument(arg + " needs a certificate file and a key file");
      flags["tls_cert"] = argv[++i];
      flags["tls_key"] = argv[++i];
      continue;
    }
    for (const auto& unsupported : UnsupportedOptions()) {
      if (unsupported.flag == arg) {
        const bool off = unsupported.boolean && inline_value &&
                         !ParseBool(arg, *inline_value);
        if (!off) {
          throw std::invalid_argument(arg + " (GizmoSQL 1.x) is not supported by this Hardcharger version");
        }
      }
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

  for (const auto& unsupported : UnsupportedOptions()) {
    for (const auto& env : unsupported.env) {
      if (auto value = Env(env)) {
        if (unsupported.boolean && !ParseBool(env, *value)) continue;
        throw std::invalid_argument(env + " is set, but " + unsupported.flag.substr(2) +
                                    " (GizmoSQL 1.x) is not supported by this Hardcharger version");
      }
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
      if (option.name == "password") {
        loaded.notes.push_back("--password is visible in process listings; prefer HARDCHARGER_PASSWORD");
      }
    } else if (auto env = Env(EnvName(option.name))) {
      value = env;
      source = "env " + EnvName(option.name);
    } else {
      for (const auto& legacy : option.legacy_env) {
        if (auto old = Env(legacy)) {
          value = old;
          source = "env " + legacy;
          loaded.notes.push_back("using " + legacy + " (GizmoSQL name) for " + option.name);
          break;
        }
      }
      if (!value) {
        if (auto from_file = yaml.find(option.name); from_file != yaml.end()) {
          value = from_file->second;
          source = "file";
        }
      }
    }
    if (value) {
      option.set(loaded.config, *value);
      loaded.sources[option.name] = source;
    }
  }

  Validate(loaded);
  return loaded;
}

std::string Usage() {
  std::ostringstream out;
  out << "usage: hardcharger [--config FILE] [options]\n\n"
      << "Every option can also be set as an environment variable (HARDCHARGER_<NAME>) or\n"
      << "as a key in the YAML config file. Precedence: flag, environment, file, default.\n\n";
  for (const auto& option : Options()) {
    out << "  " << Flag(option.name);
    for (const auto& legacy : option.legacy_flags) out << ", " << legacy;
    out << "\n      " << option.help << "  [" << EnvName(option.name);
    for (const auto& legacy : option.legacy_env) out << ", " << legacy;
    out << "]\n";
  }
  out << "\n  --tls CERT KEY  (GizmoSQL 1.x form of --tls-cert and --tls-key)\n"
      << "  --version, --print-duckdb-version, --help\n";
  return out.str();
}

}  // namespace hardcharger
