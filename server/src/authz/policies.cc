#include "authz/policies.hpp"

#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/common/vector_operations/variadic_executor.hpp>
#include <duckdb/function/scalar_function.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/parser/expression/columnref_expression.hpp>
#include <duckdb/parser/expression/conjunction_expression.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/parsed_expression_iterator.hpp>
#include <duckdb/parser/parser.hpp>
#include <duckdb/planner/binder.hpp>
#include <duckdb/planner/column_binding_map.hpp>
#include <duckdb/planner/expression/bound_cast_expression.hpp>
#include <duckdb/planner/expression/bound_columnref_expression.hpp>
#include <duckdb/planner/expression/bound_conjunction_expression.hpp>
#include <duckdb/planner/expression/bound_constant_expression.hpp>
#include <duckdb/planner/expression/bound_function_expression.hpp>
#include <duckdb/planner/expression/bound_operator_expression.hpp>
#include <duckdb/planner/expression_binder/where_binder.hpp>
#include <duckdb/planner/expression_iterator.hpp>
#include <duckdb/planner/logical_operator_visitor.hpp>
#include <duckdb/planner/operator/logical_delete.hpp>
#include <duckdb/planner/operator/logical_filter.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <duckdb/planner/operator/logical_insert.hpp>
#include <duckdb/planner/operator/logical_merge_into.hpp>
#include <duckdb/planner/operator/logical_update.hpp>
#include <functional>
#include <map>
#include <mutex>
#include <unordered_map>
#include <unordered_set>

#include "authz/duckdb_internal.hpp"
#include "authz/resolver.hpp"
#include "authz/views.hpp"

