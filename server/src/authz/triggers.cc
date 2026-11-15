#include "authz/triggers.hpp"

#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/common/enums/trigger_type.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/catalog/catalog_entry/sequence_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry_retriever.hpp>
#include <duckdb/function/scalar/sequence_utils.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/expression/function_expression.hpp>
#include <duckdb/parser/expression/subquery_expression.hpp>
#include <duckdb/parser/parsed_expression_iterator.hpp>
#include <duckdb/parser/query_node/delete_query_node.hpp>
#include <duckdb/parser/query_node/insert_query_node.hpp>
#include <duckdb/parser/query_node/update_query_node.hpp>
#include <duckdb/parser/statement/select_statement.hpp>
#include <duckdb/parser/tableref/basetableref.hpp>
#include <duckdb/parser/tableref/subqueryref.hpp>
#include <duckdb/parser/tableref/table_function_ref.hpp>
#include <duckdb/planner/binder.hpp>
#include <duckdb/planner/expression/bound_function_expression.hpp>
#include <duckdb/planner/expression_iterator.hpp>
#include <duckdb/planner/logical_operator_visitor.hpp>
#include <duckdb/planner/operator/logical_delete.hpp>
#include <duckdb/planner/operator/logical_insert.hpp>
#include <duckdb/planner/operator/logical_materialized_cte.hpp>
#include <duckdb/planner/operator/logical_merge_into.hpp>
#include <duckdb/planner/operator/logical_update.hpp>
#include <duckdb/planner/constraints/bound_check_constraint.hpp>
#include <duckdb/planner/expression/bound_columnref_expression.hpp>
#include <duckdb/planner/expression_iterator.hpp>
#include <duckdb/planner/logical_operator_visitor.hpp>
#include <duckdb/planner/operator/logical_cteref.hpp>
#include <duckdb/planner/operator/logical_dependent_join.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <algorithm>
#include <duckdb/planner/operator/logical_recursive_cte.hpp>
#include <duckdb/planner/operator/logical_trigger.hpp>
#include <duckdb/storage/table_storage_info.hpp>
#include <functional>
#include <map>
#include <set>
#include <unordered_map>
#include <unordered_set>

#include "authz/duckdb_internal.hpp"
#include "authz/resolver.hpp"
#include "authz/views.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;
namespace compat = hardcharger::authz::ddb;
using OpType = dd::LogicalOperatorType;

constexpr const char* kBasePrefix = "__duckdb_trigger_base_";
constexpr const char* kBodyPrefix = "__duckdb_trigger_body_";
constexpr const char* kBeforeBodyPrefix = "__duckdb_trigger_before_body_";
constexpr const char* kRowBodyPrefix = "__duckdb_trigger_body_row_";

// The writer's own statement under a base CTE: its table, event and (UPDATE) the columns it sets.
struct TriggerBase {
  dd::TableCatalogEntry* table = nullptr;
  dd::TriggerEventType event = dd::TriggerEventType::INSERT_EVENT;
  std::vector<std::string> updated_columns;
  const dd::LogicalOperator* dml = nullptr;
  const dd::LogicalOperator* cte = nullptr;  // the base CTE itself
  bool returning_to_user = false;
  std::string problem;
};

std::string CteName(const dd::LogicalOperator& op) {
  if (op.type != OpType::LOGICAL_MATERIALIZED_CTE) return "";
  return compat::Str(op.Cast<dd::LogicalMaterializedCTE>().ctename);
}

// "<prefix><rest>": the rest, or nullopt.
std::optional<std::string> After(const std::string& name, const char* prefix) {
  const std::string p = prefix;
  if (name.rfind(p, 0) != 0) return std::nullopt;
  return name.substr(p.size());
}

// "<n>_<uuid>": the ordinal and the uuid.
bool SplitOrdinal(const std::string& rest, size_t& ordinal, std::string& uuid) {
  const auto underscore = rest.find('_');
  if (underscore == std::string::npos || underscore == 0) return false;
  try {
    ordinal = static_cast<size_t>(std::stoul(rest.substr(0, underscore)));
  } catch (...) {
    return false;
  }
  uuid = rest.substr(underscore + 1);
  return ordinal > 0 && !uuid.empty();
}

// The table a DML operator writes, or nullptr.
dd::TableCatalogEntry* WrittenTable(const dd::LogicalOperator& op) {
  switch (op.type) {
    case OpType::LOGICAL_INSERT:
      return &op.Cast<dd::LogicalInsert>().table;
    case OpType::LOGICAL_DELETE:
      return &op.Cast<dd::LogicalDelete>().table;
    case OpType::LOGICAL_UPDATE:
      return &op.Cast<dd::LogicalUpdate>().table;
    case OpType::LOGICAL_MERGE_INTO:
      return &op.Cast<dd::LogicalMergeInto>().table;
    default:
      return nullptr;
  }
}

// The columns the statement's own SET list names. The binder's trigger chain binds the base
// UPDATE over every column of the table, each taking its value from the projection below:
// a column the statement did not set takes its own value (a plain reference to the table's
// column), the ones it set take an expression. (SET c = c reads as unchanged here; the
// binder fires its UPDATE OF c triggers all the same, and the body count then no longer
// matches, which refuses the statement rather than misattribute a body.) False when the
// plan doesn't have that shape.
bool UpdatedColumns(const dd::LogicalUpdate& update, std::vector<std::string>& names) {
  if (update.children.size() != 1 || !update.children[0] || update.children[0]->type != OpType::LOGICAL_PROJECTION) {
    return false;
  }
  const auto& projection = *update.children[0];
  // The scan of the table below the projection (under filters, or a join for UPDATE ... FROM).
  const dd::LogicalGet* get = nullptr;
  std::function<void(const dd::LogicalOperator&)> find = [&](const dd::LogicalOperator& node) {
    if (get) return;
    if (node.type == OpType::LOGICAL_GET && node.Cast<dd::LogicalGet>().GetTable().get() == &update.table) {
      get = &node.Cast<dd::LogicalGet>();
      return;
    }
    for (const auto& child : node.children) {
      if (child) find(*child);
    }
  };
  find(projection);
  if (!get || update.columns.size() > projection.expressions.size()) return false;
  const auto& column_ids = get->GetColumnIds();
  for (size_t i = 0; i < update.columns.size(); ++i) {
    const auto& column = update.table.GetColumns().GetColumn(update.columns[i]);
    const auto& expression = *projection.expressions[i];
    bool own_value = false;
    if (expression.GetExpressionClass() == dd::ExpressionClass::BOUND_COLUMN_REF) {
      const auto& binding = expression.Cast<dd::BoundColumnRefExpression>().Binding();
      const size_t index = binding.column_index.GetIndex();
      if (binding.table_index == get->table_index && index < column_ids.size() && !column_ids[index].IsRowIdColumn() &&
          !column_ids[index].IsVirtualColumn() && column_ids[index].GetPrimaryIndex() == column.Logical().index) {
        own_value = true;
      }
    }
    if (!own_value) names.push_back(compat::Str(column.Name()));
  }
  return true;
}

// Does the statement hand the base DML's RETURNING rows to the writer? The binder
// consumes the base CTE with SELECT count_star() unless the statement had a RETURNING
// clause of its own (then SELECT *), below the trigger bodies' CTEs.
bool ReturnsToUser(const dd::LogicalOperator& cte) {
  const dd::LogicalOperator* consumer = cte.children.size() > 1 ? cte.children[1].get() : nullptr;
  for (int depth = 0; consumer && depth < 64; ++depth) {
    if (!After(CteName(*consumer), "__duckdb_trigger_")) break;
    consumer = consumer->children.size() > 1 ? consumer->children[1].get() : nullptr;
  }
  // SELECT count_star() binds as a projection over an aggregate; SELECT * as a projection
  // over the CTE's rows.
  for (int depth = 0; consumer && consumer->type == OpType::LOGICAL_PROJECTION && consumer->children.size() == 1 &&
                      depth < 64;
       ++depth) {
    consumer = consumer->children[0].get();
  }
  return !consumer || consumer->type != OpType::LOGICAL_AGGREGATE_AND_GROUP_BY;
}

// The INSERT, UPDATE, DELETE and MERGE operators under `op`, never looking into a CTE's
// definition (a WITH of the statement's own, the alias of a transition table, a chain
// nested in a trigger's body) nor below one of them: a statement's own write sits under
// its WITH clauses, and, where the statement is itself a trigger's body, under the joins
// DuckDB leaves when it decorrelates NEW and OLD.
void CollectWrites(const dd::LogicalOperator& op, std::vector<const dd::LogicalOperator*>& writes) {
  if (WrittenTable(op)) {
    writes.push_back(&op);
    return;
  }
  const size_t first = op.type == OpType::LOGICAL_MATERIALIZED_CTE ? 1 : 0;
  for (size_t i = first; i < op.children.size(); ++i) {
    if (op.children[i]) CollectWrites(*op.children[i], writes);
  }
}

