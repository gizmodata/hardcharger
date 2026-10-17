// Structured logging to stderr, as text or JSON lines.
#pragma once

#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hardcharger {

enum class LogLevel { kDebug, kInfo, kWarning, kError };

using LogFields = std::vector<std::pair<std::string, std::string>>;

// Configures the process-wide logger. Throws std::invalid_argument for unknown values.
void ConfigureLogging(std::string_view level, std::string_view format);

void Log(LogLevel level, std::string_view message, const LogFields& fields = {});

}  // namespace hardcharger
