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

namespace duckdb {
class SQLStatement;
}

namespace hardcharger::authz {

// The gated family the statement belongs to ("ATTACH", "read_csv() (files and URLs)",
// "COPY TO (files and URLs)", ...), or nullopt. `permitted(category)` says whether the
// session holds the system privilege for a category; such categories are not reported.
std::optional<std::string> GatedCategory(
    duckdb::SQLStatement& statement, const std::function<bool(const std::string&)>& permitted);

}  // namespace hardcharger::authz