// The statement under a base CTE's definition. `in_body`: the chain is the body of another
// trigger, whose result DuckDB discards, so nothing the statement returns reaches the writer.
TriggerBase BaseOf(const dd::LogicalOperator& cte, bool in_body) {
  TriggerBase base;
  base.returning_to_user = !in_body && ReturnsToUser(cte);
  std::vector<const dd::LogicalOperator*> writes;
  if (!cte.children.empty() && cte.children[0]) CollectWrites(*cte.children[0], writes);
  if (writes.size() != 1) {
    base.problem = "the statement a trigger fires on could not be found in the plan; ask an administrator";
    return base;
  }
  const dd::LogicalOperator* op = writes[0];
  base.dml = op;
  switch (op->type) {
    case OpType::LOGICAL_INSERT:
      base.table = &op->Cast<dd::LogicalInsert>().table;
      base.event = dd::TriggerEventType::INSERT_EVENT;
      break;
    case OpType::LOGICAL_MERGE_INTO:
      // INSERT ... ON CONFLICT (INSERT OR IGNORE, INSERT OR REPLACE): DuckDB runs it as a
      // merge and fires the table's INSERT triggers. A MERGE INTO statement never gets
      // here: DuckDB refuses one on a table with triggers.
      base.table = &op->Cast<dd::LogicalMergeInto>().table;
      base.event = dd::TriggerEventType::INSERT_EVENT;
      break;
    case OpType::LOGICAL_DELETE:
      base.table = &op->Cast<dd::LogicalDelete>().table;
      base.event = dd::TriggerEventType::DELETE_EVENT;
      break;
    case OpType::LOGICAL_UPDATE: {
      auto& update = op->Cast<dd::LogicalUpdate>();
      base.table = &update.table;
      base.event = dd::TriggerEventType::UPDATE_EVENT;
      if (!UpdatedColumns(update, base.updated_columns)) {
        base.problem = "the columns an UPDATE with triggers sets could not be read from the plan; ask an administrator";
      }
      break;
    }
    default:
      base.dml = nullptr;
      base.problem = "the statement a trigger fires on could not be found in the plan; ask an administrator";
      break;
  }
  return base;
}

// Is this the CTE of a trigger's body (statement or row)?
bool IsBodyCte(const std::string& name) {
  return After(name, kBodyPrefix).has_value() || After(name, kBeforeBodyPrefix).has_value();
}

void CollectBases(const dd::LogicalOperator& op, std::map<std::string, TriggerBase>& bases, bool in_body) {
  const std::string name = CteName(op);
  if (auto uuid = After(name, kBasePrefix)) {
    bases[*uuid] = BaseOf(op, in_body);
    bases[*uuid].cte = &op;
  }
  for (size_t i = 0; i < op.children.size(); ++i) {
    // A body CTE's definition (its first child) is the body.
    if (op.children[i]) CollectBases(*op.children[i], bases, in_body || (i == 0 && IsBodyCte(name)));
  }
}

// The statement trigger the binder expanded `ordinal`-th (from 1) for `timing`.
dd::optional_ptr<dd::TriggerCatalogEntry> StatementTrigger(dd::ClientContext& context, const TriggerBase& base,
                                                           dd::TriggerTiming timing, size_t ordinal) {
  auto txn = base.table->ParentCatalog().GetCatalogTransaction(context);
  auto triggers = base.table->GetTriggersForEvent(txn, timing, base.event, dd::TriggerForEach::STATEMENT);
  std::vector<dd::TriggerCatalogEntry*> firing;
  for (auto& trigger : triggers) {
    const auto& of = trigger.get().columns;
    if (base.event == dd::TriggerEventType::UPDATE_EVENT && !of.empty()) {
      bool fires = false;
      for (const auto& column : of) {
        for (const auto& updated : base.updated_columns) {
          fires = fires || IdentifiersEqual(compat::Str(column), updated);
        }
      }
      if (!fires) continue;
    }
    firing.push_back(const_cast<dd::TriggerCatalogEntry*>(&trigger.get()));
  }
  if (ordinal > firing.size()) return nullptr;
  return firing[ordinal - 1];
}

// The table a trigger's action writes, as the binder of the firing statement resolves it.
dd::optional_ptr<dd::CatalogEntry> ActionTarget(dd::ClientContext& context, const dd::TriggerCatalogEntry& trigger) {
  if (!trigger.trigger_action) return nullptr;
  std::vector<std::string> parts;
  auto take = [&](const dd::QualifiedName& name) {
    for (const auto& part : name.Path()) {
      if (!compat::Str(part).empty()) parts.push_back(compat::Str(part));
    }
  };
  switch (trigger.trigger_action->type) {
    case dd::QueryNodeType::INSERT_QUERY_NODE:
      take(trigger.trigger_action->Cast<dd::InsertQueryNode>().qualified_name);
      break;
    case dd::QueryNodeType::UPDATE_QUERY_NODE: {
      auto& table = trigger.trigger_action->Cast<dd::UpdateQueryNode>().table;
      if (table && table->type == dd::TableReferenceType::BASE_TABLE) take(table->Cast<dd::BaseTableRef>().GetQualifiedName());
      break;
    }
    case dd::QueryNodeType::DELETE_QUERY_NODE: {
      auto& table = trigger.trigger_action->Cast<dd::DeleteQueryNode>().table;
      if (table && table->type == dd::TableReferenceType::BASE_TABLE) take(table->Cast<dd::BaseTableRef>().GetQualifiedName());
      break;
    }
    default:
      break;
  }
  if (parts.empty()) return nullptr;
  try {
    dd::vector<dd::Identifier> path;
    for (const auto& p : parts) path.emplace_back(p);
    return dd::Catalog::GetEntry(context, dd::EntryLookupInfo(dd::CatalogType::TABLE_ENTRY, dd::QualifiedName::FromPath(path)),
                                 dd::OnEntryNotFound::RETURN_NULL);
  } catch (...) {
    return nullptr;
  }
}

TriggerBody BodyOf(const Snapshot& snapshot, const dd::TriggerCatalogEntry& trigger, dd::TableCatalogEntry& table) {
  TriggerBody body;
  body.entries.push_back(&trigger);
  body.table = PathOfEntry(table);
  body.trigger = TriggerOwnerPath(body.table, compat::Str(trigger.name));
  body.describe = "trigger " + QuoteIdentifierIfNeeded(body.trigger.object_name) + " on table " + body.table.ToString();
  const OwnerRecord* owner = snapshot.FindTriggerOwner(body.trigger);
  if (!owner || owner->type != ObjectType::kTrigger || owner->owner_kind == OwnerKind::kSystem) return body;
  // A record of an earlier trigger of this name (dropped, and the name reused by an
  // administrator or the init SQL) must not apply: the definition must match.
  if (!owner->definition_hash.empty()) {
    std::string current;
    try {
      current = TriggerDefinitionHash(trigger);
    } catch (...) {
      current.clear();
    }
    if (current != owner->definition_hash) return body;
  }
  switch (owner->owner_kind) {
    case OwnerKind::kUser: {
      const UserRecord* user = snapshot.FindUserById(owner->owner_id);
      if (!user || !user->enabled) {
        body.problem = body.describe + " cannot run, because its owner is disabled or was dropped";
        return body;
      }
      body.definer = Principal::User(user->id, user->name);
      body.definer_name = "user " + user->name;
      return body;
    }
    case OwnerKind::kRole: {
      const RoleRecord* role = snapshot.FindRoleById(owner->owner_id);
      if (!role) {
        body.problem = body.describe + " cannot run, because its owner was dropped";
        return body;
      }
      body.definer = Principal::Role(role->id, role->name);
      body.definer_name = "role " + role->name;
      return body;
    }
    case OwnerKind::kSystem:
      break;
  }
  return body;
}

TriggerBody Unmatched(const dd::LogicalOperator& root, const std::string& why) {
  TriggerBody body;
  body.root = &root;
  body.describe = "a trigger";
  body.problem = why;
  return body;
}

class Finder {
 public:
  Finder(dd::ClientContext& context, const Snapshot& snapshot) : context_(context), snapshot_(snapshot) {}

  TriggerBodies bodies;
  std::map<std::string, TriggerBase> bases;
  // The CTE the row triggers of a base are bound under (by the base's uuid), and which of
  // `bodies` are those row triggers' bodies (by the base's write).
  std::map<std::string, const dd::LogicalOperator*> row_ctes;
  std::map<const dd::LogicalOperator*, std::vector<size_t>> row_bodies;

  void Walk(const dd::LogicalOperator& op) {
    const std::string name = CteName(op);
    if (auto rest = After(name, kRowBodyPrefix)) {
      // Two CTEs of one name: neither is taken for the place the row triggers sit.
      row_ctes[*rest] = row_ctes.count(*rest) ? nullptr : &op;
      if (!op.children.empty() && op.children[0]) RowBodies(*op.children[0], *rest);
      if (op.children.size() > 1 && op.children[1]) Walk(*op.children[1]);
      return;
    }
    if (auto before = After(name, kBeforeBodyPrefix)) {
      StatementBody(op, *before, dd::TriggerTiming::BEFORE);
    } else if (auto after = After(name, kBodyPrefix)) {
      StatementBody(op, *after, dd::TriggerTiming::AFTER);
    }
    for (const auto& child : op.children) {
      if (child) Walk(*child);
    }
  }

 private:
  const TriggerBase* Base(const std::string& uuid, const dd::LogicalOperator& root) {
    auto found = bases.find(uuid);
    if (found == bases.end()) {
      bodies.push_back(Unmatched(root, "a trigger's body could not be matched to the statement that fires it; "
                                       "ask an administrator"));
      return nullptr;
    }
    if (!found->second.problem.empty() || !found->second.table) {
      bodies.push_back(Unmatched(root, found->second.problem.empty() ? "the table a trigger fires on could not be found"
                                                                     : found->second.problem));
      return nullptr;
    }
    return &found->second;
  }

  void StatementBody(const dd::LogicalOperator& cte, const std::string& rest, dd::TriggerTiming timing) {
    if (cte.children.empty() || !cte.children[0]) return;
    const dd::LogicalOperator& root = *cte.children[0];
    size_t ordinal = 0;
    std::string uuid;
    if (!SplitOrdinal(rest, ordinal, uuid)) {
      bodies.push_back(Unmatched(root, "a trigger's body could not be read from the plan; ask an administrator"));
      return;
    }
    const TriggerBase* base = Base(uuid, root);
    if (!base) return;
    auto trigger = StatementTrigger(context_, *base, timing, ordinal);
    if (!trigger) {
      bodies.push_back(Unmatched(root, "a trigger of table " + PathOfEntry(*base->table).ToString() +
                                           " could not be matched to its body; ask an administrator"));
      return;
    }
    TriggerBody body = BodyOf(snapshot_, *trigger, *base->table);
    body.root = &root;
    bodies.push_back(std::move(body));
  }

