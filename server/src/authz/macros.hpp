// Macros a user other than the administrator creates (the CREATE MACRO and DROP MACRO
// privileges, granted ON SCHEMA or ON CATALOG).
//
// DuckDB expands a macro into the statement that calls it, so its body runs with the
// rights of whoever runs that statement, and the plan check sees it as part of the
// statement. What a macro must not do is change what someone else's SQL computes. DuckDB
// finds an unqualified function in the search path before its built-in functions (a view
// in schema s looks in s, then in its catalog's main), so a macro could:
//
//   - replace a built-in function, or one an extension would autoload, everywhere that
//     name is called (CREATE MACRO main.lower(x) AS ..., also inside every view);
//   - replace, or stand in for, a function that a view, a row or masking policy, or a
//     macro one of them calls uses: the view reads with its owner's rights, so the
//     replacement's body would read with them too, and a row policy could be made true.
//
// MacroNameRefusal refuses both. Temporary macros stay the administrator's: a session's
// macro is found before every other function, inside other principals' views too.

#pragma once

#include <optional>
#include <string>

#include "authz/types.hpp"

namespace duckdb {
class ClientContext;
}

namespace hardcharger::authz {

class Snapshot;

// Why a macro at `path` (catalog, schema, name) may not be created or replaced by a user
// other than the administrator; nothing when it may. Runs inside the statement's
// transaction (the plan check), reading the catalogs as the statement sees them.
std::optional<std::string> MacroNameRefusal(duckdb::ClientContext& context, const Snapshot& snapshot,
                                            const ObjectPath& path);

// Why a type at `path` may not be created (`verb` "create"), replaced ("replace") or dropped
// ("drop") by a user other than the administrator; nothing when it may. A type is found by
// name where a body is bound (a view's query, a macro's or trigger's body, a policy, a
// column default or CHECK constraint), so a user's type could stand in for one of those, and
// dropping or replacing one they name changes what they compute: both are refused, as is a
// built-in type's name or an extension's.
std::optional<std::string> TypeNameRefusal(duckdb::ClientContext& context, const Snapshot& snapshot,
                                           const ObjectPath& path, const std::string& verb);

// The same for a sequence: nextval('s') finds a sequence by name in the search path as a
// function call finds a macro, so a new sequence must not take (or stand in for) a name
// that a table's column default, a view or a macro gives nextval(), currval() or setval().
std::optional<std::string> SequenceNameRefusal(duckdb::ClientContext& context, const ObjectPath& path);

}  // namespace hardcharger::authz
