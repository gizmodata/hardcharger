#include "authz/name_scope.hpp"

#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/schema_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/catalog/entry_lookup_info.hpp>
#include <duckdb/main/client_context.hpp>

#include "authz/duckdb_internal.hpp"
#include "authz/resolver.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;

const char kSystemCatalog[] = "system";

// Does a name without a schema that its scope's schema does not have fall back to schema main
// of the scope's catalog, before DuckDB's own? DuckDB's own rule for a view and a default is
// "its schema, then main". Off: a stored expression finds a bare name in its own schema and
// among DuckDB's own only, and names a macro of main as main.f (2.14.0, as decided). Turning it
// on is this one line; the definition-time checks follow, since they ask FindUnderScope.
constexpr bool kBareNamesFallBackToMain = false;

// The entry at exactly this catalog, schema path and name; null when there is none (or no
// such schema, or catalog).
dd::optional_ptr<dd::CatalogEntry> ExactlyAt(dd::ClientContext& context, const dd::EntryLookupInfo& lookup,
                                             const std::string& catalog, const std::vector<std::string>& schema_path,
                                             const std::string& name) {
  if (catalog.empty() || schema_path.empty()) return nullptr;
  dd::vector<dd::Identifier> path;
  for (const auto& part : schema_path) path.emplace_back(part);
  try {
    // A fresh retriever, no scope: a full name needs no search path, and the hook is not asked.
    return dd::Catalog::GetEntry(
        context,
        dd::EntryLookupInfo(lookup, dd::QualifiedName::FromCatalogSchema(dd::Identifier(catalog), std::move(path),
                                                                         dd::Identifier(name))),
        dd::OnEntryNotFound::RETURN_NULL);
  } catch (const dd::CatalogException&) {
    return nullptr;  // no such schema there
  } catch (const dd::BinderException&) {
    return nullptr;  // no such catalog
  }
}

// One of DuckDB's own by this name: in the system catalog, in the schema given or, without
// one, in whichever of its schemas DuckDB looks (main, pg_catalog, an extension's).
dd::optional_ptr<dd::CatalogEntry> DuckDbsOwn(dd::ClientContext& context, const dd::EntryLookupInfo& lookup,
                                              const std::string& schema, const std::string& name) {
  try {
    // "system" with no schema: DuckDB scans the system schemas every search path ends with
    // (CatalogSearchPath::SetPathsInternal), which no session can add to or reorder.
    return dd::Catalog::GetEntry(
        context,
        dd::EntryLookupInfo(lookup, dd::QualifiedName(dd::Identifier(kSystemCatalog), dd::Identifier(schema),
                                                      dd::Identifier(name))),
        dd::OnEntryNotFound::RETURN_NULL);
  } catch (const dd::CatalogException&) {
    return nullptr;
  } catch (const dd::BinderException&) {
    return nullptr;
  }
}

dd::optional_ptr<dd::CatalogEntry> Find(dd::ClientContext& context, const dd::EntryLookupInfo& lookup,
                                        const std::string& scope_catalog, const std::vector<std::string>& scope_schema,
                                        const std::vector<std::string>& written) {
  if (written.empty()) return nullptr;
  const std::string& name = written.back();
  if (written.size() == 1) {
    // f: the scope's schema, else DuckDB's own.
    if (auto entry = ExactlyAt(context, lookup, scope_catalog, scope_schema, name)) return entry;
    if (kBareNamesFallBackToMain) {
      if (auto entry = ExactlyAt(context, lookup, scope_catalog, {DEFAULT_SCHEMA}, name)) return entry;
    }
    return DuckDbsOwn(context, lookup, "", name);
  }
  if (written.size() == 2) {
    // schema.f: that schema of the scope's catalog, else DuckDB's own of that schema.
    if (auto entry = ExactlyAt(context, lookup, scope_catalog, {written[0]}, name)) return entry;
    return DuckDbsOwn(context, lookup, written[0], name);
  }
  // [catalog, schemas..., f]; the catalog slot is empty for a nested schema path without one.
  std::string catalog = written[0];
  std::vector<std::string> schema_path(written.begin() + 1, written.end() - 1);
  while (!schema_path.empty() && schema_path.front().empty()) schema_path.erase(schema_path.begin());
  if (catalog.empty()) {
    if (auto entry = ExactlyAt(context, lookup, scope_catalog, schema_path, name)) return entry;
    return schema_path.size() == 1 ? DuckDbsOwn(context, lookup, schema_path[0], name) : nullptr;
  }
  if (IdentifiersEqual(catalog, TEMP_CATALOG)) {
    throw dd::CatalogException(
        "Hardcharger access control: %s is in the temporary catalog, which is each session's own: a view, a "
        "trigger, a policy or a table's default, CHECK constraint or generated column cannot call it",
        QuoteIdentifierIfNeeded(name));
  }
  if (schema_path.empty()) {
    // catalog.f: the catalog's default schema.
    if (IdentifiersEqual(catalog, kSystemCatalog)) return DuckDbsOwn(context, lookup, "", name);
    schema_path.push_back(DEFAULT_SCHEMA);
  }
  return ExactlyAt(context, lookup, catalog, schema_path, name);
}

