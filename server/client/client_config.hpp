// Ported from GizmoSQL 1.x src/client/client_config.hpp.

#pragma once

#include <iostream>
#include <optional>
#include <string>

namespace hardcharger::client {

enum class OutputMode {
  TABLE,
  BOX,
  CSV,
  TABS,
  JSON,
  JSONLINES,
  MARKDOWN,
  LINE,
  LIST,
  HTML,
  LATEX,
  INSERT,
  QUOTE,
  ASCII,
  TRASH
};

std::string OutputModeToString(OutputMode mode);
std::optional<OutputMode> OutputModeFromString(const std::string& name);

struct ClientConfig {
  // Connection
  std::string host = "localhost";
  int port = 31337;
  std::string username;
  std::string password;
  bool password_provided = false;

  // TLS
  bool use_tls = false;
  std::string tls_roots;
  bool tls_skip_verify = false;
  std::string mtls_cert;
  std::string mtls_key;

  // OAuth/External Auth (the server's browser sign-in, --oauth-port)
  bool auth_type_external = false;
  int oauth_port = 31339;
  std::string oauth_base_url;  // default: http(s)://host:oauth_port

  // Input/Output
  std::optional<std::string> command;
  std::optional<std::string> input_file;
  std::optional<std::string> output_file;
  std::optional<std::string> init_file;
  bool no_init = false;

  // Display
  OutputMode output_mode = OutputMode::BOX;
  bool show_headers = true;
  std::string null_value = "NULL";
  bool show_timer = false;
  bool quiet = false;
  bool echo = false;
  bool bail_on_error = false;
  std::string separator_col = "|";
  std::string separator_row = "\n";
  std::string insert_table = "table";
  int max_rows = 0;
  int max_width = 0;
  bool auto_width = false;  // When true, re-read terminal width before each render

  // Syntax highlighting
  bool syntax_highlighting = true;

  // Tags (sent as SET hardcharger.* after connecting)
  std::string session_tag;
  std::string query_tag;

  // Large results read in up to this many partitions at once, one connection each
  // (Warp SQL §6.10); 0: not set (HARDCHARGER_RESULT_PARTITIONS, else 8). 1: one stream.
  int result_partitions = 0;

  // Dynamic prompt (show catalog.schema)
  bool dynamic_prompt = true;

  // Built-in pager
  bool pager_enabled = true;
  int pager_threshold = 50;  // Minimum rows to trigger pager

  // Runtime
  bool is_interactive = false;
  std::string main_prompt = "hardcharger> ";
  std::string continuation_prompt = "          -> ";
  std::ostream* output_stream = &std::cout;
};

// Fills the options not set on the command line from the environment: HARDCHARGER_HOST,
// HARDCHARGER_PORT, HARDCHARGER_USER (falling back to HARDCHARGER_ADMIN_USERNAME, the
// server's), HARDCHARGER_PASSWORD, HARDCHARGER_TLS, HARDCHARGER_TLS_ROOTS,
// HARDCHARGER_OAUTH_PORT, HARDCHARGER_OAUTH_BASE_URL, HARDCHARGER_SESSION_TAG,
// HARDCHARGER_QUERY_TAG and HARDCHARGER_RESULT_PARTITIONS. No GIZMOSQL_* variable is read.
void ResolveEnvironmentVariables(ClientConfig& config);

/// Parse a hardcharger://HOST:PORT[?params] URI into config fields.
/// Returns true on success, false on parse error (writes to stderr).
bool ParseConnectionURI(const std::string& uri, ClientConfig& config);

/// Build a hardcharger:// URI from the current config (for display in .show).
/// Does not include password.
std::string BuildConnectionURI(const ClientConfig& config);

/// The user's home directory ($HOME, or %USERPROFILE% on Windows), or "" if unknown.
std::string HomeDirectory();

}  // namespace hardcharger::client
