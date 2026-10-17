// The server's SQL functions, registered once on the instance's catalog through the v2
// C API:
//
//   HARDCHARGER_USER(), _ROLE(), _VERSION(), _CURRENT_SESSION(), _CURRENT_INSTANCE(),
//   _CURRENT_CLUSTER(), _INSTRUMENTATION_ENABLED(), _INSTRUMENTATION_CATALOG(),
//   _INSTRUMENTATION_SCHEMA(); table functions hardcharger_settings(),
//   hardcharger_sessions() and hardcharger_metrics().
//
// They answer for the session whose statement is being bound: DuckDB binds a statement
// on the thread that runs it, and the engine marks that thread with its session.
#pragma once

#include <duckdb_v2.h>

namespace hardcharger {

struct Runtime;

void RegisterSqlFunctions(duckdb_v2_connection_handle connection, Runtime& runtime);

}  // namespace hardcharger
