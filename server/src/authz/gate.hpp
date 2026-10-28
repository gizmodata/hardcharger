// The pre-bind gate for store users' statements.
//
// The plan check runs after DuckDB has bound a statement, and binding is not free of
// side effects: binding read_csv('https://...') or SELECT * FROM 's3://...' already
// fetches the file to learn its columns, binding read_csv('/etc/x') says whether the
// file exists, and query('<sql>') binds SQL from a string. So the families of
// statements that reach outside the database are refused on the parsed statement,
// before DuckDB binds it, unless the user holds the system privilege that opens them.
// The plan check then checks them again. Anything this gate does not recognise is
// left to the plan check, which denies what it cannot classify.
#pragma once

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace duckdb {
class SQLStatement;
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
// that is not allowed is refused here.
//
// `storage`, if set, is called for each read or write of files, URLs or object storage the
// statement names (read_csv(...), FROM '<path>', COPY, EXPORT and IMPORT DATABASE), with
// the paths as written: `paths` is nullopt when one is an expression or a parameter, known
// only once DuckDB binds it. It may throw to refuse. Without it, those statements are
// reported as categories ("read_csv() (files and URLs)", ...).
struct StorageUse {
  std::string what;  // "read_csv()", "COPY ... TO", ...
  bool write = false;
  std::optional<std::vector<std::string>> paths;
  bool directory = false;  // writes (or reads) a directory of files
};
using NamedTableCallback = std::function<void(const std::vector<std::string>&, bool described)>;
using TableFunctionCallback = std::function<bool(const std::vector<std::string>&)>;
using StorageCallback = std::function<void(const StorageUse&)>;
std::optional<std::string> GatedCategory(
    duckdb::SQLStatement& statement, const std::function<bool(const std::string&)>& permitted,
    const NamedTableCallback& named_table = nullptr,
    const TableFunctionCallback& table_function_allowed = nullptr,
    const StorageCallback& storage = nullptr);

}  // namespace hardcharger::authz
