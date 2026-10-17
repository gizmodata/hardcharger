// Ported from GizmoSQL 1.x src/client/password_prompt.cpp.

#include "password_prompt.hpp"

#include <iostream>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <io.h>
#include <windows.h>
#else
#include <termios.h>
#include <unistd.h>
#endif

namespace hardcharger::client {

bool IsTerminal() {
#ifdef _WIN32
  return _isatty(_fileno(stdin)) != 0;
#else
  return isatty(STDIN_FILENO) != 0;
#endif
}

std::string PromptPassword(const std::string& prompt) {
  std::cerr << prompt;

#ifdef _WIN32
  HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
  DWORD mode;
  GetConsoleMode(hStdin, &mode);
  SetConsoleMode(hStdin, mode & ~ENABLE_ECHO_INPUT);

  std::string password;
  std::getline(std::cin, password);

  SetConsoleMode(hStdin, mode);
#else
  struct termios old_term, new_term;
  tcgetattr(STDIN_FILENO, &old_term);
  new_term = old_term;
  new_term.c_lflag &= ~ECHO;
  tcsetattr(STDIN_FILENO, TCSANOW, &new_term);

  std::string password;
  std::getline(std::cin, password);

  tcsetattr(STDIN_FILENO, TCSANOW, &old_term);
#endif
  std::cerr << std::endl;

  return password;
}

}  // namespace hardcharger::client
