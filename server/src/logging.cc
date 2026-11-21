#include "logging.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <format>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <io.h>
#include <windows.h>
#else
#include <unistd.h>
#endif

#include "platform.hpp"
#include "redact.hpp"
#include "thread_annotations.hpp"

namespace hardcharger {

namespace {

// What marks a text line when lines carry emoji: its level's, or an outcome's.
enum class Marker { kLevel, kSuccess, kFatal };

Mutex log_mutex;
// Written under log_mutex, read without it on every log call (a relaxed atomic: a call racing a
// change of level may use either level).
std::atomic<LogLevel> min_level{LogLevel::kInfo};
LogLevel MinLevel() { return min_level.load(std::memory_order_relaxed); }
bool json_format HC_GUARDED_BY(log_mutex) = false;
bool emoji HC_GUARDED_BY(log_mutex) = false;
FILE* output HC_GUARDED_BY(log_mutex) = stderr;
LogFields default_fields HC_GUARDED_BY(log_mutex);
std::function<void(LogLevel, std::string_view)> capture HC_GUARDED_BY(log_mutex);
std::function<void(const LogRecord&)> sink HC_GUARDED_BY(log_mutex);
thread_local int sink_exclusions = 0;

std::string Timestamp(std::chrono::system_clock::time_point now) {
  const auto millis =
      std::chrono::floor<std::chrono::milliseconds>(now.time_since_epoch()).count() % 1000;
  const std::time_t seconds = std::chrono::system_clock::to_time_t(now);
  const std::tm utc = UtcTime(seconds);
  char buffer[32];
  // Format the written length, not the array: some libc++ versions format a char array
  // as all of its elements, NULs and uninitialized bytes included.
  const std::size_t length = std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S", &utc);
  return std::format("{}.{:03}Z", std::string_view(buffer, length), millis);
}

// The marker before the level, two columns wide (as a terminal draws an emoji). Info and
// debug lines, most of them, get none: only what needs a look stands out.
std::string_view EmojiFor(LogLevel level, Marker marker) {
  if (marker == Marker::kSuccess) return "\xe2\x9c\x85";  // ✅
  if (marker == Marker::kFatal) return "\xf0\x9f\x92\xa5";  // 💥
  switch (level) {
    case LogLevel::kWarning: return "\xe2\x9a\xa0\xef\xb8\x8f";  // ⚠️
    case LogLevel::kError: return "\xe2\x9d\x8c";  // ❌
    case LogLevel::kDebug:
    case LogLevel::kInfo: return "  ";
  }
  return "  ";
}

bool IsTerminal(FILE* stream) {
#ifdef _WIN32
  return _isatty(_fileno(stream)) != 0;
#else
  return isatty(fileno(stream)) != 0;
#endif
}

void Write(LogLevel level, std::string_view message, const LogFields& fields, Marker marker = Marker::kLevel) {
  const auto now = std::chrono::system_clock::now();
  MutexLock lock(log_mutex);
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
    line = emoji ? std::format("{} {} {:<7} {}", Timestamp(now), EmojiFor(level, marker), LogLevelName(level), message)
                 : std::format("{} {:<7} {}", Timestamp(now), LogLevelName(level), message);
    // A field without a value (an optional error or detail that didn't happen) says nothing
    // in a text line: it is left out there. JSON lines keep every key.
    for (const auto& [key, value] : fields) {
      if (value.empty()) continue;
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

bool LogEmojiAuto(bool terminal, const char* lc_all, const char* lc_ctype, const char* lang) {
  if (!terminal) return false;
#ifdef _WIN32
  (void)lc_all;
  (void)lc_ctype;
  (void)lang;
  return GetConsoleOutputCP() == CP_UTF8;
#else
  // POSIX: LC_ALL overrides LC_CTYPE, which overrides LANG; unset or empty means the next.
  const char* locale = nullptr;
  for (const char* value : {lc_all, lc_ctype, lang}) {
    if (value != nullptr && *value != '\0') {
      locale = value;
      break;
    }
  }
  if (locale == nullptr) return false;
  std::string v(locale);
  for (auto& c : v) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return v.find("utf-8") != std::string::npos || v.find("utf8") != std::string::npos;
#endif
}

void ConfigureLogging(std::string_view level, std::string_view format, const std::string& file,
                      std::string_view emoji_mode) {
  const LogLevel parsed = ParseLogLevel(level);
  if (emoji_mode != "auto" && emoji_mode != "always" && emoji_mode != "never") {
    throw std::invalid_argument("log_emoji must be auto, always or never, not '" + std::string(emoji_mode) + "'");
  }
  FILE* destination = stderr;
  if (file == "-") {
    destination = stdout;
  } else if (!file.empty()) {
    destination = std::fopen(file.c_str(), "a");
    if (destination == nullptr) throw std::invalid_argument("cannot open log_file " + file);
  }
  const bool json = format == "json";
  const bool marks = !json && (emoji_mode == "always" ||
                               (emoji_mode == "auto" && LogEmojiAuto(IsTerminal(destination), std::getenv("LC_ALL"),
                                                                     std::getenv("LC_CTYPE"), std::getenv("LANG"))));
  MutexLock lock(log_mutex);
  min_level.store(parsed, std::memory_order_relaxed);
  json_format = json;
  emoji = marks;
  // A server started again in the same process (embedded in an app: embedding.hpp)
  // configures logging again: the last log file is closed, not left open for good.
  if (output != stderr && output != stdout && output != destination) std::fclose(output);
  output = destination;
}

bool LogEmojiEnabled() {
  MutexLock lock(log_mutex);
  return emoji;
}

void WriteToTextLog(std::string_view text) {
  MutexLock lock(log_mutex);
  if (json_format || capture) return;
  std::fwrite(text.data(), 1, text.size(), output);
  std::fflush(output);
}

void SetDefaultLogFields(LogFields fields) {
  MutexLock lock(log_mutex);
  default_fields = std::move(fields);
}

bool LogEnabled(LogLevel level) { return level >= MinLevel(); }

void Log(LogLevel level, std::string_view message, const LogFields& fields) {
  if (level < MinLevel()) return;
  Write(level, message, fields);
}

void LogSuccess(std::string_view message, const LogFields& fields) {
  if (LogLevel::kInfo < MinLevel()) return;
  Write(LogLevel::kInfo, message, fields, Marker::kSuccess);
}

void LogFatal(std::string_view message, const LogFields& fields) { Write(LogLevel::kError, message, fields, Marker::kFatal); }

void LogAt(LogLevel threshold, LogLevel level, std::string_view message, const LogFields& fields,
           bool ignore_logger_threshold) {
  if (level < threshold) return;
  if (!ignore_logger_threshold && level < MinLevel()) return;
  Write(level, message, fields);
}

void SetLogSink(std::function<void(const LogRecord&)> destination) {
  MutexLock lock(log_mutex);
  sink = std::move(destination);
}

LogSinkExclusion::LogSinkExclusion() { sink_exclusions++; }

LogSinkExclusion::~LogSinkExclusion() { sink_exclusions--; }

void SetLogCaptureForTesting(std::function<void(LogLevel, std::string_view)> sink) {
  MutexLock lock(log_mutex);
  capture = std::move(sink);
}

namespace {

thread_local MaskedSqlScope* masked_scope = nullptr;

}  // namespace

MaskedSqlScope::MaskedSqlScope(std::string_view sql, std::shared_ptr<const MaskedSql> masked)
    : sql_(sql), masked_(std::move(masked)), outer_(masked_scope) {
  masked_scope = this;
}

MaskedSqlScope::~MaskedSqlScope() { masked_scope = outer_; }

std::shared_ptr<const MaskedSql> MaskedOf(std::string_view sql) {
  for (const MaskedSqlScope* scope = masked_scope; scope != nullptr; scope = scope->outer_) {
    if (scope->sql_.data() == sql.data() && scope->sql_.size() == sql.size()) return scope->masked_;
  }
  return std::make_shared<const MaskedSql>(MaskSql(sql));
}

std::string LoggableSql(std::string_view sql) { return MaskedOf(sql)->text; }

bool IsCredentialStatement(std::string_view sql) {
  const auto masked = MaskedOf(sql);
  return masked->credentials || masked->unreadable.has_value();
}

std::string LoggableError(std::string_view sql, std::string_view error) {
  if (sql.empty()) return RedactError(error, MaskedStatement{});
  return RedactError(error, *MaskedOf(sql));
}

}  // namespace hardcharger