  struct Candidate {
    const dd::TriggerCatalogEntry* trigger;
    const dd::CatalogEntry* target;
  };

  // The row triggers' bodies under __duckdb_trigger_body_row_<uuid>: each is the DML on
  // the table its action names. Two of the event's row triggers writing the same table
  // can't be told apart; unless one user owns them all, the statement is refused.
  void RowBodies(const dd::LogicalOperator& plan, const std::string& uuid) {
    const TriggerBase* base = Base(uuid, plan);
    if (!base) return;
    auto txn = base->table->ParentCatalog().GetCatalogTransaction(context_);
    std::vector<Candidate> candidates;
    for (auto& trigger : base->table->GetTriggersForEvent(txn, base->event, dd::TriggerForEach::ROW)) {
      candidates.push_back({&trigger.get(), ActionTarget(context_, trigger.get()).get()});
    }
    MatchRowBodies(plan, *base, candidates);
  }

  void MatchRowBodies(const dd::LogicalOperator& op, const TriggerBase& base, const std::vector<Candidate>& candidates) {
    const std::string name = CteName(op);
    if (auto nested = After(name, kBasePrefix)) {
      // The body writes a table that has statement triggers of its own: the binder put
      // their chain where the body's INSERT, UPDATE or DELETE would be. The whole chain is
      // the body, so the triggers it fires in turn are checked beneath it; they are found
      // like any other (Walk).
      auto chain = bases.find(*nested);
      std::vector<const dd::LogicalOperator*> beside;
      if (op.children.size() > 1 && op.children[1]) CollectWrites(*op.children[1], beside);
      if (chain == bases.end() || !chain->second.problem.empty() || !chain->second.table || !beside.empty()) {
        AddRowBody(base, Unmatched(op, "a row trigger of table " + PathOfEntry(*base.table).ToString() +
                                           " could not be matched to its body; ask an administrator"));
        return;
      }
      RowBody(op, *chain->second.table, base, candidates);
      for (const auto& child : op.children) {
        if (child) Walk(*child);
      }
      return;
    }
    if (After(name, "__duckdb_trigger_")) {
      // Any other CTE of a chain has its place beneath a base CTE, never here.
      AddRowBody(base, Unmatched(op, "a row trigger of table " + PathOfEntry(*base.table).ToString() +
                                         " could not be matched to its body; ask an administrator"));
      return;
    }
    if (dd::TableCatalogEntry* written = WrittenTable(op)) {
      RowBody(op, *written, base, candidates);
      return;  // a body is one DML: nothing of another trigger sits beneath it
    }
    for (const auto& child : op.children) {
      if (child) MatchRowBodies(*child, base, candidates);
    }
  }

  // The body at `root` writes `written`: whose trigger is it?
  void RowBody(const dd::LogicalOperator& root, dd::TableCatalogEntry& written, const TriggerBase& base,
               const std::vector<Candidate>& candidates) {
    std::vector<const dd::TriggerCatalogEntry*> matching;
    for (const auto& candidate : candidates) {
      if (candidate.target && candidate.target == &written) matching.push_back(candidate.trigger);
    }
    if (matching.empty()) {
      AddRowBody(base, Unmatched(root, "a row trigger of table " + PathOfEntry(*base.table).ToString() +
                                           " could not be matched to its body; ask an administrator"));
      return;
    }
    TriggerBody body = BodyOf(snapshot_, *matching[0], *base.table);
    for (size_t i = 1; i < matching.size(); ++i) {
      TriggerBody other = BodyOf(snapshot_, *matching[i], *base.table);
      body.entries.push_back(matching[i]);
      const bool same_owner = other.definer.has_value() == body.definer.has_value() &&
                              (!body.definer || (other.definer->user_id == body.definer->user_id &&
                                                 other.definer->role_id == body.definer->role_id)) &&
                              other.problem == body.problem;
      if (!same_owner) {
        body.problem = "the row triggers " + QuoteIdentifierIfNeeded(body.trigger.object_name) + " and " +
                       QuoteIdentifierIfNeeded(other.trigger.object_name) + " on table " + body.table.ToString() +
                       " both write " + PathOfEntry(written).ToString() +
                       " and have different owners, so their bodies can't be told apart; ask an administrator";
      }
    }
    body.root = &root;
    AddRowBody(base, std::move(body));
  }

  void AddRowBody(const TriggerBase& base, TriggerBody body) {
    row_bodies[base.dml].push_back(bodies.size());
    bodies.push_back(std::move(body));
  }

  dd::ClientContext& context_;
  const Snapshot& snapshot_;
};


// ---- Names in a body (triggers.hpp) ----

// A table, view or sequence a parsed trigger action names, as written, and where: so that
// CREATE TRIGGER can write its full name there.
struct WrittenName {
  std::vector<std::string> parts;
  bool sequence = false;
  bool written_to = false;  // what an INSERT, UPDATE or DELETE writes: never a CTE's name
  dd::BaseTableRef* ref = nullptr;
  dd::InsertQueryNode* insert = nullptr;
  dd::unique_ptr<dd::ParsedExpression>* constant = nullptr;  // a sequence's name, in nextval('...')
};

bool IsSequenceFunctionName(const std::string& name) {
  return name == "nextval" || name == "currval" || name == "setval";
}

// Every table, view and sequence a parsed action names, in subqueries, CTEs, set operations
// and joins too, and the names of its CTEs. Throws on SQL it cannot read to the end.
class ActionNames {
 public:
  std::vector<WrittenName> names;
  std::set<std::string> cte_names;  // folded
  // A sequence function whose sequence is not a plain string: its name can't be read.
  bool unreadable_sequence = false;

  // The names in one expression (a column's default).
  void Default(dd::unique_ptr<dd::ParsedExpression>& expression) {
    if (expression) Expression(expression);
  }

  void Node(dd::QueryNode& node) {
    if (++depth_ > 200) throw dd::InternalException("a trigger's action is nested too deeply to read");
    Declared(node, 0);
    // DuckDB's enumerator descends CTE definitions, set operations, joins, FROM subqueries
    // and an INSERT's source, and throws on a node or a reference of a kind it does not know.
    dd::ParsedExpressionIterator::EnumerateQueryNodeChildren(
        node,
        [this](dd::unique_ptr<dd::ParsedExpression>& child) {
          if (child) Expression(child);
        },
        [this](dd::TableRef& ref) { Ref(ref); });
    --depth_;
  }

 private:
  static std::vector<std::string> Parts(const dd::QualifiedName& name) {
    std::vector<std::string> parts;
    for (const auto& part : name.Path()) {
      if (!compat::Str(part).empty()) parts.push_back(compat::Str(part));
    }
    return parts;
  }

  // What a node declares outside its table references and expressions: its CTEs' names,
  // and (the top node, or a CTE's) the table an INSERT, UPDATE or DELETE writes.
  void Declared(dd::QueryNode& node, int depth) {
    if (depth > 200) throw dd::InternalException("a trigger's action is nested too deeply to read");
    switch (node.type) {
      case dd::QueryNodeType::INSERT_QUERY_NODE: {
        auto& insert = node.Cast<dd::InsertQueryNode>();
        WrittenName name;
        name.parts = Parts(insert.qualified_name);
        name.written_to = true;
        name.insert = &insert;
        if (!name.parts.empty()) names.push_back(std::move(name));
        if (insert.table_ref) written_to_.insert(insert.table_ref.get());
        if (insert.select_statement && insert.select_statement->node) Declared(*insert.select_statement->node, depth + 1);
        break;
      }
      case dd::QueryNodeType::UPDATE_QUERY_NODE:
        if (auto& table = node.Cast<dd::UpdateQueryNode>().table) written_to_.insert(table.get());
        break;
      case dd::QueryNodeType::DELETE_QUERY_NODE:
        if (auto& table = node.Cast<dd::DeleteQueryNode>().table) written_to_.insert(table.get());
        break;
      default:
        break;
    }
    for (auto& entry : node.cte_map.map) {
      cte_names.insert(FoldIdentifier(compat::Str(entry.first)));
      if (entry.second && entry.second->query_node) Declared(*entry.second->query_node, depth + 1);
    }
  }

  void Ref(dd::TableRef& ref) {
    if (ref.type == dd::TableReferenceType::BASE_TABLE) {
      WrittenName name;
      name.ref = &ref.Cast<dd::BaseTableRef>();
      name.parts = Parts(name.ref->GetQualifiedName());
      name.written_to = written_to_.count(&ref) > 0;
      if (!name.parts.empty()) names.push_back(std::move(name));
    } else if (ref.type == dd::TableReferenceType::SUBQUERY) {
      // (The enumerator has walked it; its CTEs' names are ours to note.)
      auto& subquery = ref.Cast<dd::SubqueryRef>().subquery;
      if (subquery && subquery->node) Declared(*subquery->node, 0);
    } else if (ref.type == dd::TableReferenceType::TABLE_FUNCTION) {
      // A table function's subquery argument is not enumerated for us.
      auto& function = ref.Cast<dd::TableFunctionRef>();
      if (function.subquery && function.subquery->node) Node(*function.subquery->node);
    }
  }

