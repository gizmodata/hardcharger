#include "authz/gate.hpp"

#include <algorithm>
#include <cctype>
#include <duckdb/parser/expression/columnref_expression.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/parameter_expression.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/expression/subquery_expression.hpp>
#include <duckdb/function/scalar_macro_function.hpp>
#include <duckdb/function/table_macro_function.hpp>
#include <duckdb/parser/parsed_data/create_macro_info.hpp>
#include <duckdb/parser/parsed_data/create_table_info.hpp>
#include <duckdb/parser/parsed_data/create_trigger_info.hpp>
#include <duckdb/parser/parsed_data/create_type_info.hpp>
#include <duckdb/parser/parsed_data/create_view_info.hpp>
#include <duckdb/parser/parsed_data/drop_info.hpp>
#include <duckdb/parser/parsed_expression_iterator.hpp>
#include <duckdb/parser/query_node/copy_query_node.hpp>
#include <duckdb/parser/query_node/insert_query_node.hpp>
#include <duckdb/parser/query_node/merge_query_node.hpp>
#include <duckdb/parser/query_node/recursive_cte_node.hpp>
#include <duckdb/parser/query_node/select_node.hpp>
#include <duckdb/parser/query_node/set_operation_node.hpp>
#include <duckdb/parser/query_node/update_query_node.hpp>
#include <duckdb/parser/statement/call_statement.hpp>
#include <duckdb/parser/statement/copy_statement.hpp>
#include <duckdb/parser/statement/create_statement.hpp>
#include <duckdb/parser/statement/delete_statement.hpp>
#include <duckdb/parser/statement/drop_statement.hpp>
#include <duckdb/parser/statement/execute_statement.hpp>
#include <duckdb/parser/statement/explain_statement.hpp>
#include <duckdb/parser/statement/export_statement.hpp>
#include <duckdb/parser/statement/insert_statement.hpp>
#include <duckdb/parser/statement/load_statement.hpp>
#include <duckdb/parser/statement/merge_into_statement.hpp>
#include <duckdb/parser/statement/multi_statement.hpp>
#include <duckdb/parser/statement/pragma_statement.hpp>
#include <duckdb/parser/statement/prepare_statement.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/parser/statement/set_statement.hpp>
#include <duckdb/parser/statement/update_statement.hpp>
#include <duckdb/parser/tableref/at_clause.hpp>
#include <duckdb/parser/tableref/basetableref.hpp>
#include <duckdb/parser/tableref/showref.hpp>
#include <duckdb/parser/tableref/subqueryref.hpp>
#include <duckdb/parser/tableref/table_function_ref.hpp>
#include <unordered_set>

#include "authz/duckdb_internal.hpp"
#include "authz/types.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
using ddb::Str;

std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
  return s;
}

// Table functions no store user may call, whatever their arguments. DuckDB 2.0's
// external resources name their callbacks by string and run them on an internal
// connection, outside every check on the caller's statement
// (destroy_external_resource('query', '<sql>') runs <sql>); the metadata functions
// are instance-wide.
bool AlwaysGated(const std::string& name) {
  static const std::unordered_set<std::string> kNames = {"duckdb_secrets",
                                                         "create_external_resource",
                                                         "destroy_external_resource",
                                                         "register_external_resource",
                                                         "register_external_resource_type",
                                                         "deregister_external_resource",
                                                         "duckdb_external_resources",
                                                         "duckdb_external_resource_types",
                                                         "duckdb_extension_repositories"};
  return kNames.contains(name);
}

// DuckDB's remote-query protocol functions open network connections from inside the
// server (and quack_serve() a listener).
bool IsRemoteQueryFunction(const std::string& name) { return name.rfind("quack_", 0) == 0; }

// Settings that change the whole instance even when SET without GLOBAL: resource
// limits, external access and extensions, secrets and the filesystem.
bool IsInstanceSetting(const std::string& name) {
  static const std::unordered_set<std::string> kNames = {"memory_limit",
                                                         "max_memory",
                                                         "threads",
                                                         "worker_threads",
                                                         "external_threads",
                                                         "temp_directory",
                                                         "max_temp_directory_size",
                                                         "enable_external_access",
                                                         "allow_unsigned_extensions",
                                                         "allow_community_extensions",
                                                         "allow_extensions_metadata_query",
                                                         "autoinstall_known_extensions",
                                                         "autoload_known_extensions",
                                                         "custom_extension_repository",
                                                         "autoinstall_extension_repository",
                                                         "extension_directory",
                                                         "enable_external_file_cache",
                                                         "secret_directory",
                                                         "allow_persistent_secrets",
                                                         "disabled_filesystems",
                                                         "allowed_directories",
                                                         "allowed_paths",
                                                         "home_directory",
                                                         "lock_configuration",
                                                         "allow_extension_repositories",
                                                         "extension_repository_directory",
                                                         "extension_directories",
                                                         "redact_http_logs"};
  return kNames.contains(name);
}

// Settings that switch the session's SQL grammar (this gate reads the built-in one).
bool IsGrammarSetting(const std::string& name) {
  return name == "current_dialect" || name == "active_grammar_extensions" ||
         name == "enable_legacy_parser";
}

// FROM '<path>': an unqualified table name that looks like a file or URL is a
// replacement scan, which reads it. Under access control every path counts, remote
// ones too.
bool LooksLikePath(const std::string& name) {
  if (name.find('/') != std::string::npos || name.find('\\') != std::string::npos ||
      name.find(':') != std::string::npos || name.rfind("~", 0) == 0) {
    return true;
  }
  static const char* kExtensions[] = {".csv",  ".tsv",    ".txt",    ".parquet", ".pq",
                                      ".json", ".ndjson", ".jsonl",  ".arrow",   ".feather",
                                      ".gz",   ".zst",    ".bz2",    ".xz",      ".orc",
                                      ".avro", ".xlsx",   ".duckdb", ".db"};
  const std::string low = Lower(name);
  for (const char* ext : kExtensions) {
    const std::string e(ext);
    if (low.size() >= e.size() && low.compare(low.size() - e.size(), e.size(), e) == 0) {
      return true;
    }
  }
  return false;
}

