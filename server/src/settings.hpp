// The server's own settings, in SQL: `SET [SESSION|GLOBAL] gizmosql.<name> = <value>`
// (or hardcharger.<name>), `RESET ...`, and the gizmosql_settings() /
// hardcharger_settings() table functions that list them.
//
// Each setting has a scope: SESSION, GLOBAL (admins only, applies to every session at
// once), SESSION_OR_GLOBAL, or STARTUP (fixed by the configuration; SET says where to
// change it). Enterprise settings need their license feature.
#pragma once

#include <arrow/record_batch.h>

#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "sql_text.hpp"

namespace hardcharger {

class Session;
struct Runtime;

enum class SettingScope { kSession, kGlobal, kSessionOrGlobal, kStartup };

struct Setting {
  std::string name;  // without the prefix: "query_timeout"
  SettingScope scope;
  std::string input_type;     // VARCHAR, INTEGER or BOOLEAN
  std::string option;         // Hardcharger's configuration option, if any ("query_timeout_seconds")
  std::string legacy_flag;    // GizmoSQL 1.x's flag ("--query-timeout")
  std::string legacy_env;     // GizmoSQL 1.x's environment variable
  std::string default_value;
  std::string description;
  std::string feature;  // license feature needed to SET it ("" for none)
  bool admin_only = false;  // listed only to admins
  std::function<std::optional<std::string>(const Session&)> get_session;
  std::function<std::optional<std::string>(const Runtime&)> get_global;
  // Both throw warpsql::Error for a bad value. A reset passes std::nullopt.
  std::function<void(Session&, const std::optional<std::string>&)> set_session;
  std::function<void(Runtime&, const std::optional<std::string>&)> set_global;
};

const std::vector<Setting>& Settings();

// Applies a SET or RESET; throws warpsql::Error when it may not or the value is bad.
void ApplySetting(Session& session, const SettingCommand& command);

// The settings `session` may see, as gizmosql_settings() (prefix "gizmosql.", GizmoSQL
// 1.x flags and environment names) or hardcharger_settings() (prefix "hardcharger.",
// Hardcharger's) return them.
std::shared_ptr<arrow::RecordBatch> SettingsTable(const Session& session, bool gizmosql_names);

}  // namespace hardcharger