  void Expression(dd::unique_ptr<dd::ParsedExpression>& expression) {
    if (++depth_ > 200) throw dd::InternalException("a trigger's action is nested too deeply to read");
    if (expression->GetExpressionClass() == dd::ExpressionClass::SUBQUERY) {
      auto& subquery = expression->Cast<dd::SubqueryExpression>().SubqueryMutable();
      if (subquery && subquery->node) Node(*subquery->node);
    } else if (expression->GetExpressionClass() == dd::ExpressionClass::FUNCTION) {
      auto& function = expression->Cast<dd::FunctionExpression>();
      const auto path = function.GetQualifiedName().Path();
      if (!path.empty() && IsSequenceFunctionName(FoldIdentifier(compat::Str(path.back())))) {
        // nextval('s'): the sequence is the first argument, a string (DuckDB binds it by name).
        dd::unique_ptr<dd::ParsedExpression>* first = nullptr;
        for (auto& argument : function.GetArgumentsMutable()) {
          if (!argument.HasName()) {
            first = &argument.GetExpressionMutable();
            break;
          }
        }
        WrittenName name;
        name.sequence = true;
        if (first && *first && (*first)->GetExpressionClass() == dd::ExpressionClass::CONSTANT) {
          const auto value = compat::ConstantValue((*first)->Cast<dd::ConstantExpression>());
          if (!value.IsNull() && value.type().id() == dd::LogicalTypeId::VARCHAR) {
            for (const auto& part : dd::QualifiedName::ParseComponents(value.ToString())) {
              name.parts.push_back(compat::Str(part));
            }
            name.constant = first;
          }
        }
        if (name.parts.empty()) {
          unreadable_sequence = true;
        } else {
          names.push_back(std::move(name));
        }
      }
    }
    dd::ParsedExpressionIterator::EnumerateChildren(*expression, [this](dd::unique_ptr<dd::ParsedExpression>& child) {
      if (child) Expression(child);
    });
    --depth_;
  }

  std::set<const dd::TableRef*> written_to_;
  int depth_ = 0;
};

// The entry at exactly this place, or nullptr.
dd::optional_ptr<dd::CatalogEntry> EntryAt(dd::ClientContext& context, dd::CatalogType type, const std::string& catalog,
                                           const std::vector<std::string>& schema, const std::string& name) {
  if (schema.empty()) return nullptr;
  dd::vector<dd::Identifier> path;
  for (const auto& part : schema) path.emplace_back(part);
  try {
    return dd::Catalog::GetEntry(
        context,
        dd::EntryLookupInfo(type, dd::QualifiedName::FromCatalogSchema(dd::Identifier(catalog), std::move(path),
                                                                       dd::Identifier(name))),
        dd::OnEntryNotFound::RETURN_NULL);
  } catch (const std::exception&) {
    return nullptr;  // no such schema there: nothing by that name
  }
}

// What a name is for the trigger's own table, whatever the session ("canonical",
// triggers.hpp). `ambiguous`: it is more than one object.
dd::optional_ptr<dd::CatalogEntry> ForTheTable(dd::ClientContext& context, dd::CatalogType type,
                                               const std::vector<std::string>& parts, const ObjectPath& table,
                                               bool& ambiguous) {
  const std::vector<std::string> main_schema{DEFAULT_SCHEMA};
  const std::string& name = parts.back();
  std::vector<dd::optional_ptr<dd::CatalogEntry>> found;
  auto look = [&](const std::vector<std::string>& schema) {
    auto entry = EntryAt(context, type, table.catalog, schema, name);
    if (entry && std::find(found.begin(), found.end(), entry) == found.end()) found.push_back(entry);
  };
  if (parts.size() == 1) {
    look(table.schema_path);
    look(main_schema);
  } else {
    const std::vector<std::string> qualifier(parts.begin(), parts.end() - 1);
    look(qualifier);  // a schema path in the table's catalog
    if (IdentifiersEqual(qualifier.front(), table.catalog)) {
      // catalog.schemas.name, and catalog.name (the catalog's default schema)
      look(qualifier.size() > 1 ? std::vector<std::string>(qualifier.begin() + 1, qualifier.end()) : main_schema);
    }
  }
  ambiguous = found.size() > 1;
  return found.size() == 1 ? found[0] : nullptr;
}

// What the name is in this session, found the way DuckDB's binder finds a table's, a view's
// and a sequence's (CTEs aside). Throws where DuckDB would (a qualifier that reads two ways).
dd::optional_ptr<dd::CatalogEntry> InThisSession(dd::ClientContext& context, dd::CatalogType type,
                                                 const std::vector<std::string>& parts) {
  dd::vector<dd::Identifier> path;
  for (const auto& part : parts) path.emplace_back(part);
  dd::CatalogEntryRetriever retriever(context);
  return dd::Catalog::GetEntry(context,
                               dd::EntryLookupInfo(type, dd::Binder::BindTableName(retriever, dd::QualifiedName::FromPath(path))),
                               dd::OnEntryNotFound::RETURN_NULL);
}

// A session's own temporary object (DuckDB marks its built-in views temporary too: not those).
bool IsTemporary(dd::CatalogEntry& entry) {
  return IdentifiersEqual(compat::Str(entry.ParentCatalog().GetName()), TEMP_CATALOG) ||
         (entry.temporary && !entry.internal);
}

std::string Written(const std::vector<std::string>& parts) {
  std::string text;
  for (const auto& part : parts) text += (text.empty() ? "" : ".") + QuoteIdentifierIfNeeded(part);
  return text;
}

// The sequences an expression takes numbers from (nextval and its kin, bound by name).
void SequencesOf(const dd::Expression& expression, std::vector<dd::CatalogEntry*>& sequences) {
  if (expression.GetExpressionClass() == dd::ExpressionClass::BOUND_FUNCTION) {
    const auto& call = expression.Cast<dd::BoundFunctionExpression>();
    if (IsSequenceFunctionName(FoldIdentifier(compat::Str(call.Function().GetName()))) && call.BindInfo()) {
      sequences.push_back(&call.BindInfo()->Cast<dd::NextvalBindData>().sequence);
    }
  }
  dd::ExpressionIterator::EnumerateChildren(expression, [&](const dd::Expression& child) { SequencesOf(child, sequences); });
}

// The tables, views and sequences a body's plan reaches: scanned, read through (a view),
// written (`written`, too), taken numbers from. Not those of another trigger's body beneath
// it, which is judged on its own; nor what a view reads, the view's.
void CollectReached(dd::ClientContext& context, const dd::LogicalOperator& op, const TriggerBodies& bodies, bool root,
                    std::vector<dd::CatalogEntry*>& reached, std::vector<dd::TableCatalogEntry*>& written) {
  if (!root) {
    const TriggerBody* other = TriggerBodyAt(bodies, op);
    if (other && !other->base) return;
  }
  dd::LogicalOperatorVisitor::EnumerateExpressions(op, [&](const dd::unique_ptr<dd::Expression>* e) {
    if (e && *e) SequencesOf(**e, reached);
  });
  if (op.type == OpType::LOGICAL_EXTENSION_OPERATOR) {
    if (const auto* marker = dynamic_cast<const ViewMarker*>(&op)) {
      auto view = EntryAt(context, dd::CatalogType::TABLE_ENTRY, marker->view.catalog, marker->view.schema_path,
                          marker->view.object_name);
      if (!view) throw dd::InternalException("a view a trigger's body reads could not be found again");
      reached.push_back(view.get());
      return;
    }
  }
  if (op.type == OpType::LOGICAL_GET) {
    if (auto table = op.Cast<dd::LogicalGet>().GetTable()) reached.push_back(table.get());
  }
  if (dd::TableCatalogEntry* table = WrittenTable(op)) {
    reached.push_back(table);
    written.push_back(table);
  }
  // The defaults DuckDB bound for a written table's columns (it copies some into the plan's
  // expressions, where the walk above finds them, and keeps the rest here).
  const dd::vector<dd::unique_ptr<dd::Expression>>* defaults = nullptr;
  if (op.type == OpType::LOGICAL_INSERT) defaults = &op.Cast<dd::LogicalInsert>().bound_defaults;
  if (op.type == OpType::LOGICAL_UPDATE) defaults = &op.Cast<dd::LogicalUpdate>().bound_defaults;
  if (op.type == OpType::LOGICAL_MERGE_INTO) defaults = &op.Cast<dd::LogicalMergeInto>().bound_defaults;
  if (defaults) {
    for (const auto& expression : *defaults) {
      if (expression) SequencesOf(*expression, reached);
    }
  }
  for (const auto& child : op.children) {
    if (child) CollectReached(context, *child, bodies, false, reached, written);
  }
}

// ---- Carried columns (triggers.hpp) ----

// One use of a column: the reference, or nullptr where an operator names the column
// without an expression (a correlated-column list).
struct ColumnUse {
  dd::idx_t column;
  const dd::BoundColumnRefExpression* ref;
};

// A column of an operator: its table index and the column's place.
using PlanColumn = std::pair<dd::idx_t, dd::idx_t>;

// Every use of every column in a plan, and where its operators sit.
struct PlanIndex {
  std::vector<const dd::LogicalOperator*> operators;
  std::unordered_map<dd::idx_t, std::vector<ColumnUse>> uses;                        // by table index
  std::unordered_map<dd::idx_t, std::vector<const dd::LogicalOperator*>> producers;  // by table index
  std::unordered_map<dd::idx_t, std::vector<const dd::LogicalOperator*>> cte_scans;  // by CTE index
  std::unordered_map<const dd::LogicalOperator*, const dd::LogicalOperator*> parent;
  // The entries of projections that are nothing but a column: the projection and the place.
  std::unordered_map<const dd::Expression*, std::pair<const dd::LogicalOperator*, dd::idx_t>> projected;
  // Operators holding an expression whose column uses can't be listed (a subquery that was
  // not planned, a lambda's body, a reference by position).
  std::unordered_set<const dd::LogicalOperator*> opaque;
};