// What a column reference stands for, if it is a macro's parameter: the strings of the
// argument the call gave for it (nullopt when that is not a literal, or when the reference is
// no parameter).
using ColumnCallback = std::function<std::optional<std::vector<std::string>>(const dd::ParsedExpression&)>;

// Literal strings, direct bind values, or lists of those. Unknown parameters and
// computed expressions (including casts) stay unresolved; never evaluate them here.
std::optional<std::vector<std::string>> LiteralPaths(const dd::ParsedExpression* expression, const PathParameterCallback& parameters,
                                                     const ColumnCallback& column = nullptr) {
  if (!expression) return std::nullopt;
  if (expression->GetExpressionClass() == dd::ExpressionClass::PARAMETER) {
    return parameters ? parameters(Str(expression->Cast<dd::ParameterExpression>().Identifier())) : std::nullopt;
  }
  if (expression->GetExpressionClass() == dd::ExpressionClass::COLUMN_REF) {
    return column ? column(*expression) : std::nullopt;
  }
  auto literal = [](const dd::ParsedExpression& e) -> std::optional<std::string> {
    if (e.GetExpressionClass() != dd::ExpressionClass::CONSTANT) return std::nullopt;
    const auto value = ddb::ConstantValue(e.Cast<dd::ConstantExpression>());
    if (value.IsNull() || value.type().id() != dd::LogicalTypeId::VARCHAR) return std::nullopt;
    return value.ToString();
  };
  if (auto one = literal(*expression)) return std::vector<std::string>{*one};
  if (expression->GetExpressionClass() == dd::ExpressionClass::CONSTANT) {
    const auto value = ddb::ConstantValue(expression->Cast<dd::ConstantExpression>());
    if (value.IsNull() || value.type().id() != dd::LogicalTypeId::LIST) return std::nullopt;
    std::vector<std::string> paths;
    for (const auto& child : dd::ListValue::GetChildren(value)) {
      if (child.IsNull() || child.type().id() != dd::LogicalTypeId::VARCHAR) return std::nullopt;
      paths.push_back(child.ToString());
    }
    return paths;
  }
  if (expression->GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
    const auto& function = expression->Cast<dd::FunctionExpression>();
    const std::string name = Lower(Str(function.FunctionName()));
    if (name != "list_value" && name != "array_value") return std::nullopt;
    std::vector<std::string> paths;
    for (const auto& argument : function.GetArguments()) {
      if (argument.HasName()) return std::nullopt;
      auto some = LiteralPaths(&argument.GetExpression(), parameters, column);
      if (!some) return std::nullopt;
      paths.insert(paths.end(), some->begin(), some->end());
    }
    return paths;
  }
  return std::nullopt;
}

// The first positional argument of a call (the path, for the file-reading functions).
const dd::ParsedExpression* FirstPositional(const dd::FunctionExpression& function) {
  for (const auto& argument : function.GetArguments()) {
    if (!argument.HasName()) return &argument.GetExpression();
  }
  return nullptr;
}

// The argument an EXECUTE grant declared as a path: the n-th positional one (1 is the
// first), or the named one; nullptr when the call has no such argument.
const dd::ParsedExpression* DeclaredArgument(const dd::FunctionExpression& function, const PathArgument& declared) {
  int position = 0;
  for (const auto& argument : function.GetArguments()) {
    if (argument.HasName()) {
      if (declared.position == 0 && IdentifiersEqual(Str(argument.GetName()), declared.parameter)) {
        return &argument.GetExpression();
      }
      continue;
    }
    if (declared.position > 0 && ++position == declared.position) return &argument.GetExpression();
  }
  return nullptr;
}

// A table function call as the DuckLake callback sees it: its name parts and its first
// positional argument if that is a string literal (never a bound parameter: the catalog
// must be checkable from the text alone).
TableFunctionCall CallOf(const dd::FunctionExpression& function, const std::string& wrapper) {
  TableFunctionCall call;
  for (const auto& part : function.GetQualifiedName().Path()) {
    if (!Str(part).empty()) call.parts.push_back(Str(part));
  }
  if (call.parts.empty()) call.parts.push_back(Str(function.FunctionName()));
  if (const auto* first = FirstPositional(function); first && first->GetExpressionClass() == dd::ExpressionClass::CONSTANT) {
    const auto value = ddb::ConstantValue(first->Cast<dd::ConstantExpression>());
    if (!value.IsNull() && value.type().id() == dd::LogicalTypeId::VARCHAR) call.first_literal = value.ToString();
  }
  call.wrapper = wrapper;
  call.id = &function;
  return call;
}

// The path a COPY names: its literal, or nullopt when it is an expression.
std::optional<std::vector<std::string>> CopyPaths(const dd::CopyInfo& info, const PathParameterCallback& parameters) {
  if (info.file_path_expression) return LiteralPaths(info.file_path_expression.get(), parameters);
  return std::vector<std::string>{info.file_path};
}

bool IsSequenceFunction(const std::string& name_lower) {
  return name_lower == "nextval" || name_lower == "currval" || name_lower == "setval";
}

// How deep macros may call macros before the gate stops following them (and refuses).
constexpr std::size_t kMaxMacroDepth = 16;

class Walker {
 public:
  Walker(const std::function<bool(const std::string&)>& permitted,
         const NamedTableCallback& named_table, const TableFunctionCallback& table_function_allowed,
         const StorageCallback& storage, const PathParameterCallback& parameters,
         const DuckLakeCallCallback& ducklake_call, const BindTimeCallbacks& bind_time)
      : permitted_(permitted),
        named_table_(named_table),
        table_function_allowed_(table_function_allowed),
        storage_(storage), parameters_(parameters), ducklake_call_(ducklake_call), bind_time_(bind_time) {}

  // What the walk is inside: "" for the statement itself, else the body of a view, macro
  // or trigger, whose calls run later, with whoever uses it.
  void SetWrapper(std::string wrapper) { wrapper_ = std::move(wrapper); }

