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

#ifdef _WIN32
namespace {
std::wstring Wide(const std::string& text) {
  if (text.empty()) return {};
  const int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
  std::wstring out(static_cast<std::size_t>(size), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), size);
  return out;
}
}  // namespace

void EnableTerminalColors() {
  // Windows 10 consoles interpret ANSI escape sequences (colors, the pager's screen
  // control) only when asked to.
  for (DWORD id : {STD_OUTPUT_HANDLE, STD_ERROR_HANDLE}) {
    HANDLE handle = GetStdHandle(id);
    DWORD mode = 0;
    if (handle != INVALID_HANDLE_VALUE && GetConsoleMode(handle, &mode)) {
      SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
    }
  }
}
#else
void EnableTerminalColors() {}
#endif

bool OpenBrowser(const std::string& url) {
  const char* custom = std::getenv("HARDCHARGER_BROWSER");
  const bool has_custom = custom != nullptr && *custom != '\0';
#ifdef _WIN32
  // The URL as one quoted argument: a URL has no '"' (it would be percent-encoded).
  if (url.find('"') != std::string::npos) return false;
  const std::wstring wide_url = Wide(url);
  if (has_custom) {
    // HARDCHARGER_BROWSER is a command line here (Windows passes programs one string):
    // the program and any arguments, the URL appended as the last one. CreateProcess
    // runs it directly, never through cmd.exe, so nothing in the URL is interpreted.
    std::wstring command = Wide(custom) + L" \"" + wide_url + L"\"";
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION info{};
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr, &startup, &info)) {
      return false;
    }
    // As on POSIX: a custom command may do the sign-in itself, so it isn't waited for.
    CloseHandle(info.hThread);
    CloseHandle(info.hProcess);
    return true;
  }
  auto result = reinterpret_cast<intptr_t>(
      ShellExecuteW(nullptr, L"open", wide_url.c_str(), nullptr, nullptr, SW_SHOWNORMAL));
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
