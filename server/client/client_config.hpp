// Ported from GizmoSQL 1.x src/client/client_config.hpp.

#pragma once

#include <iostream>
#include <optional>
#include <limits>
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
  std::string base_path = "/chargewire";  // where the server mounts Chargewire (a URI's path)
  bool uri_given = false;           // host, port, TLS and base path came from a URI
  bool port_given = false;          // --port (or .connect's port) named it
  std::string username;
  std::string password;
  bool password_provided = false;

  // TLS
  bool use_tls = true;     // TLS unless --tls=false, HARDCHARGER_TLS=false or a URI's ?tls=false
  bool tls_given = false;  // --tls on the command line (the environment doesn't override it)
  std::string tls_roots;
  bool tls_skip_verify = false;
  std::string mtls_cert;
  std::string mtls_key;

  // Browser sign-in through the server's identity provider (Chargewire oauth_browser).
  bool auth_type_external = false;
  // Sign in with the mTLS client certificate (the client_certificate grant), no password.
  bool auth_type_mtls = false;
  // An access token the server already issued (an app that signed in by itself, e.g. the
  // iOS app's console after its own browser sign-in): used as is, no sign-in.
  std::string access_token;

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
  // (Chargewire §6.10); 0: not set (HARDCHARGER_RESULT_PARTITIONS, else 8). 1: one stream.
  int result_partitions = 0;

  // How long to keep sending a request again while the server answers that it is resuming
  // (Chargewire §11.2: a stopped server starting again, such as a GizmoData Cloud cluster with
  // auto-resume), in seconds; kUnsetResumeTimeout: not set (HARDCHARGER_RESUME_TIMEOUT, else 600).
  // 0: don't wait.
  static constexpr int kUnsetResumeTimeout = std::numeric_limits<int>::min();
  int resume_timeout = kUnsetResumeTimeout;

  // An option or environment variable with a value it can't take, said for the person to fix
  // (ResolveEnvironmentVariables sets it; main reports it and exits). Empty: none.
  std::string invalid_option;

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
// server's), HARDCHARGER_PASSWORD, HARDCHARGER_TLS (true/false: TLS is on unless it, --tls=false
// or a URI says false), HARDCHARGER_TLS_ROOTS,
// HARDCHARGER_SESSION_TAG,
// HARDCHARGER_QUERY_TAG and HARDCHARGER_RESULT_PARTITIONS. No GIZMOSQL_* variable is read.
void ResolveEnvironmentVariables(ClientConfig& config);

/// Whether `host` is a GizmoData Cloud host (gizmodata.com or *.gizmodata.com, any case, a
/// trailing dot allowed), whose Hardcharger clusters answer on port 443.
bool IsGizmoDataCloudHost(std::string host);

/// Parse a connection URI into host, port, TLS and base path, with the Hardcharger ADBC
/// driver's rules: hardcharger://HOST[:PORT][/PREFIX][?tls=false] (HTTPS unless ?tls=false,
/// port 31337), chargewire:// (the same, port 443), https:// and http://. Only `tls` is a
/// parameter; credentials, other parameters and hardcharger+http:// are refused.
/// Returns true on success, false on a refused URI (the reason goes to stderr).
bool ParseConnectionURI(const std::string& uri, ClientConfig& config);

/// The hardcharger:// URI of the current host, port, base path and TLS (for .show). No
/// credentials.
std::string BuildConnectionURI(const ClientConfig& config);

/// The user's home directory ($HOME, or %USERPROFILE% on Windows), or "" if unknown.
std::string HomeDirectory();

}  // namespace hardcharger::client
