#include "settings.hpp"

#include <arrow/api.h>
#include <boost/json.hpp>

#include <algorithm>

#include "engine.hpp"
#include "catalog_log.hpp"
#include "instrumentation.hpp"
#include "version.hpp"

namespace hardcharger {

namespace {

using warpsql::Error;
using warpsql::ErrorCode;

std::string Upper(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::toupper(c); });
  return text;
}

std::string Lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
  return text;
}

[[noreturn]] void Invalid(const std::string& message) { throw Error(ErrorCode::kSqlError, message, "22023"); }

int64_t ParseSetInt(const std::optional<std::string>& value, const std::string& name, int64_t minimum = 0) {
  if (!value) Invalid(name + " needs a value");
  try {
    std::size_t used = 0;
    const int64_t parsed = std::stoll(*value, &used);
    if (used == value->size()) {
      if (parsed < minimum) Invalid(name + " must be at least " + std::to_string(minimum) + ", not " + *value);
      return parsed;
    }
  } catch (const Error&) {
    throw;
  } catch (const std::exception&) {
  }
  Invalid("Invalid value for " + name + ": '" + *value + "' is not a whole number");
}

bool ParseSetBool(const std::optional<std::string>& value, const std::string& name) {
  if (!value) Invalid(name + " needs a value");
  const std::string v = Lower(*value);
  if (v == "true" || v == "1" || v == "t" || v == "yes" || v == "on") return true;
  if (v == "false" || v == "0" || v == "f" || v == "no" || v == "off") return false;
  Invalid("Invalid value for " + name + ": '" + *value + "' is not true or false");
}

LogLevel ParseSetLevel(const std::optional<std::string>& value, const std::string& name) {
  if (!value) Invalid(name + " needs a value");
  try {
    return ParseLogLevel(*value);
  } catch (const std::exception&) {
    Invalid("Invalid value for " + name + ": '" + *value + "' (use DEBUG, INFO, WARNING or ERROR)");
  }
}

std::string ValidJsonTag(const std::optional<std::string>& value, const std::string& name) {
  if (!value || value->empty()) return {};
  boost::system::error_code ec;
  const auto parsed = boost::json::parse(*value, ec);
  if (ec || !parsed.is_object()) Invalid("Invalid JSON for " + name + ": " + *value + " (it must be a JSON object)");
  return *value;
}

std::string Bool(bool value) { return value ? "true" : "false"; }

std::string ProfileModeName(int mode) { return mode == 2 ? "detailed" : mode == 1 ? "standard" : "off"; }

int ParseProfileMode(const std::string& value) {
  const std::string v = Lower(value);
  if (v == "off") return 0;
  if (v == "standard") return 1;
  if (v == "detailed") return 2;
  Invalid("Invalid value for capture_query_profile: '" + value + "' (use off, standard or detailed)");
}

const char* ScopeName(SettingScope scope) {
  switch (scope) {
    case SettingScope::kSession: return "SESSION";
    case SettingScope::kGlobal: return "GLOBAL";
    case SettingScope::kSessionOrGlobal: return "SESSION_OR_GLOBAL";
    case SettingScope::kStartup: return "STARTUP";
  }
  return "";
}

std::string FlagOf(const std::string& option) {
  std::string flag = "--" + option;
  std::replace(flag.begin(), flag.end(), '_', '-');
  return flag;
}