namespace hardcharger::authz {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;
using OpType = dd::LogicalOperatorType;

namespace {

std::string Lower(std::string s) {
  for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

[[noreturn]] void Refuse(const std::string& what) {
  throw dd::PermissionException("Hardcharger blocked this statement: " + what);
}

// ---- parse-tree helpers (DuckDB 1.x and 2.x) -------------------------------

std::string FunctionName(const dd::FunctionExpression& fe) {
  return Lower(compat::Str(fe.FunctionName()));
}

bool HasSchema(const dd::FunctionExpression& fe) { return fe.GetQualifiedName().Path().size() > 1; }

std::vector<const dd::ParsedExpression*> Arguments(const dd::FunctionExpression& fe) {
  std::vector<const dd::ParsedExpression*> args;
  for (const auto& a : fe.GetArguments()) args.push_back(&a.GetExpression());
  return args;
}

std::vector<std::string> ColumnNames(const dd::ColumnRefExpression& ref) {
  const auto names = compat::Strs(ref.ColumnNames());
  return std::vector<std::string>(names.begin(), names.end());
}

dd::unique_ptr<dd::ParsedExpression> StringConstant(const std::string& s) {
  return dd::make_uniq<dd::ConstantExpression>(dd::Literal::String(s));
}

dd::unique_ptr<dd::ParsedExpression> BoolConstant(bool b) {
  return dd::make_uniq<dd::ConstantExpression>(dd::Literal::Boolean(b));
}

dd::unique_ptr<dd::ParsedExpression> NullConstant() {
  return dd::make_uniq<dd::ConstantExpression>(dd::Literal::Null());
}

// The constant string argument of a context function, or nullopt.
std::optional<std::string> ConstantStringArgument(const dd::FunctionExpression& fe) {
  const auto args = Arguments(fe);
  if (args.size() != 1 || args[0]->GetExpressionClass() != dd::ExpressionClass::CONSTANT) {
    return std::nullopt;
  }
  const auto value = compat::ConstantValue(args[0]->Cast<dd::ConstantExpression>());
  if (value.IsNull() || value.type().id() != dd::LogicalTypeId::VARCHAR) return std::nullopt;
  return value.GetValue<std::string>();
}

// Context functions: names a policy may call, all resolved by Hardcharger.
bool IsUserFunction(const std::string& n) {
  return n == "hardcharger_user" || n == "current_user" ||
         n == "session_user" || n == "user";
}
bool IsContextFunction(const std::string& n) {
  return IsUserFunction(n) || n == "is_role_active" || n == "session_attribute";
}

// What a policy must not read: anything the session can set, or anything that
// reaches outside the row.
bool IsForbiddenFunction(const std::string& n) {
  static const std::unordered_set<std::string> kNames = {
      "getvariable",   "current_setting",  "query",   "query_table", "current_role",
      "hardcharger_role", "nextval", "currval", "setseed"};
  return kNames.count(n) > 0;
}

arrow::Status Invalid(const std::string& what) {
  return arrow::Status::Invalid("Hardcharger access control: " + what);
}

// Check one parsed policy expression (recursively).
arrow::Status CheckShape(const dd::ParsedExpression& expr, const std::string& mask_parameter) {
  switch (expr.GetExpressionClass()) {
    case dd::ExpressionClass::SUBQUERY:
      return Invalid("a policy cannot contain a subquery");
    case dd::ExpressionClass::WINDOW:
      return Invalid("a policy cannot contain a window function");
    case dd::ExpressionClass::PARAMETER:
      return Invalid("a policy cannot contain parameters or variables");
    case dd::ExpressionClass::STAR:
    case dd::ExpressionClass::DEFAULT:
    case dd::ExpressionClass::POSITIONAL_REFERENCE:
      return Invalid("a policy must be a plain expression");
    case dd::ExpressionClass::FUNCTION: {
      const auto& fe = expr.Cast<dd::FunctionExpression>();
      const std::string name = FunctionName(fe);
      if (IsForbiddenFunction(name)) {
        return Invalid("a policy cannot call " + name + "()");
      }
      if (IsContextFunction(name)) {
        if (HasSchema(fe)) return Invalid("context functions are called without a schema");
        if (IsUserFunction(name)) {
          if (!Arguments(fe).empty()) return Invalid(name + "() takes no arguments");
          return arrow::Status::OK();
        }
        if (!ConstantStringArgument(fe)) {
          return Invalid(name + "() takes one string constant");
        }
        return arrow::Status::OK();
      }
      break;
    }
    case dd::ExpressionClass::COLUMN_REF: {
      const auto names = ColumnNames(expr.Cast<dd::ColumnRefExpression>());
      if (names.size() == 1 && IsUserFunction(Lower(names[0]))) return arrow::Status::OK();
      if (!mask_parameter.empty() &&
          !(names.size() == 1 && IdentifiersEqual(names[0], mask_parameter))) {
        return Invalid("a masking policy may read only its parameter " + mask_parameter);
      }
      break;
    }
    default:
      break;
  }
  arrow::Status status = arrow::Status::OK();
  dd::ParsedExpressionIterator::EnumerateChildren(expr, [&](const dd::ParsedExpression& child) {
    if (status.ok()) status = CheckShape(child, mask_parameter);
  });
  return status;
}

arrow::Result<dd::unique_ptr<dd::ParsedExpression>> ParseOne(const std::string& sql) {
  dd::vector<dd::unique_ptr<dd::ParsedExpression>> list;
  try {
    auto parser = compat::StandaloneParser();
    list = parser.ParseExpressionList(sql);
  } catch (const std::exception& e) {
    return Invalid(std::string("the policy is not a valid expression: ") + e.what());
  }
  if (list.size() != 1) return Invalid("a policy must be exactly one expression");
  return std::move(list[0]);
}

// Replace context functions (and a masking parameter) inside `expr`.
void Substitute(dd::unique_ptr<dd::ParsedExpression>& expr, const PolicyContext& context,
                const std::string& mask_parameter, const dd::ParsedExpression* parameter_value) {
  if (expr->GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
    const auto& fe = expr->Cast<dd::FunctionExpression>();
    const std::string name = FunctionName(fe);
    if (IsUserFunction(name)) {
      expr = StringConstant(context.user_name);
      return;
    }
    if (name == "is_role_active") {
      expr =
          BoolConstant(context.role_names.count(FoldIdentifier(*ConstantStringArgument(fe))) > 0);
      return;
    }
    if (name == "session_attribute") {
      auto it = context.attributes.find(*ConstantStringArgument(fe));
      expr = it == context.attributes.end() ? NullConstant() : StringConstant(it->second);
      return;
    }
  }
  if (expr->GetExpressionClass() == dd::ExpressionClass::COLUMN_REF) {
    const auto names = ColumnNames(expr->Cast<dd::ColumnRefExpression>());
    if (names.size() == 1 && IsUserFunction(Lower(names[0]))) {
      expr = StringConstant(context.user_name);
      return;
    }
    if (parameter_value && names.size() == 1 && IdentifiersEqual(names[0], mask_parameter)) {
      expr = parameter_value->Copy();
      return;
    }
  }
  dd::ParsedExpressionIterator::EnumerateChildren(
      *expr, [&](dd::unique_ptr<dd::ParsedExpression>& child) {
        Substitute(child, context, mask_parameter, parameter_value);
      });
}

// A bound policy expression may not have reached anything the parse check
// could not see (a macro's body, say).
void CheckBound(const dd::Expression& expr) {
  if (expr.GetExpressionClass() == dd::ExpressionClass::BOUND_SUBQUERY) {
    Refuse("a row or masking policy on a table it reads contains a subquery");
  }
  if (expr.GetExpressionClass() == dd::ExpressionClass::BOUND_FUNCTION) {
    const auto& fn = expr.Cast<dd::BoundFunctionExpression>();
    const std::string name = Lower(compat::Str(fn.Function().GetName()));
    if (IsForbiddenFunction(name)) {
      Refuse("a row or masking policy on a table it reads calls " + name + "()");
    }
  }
  dd::ExpressionIterator::EnumerateChildren(expr,
                                            [](const dd::Expression& child) { CheckBound(child); });
}

// ---- the plan rewrite --------------------------------------------------------

// Every setting name (DuckDB's, its aliases and the loaded extensions') that holds a
// credential.
std::vector<std::string> SensitiveSettingNames(dd::ClientContext& context) {
  std::vector<std::string> names;
  for (const auto& name : dd::DBConfig::GetOptionNames()) {
    if (IsSensitiveSetting(compat::Str(name))) names.push_back(compat::Str(name));
  }
  for (dd::idx_t i = 0; i < dd::DBConfig::GetAliasCount(); ++i) {
    if (auto alias = dd::DBConfig::GetAliasByIndex(i)) {
      const std::string name = alias->alias;
      if (IsSensitiveSetting(name)) names.push_back(name);
    }
  }
  for (const auto& [name, option] : dd::DBConfig::GetConfig(context).GetExtensionSettings()) {
    if (IsSensitiveSetting(compat::Str(name))) names.push_back(compat::Str(name));
  }
  return names;
}

class PolicyRewriter {
 public:
  PolicyRewriter(dd::ClientContext& context, std::shared_ptr<const Snapshot> snapshot,
                 const Principal& principal)
      : context_(context),
        snapshot_owner_(std::move(snapshot)),
        snapshot_(*snapshot_owner_),
        principal_(principal),
        policy_context_(PolicyContext::For(snapshot_, principal)),
        restricted_(snapshot_.RestrictsCatalogs(principal)) {}

  void Rewrite(dd::unique_ptr<dd::LogicalOperator>& plan) {
    Walk(plan);
    if (masks_.empty()) return;
    SubstituteMasks(*plan);
    for (const auto& b : ResultColumns(*plan)) {
      if (masks_.count(b)) Refuse("a masked column is returned in a way Hardcharger cannot mask");
    }
    CheckPositionalUse(*plan);
  }

 private:
  bool Governed(const ObjectPath& path) {
    return !snapshot_.RowPoliciesOn(path).empty() ||
           !snapshot_.ApplicableMasks(principal_, path).empty();
  }

  void RefuseWriteInto(dd::TableCatalogEntry& table, const char* verb) {
    const ObjectPath path = PathOfEntry(table);
    if (IdentifiersEqual(path.catalog, TEMP_CATALOG)) return;
    if (Governed(path)) {
      Refuse(std::string(verb) + " on " + path.ToString() +
             ", which has row or masking policies, is not available under access control "
             "yet");
    }
  }

  void Walk(dd::unique_ptr<dd::LogicalOperator>& op) {
    for (auto& child : op->children) {
      if (child) Walk(child);
    }
    switch (op->type) {
      case OpType::LOGICAL_INSERT:
        RefuseWriteInto(op->Cast<dd::LogicalInsert>().table, "INSERT");
        break;
      case OpType::LOGICAL_UPDATE:
        RefuseWriteInto(op->Cast<dd::LogicalUpdate>().table, "UPDATE");
        break;
      case OpType::LOGICAL_MERGE_INTO:
        RefuseWriteInto(op->Cast<dd::LogicalMergeInto>().table, "MERGE");
        break;
      case OpType::LOGICAL_DELETE: {
        // DELETE ... RETURNING hands back whole rows, masked columns included.
        auto& del = op->Cast<dd::LogicalDelete>();
        const ObjectPath path = PathOfEntry(del.table);
        if (del.return_chunk && !snapshot_.ApplicableMasks(principal_, path).empty()) {
          Refuse("DELETE ... RETURNING on " + path.ToString() + ", which has masked columns");
        }
        break;
      }
      case OpType::LOGICAL_GET:
        RewriteGet(op);
        break;
      default:
        break;
    }
  }

  // ---- table functions that list the catalog ----------------------------------
  //
  // The table functions that describe the catalog list every object, whatever the user
  // may access. Their rows are narrowed for the user here, which covers
  // information_schema, SHOW, DESCRIBE and every other view built on them, however the
  // statement reaches them:
  //  - a restricted user (tenant, catalog_access rules, the server's hidden catalogs)
  //    sees only the catalogs it may see;
  //  - every store user sees only the schemas, tables, views and columns the snapshot's
  //    listing rules show it (Warp SQL §9.4: the same rules as SQL, as the protocol's
  //    metadata calls apply);
  //  - without MANAGE CONFIG, duckdb_settings() leaves out the settings that hold
  //    credentials (the HTTP proxy's password, ...).
  // All of one function's conditions go into one filter right above it.
  void RestrictTableFunctionRows(dd::unique_ptr<dd::LogicalOperator>& op) {
    auto& get = op->Cast<dd::LogicalGet>();
    const std::string function = Lower(compat::Str(get.function.GetName()));
    const size_t original_columns = get.GetColumnIds().size();
    dd::vector<dd::unique_ptr<dd::Expression>> conditions;

    static const std::unordered_set<std::string> kCatalogListing = {
        "duckdb_databases",   "duckdb_tables",    "duckdb_views",        "duckdb_columns",
        "duckdb_constraints", "duckdb_functions", "duckdb_indexes",      "duckdb_schemas",
        "duckdb_sequences",   "duckdb_types",     "pragma_database_size"};
    if (restricted_ && kCatalogListing.count(function)) {
      std::vector<std::string> visible;
      for (const auto& db : dd::DatabaseManager::Get(context_).GetDatabases()) {
        const std::string name = compat::Str(db->GetName());
        if (snapshot_.CatalogVisible(principal_, name)) visible.push_back(name);
      }
      // The session's own temp catalog is not among the instance's databases.
      if (snapshot_.CatalogVisible(principal_, TEMP_CATALOG)) visible.push_back(TEMP_CATALOG);
      conditions.push_back(InList(get, function, "database_name", visible, /*keep_listed=*/true));
    }
    if (function == "duckdb_settings" &&
        !snapshot_.HasSystemPrivilege(principal_, SystemPrivilege::kManageConfig)) {
      const auto sensitive = SensitiveSettingNames(context_);
      if (!sensitive.empty()) {
        conditions.push_back(InList(get, function, "name", sensitive, /*keep_listed=*/false));
      }
    }
    if (auto listed = ListedRows(get, function)) conditions.push_back(std::move(listed));
    if (conditions.empty()) return;

    auto predicate = std::move(conditions[0]);
    for (size_t i = 1; i < conditions.size(); ++i) {
      predicate = dd::make_uniq<dd::BoundConjunctionExpression>(
          dd::ExpressionType::CONJUNCTION_AND, std::move(predicate), std::move(conditions[i]));
    }
    auto filter = dd::make_uniq<dd::LogicalFilter>(std::move(predicate));
    if (get.GetColumnIds().size() > original_columns) {
      // Columns the conditions needed but the statement did not: kept inside.
      for (size_t i = 0; i < original_columns; ++i) {
        filter->projection_map.push_back(dd::ProjectionIndex(i));
      }
    }
    filter->children.push_back(std::move(op));
    op = std::move(filter);
    policy_filters_.insert(op.get());
  }

  // A reference to one of the table function's text columns, read by the scan for the
  // filter above it if the statement doesn't read it.
  dd::unique_ptr<dd::Expression> ColumnOf(dd::LogicalGet& get, const std::string& function,
                                          const std::string& column_name) {
    dd::idx_t column = dd::DConstants::INVALID_INDEX;
    for (dd::idx_t i = 0; i < get.names.size(); ++i) {
      if (IdentifiersEqual(compat::Str(get.names[i]), column_name)) column = i;
    }
    if (column == dd::DConstants::INVALID_INDEX) {
      Refuse("the table function " + function + " cannot be limited for user " +
             principal_.user_name);
    }
    auto& column_ids = get.GetMutableColumnIds();
    std::optional<size_t> position;
    for (size_t p = 0; p < column_ids.size(); ++p) {
      if (!column_ids[p].IsRowIdColumn() && !column_ids[p].IsVirtualColumn() &&
          column_ids[p].GetPrimaryIndex() == column) {
        position = p;
      }
    }
    if (!position) {
      column_ids.emplace_back(column);
      position = column_ids.size() - 1;
    }
    const dd::ColumnBinding binding(get.table_index, dd::ProjectionIndex(*position));
    return dd::make_uniq<dd::BoundColumnRefExpression>(get.returned_types[column], binding);
  }

  // The column's value is one of `values` (keep_listed), or none of them.
  dd::unique_ptr<dd::Expression> InList(dd::LogicalGet& get, const std::string& function,
                                        const std::string& column_name,
                                        const std::vector<std::string>& values, bool keep_listed) {
    if (values.empty()) return dd::make_uniq<dd::BoundConstantExpression>(dd::Value::BOOLEAN(!keep_listed));
    auto in = dd::make_uniq<dd::BoundOperatorExpression>(
        keep_listed ? dd::ExpressionType::COMPARE_IN : dd::ExpressionType::COMPARE_NOT_IN,
        dd::LogicalType::BOOLEAN);
    auto& in_children = in->GetChildrenMutable();
    in_children.push_back(ColumnOf(get, function, column_name));
    for (const auto& value : values) {
      in_children.push_back(dd::make_uniq<dd::BoundConstantExpression>(dd::Value(value)));
    }
    return in;
  }

  // The listing rule for a catalog table function's rows (nullptr: it lists nothing the
  // rules cover). Evaluated as the rows are produced, by a function of Hardcharger's own
  // that no statement can name: it holds the snapshot and principal the statement was
  // checked with (a prepared statement is checked again when the store changes).
  dd::unique_ptr<dd::Expression> ListedRows(dd::LogicalGet& get, const std::string& function) {
    if (principal_.superuser) return nullptr;
    enum class Rule { kSchema, kObject, kColumn, kColumnOfArgument };
    struct Listing {
      Rule rule;
      std::vector<std::string> columns;
    };
    static const std::map<std::string, Listing> kListings = {
        {"duckdb_schemas", {Rule::kSchema, {"database_name", "schema_name"}}},
        {"duckdb_tables", {Rule::kObject, {"database_name", "schema_name", "table_name"}}},
        {"duckdb_views", {Rule::kObject, {"database_name", "schema_name", "view_name"}}},
        {"duckdb_constraints", {Rule::kObject, {"database_name", "schema_name", "table_name"}}},
        {"duckdb_indexes", {Rule::kObject, {"database_name", "schema_name", "table_name"}}},
        {"duckdb_columns",
         {Rule::kColumn, {"database_name", "schema_name", "table_name", "column_name"}}},
        // The columns of the one table they describe (the plan check refuses one the
        // user may not access at all).
        {"pragma_table_info", {Rule::kColumnOfArgument, {"name"}}},
        {"pragma_show", {Rule::kColumnOfArgument, {"column_name"}}},
    };
    auto listing = kListings.find(function);
    if (listing == kListings.end()) return nullptr;
    const Rule rule = listing->second.rule;

    ObjectPath described;  // kColumnOfArgument: the table the argument names
    if (rule == Rule::kColumnOfArgument) {
      if (get.parameters.empty() || get.parameters[0].IsNull()) return nullptr;
      auto entry = DescribedTable(get.parameters[0]);
      if (!entry) return nullptr;  // DuckDB reports what it makes of it
      described = PathOfEntry(*entry);
    }

    dd::vector<dd::unique_ptr<dd::Expression>> arguments;
    dd::vector<dd::LogicalType> types;
    for (const auto& column : listing->second.columns) {
      arguments.push_back(ColumnOf(get, function, column));
      types.push_back(dd::LogicalType::VARCHAR);
    }
    // Answers by name: catalog listings repeat the same table on many rows.
    struct Answers {
      std::mutex mutex;
      std::unordered_map<std::string, bool> known;
    };
    auto answers = std::make_shared<Answers>();
    auto decide = [answers](
                      const std::string& key, const std::function<bool()>& ask) {
      {
        std::lock_guard<std::mutex> lock(answers->mutex);
        if (auto it = answers->known.find(key); it != answers->known.end()) return it->second;
      }
      const bool listed = ask();
      std::lock_guard<std::mutex> lock(answers->mutex);
      answers->known.emplace(key, listed);
      return listed;
    };
    const auto snapshot = snapshot_owner_;
    const Principal principal = principal_;
    dd::scalar_function_t body;
    switch (rule) {
      case Rule::kSchema:
        body = [=](dd::DataChunk& args, dd::ExpressionState&, dd::Vector& result) {
          dd::VariadicExecutor::Execute<bool, dd::string_t, dd::string_t>(
              args, result, [&](dd::string_t catalog, dd::string_t schema) {
                const ObjectPath path{catalog.GetString(), {schema.GetString()}, {}};
                return decide(ObjectKey(path),
                              [&] { return snapshot->SchemaListed(principal, path); });
              });
        };
        break;
      case Rule::kObject:
        body = [=](dd::DataChunk& args, dd::ExpressionState&, dd::Vector& result) {
          dd::VariadicExecutor::Execute<bool, dd::string_t, dd::string_t, dd::string_t>(
              args, result, [&](dd::string_t catalog, dd::string_t schema, dd::string_t name) {
                const ObjectPath path{catalog.GetString(), {schema.GetString()}, name.GetString()};
                return decide(ObjectKey(path),
                              [&] { return snapshot->ObjectListed(principal, path); });
              });
        };
        break;
      case Rule::kColumn:
        body = [=](dd::DataChunk& args, dd::ExpressionState&, dd::Vector& result) {
          dd::VariadicExecutor::Execute<bool, dd::string_t, dd::string_t, dd::string_t, dd::string_t>(
              args, result,
              [&](dd::string_t catalog, dd::string_t schema, dd::string_t name, dd::string_t column) {
                const ObjectPath path{catalog.GetString(), {schema.GetString()}, name.GetString()};
                const std::string column_name = column.GetString();
                return decide(ObjectKey(path) + "\x1d" + FoldIdentifier(column_name),
                              [&] { return snapshot->ColumnListed(principal, path, column_name); });
              });
        };
        break;
      case Rule::kColumnOfArgument:
        body = [=](dd::DataChunk& args, dd::ExpressionState&, dd::Vector& result) {
          dd::VariadicExecutor::Execute<bool, dd::string_t>(args, result, [&](dd::string_t column) {
            const std::string column_name = column.GetString();
            return decide(FoldIdentifier(column_name),
                          [&] { return snapshot->ColumnListed(principal, described, column_name); });
          });
        };
        break;
    }
    dd::ScalarFunction listed(dd::Identifier("hardcharger_listed"), std::move(types),
                              dd::LogicalType::BOOLEAN, std::move(body));
    return dd::make_uniq<dd::BoundFunctionExpression>(dd::BoundScalarFunction(listed),
                                                      std::move(arguments), nullptr);
  }

  // The table pragma_table_info('...') / pragma_show('...') describes, as DuckDB finds it.
  dd::optional_ptr<dd::CatalogEntry> DescribedTable(const dd::Value& argument) {
    try {
      dd::vector<dd::Identifier> path;
      for (const auto& part : dd::QualifiedName::ParseComponents(argument.ToString())) {
        path.emplace_back(compat::Str(part));
      }
      if (path.empty()) return nullptr;
      return dd::Catalog::GetEntry(context_,
                                   dd::EntryLookupInfo(dd::CatalogType::TABLE_ENTRY,
                                                       dd::QualifiedName::FromPath(path)),
                                   dd::OnEntryNotFound::RETURN_NULL);
    } catch (...) {
      return nullptr;
    }
  }

  void RewriteGet(dd::unique_ptr<dd::LogicalOperator>& op) {
    auto& get = op->Cast<dd::LogicalGet>();
    auto table = get.GetTable();
    if (!table) {
      RestrictTableFunctionRows(op);
      return;
    }
    const ObjectPath path = PathOfEntry(*table);
    if (IdentifiersEqual(path.catalog, TEMP_CATALOG)) return;
    const auto policies = snapshot_.RowPoliciesOn(path);
    const auto masks = snapshot_.ApplicableMasks(principal_, path);
    if (policies.empty() && masks.empty()) return;

    // Bind policy SQL against this scan, as DuckDB's binder would: column
    // references resolve to the scan's columns (and add any it lacks).
    auto binder = dd::Binder::CreateBinder(context_);
    auto& column_ids = get.GetMutableColumnIds();
    const size_t original_columns = column_ids.size();
    const std::string alias = compat::Str(table->name);
    dd::vector<dd::Identifier> names;
    dd::vector<dd::LogicalType> types;
    for (auto& col : table->GetColumns().Logical()) {
      names.push_back(col.Name());
      types.push_back(col.Type());
    }
    binder->bind_context.AddBaseTable(get.table_index, dd::Identifier(alias), names, types,
                                      column_ids, *table);
    auto bind = [&](dd::unique_ptr<dd::ParsedExpression> parsed, const dd::LogicalType& type) {
      dd::WhereBinder where_binder(*binder, context_);
      where_binder.target_type = type;  // a mask keeps its column's type; a predicate is BOOLEAN
      auto bound = where_binder.Bind(parsed);
      bound = dd::BoundCastExpression::AddCastToType(context_, std::move(bound), type);
      CheckBound(*bound);
      return bound;
    };

    // Masks: only for columns this scan reads.
    for (const auto& [column, policy] : masks) {
      if (!policy) Refuse("a masking policy on " + path.ToString() + "." + column + " is missing");
      dd::idx_t logical = dd::DConstants::INVALID_INDEX;
      dd::LogicalType column_type;
      for (auto& col : table->GetColumns().Logical()) {
        if (IdentifiersEqual(compat::Str(col.Name()), column)) {
          logical = col.Logical().index;
          column_type = col.Type();
        }
      }
      if (logical == dd::DConstants::INVALID_INDEX) {
        Refuse("the masked column " + column + " of " + path.ToString() + " no longer exists");
      }
      std::optional<size_t> position;
      for (size_t p = 0; p < original_columns; ++p) {
        if (!column_ids[p].IsRowIdColumn() && !column_ids[p].IsVirtualColumn() &&
            column_ids[p].GetPrimaryIndex() == logical) {
          position = p;
        }
      }
      if (!position) continue;  // not read by this statement
      auto parameter =
          dd::make_uniq<dd::ColumnRefExpression>(dd::Identifier(column), dd::Identifier(alias));
      auto parsed = PreparePolicyExpression(policy->body, policy_context_, policy->parameter,
                                            parameter.get());
      if (!parsed.ok()) Refuse(parsed.status().message());
      const dd::ColumnBinding binding(get.table_index, dd::ProjectionIndex(*position));
      masks_[binding] = bind(std::move(*parsed), column_type);
    }

    // Row policies: permissive ones OR-ed, restrictive ones AND-ed; nothing
    // permissive that applies means no rows.
    if (policies.empty()) return;
    std::vector<dd::unique_ptr<dd::ParsedExpression>> permissive, restrictive;
    for (const auto* p : snapshot_.ApplicableRowPolicies(principal_, path)) {
      auto parsed = PreparePolicyExpression(p->predicate, policy_context_);
      if (!parsed.ok()) Refuse(parsed.status().message());
      (p->permissive ? permissive : restrictive).push_back(std::move(*parsed));
    }
    dd::unique_ptr<dd::ParsedExpression> predicate;
    if (permissive.empty()) {
      predicate = BoolConstant(false);
    } else if (permissive.size() == 1) {
      predicate = std::move(permissive[0]);
    } else {
      dd::vector<dd::unique_ptr<dd::ParsedExpression>> children;
      for (auto& p : permissive) children.push_back(std::move(p));
      predicate = dd::make_uniq<dd::ConjunctionExpression>(dd::ExpressionType::CONJUNCTION_OR,
                                                           std::move(children));
    }
    for (auto& r : restrictive) {
      predicate = dd::make_uniq<dd::ConjunctionExpression>(dd::ExpressionType::CONJUNCTION_AND,
                                                           std::move(predicate), std::move(r));
    }
    auto filter =
        dd::make_uniq<dd::LogicalFilter>(bind(std::move(predicate), dd::LogicalType::BOOLEAN));
    if (column_ids.size() > original_columns) {
      // Columns the policy needed but the statement did not: keep them inside.
      for (size_t i = 0; i < original_columns; ++i) {
        filter->projection_map.push_back(dd::ProjectionIndex(i));
      }
    }
    filter->children.push_back(std::move(op));
    op = std::move(filter);
    policy_filters_.insert(op.get());
  }

  // Every use of a masked column (outside the policy filters, which decide on
  // the real values) becomes the mask.
  void SubstituteMasks(dd::LogicalOperator& op) {
    if (!policy_filters_.count(&op)) {
      dd::LogicalOperatorVisitor::EnumerateExpressions(op, [&](dd::unique_ptr<dd::Expression>* e) {
        if (e && *e) SubstituteExpression(*e);
      });
    }
    for (auto& child : op.children) {
      if (child) SubstituteMasks(*child);
    }
  }

  void SubstituteExpression(dd::unique_ptr<dd::Expression>& expr) {
    if (expr->GetExpressionClass() == dd::ExpressionClass::BOUND_COLUMN_REF) {
      const auto& ref = expr->Cast<dd::BoundColumnRefExpression>();
      auto it = masks_.find(ref.Binding());
      if (it != masks_.end()) {
        auto alias = expr->GetAlias();
        expr = it->second->Copy();
        expr->SetAlias(alias);
        return;  // the mask's own reference to the column stays as it is
      }
    }
    dd::ExpressionIterator::EnumerateChildren(
        *expr, [&](dd::unique_ptr<dd::Expression>& child) { SubstituteExpression(child); });
  }

  // An operator that takes its child's columns by position (not through
  // expressions) would pass a masked column on unmasked.
  void CheckPositionalUse(dd::LogicalOperator& op) {
    if (UsesChildColumnsByPosition(op) && !policy_filters_.count(&op)) {
      for (auto& child : op.children) {
        if (!child) continue;
        for (const auto& b : child->GetColumnBindings()) {
          if (masks_.count(b)) Refuse("a masked column is used in a way Hardcharger cannot mask");
        }
      }
    }
    for (auto& child : op.children) {
      if (child) CheckPositionalUse(*child);
    }
  }

  dd::ClientContext& context_;
  const std::shared_ptr<const Snapshot> snapshot_owner_;  // for the listing filters, which run later
  const Snapshot& snapshot_;
  const Principal& principal_;
  PolicyContext policy_context_;
  dd::column_binding_map_t<dd::unique_ptr<dd::Expression>> masks_;
  std::unordered_set<const dd::LogicalOperator*> policy_filters_;
  const bool restricted_;  // sees only some catalogs (tenant, catalog_access, hidden)
};

}  // namespace

PolicyContext PolicyContext::For(const Snapshot& snapshot, const Principal& principal) {
  PolicyContext context;
  context.user_name = principal.user_name;
  for (const auto& id : snapshot.RolesOf(principal)) {
    if (const auto* role = snapshot.FindRoleById(id))
      context.role_names.insert(FoldIdentifier(role->name));
  }
  if (const auto* user = snapshot.FindUserById(principal.user_id))
    context.attributes = user->attributes;
  return context;
}

arrow::Status ValidatePolicySql(const std::string& sql, const std::string& mask_parameter) {
  ARROW_ASSIGN_OR_RAISE(auto parsed, ParseOne(sql));
  return CheckShape(*parsed, mask_parameter);
}

arrow::Result<dd::unique_ptr<dd::ParsedExpression>> PreparePolicyExpression(
    const std::string& sql, const PolicyContext& context, const std::string& mask_parameter,
    const dd::ParsedExpression* parameter_value) {
  ARROW_ASSIGN_OR_RAISE(auto parsed, ParseOne(sql));
  ARROW_RETURN_NOT_OK(CheckShape(*parsed, mask_parameter));
  Substitute(parsed, context, mask_parameter, parameter_value);
  return parsed;
}

void ApplyPolicies(dd::ClientContext& context, const std::shared_ptr<const Snapshot>& snapshot,
                   const Principal& principal, dd::unique_ptr<dd::LogicalOperator>& plan) {
  if (!plan || principal.superuser) return;
  // Most statements need no rewrite: no policies, no tenants, no catalog limits, and no
  // table function that lists the catalog or the settings.
  static const std::unordered_set<std::string> kListingFunctions = {
      "duckdb_databases", "duckdb_tables",    "duckdb_views",         "duckdb_columns",
      "duckdb_constraints", "duckdb_functions", "duckdb_indexes",     "duckdb_schemas",
      "duckdb_sequences", "duckdb_types",     "pragma_database_size", "duckdb_settings",
      "pragma_table_info", "pragma_show"};
  std::function<bool(const dd::LogicalOperator&)> lists = [&](const dd::LogicalOperator& op) {
    if (op.type == OpType::LOGICAL_GET &&
        kListingFunctions.count(Lower(compat::Str(op.Cast<dd::LogicalGet>().function.GetName())))) {
      return true;
    }
    for (const auto& child : op.children) {
      if (child && lists(*child)) return true;
    }
    return false;
  };
  if (snapshot->row_policies().empty() && snapshot->mask_bindings().empty() &&
      snapshot->catalog_tenants().empty() && !snapshot->RestrictsCatalogs(principal) &&
      !lists(*plan)) {
    return;
  }
  PolicyRewriter(context, snapshot, principal).Rewrite(plan);
}

}  // namespace hardcharger::authz
