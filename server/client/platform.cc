#include "platform.hpp"

#include <cstdlib>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <io.h>
#include <windows.h>
#include <shellapi.h>
#else
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace hardcharger::client {

bool OpenBrowser(const std::string& url) {
  const char* custom = std::getenv("HARDCHARGER_BROWSER");
  const bool has_custom = custom != nullptr && *custom != '\0';
#ifdef _WIN32
  if (has_custom) {
    // TODO(windows): start HARDCHARGER_BROWSER with CreateProcess (the URL as its argument).
    auto result = reinterpret_cast<intptr_t>(ShellExecuteA(nullptr, "open", custom, url.c_str(), nullptr, SW_SHOWNORMAL));
    return result > 32;
  }
  auto result = reinterpret_cast<intptr_t>(ShellExecuteA(nullptr, "open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL));
  return result > 32;
#else
#if defined(__APPLE__)
  const char* opener = "open";
#else
  const char* opener = "xdg-open";
#endif
  std::string program = has_custom ? custom : opener;
  std::vector<char*> argv{program.data(), const_cast<char*>(url.c_str()), nullptr};
  pid_t pid = 0;
  if (posix_spawnp(&pid, program.c_str(), nullptr, nullptr, argv.data(), environ) != 0) return false;
  // A custom command (a test's, a script) may do the sign-in itself: don't wait for it,
  // but reap it when it ends.
  if (has_custom) {
    std::thread([pid] { waitpid(pid, nullptr, 0); }).detach();
    return true;
  }
  int status = 0;
  if (waitpid(pid, &status, 0) != pid) return false;
  return WIFEXITED(status) && WEXITSTATUS(status) == 0;
#endif
}

bool IsOutputTerminal() {
#ifdef _WIN32
  return _isatty(_fileno(stdout)) != 0;
#else
  return isatty(STDOUT_FILENO) != 0;
#endif
}

bool IsErrorTerminal() {
#ifdef _WIN32
  return _isatty(_fileno(stderr)) != 0;
#else
  return isatty(STDERR_FILENO) != 0;
#endif
}

namespace {
bool NoColorRequested() {
  const char* value = std::getenv("NO_COLOR");
  return value != nullptr && value[0] != '\0';
}
}  // namespace

bool ColorOnStdout() { return !NoColorRequested() && IsOutputTerminal(); }
bool ColorOnStderr() { return !NoColorRequested() && IsErrorTerminal(); }

// Bold red, as DuckDB's shell shows errors.
const char* ErrorStyle() { return ColorOnStderr() ? "\033[1;31m" : ""; }
const char* ErrorStyleReset() { return ColorOnStderr() ? "\033[0m" : ""; }

}  // namespace hardcharger::client