std::vector<Setting> Build() {
  std::vector<Setting> s;
  using O = std::optional<std::string>;

  s.push_back({.name = "query_timeout",
               .scope = SettingScope::kSessionOrGlobal,
               .input_type = "INTEGER",
               .option = "query_timeout_seconds",
               .legacy_flag = "--query-timeout",
               .legacy_env = "GIZMOSQL_QUERY_TIMEOUT",
               .default_value = "0",
               .description = "Statement timeout in seconds (0: none). A request's Warp-Statement-Timeout wins.",
               .get_session = [](const Session& x) -> O {
                 auto v = x.settings().query_timeout;
                 return v ? O(std::to_string(*v)) : std::nullopt;
               },
               .get_global = [](const Runtime& r) -> O { return std::to_string(r.query_timeout_seconds.load()); },
               .set_session = [](Session& x, const O& v) {
                 std::optional<int64_t> value;
                 if (v) value = ParseSetInt(v, "query_timeout");
                 x.UpdateSettings([&](Session::Settings& s) { s.query_timeout = value; });
               },
               .set_global = [](Runtime& r, const O& v) {
                 r.query_timeout_seconds = v ? ParseSetInt(v, "query_timeout") : r.config.query_timeout_seconds;
               }});
  s.push_back({.name = "query_log_level",
               .scope = SettingScope::kSessionOrGlobal,
               .input_type = "VARCHAR",
               .option = "query_log_level",
               .legacy_flag = "--query-log-level",
               .legacy_env = "GIZMOSQL_QUERY_LOG_LEVEL",
               .default_value = "INFO",
               .description = "Level of statement log lines (DEBUG, INFO, WARNING, ERROR).",
               .get_session = [](const Session& x) -> O {
                 auto v = x.settings().query_log_level;
                 return v ? O(Upper(std::string(LogLevelName(*v)))) : std::nullopt;
               },
               .get_global = [](const Runtime& r) -> O { return Upper(std::string(LogLevelName(r.query_log_level))); },
               .set_session = [](Session& x, const O& v) {
                 std::optional<LogLevel> value;
                 if (v) value = ParseSetLevel(v, "query_log_level");
                 x.UpdateSettings([&](Session::Settings& s) { s.query_log_level = value; });
               },
               .set_global = [](Runtime& r, const O& v) {
                 r.query_log_level = v ? ParseSetLevel(v, "query_log_level") : ParseLogLevel(r.config.query_log_level);
               }});
  s.push_back({.name = "capture_query_profile",
               .scope = SettingScope::kSessionOrGlobal,
               .input_type = "VARCHAR",
               .option = "capture_query_profile",
               .legacy_flag = "--capture-query-profile",
               .legacy_env = "GIZMOSQL_CAPTURE_QUERY_PROFILE",
               .default_value = "off",
               .description = "Record DuckDB's query profile with each execution in instrumentation "
                              "(off, standard, detailed).",
               .feature = "instrumentation",
               .get_session = [](const Session& x) -> O {
                 auto v = x.settings().capture_query_profile;
                 return v ? O(ProfileModeName(*v)) : std::nullopt;
               },
               .get_global = [](const Runtime& r) -> O { return ProfileModeName(r.capture_query_profile); },
               .set_session = [](Session& x, const O& v) {
                 std::optional<int> mode;
                 if (v) mode = ParseProfileMode(*v);
                 x.UpdateSettings([&](Session::Settings& s) { s.capture_query_profile = mode; });
               },
               .set_global = [](Runtime& r, const O& v) {
                 r.capture_query_profile = v ? ParseProfileMode(*v) : (r.config.capture_query_profile == "detailed" ? 2
                                                                       : r.config.capture_query_profile == "standard" ? 1
                                                                                                                      : 0);
               }});
  s.push_back({.name = "bypass_queue",
               .scope = SettingScope::kSession,
               .input_type = "BOOLEAN",
               .default_value = "false",
               .description = "Skip the statement queue for this session (admins only may turn it on).",
               .feature = "statement_queue",
               .get_session = [](const Session& x) -> O {
                 auto v = x.settings().bypass_queue;
                 return v ? O(Bool(*v)) : std::nullopt;
               },
               .set_session = [](Session& x, const O& v) {
                 std::optional<bool> value;
                 if (v) value = ParseSetBool(v, "bypass_queue");
                 // A non-admin must never lift its own queueing.
                 if (value.value_or(false) && !x.admin()) {
                   throw Error(ErrorCode::kPermissionDenied, "Only admin users may set gizmosql.bypass_queue = true");
                 }
                 x.UpdateSettings([&](Session::Settings& s) { s.bypass_queue = value; });
               }});
  for (const char* tag : {"session_tag", "query_tag"}) {
    const std::string name = tag;
    const bool session_tag = name == "session_tag";
    s.push_back({.name = name,
                 .scope = SettingScope::kSession,
                 .input_type = "VARCHAR",
                 .legacy_env = session_tag ? "" : "",
                 .description = session_tag ? "JSON object tagging this session in instrumentation."
                                            : "JSON object tagging this session's statements in instrumentation.",
                 .feature = "instrumentation",
                 .get_session = [session_tag](const Session& x) -> O {
                   const auto settings = x.settings();
                   const std::string& v = session_tag ? settings.session_tag : settings.query_tag;
                   return v.empty() ? std::nullopt : O(v);
                 },
                 .set_session = [name, session_tag](Session& x, const O& v) {
                   const std::string value = ValidJsonTag(v, name);
                   x.UpdateSettings([&](Session::Settings& s) { (session_tag ? s.session_tag : s.query_tag) = value; });
                   if (session_tag && x.runtime().instrumentation != nullptr) {
                     x.runtime().instrumentation->SessionTagChanged(x, value);
                   }
                 }});
  }
  s.push_back({.name = "max_concurrent_statements",
               .scope = SettingScope::kGlobal,
               .input_type = "INTEGER",
               .option = "max_concurrent_statements",
               .legacy_flag = "--max-concurrent-statements",
               .legacy_env = "GIZMOSQL_MAX_CONCURRENT_STATEMENTS",
               .default_value = "0",
               .description = "Statements that run at once; more wait in the queue (0: no queue).",
               .feature = "statement_queue",
               .get_global = [](const Runtime& r) -> O { return std::to_string(r.admission.Limit()); },
               .set_global = [](Runtime& r, const O& v) {
                 r.admission.SetLimit(v ? ParseSetInt(v, "max_concurrent_statements") : r.config.max_concurrent_statements);
               }});
  s.push_back({.name = "max_queued_statements",
               .scope = SettingScope::kGlobal,
               .input_type = "INTEGER",
               .option = "max_queued_statements",
               .legacy_flag = "--max-queued-statements",
               .legacy_env = "GIZMOSQL_MAX_QUEUED_STATEMENTS",
               .default_value = "0",
               .description = "Statements that may wait for a slot (0: unbounded).",
               .feature = "statement_queue",
               .get_global = [](const Runtime& r) -> O { return std::to_string(r.admission.MaxQueued()); },
               .set_global = [](Runtime& r, const O& v) {
                 r.admission.SetMaxQueued(v ? ParseSetInt(v, "max_queued_statements") : 0);
               }});
  s.push_back({.name = "max_queue_wait",
               .scope = SettingScope::kSessionOrGlobal,
               .input_type = "INTEGER",
               .option = "max_queue_wait",
               .legacy_flag = "--max-queue-wait",
               .legacy_env = "GIZMOSQL_MAX_QUEUE_WAIT",
               .default_value = "300",
               .description = "Seconds a statement may wait in the queue (0: forever).",
               .feature = "statement_queue",
               .get_session = [](const Session& x) -> O {
                 auto v = x.settings().max_queue_wait;
                 return v ? O(std::to_string(*v)) : std::nullopt;
               },
               .get_global = [](const Runtime& r) -> O { return std::to_string(r.admission.DefaultMaxQueueWaitSeconds()); },
               .set_session = [](Session& x, const O& v) {
                 std::optional<int64_t> value;
                 if (v) value = ParseSetInt(v, "max_queue_wait");
                 x.UpdateSettings([&](Session::Settings& s) { s.max_queue_wait = value; });
               },
               .set_global = [](Runtime& r, const O& v) {
                 r.admission.SetDefaultMaxQueueWaitSeconds(v ? ParseSetInt(v, "max_queue_wait") : 300);
               }});
  s.push_back({.name = "graceful_shutdown",
               .scope = SettingScope::kGlobal,
               .input_type = "BOOLEAN",
               .option = "graceful_shutdown",
               .legacy_flag = "--graceful-shutdown",
               .legacy_env = "GIZMOSQL_GRACEFUL_SHUTDOWN",
               .default_value = "true",
               .description = "On the first stop signal, let running statements finish (false: stop at once). "
                              "Takes effect at the next signal.",
               .get_global = [](const Runtime& r) -> O { return Bool(r.graceful_shutdown); },
               .set_global = [](Runtime& r, const O& v) {
                 r.graceful_shutdown = v ? ParseSetBool(v, "graceful_shutdown") : r.config.graceful_shutdown;
               }});
  s.push_back({.name = "shutdown_grace_period_seconds",
               .scope = SettingScope::kGlobal,
               .input_type = "INTEGER",
               .option = "shutdown_grace_period_seconds",
               .legacy_flag = "--shutdown-grace-period-seconds",
               .legacy_env = "GIZMOSQL_SHUTDOWN_GRACE_PERIOD_SECONDS",
               .default_value = "300",
               .description = "How long a graceful shutdown waits for running statements (0: forever); "
                              "also during a shutdown.",
               .get_global = [](const Runtime& r) -> O { return std::to_string(r.shutdown_grace_period_seconds.load()); },
               .set_global = [](Runtime& r, const O& v) {
                 r.shutdown_grace_period_seconds =
                     v ? ParseSetInt(v, "shutdown_grace_period_seconds") : r.config.shutdown_grace_period_seconds;
               }});

  // Fixed at startup: listed so clients can read them; SET says where to change them.
  auto startup = [&](Setting setting) {
    setting.scope = SettingScope::kStartup;
    s.push_back(std::move(setting));
  };
  auto config_value = [](auto getter) {
    return [getter](const Runtime& r) -> O { return getter(r); };
  };
  startup({.name = "max_sessions", .input_type = "INTEGER", .option = "max_sessions",
           .legacy_flag = "--max-sessions", .legacy_env = "GIZMOSQL_MAX_SESSIONS", .default_value = "0",
           .description = "Most non-admin sessions at once; more get too_many_sessions (0: unlimited).",
           .get_global = config_value([](const Runtime& r) { return std::to_string(r.config.max_sessions); })});
  startup({.name = "session_idle_timeout", .input_type = "INTEGER", .option = "session_idle_timeout",
           .legacy_flag = "--session-idle-timeout", .legacy_env = "GIZMOSQL_SESSION_IDLE_TIMEOUT",
           .default_value = "900", .description = "Seconds an idle session lives (0: forever).",
           .get_global = config_value([](const Runtime& r) { return std::to_string(r.config.session_idle_timeout); })});
  startup({.name = "version", .input_type = "VARCHAR", .description = "Server version (as GIZMOSQL_VERSION()).",
           .get_global = config_value([](const Runtime&) { return std::string("v") + kVersion; })});
  startup({.name = "edition", .input_type = "VARCHAR", .default_value = "Core",
           .description = "Licensed edition, Core or Enterprise (as GIZMOSQL_EDITION()).",
           .get_global = config_value([](const Runtime& r) { return r.license.Edition(); })});
  startup({.name = "backend", .input_type = "VARCHAR", .option = "backend", .legacy_flag = "--backend",
           .default_value = "duckdb", .description = "Database engine behind this server.",
           .get_global = config_value([](const Runtime&) { return std::string("duckdb"); })});
  startup({.name = "read_only", .input_type = "BOOLEAN", .option = "read_only", .legacy_flag = "--readonly",
           .legacy_env = "READONLY", .default_value = "false",
           .description = "Database opened read-only: every write is refused.",
           .get_global = config_value([](const Runtime& r) { return Bool(r.config.read_only); })});
  startup({.name = "max_metadata_size", .input_type = "INTEGER", .option = "max_header_bytes",
           .legacy_flag = "--max-metadata-size", .legacy_env = "GIZMOSQL_MAX_METADATA_SIZE",
           .default_value = "16384", .description = "Largest request header block accepted, in bytes.",
           .get_global = config_value([](const Runtime& r) { return std::to_string(r.config.max_header_bytes); })});
  startup({.name = "memory_limit", .input_type = "VARCHAR", .option = "memory_limit",
           .legacy_flag = "--memory-limit", .legacy_env = "GIZMOSQL_MEMORY_LIMIT",
           .description = "DuckDB memory_limit (empty: DuckDB's default).",
           .get_global = config_value([](const Runtime& r) { return r.config.memory_limit; })});
  startup({.name = "storage_version", .input_type = "VARCHAR", .option = "storage_version",
           .legacy_flag = "--storage-version", .legacy_env = "GIZMOSQL_STORAGE_VERSION",
           .description = "DuckDB storage version for new databases (empty: DuckDB's default).",
           .get_global = config_value([](const Runtime& r) { return r.config.storage_version; })});
  startup({.name = "allow_unsigned_extensions", .input_type = "BOOLEAN", .option = "allow_unsigned_extensions",
           .legacy_flag = "--allow-unsigned-extensions", .legacy_env = "GIZMOSQL_ALLOW_UNSIGNED_EXTENSIONS",
           .default_value = "false", .description = "DuckDB may load unsigned extensions.",
           .get_global = config_value([](const Runtime& r) { return Bool(r.config.allow_unsigned_extensions); })});
  startup({.name = "admin_bypass_queue_default", .input_type = "BOOLEAN", .option = "admin_bypass_queue_default",
           .legacy_flag = "--admin-bypass-queue-default", .legacy_env = "GIZMOSQL_ADMIN_BYPASS_QUEUE_DEFAULT",
           .default_value = "true", .description = "Admin sessions skip the statement queue by default.",
           .feature = "statement_queue",
           .get_global = config_value([](const Runtime& r) { return Bool(r.config.admin_bypass_queue_default); })});
  startup({.name = "health_check_interval_seconds", .input_type = "INTEGER", .option = "health_check_interval_seconds",
           .legacy_flag = "--health-check-interval-seconds", .legacy_env = "GIZMOSQL_HEALTH_CHECK_INTERVAL_SECONDS",
           .default_value = "5", .description = "Seconds between background health checks.",
           .get_global = config_value(
               [](const Runtime& r) { return std::to_string(r.config.health_check_interval_seconds); })});
  startup({.name = "health_check_staleness_seconds", .input_type = "INTEGER",
           .option = "health_check_staleness_seconds", .legacy_flag = "--health-check-staleness-seconds",
           .legacy_env = "GIZMOSQL_HEALTH_CHECK_STALENESS_SECONDS", .default_value = "15",
           .description = "Age at which the last health check no longer counts.",
           .get_global = config_value(
               [](const Runtime& r) { return std::to_string(r.config.health_check_staleness_seconds); })});
  startup({.name = "auth_log_level", .input_type = "VARCHAR", .option = "auth_log_level",
           .legacy_flag = "--auth-log-level", .legacy_env = "GIZMOSQL_AUTH_LOG_LEVEL", .default_value = "INFO",
           .description = "Level of sign-in log lines.",
           .get_global = config_value([](const Runtime& r) { return Upper(r.config.auth_log_level); })});
  startup({.name = "session_log_level", .input_type = "VARCHAR", .option = "session_log_level",
           .legacy_flag = "--session-log-level", .legacy_env = "GIZMOSQL_SESSION_LOG_LEVEL", .default_value = "INFO",
           .description = "Level of session log lines.",
           .get_global = config_value([](const Runtime& r) { return Upper(r.config.session_log_level); })});
  startup({.name = "instance_id", .input_type = "VARCHAR",
           .description = "This server process's UUID (as GIZMOSQL_CURRENT_INSTANCE()).",
           .get_global = config_value([](const Runtime& r) { return r.instance_id; })});
  startup({.name = "cluster_id", .input_type = "VARCHAR", .option = "cluster_id", .legacy_flag = "--cluster-id",
           .legacy_env = "GIZMOSQL_CLUSTER_ID",
           .description = "UUID shared by the replicas of one cluster (empty when unset).",
           .get_global = config_value([](const Runtime& r) { return r.config.cluster_id; })});
  startup({.name = "enable_metrics", .input_type = "BOOLEAN", .option = "enable_metrics",
           .legacy_flag = "--enable-metrics", .legacy_env = "GIZMOSQL_ENABLE_METRICS", .default_value = "false",
           .description = "Runtime metrics are collected and served (/metrics, gizmosql_metrics()).",
           .feature = "metrics",
           .get_global = config_value([](const Runtime& r) { return Bool(r.config.enable_metrics); })});
  startup({.name = "instance_tag", .input_type = "VARCHAR", .option = "instance_tag", .legacy_flag = "--instance-tag",
           .legacy_env = "GIZMOSQL_INSTANCE_TAG", .description = "JSON object recorded with this instance.",
           .feature = "instrumentation",
           .get_global = config_value([](const Runtime& r) { return r.config.instance_tag; })});
  startup({.name = "enable_instrumentation", .input_type = "BOOLEAN", .option = "enable_instrumentation",
           .legacy_flag = "--enable-instrumentation", .legacy_env = "GIZMOSQL_ENABLE_INSTRUMENTATION",
           .default_value = "false", .description = "Sessions, statements and executions are recorded.",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) { return Bool(r.instrumentation != nullptr); })});
  startup({.name = "instrumentation_catalog", .input_type = "VARCHAR", .option = "instrumentation_catalog",
           .legacy_flag = "--instrumentation-catalog", .legacy_env = "GIZMOSQL_INSTRUMENTATION_CATALOG",
           .description = "Catalog of the instrumentation tables (empty when instrumentation is off).",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) {
             return r.instrumentation ? r.instrumentation->catalog() : std::string();
           })});
  startup({.name = "instrumentation_schema", .input_type = "VARCHAR", .option = "instrumentation_schema",
           .legacy_flag = "--instrumentation-schema", .legacy_env = "GIZMOSQL_INSTRUMENTATION_SCHEMA",
           .description = "Schema of the instrumentation tables (empty when instrumentation is off).",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) {
             return r.instrumentation ? r.instrumentation->schema() : std::string();
           })});
  startup({.name = "enable_catalog_logging", .input_type = "BOOLEAN", .option = "enable_catalog_logging",
           .legacy_flag = "--enable-catalog-logging", .legacy_env = "GIZMOSQL_ENABLE_CATALOG_LOGGING",
           .default_value = "false", .description = "Log lines are also written to a logs table.",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) { return Bool(r.catalog_log != nullptr); })});
  startup({.name = "log_catalog", .input_type = "VARCHAR", .option = "log_catalog", .legacy_flag = "--log-catalog",
           .legacy_env = "GIZMOSQL_LOG_CATALOG",
           .description = "Catalog of the logs table (empty when catalog logging is off).",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) {
             return r.catalog_log ? r.catalog_log->catalog() : std::string();
           })});
  startup({.name = "log_schema", .input_type = "VARCHAR", .option = "log_schema", .legacy_flag = "--log-schema",
           .legacy_env = "GIZMOSQL_LOG_SCHEMA",
           .description = "Schema of the logs table (empty when catalog logging is off).",
           .feature = "instrumentation", .admin_only = true,
           .get_global = config_value([](const Runtime& r) {
             return r.catalog_log ? r.catalog_log->schema() : std::string();
           })});
  return s;
}

}  // namespace

