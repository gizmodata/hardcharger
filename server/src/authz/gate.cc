#include "authz/gate.hpp"

#include <algorithm>
#include <cctype>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/expression/subquery_expression.hpp>
#include <duckdb/parser/parsed_data/create_table_info.hpp>
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
#include <duckdb/parser/statement/explain_statement.hpp>
#include <duckdb/parser/statement/insert_statement.hpp>
#include <duckdb/parser/statement/load_statement.hpp>
#include <duckdb/parser/statement/merge_into_statement.hpp>
#include <duckdb/parser/statement/pragma_statement.hpp>
#include <duckdb/parser/statement/prepare_statement.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/parser/statement/set_statement.hpp>
#include <duckdb/parser/statement/update_statement.hpp>
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
bool IsQuackFunction(const std::string& name) { return name.rfind("quack_", 0) == 0; }

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

class Walker {
 public:
  Walker(const std::function<bool(const std::string&)>& permitted,
         const NamedTableCallback& named_table)
      : permitted_(permitted), named_table_(named_table) {}

  void NamedTable(const dd::QualifiedName& name) {
    if (!named_table_ || name.Path().empty()) return;
    std::vector<std::string> parts;
    for (const auto& part : name.Path()) {
      if (!Str(part).empty()) parts.push_back(Str(part));
    }
    if (!parts.empty()) named_table_(parts);
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
        Flag(std::string(copy.info->is_from ? "COPY FROM" : "COPY TO") + " (files and URLs)");
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
    if (ref.type == dd::TableReferenceType::TABLE_FUNCTION) {
      auto& function = ref.Cast<dd::TableFunctionRef>();
      CheckTableFunction(function);
      // A table-in/out function's subquery argument is not enumerated for us.
      if (function.subquery && function.subquery->node) WalkNode(*function.subquery->node);
    } else if (ref.type == dd::TableReferenceType::BASE_TABLE) {
      const auto& name = ref.Cast<dd::BaseTableRef>().GetQualifiedName();
      if (name.Path().size() == 1 && LooksLikePath(Str(name.Name()))) {
        Flag("reading files and URLs via FROM '" + Str(name.Name()) + "'");
        return;
      }
      NamedTable(name);
    } else if (ref.type == dd::TableReferenceType::SHOW_REF) {
      // DESCRIBE / SHOW / SUMMARIZE: of a query, or of a table by name.
      auto& show = ref.Cast<dd::ShowRef>();
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
      const std::string name = Lower(Str(expression.Cast<dd::FunctionExpression>().FunctionName()));
      if (IsQuackFunction(name)) {
        Flag(name + "()");
        return;
      }
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
    const std::string name =
        Lower(Str(ref.function->Cast<dd::FunctionExpression>().FunctionName()));
    if (AlwaysGated(name) || IsQuackFunction(name) || name == "query" || name == "query_table") {
      Flag(name + "()");
    } else if (ddb::IsFileReadingTableFunction(name)) {
      Flag(name + "() (files and URLs)");
    }
  }

  const std::function<bool(const std::string&)>& permitted_;
  const NamedTableCallback& named_table_;
};

std::optional<std::string> Classify(dd::SQLStatement& statement,
                                    const std::function<bool(const std::string&)>& permitted,
                                    const NamedTableCallback& named_table) {
  Walker walker(permitted, named_table);
  auto gate = [&](std::string category) -> std::optional<std::string> {
    if (permitted && permitted(category)) return std::nullopt;
    return category;
  };
  switch (statement.type) {
    case dd::StatementType::PREPARE_STATEMENT: {
      auto& prepare = statement.Cast<dd::PrepareStatement>();
      return prepare.statement ? Classify(*prepare.statement, permitted, named_table)
                               : std::nullopt;
    }
    case dd::StatementType::EXPLAIN_STATEMENT: {
      // EXPLAIN ANALYZE runs what it wraps.
      auto& explain = statement.Cast<dd::ExplainStatement>();
      return explain.stmt ? Classify(*explain.stmt, permitted, named_table) : std::nullopt;
    }
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
        if (view.query && view.query->node) walker.WalkNode(*view.query->node);
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
      return std::nullopt;
    }
    case dd::StatementType::CALL_STATEMENT: {
      auto& call = statement.Cast<dd::CallStatement>();
      if (call.function && call.function->GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
        const std::string name =
            Lower(Str(call.function->Cast<dd::FunctionExpression>().FunctionName()));
        if (name == "checkpoint" || name == "force_checkpoint") return gate("CHECKPOINT");
        if (IsQuackFunction(name) || AlwaysGated(name) || name == "query" ||
            name == "query_table") {
          return gate(name + "()");
        }
        if (ddb::IsFileReadingTableFunction(name)) return gate(name + "() (files and URLs)");
      }
      return std::nullopt;
    }
    case dd::StatementType::COPY_STATEMENT: {
      auto& copy = statement.Cast<dd::CopyStatement>();
      if (!copy.info) return gate("COPY");
      if (auto category = gate(std::string(copy.info->is_from ? "COPY FROM" : "COPY TO") +
                               " (files and URLs)")) {
        return category;
      }
      if (copy.info->select_statement) walker.WalkNode(*copy.info->select_statement);
      return walker.violation;
    }
    case dd::StatementType::EXPORT_STATEMENT:
      // The whole database, not bounded by grants.
      return gate("EXPORT DATABASE");
    case dd::StatementType::PRAGMA_STATEMENT: {
      auto& pragma = statement.Cast<dd::PragmaStatement>();
      if (pragma.info && Lower(Str(pragma.info->name)) == "import_database") {
        return gate("IMPORT DATABASE");
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
    default:
      return std::nullopt;
  }
}

}  // namespace

std::optional<std::string> GatedCategory(dd::SQLStatement& statement,
                                         const std::function<bool(const std::string&)>& permitted,
                                         const NamedTableCallback& named_table) {
  return Classify(statement, permitted, named_table);
}

}  // namespace hardcharger::authz
