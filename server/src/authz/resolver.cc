#include "authz/resolver.hpp"

#include <duckdb.hpp>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/schema_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/view_catalog_entry.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/planner/binder.hpp>

#include <openssl/evp.h>

#include "authz/duckdb_internal.hpp"
#include "authz/policies.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;

// Run `fn` inside a transaction on the session's context (catalog lookups need
// one), turning DuckDB exceptions into a Hardcharger error.
template <typename Fn>
arrow::Status InTransaction(dd::Connection& connection, Fn&& fn) {
  arrow::Status status;
  try {
    connection.context->RunFunctionInTransaction([&] { status = fn(); });
  } catch (const std::exception& e) {
    return AccessControlError(dd::ErrorData(e).Message());
  }
  return status;
}

std::vector<std::string> SchemaPathOf(dd::SchemaCatalogEntry& schema) {
  const auto names = compat::Strs(schema.GetSchemaPath());
  return std::vector<std::string>(names.begin(), names.end());
}

dd::optional_ptr<dd::Catalog> CatalogNamed(dd::ClientContext& context,
                                           const std::string& name) {
  return dd::Catalog::GetCatalogEntry(context, dd::Identifier(name));
}

}  // namespace

ObjectPath PathOfEntry(dd::CatalogEntry& entry) {
  ObjectPath path;
  path.catalog = compat::Str(entry.ParentCatalog().GetName());
  if (entry.type == dd::CatalogType::SCHEMA_ENTRY) {
    path.schema_path = SchemaPathOf(entry.Cast<dd::SchemaCatalogEntry>());
    return path;
  }
  path.schema_path = SchemaPathOf(entry.ParentSchema());
  path.object_name = compat::Str(entry.name);
  return path;
}

arrow::Result<ResolvedObject> SessionObjectResolver::ResolveObject(
    const QualifiedName& name) {
  ResolvedObject resolved;
  bool found = false;
  ARROW_RETURN_NOT_OK(InTransaction(connection_, [&]() -> arrow::Status {
    auto& context = *connection_.context;
    dd::optional_ptr<dd::CatalogEntry> entry;
    dd::vector<dd::Identifier> path;
    for (const auto& part : name.parts) path.emplace_back(part);
    entry = dd::Catalog::GetEntry(context,
                                  dd::EntryLookupInfo(dd::CatalogType::TABLE_ENTRY,
                                                      dd::QualifiedName::FromPath(path)),
                                  dd::OnEntryNotFound::RETURN_NULL);
    if (!entry) return arrow::Status::OK();
    if (entry->type != dd::CatalogType::TABLE_ENTRY &&
        entry->type != dd::CatalogType::VIEW_ENTRY) {
      return AccessControlError(name.ToString() + " is not a table or view");
    }
    found = true;
    resolved.path.catalog = compat::Str(entry->ParentCatalog().GetName());
    resolved.path.schema_path = SchemaPathOf(entry->ParentSchema());
    resolved.path.object_name = compat::Str(entry->name);
    if (entry->type == dd::CatalogType::VIEW_ENTRY) {
      resolved.type = ObjectType::kView;
      auto& view = entry->Cast<dd::ViewCatalogEntry>();
      view.BindView(context);  // DuckDB 1.4 keeps a view's names from when it was made
      resolved.columns = ViewColumnNames(view);
      resolved.definition_hash = ViewDefinitionHash(view.GetQuery());
    } else {
      resolved.type = ObjectType::kTable;
      auto& table = entry->Cast<dd::TableCatalogEntry>();
      for (const auto& n : table.GetColumns().GetColumnNames())
        resolved.columns.push_back(n);
    }
    return arrow::Status::OK();
  }));
  if (!found) {
    return AccessControlError("table or view " + name.ToString() + " does not exist");
  }
  return resolved;
}

arrow::Result<ObjectPath> SessionObjectResolver::ResolveSchema(
    const QualifiedName& name) {
  ObjectPath resolved;
  bool found = false;
  ARROW_RETURN_NOT_OK(InTransaction(connection_, [&]() -> arrow::Status {
    auto& context = *connection_.context;
    // The first part is a catalog when one by that name is attached. An
    // unqualified schema is the session's current database's, never the
    // session-local temp catalog that heads the search path.
    std::string catalog;
    std::vector<std::string> schema_path = name.parts;
    if (name.parts.size() > 1 && CatalogNamed(context, name.parts[0])) {
      catalog = name.parts[0];
      schema_path.erase(schema_path.begin());
    } else {
      catalog = compat::Str(dd::DatabaseManager::GetDefaultDatabase(context));
    }
    dd::optional_ptr<dd::SchemaCatalogEntry> schema;
    dd::vector<dd::Identifier> path;
    for (const auto& part : schema_path) path.emplace_back(part);
    // An unqualified schema lives in the session's default catalog; DuckDB 2.x
    // spells "no catalog given" as InvalidCatalog(), not an empty identifier.
    schema = dd::Catalog::GetSchema(context, dd::Identifier(catalog), path,
                                    dd::OnEntryNotFound::RETURN_NULL);
    if (!schema) return arrow::Status::OK();
    found = true;
    resolved.catalog = compat::Str(schema->ParentCatalog().GetName());
    resolved.schema_path = SchemaPathOf(*schema);
    return arrow::Status::OK();
  }));
  if (!found) return AccessControlError("schema " + name.ToString() + " does not exist");
  return resolved;
}

