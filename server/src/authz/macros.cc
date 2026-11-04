#include "authz/macros.hpp"

#include "authz/resolver.hpp"

#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/macro_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/schema_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/trigger_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/type_catalog_entry.hpp>
#include <duckdb/parser/constraints/check_constraint.hpp>
#include <duckdb/parser/expression/cast_expression.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/type_expression.hpp>
#include <duckdb/catalog/catalog_entry/view_catalog_entry.hpp>
#include <duckdb/function/scalar_macro_function.hpp>
#include <duckdb/function/table_macro_function.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/main/extension_entries.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/expression/subquery_expression.hpp>
#include <duckdb/parser/parsed_expression_iterator.hpp>
#include <duckdb/parser/query_node/cte_node.hpp>
#include <duckdb/parser/query_node/recursive_cte_node.hpp>
#include <duckdb/parser/query_node/set_operation_node.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/parser/tableref/subqueryref.hpp>
#include <duckdb/parser/tableref/table_function_ref.hpp>
#include <functional>
#include <vector>

#include "authz/duckdb_internal.hpp"
#include "authz/model.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;

// A function a view, policy or macro calls: its name as written (one to three parts),
// and the catalog of the object that calls it (empty: any catalog may be meant).
struct Reference {
  std::vector<std::string> parts;
  std::string from_catalog;
};

// Every function a parsed body calls, by name: scalar and table functions and macros
// alike, in subqueries, CTEs, set operations and joins too.
class ReferenceCollector {
 public:
  explicit ReferenceCollector(std::string from_catalog) : from_catalog_(std::move(from_catalog)) {}

  std::vector<Reference> references;
  // The sequences it names in nextval('s') / currval('s') / setval('s', n).
  std::vector<Reference> sequences;
  // The types it names: CAST targets, nested types included (resolved by name where the
  // body is bound, so a type of that name earlier in the search path would be taken).
  std::vector<Reference> types;

  // A trigger's body, a DML query node (INSERT ... SELECT, UPDATE ... WHERE, ...).
  void TriggerAction(const dd::TriggerCatalogEntry& trigger) {
    if (!trigger.trigger_action) return;
    auto copy = trigger.trigger_action->Copy();
    Node(*copy);
  }

  // A type, as parsed: a name DuckDB's parser doesn't know is an UNBOUND type holding the
  // type expression it will look up when the body is bound.
  void Type(const dd::LogicalType& type) {
    if (++depth_ > 200) {
      --depth_;
      return;
    }
    switch (type.id()) {
      case dd::LogicalTypeId::UNBOUND:
        if (type.HasParameters()) {
          const auto& expression = dd::UnboundType::GetTypeExpression(type);
          if (expression) TypeExpression(*expression);
        }
        break;
      case dd::LogicalTypeId::LIST:
        Type(dd::ListType::GetChildType(type));
        break;
      case dd::LogicalTypeId::ARRAY:
        Type(dd::ArrayType::GetChildType(type));
        break;
      case dd::LogicalTypeId::MAP:
        Type(dd::MapType::KeyType(type));
        Type(dd::MapType::ValueType(type));
        break;
      case dd::LogicalTypeId::STRUCT:
        for (const auto& child : dd::StructType::GetChildTypes(type)) Type(child.second);
        break;
      case dd::LogicalTypeId::UNION:
        for (dd::idx_t i = 0; i < dd::UnionType::GetMemberCount(type); ++i) Type(dd::UnionType::GetMemberType(type, i));
        break;
      default:
        break;
    }
    --depth_;
  }

  void TypeExpression(const dd::ParsedExpression& expression) {
    if (expression.GetExpressionClass() != dd::ExpressionClass::TYPE) return;
    auto& type = expression.Cast<dd::TypeExpression>();
    Reference reference{{}, from_catalog_};
    for (const auto& part : {type.GetCatalog(), type.GetSchema(), type.GetTypeName()}) {
      const std::string name = compat::Str(part);
      if (!name.empty()) reference.parts.push_back(name);
    }
    if (!reference.parts.empty()) types.push_back(std::move(reference));
    for (const auto& child : type.GetChildren()) {
      if (child) TypeExpression(*child);
    }
  }

