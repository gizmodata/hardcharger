#include "config.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cstdlib>
#include <functional>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>

namespace hardcharger {

namespace {

enum class Kind { kString, kInt, kBool, kList };

struct Option {
  std::string name;  // canonical: YAML key; flag --name-with-dashes; env HARDCHARGER_NAME
  Kind kind;
  std::vector<std::string> legacy_env;  // GizmoSQL 1.x names, read after HARDCHARGER_*
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

bool ParseBool(const std::string& name, const std::string& value) {
  std::string v = value;
  std::transform(v.begin(), v.end(), v.begin(), [](unsigned char c) { return std::tolower(c); });
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
    if (!item.empty()) out.push_back(item);
  }
  return out;
}

// clang-format off
#define TEXT(field) [](Config& c, const std::string& v) { c.field = v; }
#define NUMBER(field) [](Config& c, const std::string& v) { c.field = ParseInt(#field, v); }
#define FLAG(field) [](Config& c, const std::string& v) { c.field = ParseBool(#field, v); }
// clang-format on

const std::vector<Option>& Options() {
  static const std::vector<Option> options = {
      {"host", Kind::kString, {"GIZMOSQL_HOSTNAME"}, "address to listen on", TEXT(host)},
      {"port", Kind::kInt, {"GIZMOSQL_PORT"}, "port to listen on", NUMBER(port)},
      {"health_port", Kind::kInt, {"HEALTH_PORT", "GIZMOSQL_HEALTH_PORT"},
       "plaintext port serving only /health (0: none)", NUMBER(health_port)},
      {"tls_cert", Kind::kString, {"TLS_CERT", "GIZMOSQL_TLS_CERT"}, "TLS certificate chain (PEM)",
       TEXT(tls_cert)},
      {"tls_key", Kind::kString, {"TLS_KEY", "GIZMOSQL_TLS_KEY"}, "TLS private key (PEM)",
       TEXT(tls_key)},
      {"mtls_ca_cert", Kind::kString, {"MTLS_CA_CERT_FILENAME", "GIZMOSQL_MTLS_CA_CERT_FILENAME"},
       "CA (PEM) client certificates must chain to", TEXT(mtls_ca_cert)},
      {"database", Kind::kString, {"DATABASE_FILENAME", "GIZMOSQL_DATABASE_FILENAME"},
       "DuckDB database file, or :memory:", TEXT(database)},
      {"read_only", Kind::kBool, {"READONLY", "GIZMOSQL_READONLY"}, "open the database read-only",
       FLAG(read_only)},
      {"memory_limit", Kind::kString, {"GIZMOSQL_MEMORY_LIMIT"}, "DuckDB memory_limit, e.g. 8GB",
       TEXT(memory_limit)},
      {"storage_version", Kind::kString, {"GIZMOSQL_STORAGE_VERSION"},
       "DuckDB storage version for new databases", TEXT(storage_version)},
      {"allow_unsigned_extensions", Kind::kBool, {"GIZMOSQL_ALLOW_UNSIGNED_EXTENSIONS"},
       "let DuckDB load unsigned extensions", FLAG(allow_unsigned_extensions)},
      {"init_sql_commands", Kind::kString, {"INIT_SQL_COMMANDS", "GIZMOSQL_INIT_SQL_COMMANDS"},
       "SQL run at startup (before the file)", TEXT(init_sql_commands)},
      {"init_sql_commands_file", Kind::kString,
       {"INIT_SQL_COMMANDS_FILE", "GIZMOSQL_INIT_SQL_COMMANDS_FILE"}, "SQL file run at startup",
       TEXT(init_sql_commands_file)},
      {"username", Kind::kString, {"GIZMOSQL_USERNAME"}, "the administrator's user name",
       TEXT(username)},
      {"password", Kind::kString, {"GIZMOSQL_PASSWORD"},
       "the administrator's password (prefer the environment variable)", TEXT(password)},
      {"secret_key", Kind::kString, {"SECRET_KEY", "GIZMOSQL_SECRET_KEY"},
       "key that signs access tokens; share it across replicas", TEXT(secret_key)},
      {"token_lifetime_seconds", Kind::kInt, {}, "access token lifetime",
       NUMBER(token_lifetime_seconds)},
      {"allow_cross_instance_tokens", Kind::kBool, {"GIZMOSQL_ALLOW_CROSS_INSTANCE_TOKENS"},
       "accept tokens issued by other replicas", FLAG(allow_cross_instance_tokens)},
      {"token_signature_verify_cert_path", Kind::kString,
       {"TOKEN_SIGNATURE_VERIFY_CERT_PATH", "GIZMOSQL_TOKEN_SIGNATURE_VERIFY_CERT_PATH"},
       "PEM key or certificate that verifies sign-in tokens", TEXT(token_signature_verify_cert_path)},
      {"token_allowed_issuer", Kind::kString, {"TOKEN_ALLOWED_ISSUER", "GIZMOSQL_TOKEN_ALLOWED_ISSUER"},
       "required issuer of sign-in tokens", TEXT(token_allowed_issuer)},
      {"token_allowed_audience", Kind::kString,
       {"TOKEN_ALLOWED_AUDIENCE", "GIZMOSQL_TOKEN_ALLOWED_AUDIENCE"},
       "required audience of sign-in tokens", TEXT(token_allowed_audience)},
      {"token_default_role", Kind::kString, {"GIZMOSQL_TOKEN_DEFAULT_ROLE"},
       "role for sign-in tokens without one", TEXT(token_default_role)},
      {"token_groups_claim", Kind::kString, {"GIZMOSQL_TOKEN_GROUPS_CLAIM"},
       "sign-in token claim listing the user's identity-provider groups",
       TEXT(token_groups_claim)},
      {"query_timeout_seconds", Kind::kInt, {"QUERY_TIMEOUT", "GIZMOSQL_QUERY_TIMEOUT"},
       "default statement timeout (0: none)", NUMBER(query_timeout_seconds)},
      {"print_queries", Kind::kBool, {"PRINT_QUERIES", "GIZMOSQL_PRINT_QUERIES"},
       "log every statement", FLAG(print_queries)},
      {"rbac_store_uri", Kind::kString, {"GIZMOSQL_RBAC_STORE_URI"},
       "[Enterprise] turn on role-based access control, with users, roles and grants kept in "
       "this PostgreSQL database (postgresql://user:password@host/db or 'host=... dbname=...'); "
       "every replica of a cluster names the same one",
       TEXT(rbac_store_uri)},
      {"rbac_opa_url", Kind::kString, {"GIZMOSQL_RBAC_OPA_URL"},
       "[Enterprise] also ask this Open Policy Agent decision URL about every store user's "
       "session and statement; only {\"result\": true} lets them run",
       TEXT(rbac_opa_url)},
      {"rbac_opa_timeout_ms", Kind::kInt, {"GIZMOSQL_RBAC_OPA_TIMEOUT_MS"},
       "how long to wait for the policy service (no answer refuses)", NUMBER(rbac_opa_timeout_ms)},
      {"audit_chain_key", Kind::kString, {"GIZMOSQL_AUDIT_CHAIN_KEY"},
       "key that seals access-control audit records into a tamper-evident chain "
       "(HMAC-SHA256; unset: SHA-256); --verify-audit-log FILE checks a JSON log",
       TEXT(audit_chain_key)},
      {"license_key", Kind::kString, {"GIZMOSQL_LICENSE_KEY"}, "GizmoData license key (JWT)",
       TEXT(license_key)},
      {"license_key_file", Kind::kString, {"GIZMOSQL_LICENSE_KEY_FILE"},
       "file holding the GizmoData license key", TEXT(license_key_file)},
      {"log_level", Kind::kString, {"GIZMOSQL_LOG_LEVEL"}, "debug, info, warning or error",
       TEXT(log_level)},
      {"log_format", Kind::kString, {"GIZMOSQL_LOG_FORMAT"}, "text or json", TEXT(log_format)},
      {"drain_seconds", Kind::kInt, {}, "how long a shutdown lets running work finish",
       NUMBER(drain_seconds)},
      {"cors_origins", Kind::kList, {}, "browser origins allowed to call the server",
       [](Config& c, const std::string& v) { c.cors_origins = SplitList(v); }},
  };
  return options;
}

#undef TEXT
#undef NUMBER
#undef FLAG

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
    if (const auto eq = arg.find('='); eq != std::string::npos) {
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
               (i + 1 >= argc || std::string(argv[i + 1]).rfind("--", 0) == 0)) {
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
    if (auto flag = flags.find(option.name); flag != flags.end()) {
      value = flag->second;
      if (option.name == "password") {
        loaded.notes.push_back("--password is visible in process listings; prefer HARDCHARGER_PASSWORD");
      }
    } else if (auto env = Env(EnvName(option.name))) {
      value = env;
    } else {
      for (const auto& legacy : option.legacy_env) {
        if (auto old = Env(legacy)) {
          value = old;
          loaded.notes.push_back("using " + legacy + " (GizmoSQL name) for " + option.name);
          break;
        }
      }
      if (!value) {
        if (auto from_file = yaml.find(option.name); from_file != yaml.end()) {
          value = from_file->second;
        }
      }
    }
    if (value) option.set(loaded.config, *value);
  }

