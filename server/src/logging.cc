#include "logging.hpp"

#include <boost/json.hpp>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <format>
#include <mutex>
#include <cctype>
#include <optional>
#include <regex>
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

namespace hardcharger {

namespace {

// What marks a text line when lines carry emoji: its level's, or an outcome's.
enum class Marker { kLevel, kSuccess, kFatal };

std::mutex log_mutex;
LogLevel min_level = LogLevel::kInfo;
bool json_format = false;
bool emoji = false;
FILE* output = stderr;
LogFields default_fields;
std::function<void(LogLevel, std::string_view)> capture;
std::function<void(const LogRecord&)> sink;
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
  std::lock_guard lock(log_mutex);
  min_level = parsed;
  json_format = json;
  emoji = marks;
  // A server started again in the same process (embedded in an app: embedding.hpp)
  // configures logging again: the last log file is closed, not left open for good.
  if (output != stderr && output != stdout && output != destination) std::fclose(output);
  output = destination;
}

bool LogEmojiEnabled() {
  std::lock_guard lock(log_mutex);
  return emoji;
}

void WriteToTextLog(std::string_view text) {
  std::lock_guard lock(log_mutex);
  if (json_format || capture) return;
  std::fwrite(text.data(), 1, text.size(), output);
  std::fflush(output);
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

void LogSuccess(std::string_view message, const LogFields& fields) {
  if (LogLevel::kInfo < min_level) return;
  Write(LogLevel::kInfo, message, fields, Marker::kSuccess);
}

void LogFatal(std::string_view message, const LogFields& fields) { Write(LogLevel::kError, message, fields, Marker::kFatal); }

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

namespace {

// The SQL with comments (nested block comments too) and string-literal bodies dropped,
// so a keyword match reads the statement's real words — not a keyword hidden in a
// comment, nor one quoted inside a string. Quoted identifiers are kept (they can be an
// object name but never a keyword the match cares about). Handles '', E'', and
// $tag$...$tag$ dollar-quoting.
std::string WordsOnly(std::string_view sql) {
  std::string out;
  out.reserve(sql.size());
  std::size_t i = 0;
  const std::size_t n = sql.size();
  while (i < n) {
    const char c = sql[i];
    if (c == '-' && i + 1 < n && sql[i + 1] == '-') {
      for (i += 2; i < n && sql[i] != '\n'; ++i) {
      }
      out += ' ';
    } else if (c == '/' && i + 1 < n && sql[i + 1] == '*') {
      int depth = 1;
      i += 2;
      while (i < n && depth > 0) {
        if (sql[i] == '/' && i + 1 < n && sql[i + 1] == '*') {
          depth++;
          i += 2;
        } else if (sql[i] == '*' && i + 1 < n && sql[i + 1] == '/') {
          depth--;
          i += 2;
        } else {
          ++i;
        }
      }
      out += ' ';
    } else if (c == '\'' || ((c == 'e' || c == 'E') && i + 1 < n && sql[i + 1] == '\'')) {
      i += (c == '\'') ? 1 : 2;  // a string body, dropped
      while (i < n) {
        if (sql[i] == '\'') {
          if (i + 1 < n && sql[i + 1] == '\'') {
            i += 2;
            continue;
          }
          ++i;
          break;
        }
        ++i;
      }
      out += ' ';
    } else if (c == '$') {
      std::size_t tag_end = i + 1;
      while (tag_end < n && (std::isalnum(static_cast<unsigned char>(sql[tag_end])) || sql[tag_end] == '_')) {
        ++tag_end;
      }
      if (tag_end < n && sql[tag_end] == '$') {  // $tag$ ... $tag$
        const std::string tag(sql.substr(i, tag_end - i + 1));
        const std::size_t close = sql.find(tag, tag_end + 1);
        i = close == std::string_view::npos ? n : close + tag.size();
        out += ' ';
      } else {
        out += c;
        ++i;
      }
    } else if (c == '"') {  // a quoted identifier, kept
      out += c;
      for (++i; i < n; ++i) {
        out += sql[i];
        if (sql[i] == '"') {
          if (i + 1 < n && sql[i + 1] == '"') {
            out += sql[++i];
            continue;
          }
          ++i;
          break;
        }
      }
    } else {
      out += c;
      ++i;
    }
  }
  return out;
}

// A credential-bearing statement, matched on its words (comments and strings removed)
// and, as a backstop, on a plain split of the raw text. Returns what it is, or none.
std::optional<std::string> SensitiveStatement(std::string_view sql) {
  static const std::regex kSecret(
      R"(\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:PERSISTENT|TEMPORARY|TEMP)\s+)?SECRET\b)",
      std::regex::icase | std::regex::optimize);
  static const std::regex kUser(R"(\b(?:CREATE|ALTER)\s+USER\b)", std::regex::icase | std::regex::optimize);
  // SET / PRAGMA [GLOBAL|SESSION|LOCAL] <name with a credential word>.
  static const std::regex kSetting(
      R"(\b(?:SET|PRAGMA)\s+(?:(?:GLOBAL|SESSION|LOCAL)\s+)?[A-Za-z0-9_.]*(?:password|passwd|pwd|secret|token|access_key|accountkey)[A-Za-z0-9_.]*\b)",
      std::regex::icase | std::regex::optimize);
  auto check = [&](const std::string& text) -> std::optional<std::string> {
    if (std::regex_search(text, kSecret)) return "secret DDL";
    if (std::regex_search(text, kUser)) return "user DDL";
    if (std::regex_search(text, kSetting)) return "a credential setting";
    return std::nullopt;
  };
  if (auto kind = check(WordsOnly(sql))) return kind;
  // Backstop: the raw text with punctuation turned to spaces, in case the tokenizer
  // ever mis-reads a statement.
  std::string raw(sql);
  for (char& ch : raw) {
    if (!std::isalnum(static_cast<unsigned char>(ch)) && ch != '_' && ch != '.') ch = ' ';
  }
  return check(raw);
}

// Masks credential values a statement or a connection error may carry (redact.hpp).
std::string MaskCredentials(std::string text) { return RedactCredentials(std::move(text)); }

}  // namespace

std::string LoggableSql(std::string_view sql) {
  if (auto kind = SensitiveStatement(sql)) return "[REDACTED: " + *kind + "]";
  // Not a whole-statement credential statement, but an ATTACH connection string can
  // still carry a password: mask just the value, keep the rest.
  std::string text(sql);
  if (text.size() > 4000) text = text.substr(0, 4000) + "\xe2\x80\xa6";
  return MaskCredentials(std::move(text));
}

std::string LoggableError(std::string_view sql, std::string_view error) {
  // DuckDB's parser errors and "LINE n: ..." excerpts echo the statement; the error of
  // a credential statement is dropped whole.
  if (SensitiveStatement(sql)) return "[REDACTED: error of a credential statement]";
  std::string text(error);
  if (auto at = text.find("\nLINE "); at != std::string::npos) text.resize(at);
  return MaskCredentials(std::move(text));
}

}  // namespace hardcharger