  void Node(dd::QueryNode& node) {
    if (++depth_ > 200) return;  // deeper than any SQL DuckDB parses
    switch (node.type) {
      case dd::QueryNodeType::SET_OPERATION_NODE:
        for (auto& child : node.Cast<dd::SetOperationNode>().children) {
          if (child) Node(*child);
        }
        break;
      case dd::QueryNodeType::RECURSIVE_CTE_NODE: {
        auto& cte = node.Cast<dd::RecursiveCTENode>();
        if (cte.left) Node(*cte.left);
        if (cte.right) Node(*cte.right);
        break;
      }
      default:
        break;
    }
    for (auto& entry : node.cte_map.map) {
      if (entry.second && entry.second->query_node) Node(*entry.second->query_node);
    }
    dd::ParsedExpressionIterator::EnumerateQueryNodeChildren(
        node,
        [this](dd::unique_ptr<dd::ParsedExpression>& child) {
          if (child) Expression(*child);
        },
        [this](dd::TableRef& ref) { Ref(ref); });
    --depth_;
  }

  void Expression(dd::ParsedExpression& expression) {
    if (++depth_ > 200) return;
    if (expression.GetExpressionClass() == dd::ExpressionClass::CAST) {
      if (const auto& target = expression.Cast<dd::CastExpression>().GetTargetType()) TypeExpression(*target);
    } else if (expression.GetExpressionClass() == dd::ExpressionClass::TYPE) {
      TypeExpression(expression);
    }
    if (expression.GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
      auto& function = expression.Cast<dd::FunctionExpression>();
      const auto path = compat::Strs(function.GetQualifiedName().Path());
      Reference reference{{path.begin(), path.end()}, from_catalog_};
      while (!reference.parts.empty() && reference.parts.front().empty()) {
        reference.parts.erase(reference.parts.begin());
      }
      const std::string name = reference.parts.empty() ? "" : FoldIdentifier(reference.parts.back());
      const dd::ParsedExpression* first = nullptr;
      for (const auto& argument : function.GetArguments()) {
        if (!argument.HasName()) {
          first = &argument.GetExpression();
          break;
        }
      }
      if ((name == "nextval" || name == "currval" || name == "setval") && first &&
          first->GetExpressionClass() == dd::ExpressionClass::CONSTANT) {
        const auto value = compat::ConstantValue(first->Cast<dd::ConstantExpression>());
        if (!value.IsNull()) {
          try {
            Reference sequence{{}, from_catalog_};
            for (const auto& part : dd::QualifiedName::ParseComponents(value.ToString())) {
              sequence.parts.push_back(compat::Str(part));
            }
            if (!sequence.parts.empty()) sequences.push_back(std::move(sequence));
          } catch (...) {
            // a name DuckDB can't read names no sequence
          }
        }
      }
      if (!reference.parts.empty()) references.push_back(std::move(reference));
    }
    if (expression.GetExpressionClass() == dd::ExpressionClass::SUBQUERY) {
      auto& subquery = expression.Cast<dd::SubqueryExpression>().SubqueryMutable();
      if (subquery && subquery->node) Node(*subquery->node);
    }
    dd::ParsedExpressionIterator::EnumerateChildren(expression,
                                                    [this](dd::unique_ptr<dd::ParsedExpression>& child) {
                                                      if (child) Expression(*child);
                                                    });
    --depth_;
  }