const std::vector<Setting>& Settings() {
  static const std::vector<Setting> settings = Build();
  return settings;
}

void ApplySetting(Session& session, const SettingCommand& command) {
  const auto& all = Settings();
  const auto it = std::find_if(all.begin(), all.end(), [&](const Setting& s) { return s.name == command.name; });
  if (it == all.end()) {
    throw Error(ErrorCode::kSqlError, "Unknown GizmoSQL configuration parameter: gizmosql." + command.name, "42704");
  }
  const Setting& setting = *it;
  if (setting.scope == SettingScope::kStartup) {
    std::string how;
    if (!setting.option.empty()) how = FlagOf(setting.option);
    if (!setting.legacy_flag.empty() && setting.legacy_flag != how) how += (how.empty() ? "" : " / ") + setting.legacy_flag;
    if (!setting.legacy_env.empty()) how += (how.empty() ? "" : " / ") + setting.legacy_env;
    throw Error(ErrorCode::kSqlError, "gizmosql." + setting.name + " is fixed at server startup" +
                                          (how.empty() ? "" : " (" + how + ")") + " and cannot be changed with SET",
                "55P02");
  }
  Runtime& runtime = session.runtime();
  if (!setting.feature.empty() && !runtime.license.Has(setting.feature)) {
    throw Error(ErrorCode::kPermissionDenied, LicenseRequired(setting.feature, "SET gizmosql." + setting.name));
  }
  using Scope = SettingCommand::Scope;
  const bool to_global = command.scope == Scope::kGlobal ||
                         (command.scope == Scope::kAutomatic && setting.scope == SettingScope::kGlobal);
  if (to_global && setting.scope == SettingScope::kSession) {
    throw Error(ErrorCode::kSqlError, "gizmosql." + setting.name + " can only be set at SESSION scope");
  }
  if (!to_global && setting.scope == SettingScope::kGlobal) {
    throw Error(ErrorCode::kSqlError, "gizmosql." + setting.name + " can only be set with SET GLOBAL");
  }
  const std::optional<std::string> value = command.reset ? std::nullopt : std::optional(command.value);
  if (to_global) {
    if (!session.admin()) {
      throw Error(ErrorCode::kPermissionDenied, "Only admin users can SET GLOBAL gizmosql." + setting.name, "42501");
    }
    setting.set_global(runtime, value);
  } else {
    setting.set_session(session, value);
  }
  Log(LogLevel::kInfo, command.reset ? "setting reset" : "setting changed",
      {{"setting", "gizmosql." + setting.name},
       {"scope", to_global ? "global" : "session"},
       {"value", value.value_or("")},
       {"user", session.principal().name},
       {"session_id", session.id()}});
}