  // An expression DuckDB evaluates while it binds the statement (a table function's or a
  // PRAGMA's argument, a setting's value, AT, EXECUTE's values, COPY's options): every
  // sequence call in it goes to the sequence callback, with the macro arguments its
  // parameters stand for followed. Subqueries are not evaluated there (they are planned).
  void BindTimeExpression(const dd::ParsedExpression& expression) { Sequences(expression, level_); }

  // The strings `expression` names: literals, bind values, or the literal argument of the
  // macro parameter it is.
  std::optional<std::vector<std::string>> Strings(const dd::ParsedExpression* expression) {
    return StringsAt(expression, level_);
  }

  // A read or write of storage: to the callback with its paths, or flagged as a category.
  void Storage(StorageUse use, const std::string& category) {
    if (violation) return;
    if (storage_) {
      storage_(use);
      return;
    }
    Flag(category);
  }

  void NamedTable(const dd::QualifiedName& name) {
    if (!named_table_ || name.Path().empty()) return;
    std::vector<std::string> parts;
    for (const auto& part : name.Path()) {
      if (!Str(part).empty()) parts.push_back(Str(part));
    }
    if (!parts.empty()) named_table_(parts, describing_ > 0);
  }

  std::optional<std::string> violation;

  void Flag(std::string category) {
    if (violation) return;
    if (permitted_ && permitted_(category)) return;
    violation = std::move(category);
  }

  // A query node and everything reachable from it. DuckDB's enumerator descends
  // joins, subqueries and CTE definitions and calls back on every table ref; it does
  // not report the node-to-node edges (CTE bodies, set operations, COPY and INSERT
  // sources), which CheckNodeTree follows.
  void WalkNode(dd::QueryNode& node) {
    if (violation) return;
    CheckNodeTree(node);
    if (violation) return;
    dd::ParsedExpressionIterator::EnumerateQueryNodeChildren(
        node,
        [this](dd::unique_ptr<dd::ParsedExpression>& child) {
          if (child) CheckExpression(*child);
        },
        [this](dd::TableRef& ref) { CheckRef(ref); });
  }

  // An expression (a scalar macro's body, a parameter's default) and what it reaches.
  void WalkExpression(dd::ParsedExpression& expression) { CheckExpression(expression); }

 private:
  // A macro's parameter, while its body is walked: the argument the call gave for it, and
  // the depth (in scopes_) that argument itself is to be read at.
  struct MacroArgument {
    const dd::ParsedExpression* expression;
    std::size_t level;
  };
  using MacroScope = std::unordered_map<std::string, MacroArgument>;

  // The argument a column reference at `level` stands for, if it names a parameter of the
  // macro whose body is walked there.
  std::optional<MacroArgument> ArgumentOf(const dd::ParsedExpression& expression, std::size_t level) const {
    if (level == 0 || level > scopes_.size()) return std::nullopt;
    if (expression.GetExpressionClass() != dd::ExpressionClass::COLUMN_REF) return std::nullopt;
    const auto& column = expression.Cast<dd::ColumnRefExpression>();
    if (column.IsQualified()) return std::nullopt;
    const auto& scope = scopes_[level - 1];
    auto it = scope.find(Lower(Str(column.GetColumnName())));
    if (it == scope.end()) return std::nullopt;
    return it->second;
  }

  std::optional<std::vector<std::string>> StringsAt(const dd::ParsedExpression* expression, std::size_t level) {
    return LiteralPaths(expression, parameters_, [this, level](const dd::ParsedExpression& column) {
      auto argument = ArgumentOf(column, level);
      return argument ? StringsAt(argument->expression, argument->level) : std::nullopt;
    });
  }

  void Sequences(const dd::ParsedExpression& expression, std::size_t level) {
    if (violation || !bind_time_.sequence) return;
    if (auto argument = ArgumentOf(expression, level)) {
      if (argument->expression) Sequences(*argument->expression, argument->level);
      return;
    }
    if (expression.GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
      const auto& function = expression.Cast<dd::FunctionExpression>();
      const std::string name = Lower(Str(function.FunctionName()));
      if (IsSequenceFunction(name)) {
        std::optional<std::string> sequence;
        if (const auto* first = FirstPositional(function)) {
          auto names = StringsAt(first, level);
          if (names && names->size() == 1) sequence = names->front();
        }
        bind_time_.sequence(name, sequence);
      } else if (bind_time_.macros) {
        // A user's scalar macro: DuckDB expands its body in place, with these arguments, and
        // evaluates it here too (range(bump_to(77)) with bump_to(n) AS setval('s', n)).
        const auto definitions = bind_time_.macros(PartsOf(function), false);
        if (!definitions.empty() && scopes_.size() >= kMaxMacroDepth) {
          Flag("macros that call macros more than " + std::to_string(kMaxMacroDepth) + " deep");
          return;
        }
        for (const auto& definition : definitions) {
          if (!definition || violation || definition->type != dd::MacroType::SCALAR_MACRO) continue;
          auto& body = definition->Cast<dd::ScalarMacroFunction>();
          for (auto& parameter : definition->default_parameters) {
            if (parameter.second) Sequences(*parameter.second, 0);
          }
          scopes_.push_back(ScopeOf(function, *definition, level));
          if (body.expression) Sequences(*body.expression, scopes_.size());
          scopes_.pop_back();
        }
      }
    }
    dd::ParsedExpressionIterator::EnumerateChildren(expression, [&](const dd::ParsedExpression& child) {
      Sequences(child, level);
    });
  }

  // The name parts a call names a function by.
  static std::vector<std::string> PartsOf(const dd::FunctionExpression& call) {
    std::vector<std::string> parts;
    for (const auto& part : call.GetQualifiedName().Path()) {
      if (!Str(part).empty()) parts.push_back(Str(part));
    }
    if (parts.empty()) parts.push_back(Str(call.FunctionName()));
    return parts;
  }

