// Structured logging, as text or JSON lines, to stderr, stdout or a file.
//
// Components (statements, sign-in, sessions) have their own thresholds on top of the
// logger's. A message is written only when its own level reaches the component's
// threshold, and always at its own level: a component threshold of DEBUG makes debug
// messages appear, it never turns them into info messages (GizmoSQL 1.x issue #136).
#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hardcharger {

enum class LogLevel { kDebug, kInfo, kWarning, kError };

using LogFields = std::vector<std::pair<std::string, std::string>>;

// Throws std::invalid_argument for an unknown name. Accepts debug, info, warn(ing), error.
LogLevel ParseLogLevel(std::string_view name);
std::string_view LogLevelName(LogLevel level);

// Configures the process-wide logger. `file`: empty for stderr, "-" for stdout, else a
// path appended to. Throws std::invalid_argument for unknown values.
void ConfigureLogging(std::string_view level, std::string_view format, const std::string& file = {});

// Fields added to every line (instance_id, cluster_id).
void SetDefaultLogFields(LogFields fields);

// Writes `message` if `level` reaches the logger's threshold.
void Log(LogLevel level, std::string_view message, const LogFields& fields = {});

// Writes `message` if `level` reaches `threshold` (a component's) and, unless
// `ignore_logger_threshold`, the logger's threshold too. A session that asked for
// more detail (SET gizmosql.query_log_level = DEBUG) gets it whatever the logger's
// threshold, so its own threshold decides alone.
void LogAt(LogLevel threshold, LogLevel level, std::string_view message, const LogFields& fields = {},
           bool ignore_logger_threshold = false);

// For tests: receives every line written (level and message) instead of the output.
void SetLogCaptureForTesting(std::function<void(LogLevel, std::string_view)> capture);

}  // namespace hardcharger
