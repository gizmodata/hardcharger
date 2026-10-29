// The console of embedded/hardcharger_embedded.h: the `hardcharger` command-line client's
// own machinery (client/: ChargewireConnection, CommandProcessor, SqlProcessor, the renderers and
// RunNonInteractive, the path `hardcharger -c` takes), with what it writes to stdout and
// stderr captured for the app.
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>

#include "client_config.hpp"
#include "command_processor.hpp"
#include "hardcharger_embedded.h"
#include "shell_loop.hpp"
#include "sql_processor.hpp"
#include "chargewire_connection.hpp"

namespace hc = hardcharger::client;

struct hardcharger_console {
  hc::ClientConfig config;
  hc::ChargewireConnection connection;
  std::unique_ptr<hc::CommandProcessor> commands;
};

namespace {

// The client writes to std::cout and std::cerr; a run sends both to one buffer, in the
// order written. One run at a time in the process holds them (so two consoles' statements,
// in two windows, run one after the other).
std::mutex stdio_mutex;

class Capture {
 public:
  explicit Capture(std::ostream& text) : out_(std::cout.rdbuf(text.rdbuf())), err_(std::cerr.rdbuf(text.rdbuf())) {}
  ~Capture() {
    std::cout.flush();
    std::cerr.flush();
    std::cout.rdbuf(out_);
    std::cerr.rdbuf(err_);
  }

 private:
  std::streambuf* out_;
  std::streambuf* err_;
};

char* Copy(const std::string& text) {
  char* copy = static_cast<char*>(std::malloc(text.size() + 1));
  if (copy != nullptr) std::memcpy(copy, text.c_str(), text.size() + 1);
  return copy;
}

void CopyError(const std::string& message, char* error, size_t size) {
  if (error == nullptr || size == 0) return;
  const size_t n = std::min(message.size(), size - 1);
  std::memcpy(error, message.data(), n);
  error[n] = '\0';
}

// Splits `input` as RunNonInteractive's ProcessLines does: dot commands on lines of their
// own (when no statement is open), statements up to each ';'.
bool Scan(const std::string& input, int* statements) {
  hc::SqlProcessor sql;
  int count = 0;
  bool any = false;
  std::istringstream lines(input);
  std::string line;
  while (std::getline(lines, line)) {
    if (line.empty() && !sql.IsAccumulating()) continue;
    if (!sql.IsAccumulating() && hc::CommandProcessor::IsDotCommand(line)) {
      any = true;
      continue;
    }
    std::string rest = line;
    while (!rest.empty() && sql.AccumulateLine(rest)) {
      if (!sql.GetStatement().empty()) {
        count++;
        any = true;
      }
      rest = sql.GetRemainder();
      sql.Reset();
      if (hc::CommandProcessor::IsDotCommand(rest)) {
        any = true;
        break;
      }
    }
  }
  if (sql.IsAccumulating()) count++;  // runs too, when asked to run anyway
  if (statements != nullptr) *statements = count;
  return any && !sql.IsAccumulating();
}

}  // namespace

extern "C" {

int hardcharger_console_open(const hardcharger_console_options* options, hardcharger_console** out, char* error,
                             size_t error_size) {
  if (out != nullptr) *out = nullptr;
  if (options == nullptr || out == nullptr || options->host == nullptr) {
    CopyError("hardcharger_console_open: needs options with a host", error, error_size);
    return 2;
  }
  // The client colours its prompt and errors at a terminal; an app shows plain text (and an
  // app's stdout may well be a terminal, under a debugger).
  setenv("NO_COLOR", "1", 1);
  auto console = std::make_unique<hardcharger_console>();
  hc::ClientConfig& c = console->config;
  c.host = options->host;
  c.port = options->port;
  c.use_tls = options->tls != 0;
  c.tls_given = true;
  if (options->tls_roots != nullptr) c.tls_roots = options->tls_roots;
  if (options->username != nullptr) c.username = options->username;
  if (options->password != nullptr) {
    c.password = options->password;
    c.password_provided = true;
  }
  if (options->access_token != nullptr) c.access_token = options->access_token;
  // As at a terminal (row-count footers, the box format), without the pager, colours or
  // a terminal's width: the app scrolls wide tables sideways.
  c.is_interactive = true;
  c.pager_enabled = false;
  c.syntax_highlighting = false;
  c.max_width = options->max_width;
  c.max_rows = options->max_rows;
  c.auto_width = false;
  c.bail_on_error = true;
  c.output_stream = &std::cout;
  // Connecting prints nothing (its failures are the status), so it holds no capture: a slow
  // server doesn't keep another console waiting.
  const arrow::Status status = console->connection.Connect(c);
  if (status.ok()) {
    std::ostringstream text;
    std::lock_guard lock(stdio_mutex);
    Capture capture(text);
    hc::RefreshDynamicPrompt(console->connection, c);
  }
  if (!status.ok()) {
    CopyError(status.message(), error, error_size);
    return 1;
  }
  console->commands = std::make_unique<hc::CommandProcessor>(console->connection, console->config);
  *out = console.release();
  return 0;
}

char* hardcharger_console_run(hardcharger_console* console, const char* input, int* exit_requested) {
  if (exit_requested != nullptr) *exit_requested = 0;
  if (console == nullptr || input == nullptr) return Copy("");
  int statements = 0;
  Scan(input, &statements);
  hc::ClientConfig& c = console->config;
  c.command = std::string(input);
  c.echo = statements > 1;
  c.output_stream = &std::cout;
  std::ostringstream text;
  {
    std::lock_guard lock(stdio_mutex);
    Capture capture(text);
    hc::SqlProcessor sql;
    hc::RunNonInteractive(console->connection, *console->commands, sql, c);
    hc::RefreshDynamicPrompt(console->connection, c);
  }
  c.command.reset();
  c.echo = false;
  // .exit and .quit end ProcessLines; the input says whether one did.
  if (exit_requested != nullptr) {
    std::istringstream lines(input);
    std::string line;
    while (std::getline(lines, line)) {
      const auto start = line.find_first_not_of(" \t");
      if (start != std::string::npos && (line.compare(start, 5, ".exit") == 0 || line.compare(start, 5, ".quit") == 0)) {
        *exit_requested = 1;
      }
    }
  }
  return Copy(text.str());
}

void hardcharger_console_cancel(hardcharger_console* console) {
  if (console == nullptr || !console->connection.IsExecuting()) return;
  console->connection.RequestCancel();
  console->connection.SendCancelToServer();
}

char* hardcharger_console_prompt(hardcharger_console* console, int continuation) {
  if (console == nullptr) return Copy("hardcharger> ");
  return Copy(continuation ? console->config.continuation_prompt : console->config.main_prompt);
}

int hardcharger_console_input_complete(const char* input, int* statements) {
  if (input == nullptr) {
    if (statements != nullptr) *statements = 0;
    return 0;
  }
  return Scan(input, statements) ? 1 : 0;
}

void hardcharger_console_close(hardcharger_console* console) { delete console; }

void hardcharger_console_free_text(char* text) { std::free(text); }

}  // extern "C"