 private:
  void Ref(dd::TableRef& ref) {
    if (ref.type == dd::TableReferenceType::TABLE_FUNCTION) {
      auto& function = ref.Cast<dd::TableFunctionRef>();
      if (function.function) Expression(*function.function);
      if (function.subquery && function.subquery->node) Node(*function.subquery->node);
    } else if (ref.type == dd::TableReferenceType::SUBQUERY) {
      auto& subquery = ref.Cast<dd::SubqueryRef>();
      if (subquery.subquery && subquery.subquery->node) Node(*subquery.subquery->node);
    }
  }

  std::string from_catalog_;
  int depth_ = 0;
};

void CollectMacro(const dd::MacroFunction& macro, ReferenceCollector& collector) {
  auto copy = macro.Copy();
  for (auto& parameter : copy->default_parameters) {
    if (parameter.second) collector.Expression(*parameter.second);
  }
  if (copy->type == dd::MacroType::SCALAR_MACRO) {
    auto& scalar = copy->Cast<dd::ScalarMacroFunction>();
    if (scalar.expression) collector.Expression(*scalar.expression);
  } else if (copy->type == dd::MacroType::TABLE_MACRO) {
    auto& table = copy->Cast<dd::TableMacroFunction>();
    if (table.query_node) collector.Node(*table.query_node);
  }
}

void CollectExpressionText(const std::string& sql, ReferenceCollector& collector) {
  dd::vector<dd::unique_ptr<dd::ParsedExpression>> list;
  try {
    auto parser = compat::StandaloneParser();
    list = parser.ParseExpressionList(sql);
  } catch (...) {
    return;  // a policy that doesn't parse calls nothing (and is refused where it is used)
  }
  for (auto& expression : list) {
    if (expression) collector.Expression(*expression);
  }
}

bool SameParts(const std::vector<std::string>& a, const std::vector<std::string>& b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i) {
    if (!IdentifiersEqual(a[i], b[i])) return false;
  }
  return true;
}

// Could `reference`, written in an object of catalog `from_catalog`, mean the macro at
// `macro`? DuckDB reads a two-part name as schema.name, or as catalog.name (its default
// schema), so either counts; an unqualified name counts in the caller's catalog (a view
// looks in its own schema, then its catalog's main). A nested schema (DuckDB 2.0) is
// reached only by its whole path, `sales.payroll.m` or `cat.sales.payroll.m`.
bool CouldMean(const Reference& reference, const ObjectPath& macro) {
  const auto& parts = reference.parts;
  if (parts.empty() || !IdentifiersEqual(parts.back(), macro.object_name)) return false;
  const bool in_catalog = reference.from_catalog.empty() || IdentifiersEqual(reference.from_catalog, macro.catalog);
  const std::vector<std::string> qualifier(parts.begin(), parts.end() - 1);
  if (qualifier.empty()) return in_catalog;
  if (in_catalog && SameParts(qualifier, macro.schema_path)) return true;
  if (qualifier.size() == 1 && IdentifiersEqual(qualifier[0], macro.catalog)) return true;
  std::vector<std::string> qualified{macro.catalog};
  qualified.insert(qualified.end(), macro.schema_path.begin(), macro.schema_path.end());
  return SameParts(qualifier, qualified);
}

struct MacroEntry {
  ObjectPath path;
  std::vector<Reference> references;
};

// The catalogs whose views and macros are DuckDB SQL: DuckDB's own and DuckLake. (An
// attached PostgreSQL, MySQL or SQLite database's views are in its own dialect and can't
// call a DuckDB macro.)
bool HoldsDuckDbSql(dd::Catalog& catalog) {
  if (catalog.IsDuckCatalog()) return true;
  return compat::Str(catalog.GetCatalogType()) == "ducklake";
}