std::vector<std::string> WrittenParts(const dd::QualifiedName& name) {
  std::vector<std::string> parts;
  for (const auto& part : name.Path()) parts.push_back(compat::Str(part));
  // A path of two is [schema, name]; of three or more, [catalog, schemas..., name].
  if (parts.size() == 2 && parts[0].empty()) parts.erase(parts.begin());
  return parts;
}

std::string Shown(const std::vector<std::string>& parts) {
  std::string text;
  for (const auto& part : parts) {
    if (part.empty()) continue;
    text += (text.empty() ? "" : ".") + QuoteIdentifierIfNeeded(part);
  }
  return text;
}

const char* NounOf(dd::CatalogType type) {
  switch (type) {
    case dd::CatalogType::TYPE_ENTRY:
      return "type";
    case dd::CatalogType::TABLE_FUNCTION_ENTRY:
    case dd::CatalogType::TABLE_MACRO_ENTRY:
      return "table function";
    default:
      return "function";
  }
}

// DuckDB's hook (catalog_entry_retriever.hpp): a lookup made through a retriever that has a
// scope. True when it is ours to answer.
bool ScopedLookup(dd::ClientContext& context, const dd::HardchargerNameScope& scope, const dd::EntryLookupInfo& lookup,
                  dd::OnEntryNotFound if_not_found, dd::optional_ptr<dd::CatalogEntry>& result) {
  if (!ScopedCatalogType(lookup.GetCatalogType())) return false;
  auto& catalog = scope.schema.ParentCatalog();
  // A session's own temporary table (and its view, its trigger) is the session's, names and
  // all; DuckDB's own catalog holds nothing stored by anyone.
  if (catalog.IsTemporaryCatalog() || catalog.IsSystemCatalog()) return false;
  const ObjectPath schema = PathOfEntry(scope.schema);
  const auto written = WrittenParts(lookup.GetQualifiedName());
  result = Find(context, lookup, schema.catalog, schema.schema_path, written);
  if (result || if_not_found == dd::OnEntryNotFound::RETURN_NULL) return true;
  std::string subject = scope.object;
  if (scope.kind != "policy") {
    ObjectPath object = schema;
    object.object_name = scope.object;
    subject = object.ToString();
  }
  throw dd::CatalogException(lookup.GetErrorContext(), "%s",
                             NotFoundUnderScope(NounOf(lookup.GetCatalogType()), Shown(written), written.size() > 1,
                                                scope.kind, subject, schema.ToString(),
                                                QuoteIdentifierIfNeeded(schema.catalog)));
}

}  // namespace

bool ScopedCatalogType(dd::CatalogType type) {
  switch (type) {
    case dd::CatalogType::SCALAR_FUNCTION_ENTRY:
    case dd::CatalogType::AGGREGATE_FUNCTION_ENTRY:
    case dd::CatalogType::WINDOW_FUNCTION_ENTRY:
    case dd::CatalogType::TABLE_FUNCTION_ENTRY:
    case dd::CatalogType::MACRO_ENTRY:
    case dd::CatalogType::TABLE_MACRO_ENTRY:
    case dd::CatalogType::PRAGMA_FUNCTION_ENTRY:
    case dd::CatalogType::TYPE_ENTRY:
      return true;
    default:
      return false;
  }
}

