// hardcharger: Hardcharger's command-line SQL client, over Warp SQL. Ported from GizmoSQL
// 1.x's gizmosql_client (src/client/main.cpp): the same options, with Hardcharger's names
// (docs/migrating-from-gizmosql.md maps them).
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#include "client_config.hpp"
#include "command_processor.hpp"
#include "options.hpp"
#include "output_renderer.hpp"
#include "password_prompt.hpp"
#include "shell_loop.hpp"
#include "signal_watcher.hpp"
#include "sql_processor.hpp"
#include "sql_quoting.hpp"
#include "version.hpp"
#include "warp_connection.hpp"

using namespace hardcharger::client;

namespace {

void SigtermHandler(int /*signum*/) {
  // Nothing here is async-signal-safe to tear down. The server reaps abandoned sessions;
  // an ordinary exit closes the session explicitly.
  std::_Exit(0);
}

}  // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
  SetConsoleOutputCP(CP_UTF8);
#endif

  ClientConfig config;
  CommandLine cl;
  std::string auth_type = "password";
  std::string uri_flag;
  std::string command, file, output, init;
  bool help = false, version = false, password_prompt = false;
  bool csv = false, json = false, table = false, box = false, markdown = false, no_header = false;

  cl.Section("Connection");
  cl.Value("host", 'h', "HOST", "Server host (default: localhost; env: HARDCHARGER_HOST)", &config.host);
  cl.Int("port", 'p', "PORT", "Server port (default: 31337; env: HARDCHARGER_PORT)", &config.port);
  cl.Value("username", 'u', "USER",
           "Username (env: HARDCHARGER_USER, falls back to HARDCHARGER_ADMIN_USERNAME for parity with the server)",
           &config.username);
  cl.Flag("password", 'W', "Force a password prompt (else env: HARDCHARGER_PASSWORD, or a prompt)", &password_prompt);
  cl.Value("uri", 0, "URI",
           "Connection URI: hardcharger://HOST:PORT[?params] (also the one positional argument; "
           "cannot be combined with --host, --port, --username, --tls, etc.)",
           &uri_flag);
  cl.Section("TLS");
  cl.Flag("tls", 0, "Use TLS (HTTPS) for the connection (env: HARDCHARGER_TLS)", &config.use_tls);
  cl.Value("tls-roots", 0, "FILE", "Trusted root certificates (PEM; default: the system's) (env: HARDCHARGER_TLS_ROOTS)",
           &config.tls_roots);
  cl.Flag("tls-skip-verify", 0, "Skip TLS server certificate verification (development only)", &config.tls_skip_verify);
  cl.Value("mtls-cert", 0, "FILE", "mTLS client certificate (PEM)", &config.mtls_cert);
  cl.Value("mtls-key", 0, "FILE", "mTLS client private key (PEM)", &config.mtls_key);
  cl.Section("Sign-in");
  cl.Value("auth-type", 0, "TYPE", "'password' (default) or 'external' (the server's browser sign-in, OAuth/SSO)",
           &auth_type);
  cl.Int("oauth-port", 0, "PORT", "The server's browser sign-in port (default: 31339; env: HARDCHARGER_OAUTH_PORT)",
         &config.oauth_port);
  cl.Value("oauth-base-url", 0, "URL",
           "The browser sign-in server's URL (default: http(s)://HOST:OAUTH_PORT; env: HARDCHARGER_OAUTH_BASE_URL)",
           &config.oauth_base_url);
  cl.Section("Input and output");
  cl.Value("command", 'c', "SQL", "Execute SQL (and dot commands) and exit", &command);
  cl.Value("file", 'f', "FILE", "Execute SQL from FILE and exit", &file);
  cl.Value("output", 'o', "FILE", "Write output to FILE", &output);
  cl.Value("init", 0, "FILE", "Init file to execute on startup (default: ~/.hardchargerrc)", &init);
  cl.Flag("no-init", 0, "Do not execute the init file", &config.no_init);
  cl.Section("Tags");
  cl.Value("session-tag", 0, "JSON", "Session tag sent after connecting (env: HARDCHARGER_SESSION_TAG)",
           &config.session_tag);
  cl.Value("query-tag", 0, "JSON", "Query tag sent after connecting (env: HARDCHARGER_QUERY_TAG)", &config.query_tag);
  cl.Int("result-partitions", 0, "N",
         "Read large results in up to N partitions at once, over N connections (env: "
         "HARDCHARGER_RESULT_PARTITIONS; default 1)",
         &config.result_partitions);
  cl.Section("Output modes");
  cl.Flag("csv", 0, "Set output mode to CSV", &csv);
  cl.Flag("json", 0, "Set output mode to JSON", &json);
  cl.Flag("table", 0, "Set output mode to table (ASCII borders)", &table);
  cl.Flag("box", 0, "Set output mode to box (Unicode borders, default)", &box);
  cl.Flag("markdown", 0, "Set output mode to markdown", &markdown);
  cl.Flag("no-header", 0, "Disable column headers", &no_header);
  cl.Section("Display");
  cl.Flag("quiet", 'q', "Suppress the welcome banner and informational messages", &config.quiet);
  cl.Flag("echo", 'e', "Echo executed SQL statements", &config.echo);
  cl.Flag("bail", 0, "Stop on first error", &config.bail_on_error);
  cl.Value("null", 0, "TEXT", "String to display for NULL values (default: NULL)", &config.null_value);
  cl.Section("Other");
  cl.Flag("help", '?', "Show this help message", &help);
  cl.Flag("version", 'v', "Show version", &version);

  std::string positional_uri;
  if (auto error = cl.Parse(argc, argv, &positional_uri)) {
    std::cerr << "Error: " << *error << std::endl;
    std::cerr << "Try '" << argv[0] << " --help' for more information." << std::endl;
    return 1;
  }

  if (help) {
    std::cout << "Usage: hardcharger [OPTIONS] [URI]" << std::endl << std::endl;
    std::cout << "Hardcharger Client Options" << std::endl;
    cl.PrintHelp(std::cout);
    return 0;
  }

  if (version) {
    std::cout << "Hardcharger Client " << hardcharger::kVersion << std::endl;
    return 0;
  }

  // Resolve auth type
  if (auth_type == "external") {
    config.auth_type_external = true;
  } else if (auth_type != "password") {
    std::cerr << "Error: unknown auth-type '" << auth_type << "'. Use 'password' or 'external'." << std::endl;
    return 1;
  }

  // Parse connection URI if provided
  bool uri_provided = false;
  if (!positional_uri.empty() && cl.Given("uri")) {
    std::cerr << "Error: give the URI once (positionally or with --uri)" << std::endl;
    return 1;
  }
  const std::string uri = !positional_uri.empty() ? positional_uri : uri_flag;
  if (!uri.empty()) {
    // --uri is mutually exclusive with individual connection flags
    std::vector<std::string> conflicts;
    for (const char* name : {"host", "port", "username", "password", "tls", "tls-roots", "tls-skip-verify", "auth-type"}) {
      if (cl.Given(name)) conflicts.push_back(std::string("--") + name);
    }
    if (!conflicts.empty()) {
      std::cerr << "Error: --uri cannot be combined with";
      for (size_t i = 0; i < conflicts.size(); ++i) {
        if (i > 0) std::cerr << ",";
        std::cerr << " " << conflicts[i];
      }
      std::cerr << std::endl;
      return 1;
    }
    if (!ParseConnectionURI(uri, config)) {
      return 1;
    }
    uri_provided = true;
  }

  // Resolve environment variables for unset options
  ResolveEnvironmentVariables(config);

  // Output mode shortcuts
  if (csv) config.output_mode = OutputMode::CSV;
  if (json) config.output_mode = OutputMode::JSON;
  if (table) config.output_mode = OutputMode::TABLE;
  if (box) config.output_mode = OutputMode::BOX;
  if (markdown) config.output_mode = OutputMode::MARKDOWN;
  if (no_header) config.show_headers = false;

  // Handle -c and -f
  if (cl.Given("command")) config.command = command;
  if (cl.Given("file")) config.input_file = file;
  if (cl.Given("output")) config.output_file = output;
  if (cl.Given("init")) config.init_file = init;

  // Password resolution (only for password auth). Like psql, --password/-W only forces
  // the interactive prompt; the sources are HARDCHARGER_PASSWORD or the prompt.
  if (!config.auth_type_external) {
    if (password_prompt) {
      if (IsTerminal()) {
        config.password = PromptPassword();
        config.password_provided = true;
      } else {
        std::cerr << "Error: password prompt requires a terminal" << std::endl;
        return 1;
      }
    }
    // After env var resolution, if we still need a password and have a username
    if (!config.password_provided && !config.username.empty()) {
      if (IsTerminal()) {
        config.password = PromptPassword();
        config.password_provided = true;
      }
    }
  }

  // Determine if interactive
  config.is_interactive = !config.command.has_value() && !config.input_file.has_value() && IsTerminal();

  // Set truncation defaults for interactive box/table mode
  if (config.is_interactive && !config.output_file.has_value()) {
    config.max_rows = 40;
    config.max_width = GetTerminalWidth();
    config.auto_width = true;
  }

  // Set up output file redirect
  std::ofstream output_file_stream;
  if (config.output_file.has_value()) {
    output_file_stream.open(*config.output_file);
    if (!output_file_stream.is_open()) {
      std::cerr << "Error: cannot open output file '" << *config.output_file << "'" << std::endl;
      return 1;
    }
    config.output_stream = &output_file_stream;
  }

  // Determine whether connection params were explicitly provided.
  // In interactive mode, we allow starting without a connection.
  auto env_set = [](const char* name) {
    const char* val = std::getenv(name);
    return val != nullptr && val[0] != '\0';
  };
  bool host_explicit = cl.Given("host") || env_set("HARDCHARGER_HOST");
  bool user_explicit = cl.Given("username") || env_set("HARDCHARGER_USER") || env_set("HARDCHARGER_ADMIN_USERNAME");
  bool has_connection_params = host_explicit || user_explicit || config.auth_type_external || uri_provided;

  WarpConnection conn;

  std::signal(SIGTERM, SigtermHandler);

  // Ctrl-C while a statement runs cancels it through Warp SQL's cancel and the shell
  // goes on. At the prompt, replxx reads Ctrl-C as a key (it turns ISIG off), so no
  // signal arrives there. A script run (not interactive) that is not waiting on a
  // statement stops, as Ctrl-C would stop any other program.
  const bool interactive = config.is_interactive;
  SignalWatcher signal_watcher([&conn, interactive] {
    if (!interactive && !conn.IsExecuting()) std::_Exit(130);
    conn.RequestCancel();
    conn.SendCancelToServer();
  });

  if (has_connection_params) {
    auto connect_status = conn.Connect(config);
    if (!connect_status.ok()) {
      std::cerr << "Error: " << connect_status.message() << std::endl;
      return 1;
    }

    // Set tags if configured (can be overridden later via SET hardcharger.*)
    if (!config.session_tag.empty()) {
      auto tag_result = conn.ExecuteUpdate("SET hardcharger.session_tag = " + QuoteSqlLiteral(config.session_tag));
      if (!tag_result.ok()) {
        std::cerr << "Warning: failed to set session_tag: " << tag_result.status().message() << std::endl;
      }
    }
    if (!config.query_tag.empty()) {
      auto tag_result = conn.ExecuteUpdate("SET hardcharger.query_tag = " + QuoteSqlLiteral(config.query_tag));
      if (!tag_result.ok()) {
        std::cerr << "Warning: failed to set query_tag: " << tag_result.status().message() << std::endl;
      }
    }
  }
  // else: start in disconnected state — user can use .connect later

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  if (config.is_interactive) {
    return RunShellLoop(conn, cmd_proc, sql_proc, config);
  }
  return RunNonInteractive(conn, cmd_proc, sql_proc, config);
}