// The expression, if it is nothing but a column of the query it sits in.
const dd::BoundColumnRefExpression* BareColumn(const dd::unique_ptr<dd::Expression>& expr) {
  if (!expr || expr->GetExpressionClass() != dd::ExpressionClass::BOUND_COLUMN_REF) return nullptr;
  const auto& ref = expr->Cast<dd::BoundColumnRefExpression>();
  return ref.Depth() == 0 ? &ref : nullptr;
}

PlanColumn ColumnOf(const dd::ColumnBinding& binding) {
  return {binding.table_index.index, binding.column_index.GetIndex()};
}

void IndexExpression(const dd::Expression& expr, const dd::LogicalOperator& op, PlanIndex& index) {
  switch (expr.GetExpressionClass()) {
    case dd::ExpressionClass::BOUND_COLUMN_REF: {
      const auto& ref = expr.Cast<dd::BoundColumnRefExpression>();
      index.uses[ref.Binding().table_index.index].push_back({ref.Binding().column_index.GetIndex(), &ref});
      return;
    }
    case dd::ExpressionClass::BOUND_SUBQUERY:
    case dd::ExpressionClass::BOUND_LAMBDA:
    case dd::ExpressionClass::BOUND_LAMBDA_REF:
    case dd::ExpressionClass::BOUND_REF:
      index.opaque.insert(&op);
      break;
    default:
      break;
  }
  // Throws on an expression class it does not know: the caller then carries nothing.
  dd::ExpressionIterator::EnumerateChildren(
      expr, [&](const dd::Expression& child) { IndexExpression(child, op, index); });
}

void IndexPlan(const dd::LogicalOperator& op, const dd::LogicalOperator* parent, PlanIndex& index) {
  index.operators.push_back(&op);
  index.parent[&op] = parent;
  for (const auto& table : op.GetTableIndex()) index.producers[table.index].push_back(&op);
  dd::LogicalOperatorVisitor::EnumerateExpressions(op, [&](const dd::unique_ptr<dd::Expression>* e) {
    if (e && *e) IndexExpression(**e, op, index);
  });
  const dd::CorrelatedColumns* correlated = nullptr;
  switch (op.type) {
    case OpType::LOGICAL_PROJECTION:
      for (dd::idx_t place = 0; place < op.expressions.size(); ++place) {
        if (BareColumn(op.expressions[place])) index.projected[op.expressions[place].get()] = {&op, place};
      }
      break;
    case OpType::LOGICAL_DEPENDENT_JOIN:
      correlated = &op.Cast<dd::LogicalDependentJoin>().correlated_columns;
      break;
    case OpType::LOGICAL_TRIGGER:
      correlated = &op.Cast<dd::LogicalTrigger>().correlated_columns;
      break;
    case OpType::LOGICAL_MATERIALIZED_CTE:
      correlated = &op.Cast<dd::LogicalMaterializedCTE>().correlated_columns;
      break;
    case OpType::LOGICAL_RECURSIVE_CTE:
      correlated = &op.Cast<dd::LogicalRecursiveCTE>().correlated_columns;
      break;
    case OpType::LOGICAL_CTE_REF:
      index.cte_scans[op.Cast<dd::LogicalCTERef>().cte_index.index].push_back(&op);
      break;
    default:
      break;
  }
  if (correlated) {
    for (const auto& column : *correlated) {
      index.uses[column.binding.table_index.index].push_back({column.binding.column_index.GetIndex(), nullptr});
    }
  }
  for (const auto& child : op.children) {
    if (child) IndexPlan(*child, &op, index);
  }
}

// The operators whose every use of a column IndexPlan lists: a use is an expression of
// theirs that LogicalOperatorVisitor::EnumerateExpressions hands out, or an entry of a
// correlated-column list. (A table function with an input, a recursive CTE, a pivot, a
// positional or ASOF join, a write, ... are not among them: beneath a write that has one,
// nothing is carried.)
bool ListsItsColumnUses(const dd::LogicalOperator& op, const PlanIndex& index) {
  if (index.opaque.count(&op)) return false;
  switch (op.type) {
    case OpType::LOGICAL_GET:
      return op.children.empty();
    case OpType::LOGICAL_FILTER:
    case OpType::LOGICAL_PROJECTION:
    case OpType::LOGICAL_AGGREGATE_AND_GROUP_BY:
    case OpType::LOGICAL_WINDOW:
    case OpType::LOGICAL_UNNEST:
    case OpType::LOGICAL_LIMIT:
    case OpType::LOGICAL_ORDER_BY:
    case OpType::LOGICAL_TOP_N:
    case OpType::LOGICAL_DISTINCT:
    case OpType::LOGICAL_CROSS_PRODUCT:
    case OpType::LOGICAL_COMPARISON_JOIN:
    case OpType::LOGICAL_DELIM_JOIN:
    case OpType::LOGICAL_DEPENDENT_JOIN:
    case OpType::LOGICAL_ANY_JOIN:
    case OpType::LOGICAL_UNION:
    case OpType::LOGICAL_EXCEPT:
    case OpType::LOGICAL_INTERSECT:
    case OpType::LOGICAL_MATERIALIZED_CTE:
    case OpType::LOGICAL_CTE_REF:
    case OpType::LOGICAL_DELIM_GET:
    case OpType::LOGICAL_EXPRESSION_GET:
    case OpType::LOGICAL_CHUNK_GET:
    case OpType::LOGICAL_DUMMY_SCAN:
    case OpType::LOGICAL_EMPTY_RESULT:
      return true;
    case OpType::LOGICAL_EXTENSION_OPERATOR:
      return dynamic_cast<const ViewMarker*>(&op) != nullptr;  // passes its view's columns on
    default:
      return false;
  }
}

bool SubtreeListsItsColumnUses(const dd::LogicalOperator& op, const PlanIndex& index) {
  if (!ListsItsColumnUses(op, index)) return false;
  for (const auto& child : op.children) {
    if (child && !SubtreeListsItsColumnUses(*child, index)) return false;
  }
  return true;
}

// Filters and joins hand their children's columns on as themselves: above them a column
// is still that column, used only where an expression names it.
bool HandsColumnsOn(OpType type) {
  return type == OpType::LOGICAL_FILTER || type == OpType::LOGICAL_CROSS_PRODUCT ||
         type == OpType::LOGICAL_COMPARISON_JOIN || type == OpType::LOGICAL_DELIM_JOIN ||
         type == OpType::LOGICAL_DEPENDENT_JOIN || type == OpType::LOGICAL_ANY_JOIN;
}

// Is `target` reached from `from` through joins alone?
bool ReachesThroughJoins(const dd::LogicalOperator& from, const dd::LogicalOperator& target) {
  if (&from == &target) return true;
  if (!HandsColumnsOn(from.type) || from.type == OpType::LOGICAL_FILTER || from.type == OpType::LOGICAL_DEPENDENT_JOIN) {
    return false;
  }
  for (const auto& child : from.children) {
    if (child && ReachesThroughJoins(*child, target)) return true;
  }
  return false;
}

// Is `op` inside the subtree of `ancestor` (or `ancestor` itself)?
bool Within(const PlanIndex& index, const dd::LogicalOperator* op, const dd::LogicalOperator* ancestor) {
  if (!ancestor) return false;
  for (int depth = 0; op && depth < 100000; ++depth) {
    if (op == ancestor) return true;
    auto parent = index.parent.find(op);
    op = parent == index.parent.end() ? nullptr : parent->second;
  }
  return false;
}

const std::vector<ColumnUse>& UsesOfTable(const PlanIndex& index, dd::idx_t table) {
  static const std::vector<ColumnUse> none;
  auto found = index.uses.find(table);
  return found == index.uses.end() ? none : found->second;
}

// The one operator with table index `table`, or nullptr.
const dd::LogicalOperator* SoleProducer(const PlanIndex& index, dd::idx_t table) {
  auto found = index.producers.find(table);
  return found != index.producers.end() && found->second.size() == 1 ? found->second[0] : nullptr;
}

// The columns a materialized CTE keeps (its definition's, by place), for a scan of it.
std::optional<std::vector<PlanColumn>> ScannedDefinition(const PlanIndex& index, const dd::LogicalOperator& cte_scan) {
  const auto& ref = cte_scan.Cast<dd::LogicalCTERef>();
  const dd::LogicalOperator* cte = SoleProducer(index, ref.cte_index.index);
  if (ref.is_recurring || SoleProducer(index, ref.table_index.index) != &cte_scan || !cte ||
      cte->type != OpType::LOGICAL_MATERIALIZED_CTE || cte->children.size() != 2 || !cte->children[0]) {
    return std::nullopt;
  }
  // (GetColumnBindings is not const, and changes nothing.)
  const auto bindings = const_cast<dd::LogicalOperator&>(*cte->children[0]).GetColumnBindings();
  if (bindings.size() != ref.chunk_types.size()) return std::nullopt;
  std::vector<PlanColumn> columns;
  for (const auto& binding : bindings) columns.push_back(ColumnOf(binding));
  return columns;
}

// The column of a table scan that `column` is, unchanged: followed down through
// projections that give it on as itself and scans of materialized CTEs that keep it.
std::optional<PlanColumn> ScannedColumn(const PlanIndex& index, PlanColumn column) {
  for (int depth = 0; depth < 1000; ++depth) {
    const dd::LogicalOperator* op = SoleProducer(index, column.first);
    if (!op) return std::nullopt;
    if (op->type == OpType::LOGICAL_GET) return column;
    if (op->type == OpType::LOGICAL_PROJECTION) {
      const auto* ref = column.second < op->expressions.size() ? BareColumn(op->expressions[column.second]) : nullptr;
      if (!ref) return std::nullopt;
      column = ColumnOf(ref->Binding());
    } else if (op->type == OpType::LOGICAL_CTE_REF) {
      const auto kept = ScannedDefinition(index, *op);
      if (!kept || column.second >= kept->size()) return std::nullopt;
      column = (*kept)[column.second];
    } else {
      return std::nullopt;
    }
  }
  return std::nullopt;
}

