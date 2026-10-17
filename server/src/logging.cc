#include "logging.hpp"

#include <boost/json.hpp>

#include <chrono>
#include <cstdio>
#include <ctime>
#include <format>
#include <mutex>
#include <stdexcept>

namespace hardcharger {

namespace {

std::mutex log_mutex;
LogLevel min_level = LogLevel::kInfo;
bool json_format = false;

std::string_view LevelName(LogLevel level) {
  switch (level) {
    case LogLevel::kDebug: return "debug";
    case LogLevel::kInfo: return "info";
    case LogLevel::kWarning: return "warning";
    case LogLevel::kError: return "error";
  }
  return "info";
}

std::string Timestamp() {
  const auto now = std::chrono::system_clock::now();
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

}  // namespace

void ConfigureLogging(std::string_view level, std::string_view format) {
  if (level == "debug") min_level = LogLevel::kDebug;
  else if (level == "info") min_level = LogLevel::kInfo;
  else if (level == "warning" || level == "warn") min_level = LogLevel::kWarning;
  else if (level == "error") min_level = LogLevel::kError;
  else throw std::invalid_argument("log_level must be debug, info, warning or error");
  json_format = format == "json";
}

void Log(LogLevel level, std::string_view message, const LogFields& fields) {
  if (level < min_level) {
    return;
  }
  std::string line;
  if (json_format) {
    boost::json::object record{{"time", Timestamp()}, {"level", LevelName(level)},
                               {"message", message}};
    for (const auto& [key, value] : fields) {
      record[key] = value;
    }
    line = boost::json::serialize(record);
  } else {
    line = std::format("{} {:<7} {}", Timestamp(), LevelName(level), message);
    for (const auto& [key, value] : fields) {
      line += std::format(" {}={}", key, value);
    }
  }
  std::lock_guard lock(log_mutex);
  std::fprintf(stderr, "%s\n", line.c_str());
}

}  // namespace hardcharger
