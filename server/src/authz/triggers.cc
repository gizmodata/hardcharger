#include "authz/triggers.hpp"

#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/table_catalog_entry.hpp>
#include <duckdb/common/enums/trigger_type.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/parser/query_node/delete_query_node.hpp>
#include <duckdb/parser/query_node/insert_query_node.hpp>
#include <duckdb/parser/query_node/update_query_node.hpp>
#include <duckdb/parser/tableref/basetableref.hpp>
#include <duckdb/planner/operator/logical_delete.hpp>
#include <duckdb/planner/operator/logical_insert.hpp>
#include <duckdb/planner/operator/logical_materialized_cte.hpp>
#include <duckdb/planner/operator/logical_merge_into.hpp>
#include <duckdb/planner/operator/logical_update.hpp>
#include <duckdb/planner/expression/bound_columnref_expression.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <functional>
#include <map>

#include "authz/duckdb_internal.hpp"
#include "authz/resolver.hpp"

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

// The DML under a base CTE's definition: down through single-child operators.
TriggerBase BaseOf(const dd::LogicalOperator& cte) {
  TriggerBase base;
  base.returning_to_user = ReturnsToUser(cte);
  const dd::LogicalOperator* op = cte.children.empty() ? nullptr : cte.children[0].get();
  for (int depth = 0; op && depth < 64; ++depth) {
    switch (op->type) {
      case OpType::LOGICAL_INSERT:
        base.table = &op->Cast<dd::LogicalInsert>().table;
        base.event = dd::TriggerEventType::INSERT_EVENT;
        base.dml = op;
        return base;
      case OpType::LOGICAL_DELETE:
        base.table = &op->Cast<dd::LogicalDelete>().table;
        base.event = dd::TriggerEventType::DELETE_EVENT;
        base.dml = op;
        return base;
      case OpType::LOGICAL_UPDATE: {
        auto& update = op->Cast<dd::LogicalUpdate>();
        base.table = &update.table;
        base.event = dd::TriggerEventType::UPDATE_EVENT;
        base.dml = op;
        if (!UpdatedColumns(update, base.updated_columns)) {
          base.problem = "the columns an UPDATE with triggers sets could not be read from the plan; ask an administrator";
        }
        return base;
      }
      case OpType::LOGICAL_MERGE_INTO:
        base.problem = "a trigger fired by MERGE INTO is not available under access control";
        return base;
      default:
        break;
    }
    op = op->children.size() == 1 ? op->children[0].get() : nullptr;
  }
  base.problem = "the statement a trigger fires on could not be found in the plan";
  return base;
}

void CollectBases(const dd::LogicalOperator& op, std::map<std::string, TriggerBase>& bases) {
  if (auto uuid = After(CteName(op), kBasePrefix)) bases[*uuid] = BaseOf(op);
  for (const auto& child : op.children) {
    if (child) CollectBases(*child, bases);
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

  void Walk(const dd::LogicalOperator& op) {
    const std::string name = CteName(op);
    if (auto rest = After(name, kRowBodyPrefix)) {
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
    if (dd::TableCatalogEntry* written = WrittenTable(op)) {
      std::vector<const dd::TriggerCatalogEntry*> matching;
      for (const auto& candidate : candidates) {
        if (candidate.target && candidate.target == written) matching.push_back(candidate.trigger);
      }
      if (matching.empty()) {
        bodies.push_back(Unmatched(op, "a row trigger of table " + PathOfEntry(*base.table).ToString() +
                                           " could not be matched to its body; ask an administrator"));
        return;
      }
      TriggerBody body = BodyOf(snapshot_, *matching[0], *base.table);
      for (size_t i = 1; i < matching.size(); ++i) {
        TriggerBody other = BodyOf(snapshot_, *matching[i], *base.table);
        const bool same_owner = other.definer.has_value() == body.definer.has_value() &&
                                (!body.definer || (other.definer->user_id == body.definer->user_id &&
                                                   other.definer->role_id == body.definer->role_id)) &&
                                other.problem == body.problem;
        if (!same_owner) {
          body.problem = "the row triggers " + QuoteIdentifierIfNeeded(body.trigger.object_name) + " and " +
                         QuoteIdentifierIfNeeded(other.trigger.object_name) + " on table " + body.table.ToString() +
                         " both write " + PathOfEntry(*written).ToString() +
                         " and have different owners, so their bodies can't be told apart; ask an administrator";
        }
      }
      body.root = &op;
      bodies.push_back(std::move(body));
      return;  // a body is one DML: nothing of another trigger sits beneath it
    }
    for (const auto& child : op.children) {
      if (child) MatchRowBodies(*child, base, candidates);
    }
  }

  dd::ClientContext& context_;
  const Snapshot& snapshot_;
};

}  // namespace

TriggerBodies FindTriggerBodies(dd::ClientContext& context, const Snapshot& snapshot, dd::LogicalOperator& plan) {
  Finder finder(context, snapshot);
  CollectBases(plan, finder.bases);
  finder.Walk(plan);
  for (const auto& [uuid, base] : finder.bases) {
    if (!base.dml || !base.table) continue;
    TriggerBody entry;
    entry.root = base.dml;
    entry.base = true;
    entry.returning_to_user = base.returning_to_user;
    entry.table = PathOfEntry(*base.table);
    entry.describe = "the statement on table " + entry.table.ToString();
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