dd::optional_ptr<dd::CatalogEntry> FindUnderScope(dd::ClientContext& context, dd::CatalogType type,
                                                  const std::string& catalog,
                                                  const std::vector<std::string>& schema_path,
                                                  const std::vector<std::string>& parts) {
  if (parts.empty()) return nullptr;
  const dd::EntryLookupInfo lookup(type, dd::QualifiedName(dd::Identifier(parts.back())));
  // As DuckDB hands a written name to the hook: two parts are schema.name; more start with the
  // catalog when the first part is an attached database, else with no catalog (a nested path).
  std::vector<std::string> written = parts;
  if (written.size() >= 3 && !dd::Catalog::GetCatalogEntry(context, dd::Identifier(written[0]))) {
    written.insert(written.begin(), "");
  } else if (written.size() == 2 && dd::Catalog::GetCatalogEntry(context, dd::Identifier(written[0])) &&
             !ExactlyAt(context, lookup, catalog, {written[0]}, written[1])) {
    written.insert(written.begin() + 1, "");  // catalog.f
  }
  return Find(context, lookup, catalog, schema_path, written);
}

bool IsDuckDbsOwn(dd::CatalogEntry& entry) {
  try {
    return entry.ParentCatalog().IsSystemCatalog();
  } catch (const std::exception&) {
    return false;
  }
}

dd::shared_ptr<const dd::HardchargerNameScope> NameScopeIn(dd::SchemaCatalogEntry& schema, const std::string& kind,
                                                          const std::string& object) {
  return dd::make_shared_ptr<dd::HardchargerNameScope>(schema, kind, object);
}

dd::shared_ptr<const dd::HardchargerNameScope> PolicyNameScope(dd::ClientContext& context,
                                                              dd::TableCatalogEntry& table) {
  auto& catalog = table.ParentCatalog();
  if (catalog.IsTemporaryCatalog() || catalog.IsSystemCatalog()) return nullptr;
  auto main_schema = catalog.GetSchema(context, dd::Identifier(DEFAULT_SCHEMA), dd::OnEntryNotFound::RETURN_NULL);
  if (!main_schema) return nullptr;
  return dd::make_shared_ptr<dd::HardchargerNameScope>(*main_schema, "policy", PathOfEntry(table).ToString());
}

std::string NotFoundUnderScope(const std::string& noun, const std::string& written, bool qualified,
                               const std::string& kind, const std::string& subject, const std::string& scope_schema,
                               const std::string& scope_catalog) {
  std::string whose, remedy;
  if (kind == "view") {
    whose = "view " + subject;
    remedy = "Run CREATE OR REPLACE VIEW with the " + noun + " named with its schema (schema.name)";
  } else if (kind == "trigger") {
    whose = "a trigger on table " + subject;
    remedy = "Its owner can run CREATE OR REPLACE TRIGGER with the " + noun + " named with its schema (schema.name)";
  } else if (kind == "policy") {
    whose = "a row or masking policy on table " + subject;
    remedy = "An administrator can re-create the policy with the " + noun + " named with its schema (schema.name)";
  } else {
    whose = "a CHECK constraint, generated column, column default or index of table " + subject;
    remedy = "Re-create the table with the " + noun + " named with its schema (schema.name; a column default can be "
             "changed with ALTER TABLE, an index dropped and created again), or an administrator can create a macro "
             "of that name in " + scope_schema + " that calls the one meant";
  }
  std::string text = "Hardcharger access control: " + noun + " " + written + ", which " + whose + " " +
                     (noun == "type" ? "names" : "calls") + ", was not found. There a " + noun +
                     " is never looked up through the session's search path: ";
  if (qualified) {
    text += "a name with a schema is looked up in that schema of catalog " + scope_catalog +
            " (or as one of DuckDB's own), and a call written value." + written.substr(written.rfind('.') + 1) +
            "() is read as a method call for DuckDB's own functions only. ";
  } else {
    text += "a name without a schema is looked up in " + scope_schema + " and among DuckDB's own only" +
            (kind == "policy" ? " (a policy belongs to no schema, so its catalog's main)" : "") +
            ", also inside the macros it calls. ";
  }
  return text + remedy;
}

bool InstallNameScopeHook() {
  dd::hardcharger_scoped_lookup_hook = &ScopedLookup;
  return true;
}

}  // namespace hardcharger::authz
