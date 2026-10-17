#include "logging.hpp"

#include <boost/json.hpp>

#include <chrono>
#include <cstdio>
#include <ctime>
#include <format>
#include <mutex>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace hardcharger {

namespace {

std::mutex log_mutex;
LogLevel min_level = LogLevel::kInfo;
bool json_format = false;
FILE* output = stderr;
LogFields default_fields;
std::function<void(LogLevel, std::string_view)> capture;
std::function<void(const LogRecord&)> sink;
thread_local int sink_exclusions = 0;

std::string Timestamp(std::chrono::system_clock::time_point now) {
  const auto millis =
      std::chrono::floor<std::chrono::milliseconds>(now.time_since_epoch()).count() % 1000;
  const std::time_t seconds = std::chrono::system_clock::to_time_t(now);
  std::tm utc{};
  gmtime_r(&seconds, &utc);
  char buffer[32];
  // Format the written length, not the array: some libc++ versions format a char array
  // as all of its elements, NULs and uninitialized bytes included.
  const std::size_t length = std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S", &utc);
  return std::format("{}.{:03}Z", std::string_view(buffer, length), millis);
}

void Write(LogLevel level, std::string_view message, const LogFields& fields) {
  const auto now = std::chrono::system_clock::now();
  std::lock_guard lock(log_mutex);
  if (capture) {
    capture(level, message);
    return;
  }
  if (sink && sink_exclusions == 0) {
    LogRecord record{now, level, std::string(message), default_fields};
    record.fields.insert(record.fields.end(), fields.begin(), fields.end());
    std::ostringstream thread;
    thread << std::this_thread::get_id();
    record.thread = thread.str();
    sink(record);
  }
  std::string line;
  if (json_format) {
    boost::json::object record{{"time", Timestamp(now)}, {"level", LogLevelName(level)},
                               {"message", message}};
    for (const auto& [key, value] : default_fields) record[key] = value;
    for (const auto& [key, value] : fields) record[key] = value;
    line = boost::json::serialize(record);
  } else {
    line = std::format("{} {:<7} {}", Timestamp(now), LogLevelName(level), message);
    for (const auto& [key, value] : fields) {
      line += std::format(" {}={}", key, value);
    }
  }
  std::fprintf(output, "%s\n", line.c_str());
  std::fflush(output);
}

}  // namespace

LogLevel ParseLogLevel(std::string_view name) {
  std::string v(name);
  for (auto& c : v) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  if (v == "debug") return LogLevel::kDebug;
  if (v == "info") return LogLevel::kInfo;
  if (v == "warning" || v == "warn") return LogLevel::kWarning;
  if (v == "error") return LogLevel::kError;
  throw std::invalid_argument("log levels are debug, info, warning or error, not '" + std::string(name) + "'");
}

std::string_view LogLevelName(LogLevel level) {
  switch (level) {
    case LogLevel::kDebug: return "debug";
    case LogLevel::kInfo: return "info";
    case LogLevel::kWarning: return "warning";
    case LogLevel::kError: return "error";
  }
  return "info";
}

void ConfigureLogging(std::string_view level, std::string_view format, const std::string& file) {
  const LogLevel parsed = ParseLogLevel(level);
  FILE* destination = stderr;
  if (file == "-") {
    destination = stdout;
  } else if (!file.empty()) {
    destination = std::fopen(file.c_str(), "a");
    if (destination == nullptr) throw std::invalid_argument("cannot open log_file " + file);
  }
  std::lock_guard lock(log_mutex);
  min_level = parsed;
  json_format = format == "json";
  output = destination;
}

void SetDefaultLogFields(LogFields fields) {
  std::lock_guard lock(log_mutex);
  default_fields = std::move(fields);
}

bool LogEnabled(LogLevel level) { return level >= min_level; }

void Log(LogLevel level, std::string_view message, const LogFields& fields) {
  if (level < min_level) return;
  Write(level, message, fields);
}

void LogAt(LogLevel threshold, LogLevel level, std::string_view message, const LogFields& fields,
           bool ignore_logger_threshold) {
  if (level < threshold) return;
  if (!ignore_logger_threshold && level < min_level) return;
  Write(level, message, fields);
}

void SetLogSink(std::function<void(const LogRecord&)> destination) {
  std::lock_guard lock(log_mutex);
  sink = std::move(destination);
}

LogSinkExclusion::LogSinkExclusion() { sink_exclusions++; }

LogSinkExclusion::~LogSinkExclusion() { sink_exclusions--; }

void SetLogCaptureForTesting(std::function<void(LogLevel, std::string_view)> sink) {
  std::lock_guard lock(log_mutex);
  capture = std::move(sink);
}

std::string LoggableSql(std::string_view sql) {
  static const std::regex kSecret(R"(\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:PERSISTENT|TEMPORARY|TEMP)\s+)?SECRET\b)",
                                  std::regex::icase | std::regex::optimize);
  static const std::regex kUser(R"(\b(?:CREATE|ALTER)\s+USER\b)", std::regex::icase | std::regex::optimize);
  if (std::regex_search(sql.begin(), sql.end(), kSecret)) return "[REDACTED: secret DDL]";
  if (std::regex_search(sql.begin(), sql.end(), kUser)) return "[REDACTED: user DDL]";
  constexpr std::size_t kMax = 4000;
  return sql.size() <= kMax ? std::string(sql) : std::string(sql.substr(0, kMax)) + "…";
}

}  // namespace hardcharger