  const Config& c = loaded.config;
  if (loaded.action == Action::kVerifyAuditLog) {
    return loaded;  // checks a log file; nothing to serve
  }
  if (c.password.empty()) {
    throw std::invalid_argument("a password is required: set HARDCHARGER_PASSWORD");
  }
  if (c.tls_cert.empty() != c.tls_key.empty()) {
    throw std::invalid_argument("tls_cert and tls_key go together");
  }
  if (c.port < 0 || c.port > 65535 || c.health_port < 0 || c.health_port > 65535) {
    throw std::invalid_argument("ports must be between 0 and 65535");
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
    loaded.config.rbac_opa_timeout_ms = 2000;  // 0: the default, as in GizmoSQL 1.x
  }
  if (c.log_format != "text" && c.log_format != "json") {
    throw std::invalid_argument("log_format must be text or json");
  }
  return loaded;
}

std::string Usage() {
  std::ostringstream out;
  out << "usage: hardcharger [--config FILE] [options]\n\n"
      << "Every option can also be set as an environment variable (HARDCHARGER_<NAME>) or\n"
      << "as a key in the YAML config file. Precedence: flag, environment, file, default.\n\n";
  for (const auto& option : Options()) {
    out << "  " << Flag(option.name) << "\n      " << option.help << "  [" << EnvName(option.name);
    for (const auto& legacy : option.legacy_env) out << ", " << legacy;
    out << "]\n";
  }
  out << "\n  --verify-audit-log FILE [--verify-audit-log-partial]\n"
      << "      check the access-control audit records in a JSON log and exit (0: intact)\n"
      << "  --version, --print-duckdb-version, --help\n";
  return out.str();
}

}  // namespace hardcharger