  // A macro's parameters, as a call at `level` binds them: its arguments by position and by
  // name, and the defaults it leaves out (constants of the definition's, read at the statement's level).
  static MacroScope ScopeOf(const dd::FunctionExpression& call, const dd::MacroFunction& definition, std::size_t level) {
    MacroScope scope;
    std::size_t position = 0;
    for (const auto& argument : call.GetArguments()) {
      if (argument.HasName()) {
        scope[Lower(Str(argument.GetName()))] = {&argument.GetExpression(), level};
      } else if (position < definition.parameters.size()) {
        const auto& parameter = *definition.parameters[position++];
        if (parameter.GetExpressionClass() == dd::ExpressionClass::COLUMN_REF) {
          scope[Lower(Str(parameter.Cast<dd::ColumnRefExpression>().GetColumnName()))] = {&argument.GetExpression(), level};
        }
      }
    }
    for (const auto& parameter : definition.default_parameters) {
      if (parameter.second) scope.emplace(Lower(Str(parameter.first)), MacroArgument{parameter.second.get(), 0});
    }
    return scope;
  }

  // The bodies of the user macros a call names, walked as DuckDB binds them: with the call's
  // arguments (and the defaults it leaves out) in place of the parameters.
  void WalkMacros(const dd::FunctionExpression& call, bool table) {
    if (violation || !bind_time_.macros) return;
    const auto parts = PartsOf(call);
    const auto definitions = bind_time_.macros(parts, table);
    if (definitions.empty()) return;
    if (scopes_.size() >= kMaxMacroDepth) {
      Flag("macros that call macros more than " + std::to_string(kMaxMacroDepth) + " deep");
      return;
    }
    macro_names_.push_back(parts.back());
    struct PopName {
      std::vector<std::string>& names;
      ~PopName() { names.pop_back(); }
    } pop_name{macro_names_};
    for (const auto& definition : definitions) {
      if (!definition || violation) continue;
      // A default is a constant of the definition's: read as written, at the statement's level.
      for (auto& parameter : definition->default_parameters) {
        if (parameter.second) CheckExpression(*parameter.second);
      }
      scopes_.push_back(ScopeOf(call, *definition, level_));
      const std::size_t outer = level_;
      level_ = scopes_.size();
      // A DuckLake call in the body is one the statement does not make itself.
      const std::string outer_wrapper = wrapper_;
      if (wrapper_.empty()) wrapper_ = "a macro";
      if (definition->type == dd::MacroType::TABLE_MACRO) {
        auto& body = definition->Cast<dd::TableMacroFunction>();
        if (body.query_node) WalkNode(*body.query_node);
      } else if (definition->type == dd::MacroType::SCALAR_MACRO) {
        auto& body = definition->Cast<dd::ScalarMacroFunction>();
        if (body.expression) CheckExpression(*body.expression);
      }
      wrapper_ = outer_wrapper;
      level_ = outer;
      scopes_.pop_back();
    }
  }

  // query('<sql>') / query_table(...) inside a macro's body: what they are handed, followed.
  void MacroQuery(const dd::FunctionExpression& function, const std::string& name) {
    const auto* first = FirstPositional(function);
    auto texts = first ? Strings(first) : std::nullopt;
    if (!texts) {
      Flag(name + "() with " + (name == "query" ? "SQL" : "a name") + " that is not a string literal");
      return;
    }
    if (name == "query_table") {
      for (const auto& text : *texts) {
        if (LooksLikePath(text)) {
          Storage({"query_table()", false, std::vector<std::string>{text}}, "query_table() (files and URLs)");
        } else {
          std::vector<std::string> parts;
          try {
            for (const auto& part : dd::QualifiedName::ParseComponents(text)) parts.push_back(Str(part));
          } catch (const std::exception&) {
            Flag("query_table() of a name that does not parse");
            return;
          }
          if (named_table_ && !parts.empty()) named_table_(parts, false);
        }
        if (violation) return;
      }
      return;
    }
    for (const auto& text : *texts) {
      auto parser = ddb::StandaloneParser();
      try {
        parser.ParseQuery(text);
      } catch (const std::exception&) {
        Flag("query() with SQL that does not parse");
        return;
      }
      for (auto& inner : parser.statements) {
        if (!inner) continue;
        if (auto category = classify_(*inner)) {
          Flag(*category);
          return;
        }
      }
    }
  }

 public:
  // Classify(), for SQL a macro hands to query() (set by Classify).
  std::function<std::optional<std::string>(dd::SQLStatement&)> classify_;

 private:
  // DuckDB 2.0 parses COPY and DML as query nodes, and a CTE body may be any of them:
  // WITH x AS (COPY t TO '/file') SELECT 1.
  void CheckNodeTree(dd::QueryNode& node) {
    if (violation) return;
    switch (node.type) {
      case dd::QueryNodeType::COPY_QUERY_NODE: {
        auto& copy = node.Cast<dd::CopyQueryNode>();
        if (!copy.info) {
          Flag("COPY");
          return;
        }
        CopyBindTime(*copy.info);
        Storage({copy.info->is_from ? "COPY ... FROM" : "COPY ... TO", !copy.info->is_from, CopyPaths(*copy.info, parameters_),
                 !copy.info->is_from},
                std::string(copy.info->is_from ? "COPY FROM" : "COPY TO") + " (files and URLs)");
        if (copy.info->select_statement) WalkNode(*copy.info->select_statement);
        break;
      }
      case dd::QueryNodeType::SET_OPERATION_NODE:
        for (auto& child : node.Cast<dd::SetOperationNode>().children) {
          if (child) WalkNode(*child);
        }
        break;
      case dd::QueryNodeType::RECURSIVE_CTE_NODE: {
        auto& cte = node.Cast<dd::RecursiveCTENode>();
        if (cte.left) WalkNode(*cte.left);
        if (cte.right) WalkNode(*cte.right);
        break;
      }
      case dd::QueryNodeType::INSERT_QUERY_NODE: {
        auto& insert = node.Cast<dd::InsertQueryNode>();
        if (insert.select_statement && insert.select_statement->node) {
          WalkNode(*insert.select_statement->node);
        }
        break;
      }
      default:
        break;
    }
    for (auto& entry : node.cte_map.map) {
      if (entry.second && entry.second->query_node) WalkNode(*entry.second->query_node);
    }
  }

