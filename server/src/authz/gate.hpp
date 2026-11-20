// The pre-bind gate for store users' statements.
//
// The plan check runs after DuckDB has bound a statement, and binding is not free of
// side effects: binding read_csv('https://...') or SELECT * FROM 's3://...' already
// fetches the file to learn its columns, binding read_csv('/etc/x') says whether the
// file exists, and query('<sql>') binds SQL from a string. So the families of
// statements that reach outside the database are refused on the parsed statement,
// before DuckDB binds it, unless the user holds the system privilege that opens them.
// The plan check then checks them again. A kind of statement this gate has no rule for
// is refused (it is never bound), and so is anything DuckDB evaluates while it binds
// that the plan check would refuse: a sequence moved by a table function's argument,
// and the bodies of the user macros a statement calls, with their arguments (a table
// macro that hands its argument to query() or read_csv()).
#pragma once

#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "authz/types.hpp"

namespace duckdb {
class SQLStatement;
class MacroFunction;
}

namespace hardcharger::authz {

// The gated family the statement belongs to ("ATTACH", "read_csv() (files and URLs)",
// "COPY TO (files and URLs)", ...), or nullopt. `permitted(category)` says whether the
// session holds the system privilege for a category; such categories are not reported.
//
// `named_table`, if set, is called with the name parts of every table or view the
// statement names (including those DuckDB binds only to describe them, as DESCRIBE and
// CREATE VIEW do, whose plans the plan check never sees), and whether the statement
// describes it (DESCRIBE of it or of a query that names it); it may throw to refuse.
//
// `table_function_allowed`, if set, says whether a table function that is not one of
// the families above may be called (by its name parts); binding one already runs it
// (a remote database's scan connects, an unknown name autoloads an extension), so one
// that is not allowed (nullopt) is refused here. One that is allowed comes with the
// arguments that name the files it reads (an EXECUTE grant's declarations; none for the
// functions everyone may call and for macros): their literal or bound values go to `storage`
// before binding opens them, as a file-reading function's first argument does.
//
// `storage`, if set, is called for each read or write of files, URLs or object storage the
// statement names (read_csv(...), FROM '<path>', COPY, EXPORT and IMPORT DATABASE), with
// the paths as written or resolved by `parameters`: `paths` is nullopt for computed
// expressions and unresolved parameters. It may throw to refuse. Without it, those statements are
// reported as categories ("read_csv() (files and URLs)", ...).
struct StorageUse {
  std::string what;  // "read_csv()", "COPY ... TO", ...
  bool write = false;
  std::optional<std::vector<std::string>> paths;
  bool directory = false;  // writes (or reads) a directory of files
  bool own_client = false;  // delta_*() / iceberg_*(): reads what its table's metadata names (paths unknown)
};
using NamedTableCallback = std::function<void(const std::vector<std::string>&, bool described)>;
using TableFunctionCallback =
    std::function<std::optional<std::vector<PathArgument>>(const std::vector<std::string>&)>;
// Resolve only actual bind values; never evaluate a SQL expression to discover a path.
using PathParameterCallback = std::function<std::optional<std::vector<std::string>>(const std::string&)>;
using StorageCallback = std::function<void(const StorageUse&)>;

// `sequence`, if set, is called for every nextval() / currval() / setval() in a place DuckDB
// evaluates while it binds: a table function's arguments (CALL's too, and a table macro's,
// wherever its body puts them), a PRAGMA's parameters, a setting's value, an AT clause,
// EXECUTE's values, and COPY's file name and options. DuckDB has run such a call, and folded
// its result into a constant, before there is a plan to check, so the plan check never sees
// it. It gets the function and the sequence's name when that is a string literal (nullopt
// otherwise), and may throw to refuse.
//
// `macros`, if set, returns copies of the definitions of the user macro (a table macro when
// `table`) that the name parts find, or none. DuckDB binds a macro's body where it is called,
// with the call's arguments in place of its parameters: the body is walked here the same way,
// and an argument a parameter stands for counts as written in the body (so a table macro that
// hands its parameter to read_csv() or query() is checked with the string the caller gave).
// Inside a macro's body, query() runs the SQL it is handed: a string literal there is walked
// as a statement of its own, anything else is refused.
using SequenceCallback = std::function<void(const std::string& function, const std::optional<std::string>& sequence)>;
using MacroCallback =
    std::function<std::vector<std::shared_ptr<duckdb::MacroFunction>>(const std::vector<std::string>& parts, bool table)>;
// A table function refused in the body of a macro the statement calls is reported as
// "the table function <f>" + kInMacro + "<macro>".
inline const std::string kInMacro = " in the body of macro ";

struct BindTimeCallbacks {
  SequenceCallback sequence;
  MacroCallback macros;
};

// `ducklake_call`, if set (a catalog_access principal's statements only: authz/ducklake_calls.hpp),
// is called first for every table function in FROM and every CALL, with the call's name
// parts, its first positional argument if that is a string literal, what wraps the call
// ("" when the statement runs it itself, else "the view <name>", "a macro", "a trigger")
// and an id for the call (the same each time the walk meets it). It says whether the call
// is a DuckLake call, which it then has decided (it throws to refuse); other calls go on
// to the rules above.
struct TableFunctionCall {
  std::vector<std::string> parts;
  std::optional<std::string> first_literal;
  std::string wrapper;
  const void* id = nullptr;
};
using DuckLakeCallCallback = std::function<bool(const TableFunctionCall&)>;
std::optional<std::string> GatedCategory(
    duckdb::SQLStatement& statement, const std::function<bool(const std::string&)>& permitted,
    const NamedTableCallback& named_table = nullptr,
    const TableFunctionCallback& table_function_allowed = nullptr,
    const StorageCallback& storage = nullptr,
    const PathParameterCallback& parameters = nullptr,
    const DuckLakeCallCallback& ducklake_call = nullptr,
    const BindTimeCallbacks& bind_time = {});

}  // namespace hardcharger::authz