// Every trigger of the schema's tables: triggers hang off their tables, not the schema.
// The tables first, then each one's triggers, outside the schema scan's callback (see
// SequenceNameRefusal on lock order).
void ForEachTrigger(dd::ClientContext& context, dd::SchemaCatalogEntry& schema,
                    const std::function<void(const dd::TriggerCatalogEntry&)>& fn) {
  std::vector<dd::reference<dd::TableCatalogEntry>> tables;
  schema.Scan(context, dd::CatalogType::TABLE_ENTRY, [&](dd::CatalogEntry& entry) {
    if (entry.type == dd::CatalogType::TABLE_ENTRY) tables.push_back(entry.Cast<dd::TableCatalogEntry>());
  });
  for (auto& table : tables) {
    auto txn = table.get().ParentCatalog().GetCatalogTransaction(context);
    table.get().ScanTriggers(txn, [&](dd::CatalogEntry& trigger) {
      if (trigger.type == dd::CatalogType::TRIGGER_ENTRY) fn(trigger.Cast<dd::TriggerCatalogEntry>());
    });
  }
}

}  // namespace

std::optional<std::string> SequenceNameRefusal(dd::ClientContext& context, const ObjectPath& path) {
  std::vector<Reference> named;
  for (const auto& database : dd::DatabaseManager::Get(context).GetDatabases(context)) {
    if (!database || database->IsSystem() || database->IsTemporary()) continue;
    auto& catalog = database->GetCatalog();
    if (!HoldsDuckDbSql(catalog)) continue;
    const std::string catalog_name = compat::Str(database->GetName());
    try {
      // The schemas first, then each one's entries: scanning a schema inside ScanSchemas' callback
      // starts its catalog transaction while the schema set's lock is held, the reverse of the
      // order DuckDB takes those locks in elsewhere (ThreadSanitizer: lock-order inversion, a
      // possible deadlock between two sessions; v2.5.0's gate).
      for (auto& found : catalog.GetSchemas(context)) {
        auto& schema = found.get();
        ReferenceCollector collector(catalog_name);
        schema.Scan(context, dd::CatalogType::TABLE_ENTRY, [&](dd::CatalogEntry& entry) {
          if (entry.type == dd::CatalogType::TABLE_ENTRY) {
            for (const auto& column : entry.Cast<dd::TableCatalogEntry>().GetColumns().Logical()) {
              if (!column.HasDefaultValue()) continue;
              auto copy = column.DefaultValue().Copy();
              collector.Expression(*copy);
            }
          } else if (entry.type == dd::CatalogType::VIEW_ENTRY) {
            try {
              auto query = entry.Cast<dd::ViewCatalogEntry>().GetQuery().Copy();
              auto& select = query->Cast<dd::SelectStatement>();
              if (select.node) collector.Node(*select.node);
            } catch (...) {
            }
          }
        });
        for (const auto type : {dd::CatalogType::MACRO_ENTRY, dd::CatalogType::TABLE_MACRO_ENTRY}) {
          schema.Scan(context, type, [&](dd::CatalogEntry& entry) {
            if (entry.type != dd::CatalogType::MACRO_ENTRY && entry.type != dd::CatalogType::TABLE_MACRO_ENTRY) return;
            for (const auto& function : entry.Cast<dd::MacroCatalogEntry>().macros) {
              if (function) CollectMacro(*function, collector);
            }
          });
        }
        ForEachTrigger(context, schema, [&](const dd::TriggerCatalogEntry& trigger) { collector.TriggerAction(trigger); });
        named.insert(named.end(), collector.sequences.begin(), collector.sequences.end());
      }
    } catch (...) {
      return "a sequence named " + QuoteIdentifierIfNeeded(path.object_name) +
             " could not be checked against the tables, views and macros of every catalog; ask an administrator";
    }
  }
  for (const auto& reference : named) {
    if (CouldMean(reference, path)) {
      return "a sequence named " + QuoteIdentifierIfNeeded(path.object_name) +
             " would change the numbers a table default, a view, a macro or a trigger takes: it names a sequence by "
             "that name. Only the administrator may create or replace it";
    }
  }
  return std::nullopt;
}