  void CheckRef(dd::TableRef& ref) {
    if (violation) return;
    // AT (VERSION => ...) / AT (TIMESTAMP => ...) is evaluated to find the version to read.
    if (ref.type == dd::TableReferenceType::BASE_TABLE) {
      auto& base = ref.Cast<dd::BaseTableRef>();
      if (base.at_clause && base.at_clause->ExpressionMutable()) BindTimeExpression(*base.at_clause->ExpressionMutable());
    }
    if (ref.type == dd::TableReferenceType::TABLE_FUNCTION) {
      auto& function = ref.Cast<dd::TableFunctionRef>();
      CheckTableFunction(function);
      // A table-in/out function's subquery argument is not enumerated for us.
      if (function.subquery && function.subquery->node) WalkNode(*function.subquery->node);
    } else if (ref.type == dd::TableReferenceType::BASE_TABLE) {
      const auto& name = ref.Cast<dd::BaseTableRef>().GetQualifiedName();
      if (name.Path().size() == 1 && LooksLikePath(Str(name.Name()))) {
        Storage({"reading a path in FROM", false, std::vector<std::string>{Str(name.Name())}},
                "reading files and URLs via FROM '" + Str(name.Name()) + "'");
        return;
      }
      NamedTable(name);
    } else if (ref.type == dd::TableReferenceType::SHOW_REF) {
      // DESCRIBE / SHOW / SUMMARIZE: of a query, or of a table by name.
      auto& show = ref.Cast<dd::ShowRef>();
      // DESCRIBE (and SHOW of a table) is bound without being planned: what it names,
      // it describes. SUMMARIZE runs its query, which is planned and checked.
      const int describes = show.show_type != dd::ShowType::SUMMARY ? 1 : 0;
      describing_ += describes;
      struct Undescribe {
        int& depth;
        int by;
        ~Undescribe() { depth -= by; }
      } undescribe{describing_, describes};
      if (show.query) WalkNode(*show.query);
      if (!show.GetTableName().GetIdentifierName().empty() &&
          show.show_type != dd::ShowType::SHOW_SPECIAL &&
          show.show_type != dd::ShowType::SHOW_FROM) {
        // SHOW name shows a setting's value when one has that name (bound into a
        // constant: the plan check never sees which).
        const std::string name = show.GetTableName().GetIdentifierName();
        if (show.show_type == dd::ShowType::SHOW && IsSensitiveSetting(name)) {
          Flag("reading the setting " + name);
          return;
        }
        NamedTable(show.qualified_name);
      }
    } else if (ref.type == dd::TableReferenceType::SUBQUERY) {
      auto& subquery = ref.Cast<dd::SubqueryRef>();
      if (subquery.subquery && subquery.subquery->node) CheckNodeTree(*subquery.subquery->node);
    }
  }

  void CheckExpression(dd::ParsedExpression& expression) {
    if (violation) return;
    if (expression.GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
      const auto& function = expression.Cast<dd::FunctionExpression>();
      const std::string name = Lower(Str(function.FunctionName()));
      if (IsRemoteQueryFunction(name)) {
        Flag(name + "()");
        return;
      }
      // A user's scalar macro: its body is bound here, with these arguments.
      WalkMacros(function, /*table=*/false);
      if (violation) return;
    }
    if (expression.GetExpressionClass() == dd::ExpressionClass::SUBQUERY) {
      auto& subquery = expression.Cast<dd::SubqueryExpression>().SubqueryMutable();
      if (subquery && subquery->node) WalkNode(*subquery->node);
    }
    if (violation) return;
    dd::ParsedExpressionIterator::EnumerateChildren(
        expression, [this](dd::unique_ptr<dd::ParsedExpression>& child) {
          if (child) CheckExpression(*child);
        });
  }

  void CheckTableFunction(const dd::TableFunctionRef& ref) {
    if (!ref.function || ref.function->GetExpressionClass() != dd::ExpressionClass::FUNCTION) {
      return;
    }
    const auto& call = ref.function->Cast<dd::FunctionExpression>();
    const std::string name = Lower(Str(call.FunctionName()));
    // DuckDB evaluates a table function's arguments while it binds the call.
    for (const auto& argument : call.GetArguments()) BindTimeExpression(argument.GetExpression());
    if (violation) return;
    // A user's table macro (also one named like a built-in, which it would shadow).
    WalkMacros(call, /*table=*/true);
    if (violation) return;
    if (!scopes_.empty() && (name == "query" || name == "query_table")) {
      MacroQuery(call, name);
    } else if (AlwaysGated(name) || IsRemoteQueryFunction(name) || name == "query" ||
        name == "query_table") {
      Flag(name + "()");
    } else if (ducklake_call_ && ducklake_call_(CallOf(ref.function->Cast<dd::FunctionExpression>(), wrapper_))) {
      // A DuckLake call, checked against the catalog_access rules by the callback.
    } else if (ddb::IsFileReadingTableFunction(name)) {
      Storage({name + "()", false, Strings(FirstPositional(call))}, name + "() (files and URLs)");
    } else if (name == "checkpoint" || name == "force_checkpoint") {
      Flag("CHECKPOINT");
    } else if (table_function_allowed_) {
      // With its qualification: a table macro in a schema (tools.first_n(3)) is found there.
      std::vector<std::string> parts;
      for (const auto& part : ref.function->Cast<dd::FunctionExpression>().GetQualifiedName().Path()) {
        if (!Str(part).empty()) parts.push_back(Str(part));
      }
      if (parts.empty()) parts.push_back(name);
      parts.back() = name;  // folded, as the allowed list holds it
      const auto declared = table_function_allowed_(parts);
      if (!declared) {
        // In a macro's body, say so (the caller named only the macro).
        Flag("the table function " + name + (macro_names_.empty() ? "" : kInMacro + macro_names_.back()));
        return;
      }
      // A function that reads through its own client reads what its table's metadata names:
      // like a computed path, whatever path it is handed. (A user's table macro named like one is
      // a macro: its body, walked above, decides.)
      if (ddb::ReadsThroughItsOwnClient(name) && !IsUserTableMacro(call)) {
        if (storage_) storage_({name + "()", false, std::nullopt, false, true});
        else Flag(name + "() (files and URLs)");
        return;
      }
      // The paths an EXECUTE grant declared, as written, before binding opens them.
      auto& function = ref.function->Cast<dd::FunctionExpression>();
      for (const auto& argument : *declared) {
        if (const auto* expression = DeclaredArgument(function, argument)) {
          Storage({name + "()", false, Strings(expression)}, name + "() (files and URLs)");
        }
      }
    }
  }

