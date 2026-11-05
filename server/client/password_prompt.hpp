// Ported from GizmoSQL 1.x src/client/password_prompt.hpp.

#pragma once

#include <string>

namespace hardcharger::client {

bool IsTerminal();
// Whether stderr is a terminal (a status line can be rewritten in place).
bool StderrIsTerminal();
std::string PromptPassword(const std::string& prompt = "Password: ");

}  // namespace hardcharger::client