std::optional<std::string> MacroNameRefusal(dd::ClientContext& context, const Snapshot& snapshot,
                                            const ObjectPath& path) {
  const std::string name = path.object_name;
  const std::string what = "a macro named " + QuoteIdentifierIfNeeded(name);

  // A built-in function (DuckDB's own, and those of every loaded extension).
  for (const auto type : {dd::CatalogType::SCALAR_FUNCTION_ENTRY, dd::CatalogType::AGGREGATE_FUNCTION_ENTRY,
                          dd::CatalogType::TABLE_FUNCTION_ENTRY, dd::CatalogType::MACRO_ENTRY,
                          dd::CatalogType::TABLE_MACRO_ENTRY, dd::CatalogType::PRAGMA_FUNCTION_ENTRY}) {
    bool found = false;
    try {
      dd::vector<dd::Identifier> lookup{dd::Identifier("system"), dd::Identifier(DEFAULT_SCHEMA), dd::Identifier(name)};
      found = dd::Catalog::GetEntry(context, dd::EntryLookupInfo(type, dd::QualifiedName::FromPath(lookup)),
                                    dd::OnEntryNotFound::RETURN_NULL) != nullptr;
    } catch (...) {
      found = false;
    }
    if (found) {
      return what + " would replace the built-in function " + name +
             " wherever it is called, in queries and views alike; give the macro a name of its own";
    }
  }
  // A function an extension would autoload when it is first called.
  for (const auto& entry : dd::EXTENSION_FUNCTIONS) {
    if (IdentifiersEqual(entry.name, name)) {
      return what + " would replace the function " + name + " of the " + std::string(entry.extension) +
             " extension wherever it is called; give the macro a name of its own";
    }
  }

  // What views and policies call, and, through the macros they call, what those call.
  std::vector<Reference> pinned;
  std::vector<MacroEntry> macros;
  for (const auto& database : dd::DatabaseManager::Get(context).GetDatabases(context)) {
    if (!database || database->IsSystem() || database->IsTemporary()) continue;
    auto& catalog = database->GetCatalog();
    if (!HoldsDuckDbSql(catalog)) continue;
    const std::string catalog_name = compat::Str(database->GetName());
    try {
      // The schemas first, then each one's entries (see SequenceNameRefusal).
      for (auto& found : catalog.GetSchemas(context)) {
        auto& schema = found.get();
        schema.Scan(context, dd::CatalogType::VIEW_ENTRY, [&](dd::CatalogEntry& entry) {
          if (entry.type != dd::CatalogType::VIEW_ENTRY) return;
          try {
            auto query = entry.Cast<dd::ViewCatalogEntry>().GetQuery().Copy();
            ReferenceCollector collector(catalog_name);
            auto& select = query->Cast<dd::SelectStatement>();
            if (select.node) collector.Node(*select.node);
            pinned.insert(pinned.end(), collector.references.begin(), collector.references.end());
          } catch (...) {
            // a view whose query can't be read calls nothing we can name
          }
        });
        // A trigger's body is bound again each time the trigger fires, with its owner's
        // rights: what it calls is pinned like what a view calls.
        ForEachTrigger(context, schema, [&](const dd::TriggerCatalogEntry& trigger) {
          ReferenceCollector collector(catalog_name);
          collector.TriggerAction(trigger);
          pinned.insert(pinned.end(), collector.references.begin(), collector.references.end());
        });
        for (const auto type : {dd::CatalogType::MACRO_ENTRY, dd::CatalogType::TABLE_MACRO_ENTRY}) {
          schema.Scan(context, type, [&](dd::CatalogEntry& entry) {
            if (entry.type != dd::CatalogType::MACRO_ENTRY && entry.type != dd::CatalogType::TABLE_MACRO_ENTRY) return;
            MacroEntry macro;
            macro.path = PathOfEntry(entry);  // the schema's whole path: nested schemas
            ReferenceCollector collector(catalog_name);
            for (const auto& function : entry.Cast<dd::MacroCatalogEntry>().macros) {
              if (function) CollectMacro(*function, collector);
            }
            macro.references = std::move(collector.references);
            macros.push_back(std::move(macro));
          });
        }
      }
    } catch (...) {
      return what + " could not be checked against the views and macros of every catalog; ask an administrator";
    }
  }
  for (const auto& policy : snapshot.row_policies()) {
    ReferenceCollector collector("");  // resolved where the table is read: any catalog
    CollectExpressionText(policy.predicate, collector);
    pinned.insert(pinned.end(), collector.references.begin(), collector.references.end());
  }
  for (const auto& policy : snapshot.masking_policies()) {
    ReferenceCollector collector("");
    CollectExpressionText(policy.body, collector);
    pinned.insert(pinned.end(), collector.references.begin(), collector.references.end());
  }
  // A macro is pinned when something pinned calls it: what it calls is pinned too.
  std::vector<bool> reached(macros.size(), false);
  for (bool grew = true; grew;) {
    grew = false;
    for (size_t i = 0; i < macros.size(); ++i) {
      if (reached[i]) continue;
      for (const auto& reference : pinned) {
        if (CouldMean(reference, macros[i].path)) {
          reached[i] = true;
          grew = true;
          break;
        }
      }
      if (reached[i]) pinned.insert(pinned.end(), macros[i].references.begin(), macros[i].references.end());
    }
  }
  for (const auto& reference : pinned) {
    if (CouldMean(reference, path)) {
      return what + " would change what a view, a trigger, a row or masking policy, or a macro one of them uses "
             "computes: it calls a function by that name. Only the administrator may create or replace it";
    }
  }
  return std::nullopt;
}