arrow::Result<std::string> SessionObjectResolver::ResolveCatalog(
    const std::string& name) {
  std::string resolved;
  ARROW_RETURN_NOT_OK(InTransaction(connection_, [&]() -> arrow::Status {
    auto catalog = CatalogNamed(*connection_.context, name);
    if (catalog) resolved = compat::Str(catalog->GetName());
    return arrow::Status::OK();
  }));
  if (resolved.empty()) return AccessControlError("catalog " + name + " does not exist");
  return resolved;
}

namespace {

// A sample session for trying policy SQL: no roles, no attributes.
PolicyContext TrialContext() {
  PolicyContext context;
  context.user_name = "hardcharger_policy_check";
  return context;
}

// Prepare `sql` on the session's connection; the error, if any, as a refusal.
arrow::Status TryPrepare(dd::Connection& connection, const std::string& sql,
                         const std::string& what) {
  auto prepared = connection.Prepare(sql);
  if (prepared->HasError()) {
    return arrow::Status::Invalid("Hardcharger access control: " + what + ": " +
                                  prepared->GetError());
  }
  return arrow::Status::OK();
}

arrow::Result<dd::unique_ptr<dd::ParsedExpression>> ParseSql(const std::string& sql) {
  try {
    auto parser = compat::StandaloneParser();
    auto list = parser.ParseExpressionList(sql);
    if (list.size() == 1) return std::move(list[0]);
  } catch (const std::exception& e) {
    return arrow::Status::Invalid("Hardcharger access control: " + std::string(e.what()));
  }
  return arrow::Status::Invalid("Hardcharger access control: not one expression: " + sql);
}

}  // namespace

arrow::Status SessionObjectResolver::CheckRowPredicate(const ResolvedObject& table,
                                                       const std::string& predicate) {
  ARROW_ASSIGN_OR_RAISE(auto parsed, PreparePolicyExpression(predicate, TrialContext()));
  return TryPrepare(connection_,
                    "SELECT count(*) FROM " + table.path.ToString() + " WHERE (" +
                        compat::Str(parsed->ToString()) + ")",
                    "the policy does not fit " + table.path.ToString());
}

arrow::Status SessionObjectResolver::CheckMaskingPolicy(
    const MaskingPolicyRecord& policy) {
  ARROW_ASSIGN_OR_RAISE(auto value,
                        ParseSql("CAST(NULL AS " + policy.parameter_type + ")"));
  ARROW_ASSIGN_OR_RAISE(
      auto parsed, PreparePolicyExpression(policy.body, TrialContext(), policy.parameter,
                                           value.get()));
  return TryPrepare(connection_,
                    "SELECT CAST((" + compat::Str(parsed->ToString()) + ") AS " +
                        policy.return_type + ")",
                    "the masking policy " + policy.name + " is not valid");
}

arrow::Status SessionObjectResolver::CheckMaskBinding(const ResolvedObject& table,
                                                      const std::string& column,
                                                      const MaskingPolicyRecord& policy) {
  const std::string quoted = "\"" + column + "\"";
  auto probe = connection_.Prepare("SELECT " + quoted + " FROM " + table.path.ToString() +
                                   " LIMIT 0");
  if (probe->HasError()) {
    return arrow::Status::Invalid("Hardcharger access control: " + probe->GetError());
  }
  const std::string column_type = probe->GetTypes()[0].ToString();
  ARROW_ASSIGN_OR_RAISE(
      auto value, ParseSql("CAST(" + quoted + " AS " + policy.parameter_type + ")"));
  ARROW_ASSIGN_OR_RAISE(
      auto parsed, PreparePolicyExpression(policy.body, TrialContext(), policy.parameter,
                                           value.get()));
  return TryPrepare(connection_,
                    "SELECT CAST(CAST((" + compat::Str(parsed->ToString()) + ") AS " +
                        policy.return_type + ") AS " + column_type + ") FROM " +
                        table.path.ToString() + " LIMIT 0",
                    "masking policy " + policy.name + " does not fit " +
                        table.path.ToString() + "." + column);
}

std::string ViewDefinitionHash(const dd::SelectStatement& query) {
  const std::string text = compat::Str(query.ToString());
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int length = 0;
  if (EVP_Digest(text.data(), text.size(), digest, &length, EVP_sha256(), nullptr) != 1) {
    return "";  // no hash: the view reads with the caller's rights
  }
  static const char* kHex = "0123456789abcdef";
  std::string hex;
  for (unsigned int i = 0; i < length; ++i) {
    hex.push_back(kHex[digest[i] >> 4]);
    hex.push_back(kHex[digest[i] & 0xf]);
  }
  return hex;
}

std::vector<std::string> ViewColumnNames(const dd::ViewCatalogEntry& view) {
  std::vector<std::string> names;
  auto info = view.GetColumnInfo();
  if (!info) return names;
  const auto& bound = info->names;
  for (size_t i = 0; i < bound.size(); ++i) {
    names.push_back(i < view.aliases.size() ? compat::Str(view.aliases[i])
                                            : compat::Str(bound[i]));
  }
  return names;
}

}  // namespace hardcharger::authz