struct PlanColumnHash {
  size_t operator()(const PlanColumn& column) const {
    return std::hash<dd::idx_t>()(column.first) * 1000003u ^ std::hash<dd::idx_t>()(column.second);
  }
};

struct Carried {
  const dd::LogicalGet* scan = nullptr;
  std::vector<std::string> read;
  std::vector<std::string> carried;
  std::vector<const dd::LogicalOperator*> readers;
};

// Tells the columns the base write `dml` (under the base CTE `cte`) reads for the
// statement from the ones the binder carries to the triggers; nullopt when the plan is not
// laid out as triggers.hpp describes, and then every column the scan reads is a read.
std::optional<Carried> CarriedColumns(dd::ClientContext& context, const PlanIndex& index, const dd::LogicalOperator& cte,
                                      const dd::LogicalOperator& dml) {
  if (cte.type != OpType::LOGICAL_MATERIALIZED_CTE || cte.children.size() != 2 || !cte.children[0] || !cte.children[1] ||
      dml.children.size() != 1 || !dml.children[0] || index.opaque.count(&dml)) {
    return std::nullopt;
  }
  dd::TableCatalogEntry* table = WrittenTable(dml);
  if (!table) return std::nullopt;

  // ---- The write: where it takes its row ids from, and the index of its own rows ----
  const dd::BoundColumnRefExpression* row_id = nullptr;
  const dd::LogicalOperator* projection = nullptr;  // an UPDATE's or a merge's input
  dd::idx_t dml_index = 0;
  const dd::vector<dd::unique_ptr<dd::BoundConstraint>>* constraints = nullptr;  // checked on written rows
  if (dml.type != OpType::LOGICAL_DELETE) {
    projection = dml.children[0].get();
    if (projection->type != OpType::LOGICAL_PROJECTION || projection->children.size() != 1 || !projection->children[0] ||
        projection->expressions.empty() || SoleProducer(index, projection->GetTableIndex()[0].index) != projection) {
      return std::nullopt;
    }
  }
  switch (dml.type) {
    case OpType::LOGICAL_DELETE: {
      const auto& del = dml.Cast<dd::LogicalDelete>();
      if (!del.return_chunk || del.expressions.empty()) return std::nullopt;
      row_id = BareColumn(del.expressions[0]);
      dml_index = del.table_index.index;
      break;
    }
    case OpType::LOGICAL_UPDATE: {
      const auto& update = dml.Cast<dd::LogicalUpdate>();
      if (!update.return_chunk) return std::nullopt;
      row_id = BareColumn(projection->expressions.back());
      dml_index = update.table_index.index;
      constraints = &update.bound_constraints;
      break;
    }
    case OpType::LOGICAL_MERGE_INTO: {
      const auto& merge = dml.Cast<dd::LogicalMergeInto>();
      // INSERT ... ON CONFLICT has no delete action; one would take columns by place.
      if (!merge.return_chunk || !merge.delete_return_columns.empty() ||
          merge.row_id_start >= projection->expressions.size()) {
        return std::nullopt;
      }
      row_id = BareColumn(projection->expressions[merge.row_id_start]);
      dml_index = merge.table_index.index;
      constraints = &merge.bound_constraints;
      break;
    }
    default:
      return std::nullopt;
  }
  if (!row_id) return std::nullopt;

  // ---- The scan: the plain scan of the written table the row ids come from ----
  const auto origin = ScannedColumn(index, ColumnOf(row_id->Binding()));
  if (!origin) return std::nullopt;
  const dd::idx_t scanned = origin->first;
  const auto& scan = SoleProducer(index, scanned)->Cast<dd::LogicalGet>();
  const auto& column_ids = scan.GetColumnIds();
  if (scan.GetTable().get() != table || !scan.children.empty() || scan.table_filters.HasFilters() ||
      origin->second >= column_ids.size() || !column_ids[origin->second].IsRowIdColumn()) {
    return std::nullopt;
  }
  // Every operator beneath the write is one whose column uses the index lists.
  if (!SubtreeListsItsColumnUses(*dml.children[0], index)) return std::nullopt;

  // ---- Where the scan's columns go as themselves ----
  // A column given on unchanged is still the scan's column, whatever it is called there:
  // an entry of a projection that is nothing but the column, and a column of a
  // materialized CTE's definition for every scan of that CTE (DuckDB keeps the outer rows
  // of a correlated subquery in such CTEs).
  std::unordered_map<PlanColumn, dd::idx_t, PlanColumnHash> same_as;  // -> the scan's position
  for (dd::idx_t position = 0; position < column_ids.size(); ++position) same_as[{scanned, position}] = position;
  for (bool grown = true; grown;) {
    grown = false;
    for (const dd::LogicalOperator* op : index.operators) {
      if (op->type == OpType::LOGICAL_PROJECTION && !op->GetTableIndex().empty() &&
          SoleProducer(index, op->GetTableIndex()[0].index) == op) {
        const dd::idx_t projected = op->GetTableIndex()[0].index;
        for (dd::idx_t place = 0; place < op->expressions.size(); ++place) {
          const auto* ref = BareColumn(op->expressions[place]);
          auto source = ref ? same_as.find(ColumnOf(ref->Binding())) : same_as.end();
          if (source == same_as.end()) continue;
          const dd::idx_t position = source->second;  // (emplace may move the map's entries)
          if (same_as.emplace(PlanColumn{projected, place}, position).second) grown = true;
        }
      } else if (op->type == OpType::LOGICAL_MATERIALIZED_CTE && op->children.size() == 2 && op->children[0]) {
        const auto kept = const_cast<dd::LogicalOperator&>(*op->children[0]).GetColumnBindings();
        const bool keeps_one = std::any_of(kept.begin(), kept.end(), [&](const dd::ColumnBinding& binding) {
          return same_as.count(ColumnOf(binding)) > 0;
        });
        if (!keeps_one) continue;
        // Every scan of a CTE that keeps a column of the scan must be one this can follow.
        const dd::idx_t cte_index = op->Cast<dd::LogicalMaterializedCTE>().table_index.index;
        if (SoleProducer(index, cte_index) != op) return std::nullopt;
        auto cte_scans = index.cte_scans.find(cte_index);
        if (cte_scans == index.cte_scans.end()) continue;
        for (const dd::LogicalOperator* cte_scan : cte_scans->second) {
          const auto columns = ScannedDefinition(index, *cte_scan);
          if (!columns) return std::nullopt;
          const dd::idx_t scan_index = cte_scan->Cast<dd::LogicalCTERef>().table_index.index;
          for (dd::idx_t place = 0; place < columns->size(); ++place) {
            auto source = same_as.find((*columns)[place]);
            if (source == same_as.end()) continue;
            const dd::idx_t position = source->second;
            if (same_as.emplace(PlanColumn{scan_index, place}, position).second) grown = true;
          }
        }
      }
    }
  }
  // Each operator that has such a column sits beneath the write, and from it up only
  // filters and joins (which hand it on as itself) come before the operator that takes
  // it: a projection or an aggregate (which give on only what their expressions name), a
  // materialized CTE's definition (followed above), or the write.
  std::unordered_set<const dd::LogicalOperator*> holders;
  for (const auto& [column, position] : same_as) {
    const dd::LogicalOperator* holder = SoleProducer(index, column.first);
    if (!holder) return std::nullopt;
    holders.insert(holder);
  }
  for (const dd::LogicalOperator* holder : holders) {
    if (!Within(index, holder, dml.children[0].get())) return std::nullopt;
    for (const dd::LogicalOperator* below = holder;;) {
      const dd::LogicalOperator* above = index.parent.at(below);
      if (!above) return std::nullopt;
      if (above == &dml || above->type == OpType::LOGICAL_PROJECTION ||
          above->type == OpType::LOGICAL_AGGREGATE_AND_GROUP_BY) {
        break;
      }
      if (above->type == OpType::LOGICAL_MATERIALIZED_CTE && above->children.size() == 2) {
        if (above->children[0].get() == below) break;  // its definition
      } else if (!HandsColumnsOn(above->type)) {
        return std::nullopt;
      }
      below = above;
    }
  }

  // ---- Each use of such a column: handed to the write, given on as itself, or a read ----
  // Which column of the table a position of the scan is.
  auto scans_column = [&](dd::idx_t position, dd::PhysicalIndex column) {
    const auto& id = column_ids[position];
    return !id.IsRowIdColumn() && !id.IsVirtualColumn() && !id.IsEmptyColumn() &&
           id.GetPrimaryIndex() == table->GetColumns().GetColumn(column).Logical().index;
  };
  // Is `ref` the write's own entry that puts the column back where it came from ("c = c"),
  // or (DELETE) an entry of the list of the row it deletes?
  auto hands_over = [&](dd::idx_t position, const dd::BoundColumnRefExpression* ref) {
    switch (dml.type) {
      case OpType::LOGICAL_DELETE:
        return std::any_of(dml.expressions.begin(), dml.expressions.end(),
                           [&](const dd::unique_ptr<dd::Expression>& e) { return e.get() == ref; });
      case OpType::LOGICAL_UPDATE: {
        const auto& update = dml.Cast<dd::LogicalUpdate>();
        for (dd::idx_t i = 0; i < update.expressions.size() && i < update.columns.size(); ++i) {
          if (update.expressions[i].get() == ref && scans_column(position, update.columns[i])) return true;
        }
        return false;
      }
      default: {
        for (const auto& [when, actions] : dml.Cast<dd::LogicalMergeInto>().actions) {
          for (const auto& action : actions) {
            if (!action || action->action_type != dd::MergeActionType::MERGE_UPDATE) continue;
            for (dd::idx_t i = 0; i < action->expressions.size() && i < action->columns.size(); ++i) {
              if (action->expressions[i].get() == ref && scans_column(position, action->columns[i])) return true;
            }
          }
        }
        return false;
      }
    }
  };
  std::vector<bool> read(column_ids.size(), false);
  std::vector<bool> handed(column_ids.size(), false);
  for (const auto& [table_index, uses] : index.uses) {
    for (const auto& use : uses) {
      auto same = same_as.find({table_index, use.column});
      if (same == same_as.end()) continue;
      const dd::idx_t position = same->second;
      if (!use.ref || use.ref->Depth() != 0) {
        read[position] = true;  // a subquery's, or named without an expression
        continue;
      }
      // Given on as itself: the entry of a projection that is followed above.
      auto given_on = index.projected.find(use.ref);
      if (given_on != index.projected.end() &&
          same_as.count({given_on->second.first->GetTableIndex()[0].index, given_on->second.second})) {
        continue;
      }
      if (hands_over(position, use.ref)) {
        handed[position] = true;
      } else {
        read[position] = true;
      }
    }
  }
  // What the write takes from its input by place rather than through an expression.
  if (projection) {
    const dd::idx_t projected = projection->GetTableIndex()[0].index;
    for (dd::idx_t place = 0; place < projection->expressions.size(); ++place) {
      auto same = same_as.find({projected, place});
      if (same == same_as.end()) continue;
      if (dml.type == OpType::LOGICAL_UPDATE) {
        // The OLD row image (kept only for the triggers' rows): handed over.
        const auto& old_row = dml.Cast<dd::LogicalUpdate>().old_row_columns;
        if (dml.Cast<dd::LogicalUpdate>().capture_old_rows &&
            std::find(old_row.begin(), old_row.end(), place) != old_row.end()) {
          handed[same->second] = true;
        }
      } else {
        // A merge: its row ids, and the marker of a source row. A column there is a read.
        const auto& merge = dml.Cast<dd::LogicalMergeInto>();
        if (place >= merge.row_id_start || (merge.source_marker.IsValid() && merge.source_marker.GetIndex() == place)) {
          read[same->second] = true;
        }
      }
    }
  }

  // ---- What the write itself can tell of a column it is handed ----
  // A constraint error of the write can name a key's value (a duplicate key, a key a
  // foreign key still refers to: DuckDB runs an UPDATE that returns its rows as a delete
  // and an insert), and a failed CHECK over several columns tells of the ones the
  // statement did not set. Such columns are never carried: they need SELECT, as a DELETE
  // on a table without triggers does for its unique indexes' columns and an UPDATE for
  // such a CHECK's (DuckDB scans them there for the same checks). Every index counts: an
  // index of a kind this does not know may have errors of its own.
  std::unordered_set<dd::idx_t> told;  // logical column indexes
  for (const auto& table_index : table->GetStorageInfo(context).index_info) {
    for (const auto& column : table_index.column_set) {
      told.insert(table->GetColumns().GetColumn(dd::PhysicalIndex(column)).Logical().index);
    }
  }
  if (constraints) {
    for (const auto& constraint : *constraints) {
      if (!constraint || constraint->type != dd::ConstraintType::CHECK) continue;
      const auto& checked = constraint->Cast<dd::BoundCheckConstraint>().bound_columns;
      if (checked.size() < 2) continue;
      for (const auto& column : checked) told.insert(table->GetColumns().GetColumn(column).Logical().index);
    }
  }

  Carried result;
  result.scan = &scan;
  std::vector<std::string> candidates;
  for (dd::idx_t position = 0; position < column_ids.size(); ++position) {
    const auto& id = column_ids[position];
    if (id.IsRowIdColumn() || id.IsVirtualColumn() || id.IsEmptyColumn()) continue;
    const std::string name = compat::Str(table->GetColumn(dd::LogicalIndex(id.GetPrimaryIndex())).Name());
    // A column nothing uses is not one this can explain: a read.
    const bool carried = handed[position] && !read[position] && !told.count(id.GetPrimaryIndex());
    auto& names = carried ? candidates : result.read;
    if (std::find(names.begin(), names.end(), name) == names.end()) names.push_back(name);
  }
  for (const auto& name : candidates) {
    if (std::find(result.read.begin(), result.read.end(), name) == result.read.end()) result.carried.push_back(name);
  }

  // ---- The write's rows: into the base CTE through a projection of bare columns only ----
  const dd::LogicalOperator* top = cte.children[0].get();
  while (top->type == OpType::LOGICAL_MATERIALIZED_CTE && top->children.size() == 2 && top->children[1]) {
    top = top->children[1].get();  // a WITH of the statement's own
  }
  if (top->type != OpType::LOGICAL_PROJECTION || top->children.size() != 1 || !top->children[0] ||
      !ReachesThroughJoins(*top->children[0], dml)) {
    return std::nullopt;
  }
  if (index.producers.count(dml_index) && SoleProducer(index, dml_index) != &dml) return std::nullopt;
  std::vector<bool> from_write(top->expressions.size(), false);
  for (const auto& use : UsesOfTable(index, dml_index)) {
    bool bare = false;
    for (dd::idx_t i = 0; i < top->expressions.size(); ++i) {
      if (use.ref && use.ref->Depth() == 0 && top->expressions[i].get() == use.ref) bare = from_write[i] = true;
    }
    if (!bare) return std::nullopt;  // computed from the rows (a generated column), or used elsewhere
  }

  // ---- The base CTE's scans: all within its scope; blind, or a row reader ----
  auto scans = index.cte_scans.find(cte.Cast<dd::LogicalMaterializedCTE>().table_index.index);
  if (scans != index.cte_scans.end()) {
    for (const dd::LogicalOperator* cte_scan : scans->second) {
      if (!Within(index, cte_scan, cte.children[1].get())) return std::nullopt;
      const auto& ref = cte_scan->Cast<dd::LogicalCTERef>();
      const dd::LogicalOperator* parent = index.parent.at(cte_scan);
      bool blind = parent && !index.opaque.count(parent) &&
                   (parent->type == OpType::LOGICAL_PROJECTION ||
                    parent->type == OpType::LOGICAL_AGGREGATE_AND_GROUP_BY) &&
                   ref.chunk_types.size() == from_write.size() &&
                   SoleProducer(index, ref.table_index.index) == cte_scan;
      for (const auto& use : UsesOfTable(index, ref.table_index.index)) {
        blind = blind && use.column < from_write.size() && !from_write[use.column];
      }
      if (!blind) result.readers.push_back(cte_scan);
    }
  }
  return result;
}

}  // namespace

