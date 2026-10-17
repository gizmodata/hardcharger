// DuckDB side of access control: resolving the names in GRANT / REVOKE the way
// the caller's session resolves them in a query.

#pragma once

#include "authz/engine.hpp"

namespace duckdb {
class Connection;
class CatalogEntry;
class SelectStatement;
class ViewCatalogEntry;
}  // namespace duckdb

namespace hardcharger::authz {

// Resolves names through DuckDB's own catalog lookup on one session's
// connection: its search path, its default catalog, and DuckDB's rule for
// whether the first part of a dotted name is a catalog.
class SessionObjectResolver : public ObjectResolver {
 public:
  explicit SessionObjectResolver(duckdb::Connection& connection) : connection_(connection) {}
  arrow::Result<ResolvedObject> ResolveObject(const QualifiedName& name) override;
  arrow::Result<ObjectPath> ResolveSchema(const QualifiedName& name) override;
  arrow::Result<std::string> ResolveCatalog(const std::string& name) override;
  arrow::Status CheckRowPredicate(const ResolvedObject& table,
                                  const std::string& predicate) override;
  arrow::Status CheckMaskingPolicy(const MaskingPolicyRecord& policy) override;
  arrow::Status CheckMaskBinding(const ResolvedObject& table, const std::string& column,
                                 const MaskingPolicyRecord& policy) override;

  // Does `name` (as a FROM clause names it) find a table macro, as DuckDB would?
  bool IsTableMacro(const QualifiedName& name);

 private:
  duckdb::Connection& connection_;
};

// catalog, schema path and name of a catalog entry (a table, view or schema).
ObjectPath PathOfEntry(duckdb::CatalogEntry& entry);

// SHA-256 (hex) of a view's query as DuckDB prints it back: the same for the
// CREATE VIEW that made it and for the view DuckDB reads from any catalog.
std::string ViewDefinitionHash(const duckdb::SelectStatement& query);

// A bound view's output column names, as grants on the view name them (the
// view's own column aliases, else the names its query gives them). Empty when
// the view has not been bound.
std::vector<std::string> ViewColumnNames(const duckdb::ViewCatalogEntry& view);

}  // namespace hardcharger::authz
