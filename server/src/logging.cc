#include "logging.hpp"

#include <boost/json.hpp>

#include <chrono>
#include <cstdio>
#include <ctime>
#include <format>
#include <mutex>
#include <cctype>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <thread>

#include "platform.hpp"
#include "redact.hpp"

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
  const std::tm utc = UtcTime(seconds);
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