  const std::function<bool(const std::string&)>& permitted_;
  const NamedTableCallback& named_table_;
  const TableFunctionCallback& table_function_allowed_;
  const StorageCallback& storage_;
  const PathParameterCallback& parameters_;
  const DuckLakeCallCallback& ducklake_call_;
  const BindTimeCallbacks& bind_time_;
  std::string wrapper_;
  int describing_ = 0;  // inside a DESCRIBE
  std::vector<MacroScope> scopes_;  // the macros whose bodies are being walked, innermost last
  std::vector<std::string> macro_names_;  // and their names
  std::size_t level_ = 0;           // the scope the expressions walked now belong to (0: the statement's)

 public:
  // Whether `call` names a user's table macro (not a table function).
  bool IsUserTableMacro(const dd::FunctionExpression& call) {
    return bind_time_.macros && !bind_time_.macros(PartsOf(call), true).empty();
  }

  // CALL f(...): f's arguments, evaluated while DuckDB binds it (subqueries in them are
  // planned, and walked like any other), and the body of f when it is a user's table macro.
  void CallBindTime(const dd::FunctionExpression& call) {
    for (const auto& argument : call.GetArguments()) {
      BindTimeExpression(argument.GetExpression());
      CheckExpression(const_cast<dd::ParsedExpression&>(argument.GetExpression()));
    }
    WalkMacros(call, /*table=*/true);
  }

  // COPY's file name and options, which DuckDB evaluates while it binds.
  void CopyBindTime(const dd::CopyInfo& info) {
    if (info.file_path_expression) BindTimeExpression(*info.file_path_expression);
    for (const auto& option : info.parsed_options) {
      if (option.second) BindTimeExpression(*option.second);
    }
  }
};

