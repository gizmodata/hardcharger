// Ported from GizmoSQL 1.x src/client/shell_loop.hpp.
#pragma once

#include "client_config.hpp"
#include "command_processor.hpp"
#include "warp_connection.hpp"
#include "sql_processor.hpp"

namespace hardcharger::client {

int RunShellLoop(WarpConnection& conn, CommandProcessor& cmd_proc,
                 SqlProcessor& sql_proc, ClientConfig& config);

int RunNonInteractive(WarpConnection& conn, CommandProcessor& cmd_proc,
                      SqlProcessor& sql_proc, ClientConfig& config);

}  // namespace hardcharger::client