TriggerBodies FindTriggerBodies(dd::ClientContext& context, const Snapshot& snapshot, dd::LogicalOperator& plan) {
  Finder finder(context, snapshot);
  CollectBases(plan, finder.bases, false);
  finder.Walk(plan);
  // Made for the first base that may carry columns; left empty when the plan can't be indexed.
  std::optional<PlanIndex> index;
  bool indexed = false;
  for (const auto& [uuid, base] : finder.bases) {
    if (!base.dml || !base.table) continue;
    TriggerBody entry;
    entry.root = base.dml;
    entry.base = true;
    entry.returning_to_user = base.returning_to_user;
    entry.table = PathOfEntry(*base.table);
    entry.describe = "the statement on table " + entry.table.ToString();
    // An INSERT scans nothing of its table. A RETURNING of the writer's own hands the
    // rows back: every column is a read then, as it always was.
    std::optional<Carried> carried;
    if (base.cte && !base.returning_to_user && base.dml->type != OpType::LOGICAL_INSERT) {
      try {
        if (!indexed) {
          indexed = true;
          PlanIndex made;
          IndexPlan(plan, nullptr, made);
          index = std::move(made);
        }
        if (index) carried = CarriedColumns(context, *index, *base.cte, *base.dml);
      } catch (...) {
        carried.reset();  // not a plan this can read: nothing is carried
      }
    }
    if (carried) {
      // The rows OLD and NEW come from sit under the row triggers' CTE, beside the bodies:
      // each row trigger's body answers for them. Only where those bodies were found and
      // nothing scans that CTE; otherwise whoever's rights are in force there answers.
      auto row_cte = finder.row_ctes.find(uuid);
      auto row_bodies = finder.row_bodies.find(base.dml);
      const bool answerable =
          row_cte != finder.row_ctes.end() && row_cte->second && !row_cte->second->children.empty() &&
          row_bodies != finder.row_bodies.end() && !row_bodies->second.empty() &&
          !index->cte_scans.count(row_cte->second->Cast<dd::LogicalMaterializedCTE>().table_index.index);
      bool beside_bodies = false;
      for (const dd::LogicalOperator* reader : carried->readers) {
        bool in_a_body = false;
        for (const auto& body : finder.bodies) {
          in_a_body = in_a_body || (!body.base && Within(*index, reader, body.root));
        }
        if (answerable && !in_a_body && Within(*index, reader, row_cte->second->children[0].get())) {
          beside_bodies = true;
        } else {
          entry.row_readers.push_back(reader);
        }
      }
      if (beside_bodies) {
        for (size_t body : row_bodies->second) finder.bodies[body].reads_rows_of = base.dml;
      }
      entry.target_scan = carried->scan;
      entry.read_columns = std::move(carried->read);
      entry.carried_columns = std::move(carried->carried);
    }
    finder.bodies.push_back(std::move(entry));
  }
  return std::move(finder.bodies);
}

