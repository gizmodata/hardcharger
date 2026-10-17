// Small operating-system helpers the client needs beyond the terminal ones in
// password_prompt.hpp, pager.cc and output_renderer.cc (each has its POSIX and Windows
// implementation).
#pragma once

#include <string>

namespace hardcharger::client {

// Opens `url` in the user's browser: the command in HARDCHARGER_BROWSER when set (the URL
// is its one argument), else the platform's opener (open, xdg-open, ShellExecute). The
// URL is never passed through a shell. Returns false when nothing could be started.
bool OpenBrowser(const std::string& url);

// Whether standard output is a terminal.
bool IsOutputTerminal();

}  // namespace hardcharger::client
