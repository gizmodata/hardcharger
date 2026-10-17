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

// Would a record at `level` be written?
bool LogEnabled(LogLevel level);

// SQL as it may be logged: statements that can carry credentials (CREATE SECRET,
// CREATE / ALTER USER ... PASSWORD) are replaced by what they are, wherever their
// keywords sit (comments cannot hide them); long text is shortened.
std::string LoggableSql(std::string_view sql);

}  // namespace hardcharger