const TriggerBody* TriggerBodyAt(const TriggerBodies& bodies, const dd::LogicalOperator& op) {
  for (const auto& body : bodies) {
    if (body.root == &op) return &body;
  }
  return nullptr;
}

bool ReturningAddedForTriggers(const TriggerBodies& bodies, const dd::LogicalOperator& op) {
  const TriggerBody* body = TriggerBodyAt(bodies, op);
  return body && body->base && !body->returning_to_user;
}

std::string TriggerBodyNameProblem(dd::ClientContext& context, const TriggerBodies& bodies, const TriggerBody& body) {
  const std::string what = body.describe.empty() ? std::string("a trigger") : body.describe;
  const std::string remedy = " The trigger's owner can name it in full (catalog.schema.name) with CREATE OR REPLACE TRIGGER";
  try {
    if (body.base || !body.root || body.entries.empty()) {
      return what + " could not be matched to its body, so the names in it can't be checked; ask an administrator";
    }
    // A trigger on a session's own temporary table is the session's, names and all.
    if (IdentifiersEqual(body.table.catalog, TEMP_CATALOG)) return "";
    std::set<const dd::CatalogEntry*> meant;
    // A row trigger's body may be any of several triggers': each one's names are checked.
    for (const dd::TriggerCatalogEntry* trigger : body.entries) {
      if (!trigger || !trigger->trigger_action) return what + " has no action to read its names from; ask an administrator";
      auto action = trigger->trigger_action->Copy();
      ActionNames written;
      written.Node(*action);
      if (written.unreadable_sequence) {
        return what + " takes a sequence's numbers by a name that is not a plain string, which can't be checked "
                      "against this session's own sequences." + remedy;
      }
      for (const auto& alias : {trigger->referencing_new_table, trigger->referencing_old_table}) {
        if (!compat::Str(alias).empty()) written.cte_names.insert(FoldIdentifier(compat::Str(alias)));
      }
      for (const auto& name : written.names) {
        const auto type = name.sequence ? dd::CatalogType::SEQUENCE_ENTRY : dd::CatalogType::TABLE_ENTRY;
        bool ambiguous = false;
        auto for_the_table = ForTheTable(context, type, name.parts, body.table, ambiguous);
        if (for_the_table) meant.insert(for_the_table.get());
        // A bare name that a CTE or a transition table has is that, not a table (and where
        // it is a table after all, the plan's own tables are held against `meant` below).
        const bool cte = !name.sequence && !name.written_to && name.parts.size() == 1 &&
                         written.cte_names.count(FoldIdentifier(name.parts[0]));
        if (cte) continue;
        auto here = InThisSession(context, type, name.parts);
        if (!here) continue;  // nothing in this session: DuckDB bound no object by it
        if (here->internal) {
          meant.insert(here.get());  // DuckDB's own (information_schema, ...): no session's to shadow
          continue;
        }
        if (ambiguous) {
          return what + " names " + Written(name.parts) +
                 " without its schema, and both the table's own schema and its catalog's main schema have one: it "
                 "is not known which is meant, for any session." + remedy;
        }
        if (here.get() != for_the_table.get()) {
          return what + " names " + Written(name.parts) + ", and in this session that is " +
                 (IsTemporary(*here) ? "a temporary object of the session's own" : "another object than it is for the trigger's own table") +
                 " (a name without its catalog and schema follows the session's search path, temporary objects "
                 "first). The statement would not do what the trigger is there to do." + remedy;
        }
      }
    }
    // And the plan itself: nothing in it that no name stands for.
    std::vector<dd::CatalogEntry*> reached;
    std::vector<dd::TableCatalogEntry*> tables_written;
    CollectReached(context, *body.root, bodies, true, reached, tables_written);
    // A written table's column defaults take numbers from sequences no name in the action
    // stands for: the table's own. DuckDB binds those by name in the statement too, so what
    // each is for its table is what is meant (and a session's temporary one is not).
    for (dd::TableCatalogEntry* table : tables_written) {
      const ObjectPath table_path = PathOfEntry(*table);
      for (const auto& column : table->GetColumns().Logical()) {
        if (!column.HasDefaultValue()) continue;
        auto copy = column.DefaultValue().Copy();
        ActionNames of_default;
        of_default.Default(copy);
        for (const auto& name : of_default.names) {
          if (!name.sequence) continue;
          bool ambiguous = false;
          auto sequence = ForTheTable(context, dd::CatalogType::SEQUENCE_ENTRY, name.parts, table_path, ambiguous);
          if (sequence) meant.insert(sequence.get());
        }
      }
    }
    for (dd::CatalogEntry* entry : reached) {
      if (meant.count(entry)) continue;
      if (entry->internal) continue;
      return what + " reaches " + (IsTemporary(*entry) ? "a temporary object of this session" : "an object") +
             " named " + QuoteIdentifierIfNeeded(compat::Str(entry->name)) +
             " that none of the names in its body stands for, for the trigger's own table (through a macro, a "
             "table's default, or this session's search path): the statement is refused rather than guessed at." + remedy;
    }
    return "";
  } catch (...) {
    return what + " has names in its body that could not be checked against this session; ask an administrator";
  }
}

bool QualifyTriggerNames(dd::ClientContext& context, const ObjectPath& table, dd::CreateTriggerInfo& info) {
  if (!info.trigger_action || info.temporary || IdentifiersEqual(table.catalog, TEMP_CATALOG)) return true;
  ActionNames written;
  try {
    // (On a copy first: a walk that gives up halfway must leave the action as it was.)
    auto copy = info.trigger_action->Copy();
    ActionNames trial;
    trial.Node(*copy);
    written.Node(*info.trigger_action);
  } catch (...) {
    return false;
  }
  for (const auto& alias : {info.referencing_new_table, info.referencing_old_table}) {
    if (!compat::Str(alias).empty()) written.cte_names.insert(FoldIdentifier(compat::Str(alias)));
  }
  for (auto& name : written.names) {
    if (!name.sequence && !name.written_to && name.parts.size() == 1 &&
        written.cte_names.count(FoldIdentifier(name.parts[0]))) {
      continue;  // a CTE's or a transition table's
    }
    const auto type = name.sequence ? dd::CatalogType::SEQUENCE_ENTRY : dd::CatalogType::TABLE_ENTRY;
    dd::optional_ptr<dd::CatalogEntry> entry;
    try {
      entry = InThisSession(context, type, name.parts);
    } catch (const std::exception&) {
      continue;  // DuckDB has bound the action already: not a name of an object, then
    }
    // DuckDB lets an action name objects of its table's own catalog only (its own aside).
    if (!entry || entry->internal || IsTemporary(*entry)) continue;
    const ObjectPath path = PathOfEntry(*entry);
    if (!IdentifiersEqual(path.catalog, table.catalog) || path.schema_path.empty()) continue;
    dd::vector<dd::Identifier> schema;
    for (const auto& part : path.schema_path) schema.emplace_back(part);
    if (name.constant) {
      std::vector<std::string> full{path.catalog};
      full.insert(full.end(), path.schema_path.begin(), path.schema_path.end());
      full.push_back(path.object_name);
      std::string text;
      for (const auto& part : full) text += (text.empty() ? "" : ".") + QuoteIdentifierIfNeeded(part);
      *name.constant = dd::ConstantExpression::FromValue(dd::Value(text));
      continue;
    }
    auto full = dd::QualifiedName::FromCatalogSchema(dd::Identifier(path.catalog), std::move(schema),
                                                    dd::Identifier(path.object_name));
    if (name.insert) name.insert->qualified_name = full;
    if (name.ref) name.ref->SetQualifiedName(full);
  }
  return true;
}

const TriggerBody* BaseScannedBy(const TriggerBodies& bodies, const dd::LogicalOperator& scan) {
  for (const auto& body : bodies) {
    if (body.base && body.target_scan == &scan) return &body;
  }
  return nullptr;
}

const TriggerBody* BaseReadBy(const TriggerBodies& bodies, const dd::LogicalOperator& scan) {
  for (const auto& body : bodies) {
    if (body.base && std::find(body.row_readers.begin(), body.row_readers.end(), &scan) != body.row_readers.end()) {
      return &body;
    }
  }
  return nullptr;
}

bool HasDefinerBody(const TriggerBodies& bodies) {
  for (const auto& body : bodies) {
    if (body.definer || !body.problem.empty()) return true;
  }
  return false;
}

ObjectPath TriggerOwnerPath(const ObjectPath& table, const std::string& trigger_name) {
  ObjectPath path = table;
  path.schema_path.push_back(table.object_name);
  path.object_name = trigger_name;
  return path;
}

namespace {

// What a trigger fires on and does, from the fields CreateTriggerInfo and
// TriggerCatalogEntry share (never their ToString, which qualify names differently).
template <class Trigger>
std::string TriggerDefinitionText(const Trigger& trigger) {
  std::string text = std::to_string(static_cast<int>(trigger.timing)) + "|" +
                     std::to_string(static_cast<int>(trigger.event_type)) + "|";
  for (const auto& column : trigger.columns) text += FoldIdentifier(compat::Str(column)) + ",";
  text += "|" + std::to_string(static_cast<int>(trigger.for_each)) + "|" +
          FoldIdentifier(compat::Str(trigger.referencing_new_table)) + "|" +
          FoldIdentifier(compat::Str(trigger.referencing_old_table)) + "|";
  if (trigger.trigger_action) text += compat::Str(trigger.trigger_action->ToString());
  return text;
}

}  // namespace

std::string TriggerDefinitionHash(const dd::CreateTriggerInfo& info) {
  return DefinitionTextHash(TriggerDefinitionText(info));
}

std::string TriggerDefinitionHash(const dd::TriggerCatalogEntry& trigger) {
  return DefinitionTextHash(TriggerDefinitionText(trigger));
}

}  // namespace hardcharger::authz
