// Ported from GizmoSQL 1.x src/client/shell_loop.hpp.
#pragma once

#include "client_config.hpp"
#include "command_processor.hpp"
#include "chargewire_connection.hpp"
#include "sql_processor.hpp"

namespace hardcharger::client {

int RunShellLoop(ChargewireConnection& conn, CommandProcessor& cmd_proc,
                 SqlProcessor& sql_proc, ClientConfig& config);

int RunNonInteractive(ChargewireConnection& conn, CommandProcessor& cmd_proc,
                      SqlProcessor& sql_proc, ClientConfig& config);

// Sets config.main_prompt to the session's current catalog and schema (dynamic_prompt).
void RefreshDynamicPrompt(ChargewireConnection& conn, ClientConfig& config);

}  // namespace hardcharger::client
