// Name scopes: where the functions, macros, table macros and types of a STORED EXPRESSION are
// found. A stored expression is SQL kept as written and bound again in the session of whoever
// reads or writes, though it runs with someone else's rights or guards data from that very
// session: a view's query, a column default, a CHECK constraint, a generated column, a
// trigger's body, a row or masking policy, and the macros those call.
//
// DuckDB finds a function by the binder's search path: the session's (USE, SET search_path,
// the current catalog, the temporary schema first), and reads x.f() as f(x), and main.f(x) as
// f(main, x) once `main` is no schema it can find. So whoever could create a macro anywhere
// could have it called in place of the one the stored expression meant: a policy made true, a
// CHECK that passes, a body that runs with its owner's rights on the caller's behalf.
//
// Under access control no session decides. DuckDB (patched: server/scripts/
// patch-duckdb-name-scope.cmake) gives the binder of each stored expression a SCOPE, one schema
// fixed by the object the expression belongs to, and asks the hook below for every function and
// type looked up under it. Child binders inherit the scope (subqueries, CTEs, table macros,
// macros calling macros). The hook finds a name ONLY as follows, the same for every session,
// administrators included:
//
//   f                 in the scope's schema, else among DuckDB's own (the system catalog:
//                     built-ins and loaded extensions)
//   schema.f          in that schema of the scope's CATALOG, else DuckDB's own of that schema
//                     (main.lower); never the session's current catalog
//   catalog.schema.f  exactly there; never the temporary catalog
//   catalog.f         the catalog's default schema
//   value.f()         (DuckDB's method call) only for DuckDB's own f
//
// Scopes:
//   a table's CHECK constraints, generated columns and column defaults   the table's schema
//   a trigger's body                                                     its table's schema
//   a view's query                                                       the view's schema
//   a row or masking policy            schema main of the protected table's catalog (policies
//                                      belong to no schema)
//   a macro called from one of those   the scope of what calls it (DuckDB substitutes the
//                                      caller's arguments into the macro's body and binds both
//                                      as one expression)
//
// A name not found that way is refused with what to write instead; it is never looked up in the
// session. Objects from before 2.14 are bound the same way. Tables, views and sequences are not
// this file's: triggers.hpp ("Names in a body").
//
// The definer's own statement (CREATE TABLE, CREATE VIEW, CREATE TRIGGER, CREATE ... POLICY) is
// bound in the definer's session, as DuckDB binds it; StoredNamesProblem (triggers.hpp) then
// refuses a definition whose names would be something else under its scope, so the definer
// hears at CREATE rather than at first use.

#pragma once

#include <duckdb.hpp>
#include <duckdb/catalog/catalog_entry_retriever.hpp>
#include <string>
#include <vector>

#include "authz/types.hpp"

namespace hardcharger::authz {

// Sets DuckDB's hook. Called once, when access control is set up; without it DuckDB binds as
// it always did.
bool InstallNameScopeHook();

// Is this a kind of catalog entry a scope decides (a function of any kind, a macro, a type)?
bool ScopedCatalogType(duckdb::CatalogType type);

// The entry a name written as `parts` (catalog and schemas, if written, then the name) is under
// the scope `catalog`.`schema_path`, or null. No method-call reading: `parts` as a path.
// Throws for a name in the temporary catalog.
duckdb::optional_ptr<duckdb::CatalogEntry> FindUnderScope(duckdb::ClientContext& context, duckdb::CatalogType type,
                                                          const std::string& catalog,
                                                          const std::vector<std::string>& schema_path,
                                                          const std::vector<std::string>& parts);

// One of DuckDB's own (in the system catalog)?
bool IsDuckDbsOwn(duckdb::CatalogEntry& entry);

// The scope of a row or masking policy on `table`: schema main of its catalog. Null when there
// is none to give (a temporary table, a catalog without a main schema).
duckdb::shared_ptr<const duckdb::HardchargerNameScope> PolicyNameScope(duckdb::ClientContext& context,
                                                                      duckdb::TableCatalogEntry& table);

// The scope of an object named `object` in `schema` ("view", "table", "trigger").
duckdb::shared_ptr<const duckdb::HardchargerNameScope> NameScopeIn(duckdb::SchemaCatalogEntry& schema,
                                                                  const std::string& kind, const std::string& object);

// What to tell someone whose stored expression names `written`, which its scope does not find.
std::string NotFoundUnderScope(const std::string& noun, const std::string& written, bool qualified,
                               const std::string& kind, const std::string& subject, const std::string& scope_schema,
                               const std::string& scope_catalog);

}  // namespace hardcharger::authz
