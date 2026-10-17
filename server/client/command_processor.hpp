// Ported from GizmoSQL 1.x src/client/command_processor.hpp.
#pragma once

#include <fstream>
#include <functional>
#include <istream>
#include <memory>
#include <string>
#include <vector>

#include <arrow/table.h>

#include "client_config.hpp"
#include "warp_connection.hpp"

namespace hardcharger::client {

// Windows headers define ERROR as a macro; undefine it so we can use it as an enum value.
#ifdef ERROR
#undef ERROR
#endif

enum class CommandResult {
  OK,
  EXIT,
  ERROR,
  NOT_A_COMMAND,
};

class CommandProcessor {
 public:
  CommandProcessor(WarpConnection& conn, ClientConfig& config)
      : conn_(conn), config_(config) {}

  // Returns true if the line starts with '.' (dot command)
  static bool IsDotCommand(const std::string& line);

  // Process a dot command. Returns the result.
  CommandResult Process(const std::string& line);

  // Set a callback to be called when schema cache should be refreshed
  // (e.g., after .connect or .refresh)
  void SetRefreshCallback(std::function<void()> callback);

  // Set a callback for refreshing the dynamic prompt after context changes
  void SetPromptRefreshCallback(std::function<void()> callback);

  // Set pointer to the last result cache (for .last / .export_last)
  void SetLastResult(std::shared_ptr<arrow::Table>* last_result);

  // Set the callback that runs the statements of a file (.read FILE); it returns the
  // exit code the lines produced (0: all ran).
  void SetReadCallback(std::function<int(std::istream&)> callback);

  // Call after each statement's output: ends a .once redirection.
  void AfterStatement();

 private:
  void ShowHelp(const std::string& pattern);
  void ShowSettings();
  CommandResult HandleConnect(const std::vector<std::string>& args);

  WarpConnection& conn_;
  ClientConfig& config_;
  std::ofstream* output_file_ = nullptr;
  bool once_mode_ = false;
  std::function<void()> refresh_callback_;
  std::function<void()> prompt_refresh_callback_;
  std::shared_ptr<arrow::Table>* last_result_ = nullptr;
  std::function<int(std::istream&)> read_callback_;
};

}  // namespace hardcharger::client