std::optional<std::string> Classify(dd::SQLStatement& statement,
                                    const std::function<bool(const std::string&)>& permitted,
                                    const NamedTableCallback& named_table,
                                    const TableFunctionCallback& table_function_allowed,
                                    const StorageCallback& storage, const PathParameterCallback& parameters,
                                    const DuckLakeCallCallback& ducklake_call, const BindTimeCallbacks& bind_time) {
  Walker walker(permitted, named_table, table_function_allowed, storage, parameters, ducklake_call, bind_time);
  const auto again = [&](dd::SQLStatement& inner) {
    return Classify(inner, permitted, named_table, table_function_allowed, storage, parameters, ducklake_call, bind_time);
  };
  walker.classify_ = [&](dd::SQLStatement& inner) {
    return Classify(inner, permitted, named_table, table_function_allowed, storage, nullptr, ducklake_call, bind_time);
  };
  auto gate = [&](std::string category) -> std::optional<std::string> {
    if (permitted && permitted(category)) return std::nullopt;
    return category;
  };
  switch (statement.type) {
    case dd::StatementType::PREPARE_STATEMENT: {
      auto& prepare = statement.Cast<dd::PrepareStatement>();
      return prepare.statement ? again(*prepare.statement) : std::nullopt;
    }
    case dd::StatementType::EXPLAIN_STATEMENT: {
      // EXPLAIN ANALYZE runs what it wraps.
      auto& explain = statement.Cast<dd::ExplainStatement>();
      return explain.stmt ? again(*explain.stmt) : std::nullopt;
    }
    // Several statements DuckDB made of one (PIVOT without an IN list: a CREATE TYPE ... AS
    // ENUM of the values, then the query): each is bound in turn.
    case dd::StatementType::MULTI_STATEMENT: {
      for (auto& inner : statement.Cast<dd::MultiStatement>().statements) {
        if (!inner) continue;
        if (auto category = again(*inner)) return category;
      }
      return std::nullopt;
    }
    // EXECUTE evaluates its values while it binds; the prepared statement was gated when it
    // was prepared.
    case dd::StatementType::EXECUTE_STATEMENT: {
      for (auto& value : statement.Cast<dd::ExecuteStatement>().named_values) {
        if (!value.second) continue;
        walker.BindTimeExpression(*value.second);
        walker.WalkExpression(*value.second);
      }
      return walker.violation;
    }
    // Nothing in these is evaluated while binding, nor reaches outside the database: the plan
    // check decides them.
    case dd::StatementType::TRANSACTION_STATEMENT:
    case dd::StatementType::ANALYZE_STATEMENT:
    case dd::StatementType::VACUUM_STATEMENT:
    case dd::StatementType::ALTER_STATEMENT:
    case dd::StatementType::COPY_DATABASE_STATEMENT:
    // The plan check asks MANAGE EXTENSIONS of it; it names extensions, nothing evaluated.
    case dd::StatementType::UPDATE_EXTENSIONS_STATEMENT:
      return std::nullopt;
    case dd::StatementType::ATTACH_STATEMENT:
      return gate("ATTACH");
    case dd::StatementType::DETACH_STATEMENT:
      return gate("DETACH");
    // CONNECT attaches its target and forwards the session's raw SQL to it;
    // PASSTHROUGH is raw SQL for that target; EXTERNAL RESOURCE provisions compute.
    case dd::StatementType::CONNECT_STATEMENT:
      return gate("CONNECT");
    case dd::StatementType::DISCONNECT_STATEMENT:
      return gate("DISCONNECT");
    case dd::StatementType::EXTERNAL_RESOURCE_STATEMENT:
      return gate("EXTERNAL RESOURCE");
    case dd::StatementType::PASSTHROUGH_STATEMENT:
      return gate("passthrough SQL");
    case dd::StatementType::CREATE_STATEMENT: {
      auto& create = statement.Cast<dd::CreateStatement>();
      if (create.info && create.info->type == dd::CatalogType::SECRET_ENTRY) {
        return gate("CREATE SECRET");
      }
      if (create.info && create.info->type == dd::CatalogType::TABLE_ENTRY) {
        auto& table = create.info->Cast<dd::CreateTableInfo>();
        if (table.query && table.query->node) walker.WalkNode(*table.query->node);
      }
      // A view's query is bound when it is created (and checked when it is read).
      if (create.info && create.info->type == dd::CatalogType::VIEW_ENTRY) {
        auto& view = create.info->Cast<dd::CreateViewInfo>();
        walker.SetWrapper("a view");
        if (view.query && view.query->node) walker.WalkNode(*view.query->node);
      }
      // A trigger's body, as its creator could run it: it is bound again inside every
      // statement that fires the trigger, so it may call only what its creator may call,
      // as a macro's body (below). A type's enum query runs when the type is created.
      if (create.info && create.info->type == dd::CatalogType::TRIGGER_ENTRY) {
        auto& trigger = create.info->Cast<dd::CreateTriggerInfo>();
        walker.SetWrapper("a trigger");
        if (trigger.trigger_action) walker.WalkNode(*trigger.trigger_action);
      }
      if (create.info && create.info->type == dd::CatalogType::TYPE_ENTRY) {
        auto& type = create.info->Cast<dd::CreateTypeInfo>();
        if (type.query && type.query->type == dd::StatementType::SELECT_STATEMENT) {
          auto& select = type.query->Cast<dd::SelectStatement>();
          if (select.node) walker.WalkNode(*select.node);
        }
      }
      // A macro's body, as its creator could run it: DuckDB binds the body only where
      // the macro is called, and binding a table function already reads (a CSV's
      // sniffing), before the plan check of that statement sees it. So a body may call
      // only what its creator may call: no query() or remote queries, and files, URLs
      // and object storage only within the creator's storage privileges.
      if (create.info && (create.info->type == dd::CatalogType::MACRO_ENTRY ||
                          create.info->type == dd::CatalogType::TABLE_MACRO_ENTRY)) {
        walker.SetWrapper("a macro");
        for (auto& macro : create.info->Cast<dd::CreateMacroInfo>().macros) {
          if (!macro) continue;
          for (auto& parameter : macro->default_parameters) {
            if (parameter.second) walker.WalkExpression(*parameter.second);
          }
          if (macro->type == dd::MacroType::SCALAR_MACRO) {
            auto& scalar = macro->Cast<dd::ScalarMacroFunction>();
            if (scalar.expression) walker.WalkExpression(*scalar.expression);
          } else if (macro->type == dd::MacroType::TABLE_MACRO) {
            auto& table = macro->Cast<dd::TableMacroFunction>();
            if (table.query_node) walker.WalkNode(*table.query_node);
          }
        }
      }
      return walker.violation;
    }
    case dd::StatementType::DROP_STATEMENT: {
      auto& drop = statement.Cast<dd::DropStatement>();
      if (drop.info && drop.info->type == dd::CatalogType::SECRET_ENTRY) return gate("DROP SECRET");
      return std::nullopt;
    }
    case dd::StatementType::LOAD_STATEMENT: {
      auto& load = statement.Cast<dd::LoadStatement>();
      if (load.info && load.info->load_type == dd::LoadType::CREATE_REPOSITORY) {
        return gate("CREATE EXTENSION REPOSITORY");
      }
      if (load.info && load.info->load_type == dd::LoadType::DROP_REPOSITORY) {
        return gate("DROP EXTENSION REPOSITORY");
      }
      return gate(load.info && load.info->load_type == dd::LoadType::LOAD ? "LOAD extension"
                                                                          : "INSTALL extension");
    }
    case dd::StatementType::SET_STATEMENT: {
      auto& set = statement.Cast<dd::SetStatement>();
      const std::string name = Lower(Str(set.name));
      const std::string verb = set.set_type == dd::SetType::RESET ? "RESET GLOBAL " : "SET GLOBAL ";
      if (set.scope == dd::SetScope::GLOBAL ||
          (set.scope == dd::SetScope::AUTOMATIC && IsInstanceSetting(name))) {
        return gate(verb + Str(set.name));
      }
      if (set.set_type != dd::SetType::RESET && IsGrammarSetting(name)) {
        return gate("SET " + Str(set.name));
      }
      // SET x = <value> and SET VARIABLE v = <value>: a setting's value is evaluated while
      // DuckDB binds it; a variable's is planned (and checked) like a query, whose table
      // functions bind first.
      if (auto* variable = dynamic_cast<dd::SetVariableStatement*>(&set); variable && variable->value) {
        if (set.scope != dd::SetScope::VARIABLE) walker.BindTimeExpression(*variable->value);
        walker.WalkExpression(*variable->value);
        return walker.violation;
      }
      return std::nullopt;
    }
    case dd::StatementType::CALL_STATEMENT: {
      auto& call = statement.Cast<dd::CallStatement>();
      if (call.function && call.function->GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
        const std::string name =
            Lower(Str(call.function->Cast<dd::FunctionExpression>().FunctionName()));
        if (name == "checkpoint" || name == "force_checkpoint") return gate("CHECKPOINT");
        if (IsRemoteQueryFunction(name) || AlwaysGated(name) || name == "query" ||
            name == "query_table") {
          return gate(name + "()");
        }
        // CALL f(...) binds f as SELECT * FROM f(...) does: its arguments are evaluated, and a
        // table macro's body is bound with them (a DuckLake call's too).
        walker.CallBindTime(call.function->Cast<dd::FunctionExpression>());
        if (walker.violation) return walker.violation;
        if (ducklake_call && ducklake_call(CallOf(call.function->Cast<dd::FunctionExpression>(), ""))) {
          return std::nullopt;  // a DuckLake call, checked against the catalog_access rules
        }
        if (ddb::IsFileReadingTableFunction(name)) {
          if (!storage) return gate(name + "() (files and URLs)");
          storage({name + "()", false, LiteralPaths(FirstPositional(call.function->Cast<dd::FunctionExpression>()), parameters)});
          return std::nullopt;
        }
        if (table_function_allowed) {
          const auto declared = table_function_allowed({name});
          if (!declared) return gate("the table function " + name);
          if (ddb::ReadsThroughItsOwnClient(name) && !walker.IsUserTableMacro(call.function->Cast<dd::FunctionExpression>())) {
            if (!storage) return gate(name + "() (files and URLs)");
            storage({name + "()", false, std::nullopt, false, true});
            return std::nullopt;
          }
          auto& function = call.function->Cast<dd::FunctionExpression>();
          for (const auto& argument : *declared) {
            const auto* expression = DeclaredArgument(function, argument);
            if (!expression) continue;
            if (!storage) return gate(name + "() (files and URLs)");
            storage({name + "()", false, LiteralPaths(expression, parameters)});
          }
        }
      }
      return std::nullopt;
    }
    case dd::StatementType::COPY_STATEMENT: {
      auto& copy = statement.Cast<dd::CopyStatement>();
      if (!copy.info) return gate("COPY");
      if (!storage) {
        if (auto category = gate(std::string(copy.info->is_from ? "COPY FROM" : "COPY TO") +
                                 " (files and URLs)")) {
          return category;
        }
      } else {
        storage({copy.info->is_from ? "COPY ... FROM" : "COPY ... TO", !copy.info->is_from, CopyPaths(*copy.info, parameters),
                 !copy.info->is_from});
      }
      walker.CopyBindTime(*copy.info);
      if (copy.info->select_statement) walker.WalkNode(*copy.info->select_statement);
      return walker.violation;
    }
    case dd::StatementType::EXPORT_STATEMENT: {
      // The whole database, not bounded by grants.
      if (auto category = gate("EXPORT DATABASE")) return category;
      auto& exported = statement.Cast<dd::ExportStatement>();
      if (storage) {
        storage({"EXPORT DATABASE", true,
                 exported.info ? CopyPaths(*exported.info, parameters) : std::optional<std::vector<std::string>>{}, true});
      }
      return std::nullopt;
    }
    case dd::StatementType::PRAGMA_STATEMENT: {
      auto& pragma = statement.Cast<dd::PragmaStatement>();
      // PRAGMA json_execute_serialized_sql('<plan>') binds and runs a statement the text does
      // not hold, as the table function of that name does (which stays the administrator's).
      if (pragma.info && Lower(Str(pragma.info->name)) == "json_execute_serialized_sql") {
        return gate("json_execute_serialized_sql()");
      }
      // A pragma's parameters are evaluated while DuckDB binds it.
      if (pragma.info) {
        for (auto& parameter : pragma.info->parameters) {
          if (!parameter) continue;
          walker.BindTimeExpression(*parameter);
          walker.WalkExpression(*parameter);
        }
        for (auto& parameter : pragma.info->named_parameters) {
          if (!parameter.second) continue;
          walker.BindTimeExpression(*parameter.second);
          walker.WalkExpression(*parameter.second);
        }
        if (walker.violation) return walker.violation;
      }
      if (pragma.info && Lower(Str(pragma.info->name)) == "import_database") {
        if (auto category = gate("IMPORT DATABASE")) return category;
        if (storage) {
          storage({"IMPORT DATABASE", false,
                   pragma.info->parameters.empty() ? std::optional<std::vector<std::string>>{}
                                                   : LiteralPaths(pragma.info->parameters[0].get(), parameters),
                   true});
        }
      }
      return std::nullopt;
    }
    case dd::StatementType::SELECT_STATEMENT: {
      auto& select = statement.Cast<dd::SelectStatement>();
      if (select.node) walker.WalkNode(*select.node);
      return walker.violation;
    }
    case dd::StatementType::INSERT_STATEMENT: {
      auto& insert = statement.Cast<dd::InsertStatement>();
      if (insert.node) walker.WalkNode(*insert.node);
      return walker.violation;
    }
    // Parsed as query nodes, so their FROM / USING sources walk like a SELECT's.
    case dd::StatementType::UPDATE_STATEMENT: {
      auto& update = statement.Cast<dd::UpdateStatement>();
      if (update.node) walker.WalkNode(*update.node);
      return walker.violation;
    }
    case dd::StatementType::DELETE_STATEMENT: {
      auto& del = statement.Cast<dd::DeleteStatement>();
      if (del.node) walker.WalkNode(*del.node);
      return walker.violation;
    }
    case dd::StatementType::MERGE_INTO_STATEMENT: {
      auto& merge = statement.Cast<dd::MergeIntoStatement>();
      if (merge.node) walker.WalkNode(*merge.node);
      return walker.violation;
    }
    // A kind without a rule here (one a later DuckDB adds, an extension's statement, or one
    // no client's SQL produces) is never bound: refused, not left to the plan check.
    default:
      return gate("a statement of a kind access control has no rule for (" +
                  dd::StatementTypeToString(statement.type) + ")");
  }
}

}  // namespace

std::optional<std::string> GatedCategory(dd::SQLStatement& statement,
                                         const std::function<bool(const std::string&)>& permitted,
                                         const NamedTableCallback& named_table,
                                         const TableFunctionCallback& table_function_allowed,
                                         const StorageCallback& storage, const PathParameterCallback& parameters,
                                         const DuckLakeCallCallback& ducklake_call, const BindTimeCallbacks& bind_time) {
  return Classify(statement, permitted, named_table, table_function_allowed, storage, parameters, ducklake_call, bind_time);
}

}  // namespace hardcharger::authz