// Every type name the catalogs' re-bound SQL uses: views' queries, macros' bodies, triggers'
// bodies, tables' column defaults and CHECK constraints (bound when a row is written), and
// the store's policies. A table's columns are not among them: DuckDB copies a type into the
// column at CREATE TABLE, so dropping or replacing the type changes nothing there.
std::optional<std::vector<Reference>> TypeReferences(dd::ClientContext& context, const Snapshot& snapshot,
                                                     std::string& error) {
  std::vector<Reference> named;
  for (const auto& database : dd::DatabaseManager::Get(context).GetDatabases(context)) {
    if (!database || database->IsSystem() || database->IsTemporary()) continue;
    auto& catalog = database->GetCatalog();
    if (!HoldsDuckDbSql(catalog)) continue;
    const std::string catalog_name = compat::Str(database->GetName());
    try {
      // The schemas first, then each one's entries (see SequenceNameRefusal).
      for (auto& found : catalog.GetSchemas(context)) {
        auto& schema = found.get();
        ReferenceCollector collector(catalog_name);
        schema.Scan(context, dd::CatalogType::TABLE_ENTRY, [&](dd::CatalogEntry& entry) {
          if (entry.type == dd::CatalogType::TABLE_ENTRY) {
            auto& table = entry.Cast<dd::TableCatalogEntry>();
            for (const auto& column : table.GetColumns().Logical()) {
              if (!column.HasDefaultValue()) continue;
              auto copy = column.DefaultValue().Copy();
              collector.Expression(*copy);
            }
            for (const auto& constraint : table.GetConstraints()) {
              if (!constraint || constraint->type != dd::ConstraintType::CHECK) continue;
              auto& check = constraint->Cast<dd::CheckConstraint>();
              if (!check.expression) continue;
              auto copy = check.expression->Copy();
              collector.Expression(*copy);
            }
          } else if (entry.type == dd::CatalogType::VIEW_ENTRY) {
            try {
              auto query = entry.Cast<dd::ViewCatalogEntry>().GetQuery().Copy();
              auto& select = query->Cast<dd::SelectStatement>();
              if (select.node) collector.Node(*select.node);
            } catch (...) {
            }
          }
        });
        for (const auto type : {dd::CatalogType::MACRO_ENTRY, dd::CatalogType::TABLE_MACRO_ENTRY}) {
          schema.Scan(context, type, [&](dd::CatalogEntry& entry) {
            if (entry.type != dd::CatalogType::MACRO_ENTRY && entry.type != dd::CatalogType::TABLE_MACRO_ENTRY) return;
            for (const auto& function : entry.Cast<dd::MacroCatalogEntry>().macros) {
              if (function) CollectMacro(*function, collector);
            }
          });
        }
        ForEachTrigger(context, schema, [&](const dd::TriggerCatalogEntry& trigger) { collector.TriggerAction(trigger); });
        named.insert(named.end(), collector.types.begin(), collector.types.end());
      }
    } catch (const std::exception& e) {
      error = e.what();
      return std::nullopt;
    } catch (...) {
      return std::nullopt;
    }
  }
  for (const auto& policy : snapshot.row_policies()) {
    ReferenceCollector collector("");  // resolved where the table is read: any catalog
    CollectExpressionText(policy.predicate, collector);
    named.insert(named.end(), collector.types.begin(), collector.types.end());
  }
  for (const auto& policy : snapshot.masking_policies()) {
    ReferenceCollector collector("");
    CollectExpressionText(policy.body, collector);
    named.insert(named.end(), collector.types.begin(), collector.types.end());
  }
  return named;
}