std::shared_ptr<arrow::RecordBatch> SettingsTable(const Session& session, bool gizmosql_names) {
  arrow::StringBuilder name, value, session_value, global_value, scope, cli_flag, input_type, default_value, env_var,
      description;
  arrow::BooleanBuilder settable, enterprise;
  auto opt = [](arrow::StringBuilder& b, const std::optional<std::string>& v) {
    (void)(v ? b.Append(*v) : b.AppendNull());
  };
  const std::string prefix = gizmosql_names ? "gizmosql." : "hardcharger.";
  for (const Setting& s : Settings()) {
    if (s.admin_only && !session.admin()) continue;
    const auto sess = s.get_session ? s.get_session(session) : std::nullopt;
    const auto glob = s.get_global ? s.get_global(session.runtime()) : std::nullopt;
    (void)name.Append(prefix + s.name);
    (void)value.Append(sess.value_or(glob.value_or(s.default_value)));
    opt(session_value, sess);
    opt(global_value, glob);
    (void)scope.Append(ScopeName(s.scope));
    (void)settable.Append(s.scope != SettingScope::kStartup);
    std::optional<std::string> flag;
    std::optional<std::string> env;
    if (gizmosql_names) {
      if (!s.legacy_flag.empty()) flag = s.legacy_flag;
      else if (!s.option.empty()) flag = FlagOf(s.option);
      if (!s.legacy_env.empty()) env = s.legacy_env;
    } else if (!s.option.empty()) {
      flag = FlagOf(s.option);
      env = "HARDCHARGER_" + Upper(s.option);
    }
    opt(cli_flag, flag);
    (void)input_type.Append(s.input_type);
    (void)default_value.Append(s.default_value);
    opt(env_var, env);
    (void)enterprise.Append(!s.feature.empty());
    (void)description.Append(s.description);
  }
  std::vector<std::shared_ptr<arrow::Array>> columns(12);
  (void)name.Finish(&columns[0]);
  (void)value.Finish(&columns[1]);
  (void)session_value.Finish(&columns[2]);
  (void)global_value.Finish(&columns[3]);
  (void)scope.Finish(&columns[4]);
  (void)settable.Finish(&columns[5]);
  (void)cli_flag.Finish(&columns[6]);
  (void)input_type.Finish(&columns[7]);
  (void)default_value.Finish(&columns[8]);
  (void)env_var.Finish(&columns[9]);
  (void)enterprise.Finish(&columns[10]);
  (void)description.Finish(&columns[11]);
  auto schema = arrow::schema({arrow::field("name", arrow::utf8()), arrow::field("value", arrow::utf8()),
                               arrow::field("session_value", arrow::utf8()), arrow::field("global_value", arrow::utf8()),
                               arrow::field("scope", arrow::utf8()), arrow::field("settable", arrow::boolean()),
                               arrow::field("cli_flag", arrow::utf8()), arrow::field("input_type", arrow::utf8()),
                               arrow::field("default_value", arrow::utf8()), arrow::field("env_var", arrow::utf8()),
                               arrow::field("enterprise", arrow::boolean()), arrow::field("description", arrow::utf8())});
  return arrow::RecordBatch::Make(schema, columns[0]->length(), columns);
}

}  // namespace hardcharger