std::optional<std::string> TypeNameRefusal(dd::ClientContext& context, const Snapshot& snapshot,
                                           const ObjectPath& path, const std::string& verb) {
  const std::string name = path.object_name;
  const std::string what = "a type named " + QuoteIdentifierIfNeeded(name);
  const bool creating = verb == "create";

  if (creating) {
    // A built-in type (DuckDB's own and every loaded extension's): a type of that name in
    // the search path is found first, in every query and view.
    bool found = false;
    try {
      dd::vector<dd::Identifier> lookup{dd::Identifier("system"), dd::Identifier(DEFAULT_SCHEMA), dd::Identifier(name)};
      found = dd::Catalog::GetEntry(context, dd::EntryLookupInfo(dd::CatalogType::TYPE_ENTRY, dd::QualifiedName::FromPath(lookup)),
                                    dd::OnEntryNotFound::RETURN_NULL) != nullptr;
    } catch (...) {
      found = false;
    }
    if (!found) {
      try {
        const auto parsed = dd::UnboundType::TryParseAndDefaultBind(name);
        found = parsed.id() != dd::LogicalTypeId::INVALID && parsed.id() != dd::LogicalTypeId::UNBOUND;
      } catch (...) {
        found = false;
      }
    }
    if (found) {
      return what + " would replace the built-in type " + name +
             " wherever it is named, in queries and views alike; give the type a name of its own";
    }
    // A type an extension provides when it is loaded (json, inet).
    for (const auto& entry : dd::EXTENSION_TYPES) {
      if (IdentifiersEqual(entry.name, name)) {
        return what + " would replace the type " + name + " of the " + std::string(entry.extension) +
               " extension wherever it is named; give the type a name of its own";
      }
    }
  }

  std::string error;
  const auto named = TypeReferences(context, snapshot, error);
  if (!named) {
    return what + " could not be checked against the views, macros, triggers and tables of every catalog" +
           (error.empty() ? std::string() : " (" + error + ")") + "; ask an administrator";
  }
  for (const auto& reference : *named) {
    if (!CouldMean(reference, path)) continue;
    if (creating) {
      return what + " would change what a view, a macro, a trigger, a row or masking policy, or a column default or "
                    "check constraint computes: it names a type by that name. Only the administrator may create it";
    }
    return "type " + path.ToString() + " is used by a view, a macro, a trigger, a row or masking policy, or a column "
           "default or check constraint. Only the administrator may " + verb + " it";
  }
  return std::nullopt;
}

}  // namespace hardcharger::authz
