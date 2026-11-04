// Triggers under access control (the CREATE TRIGGER and DROP TRIGGER privileges, granted
// ON SCHEMA or ON CATALOG, plus ownership of the table).
//
// DuckDB 2.0 binds a trigger's body into the statement that fires it, so the plan check
// sees the body as part of the writer's statement. A trigger an administrator made (or one
// without an owner record) runs with the writer's rights, as DuckDB runs it. A user's
// trigger runs with its OWNER's rights (definer rights): the owner chose what the body
// does, so the body is checked, and row and masking policies applied, as if the owner ran
// it, whoever writes the table. An administrator's write lends it nothing.
//
// Where a body sits in the plan (bind_trigger_expansion.cpp): a statement trigger's body is
// the definition of a materialized CTE named __duckdb_trigger_body_<n>_<uuid> (BEFORE:
// __duckdb_trigger_before_body_<n>_<uuid>) beside the base CTE __duckdb_trigger_base_<uuid>
// that holds the writer's own INSERT / UPDATE / DELETE. Which trigger it belongs to is read
// back the way the binder chose it: the table's statement triggers of that timing and
// event, in the table's order, less the UPDATE OF ones no updated column fires. Row
// triggers' bodies are bound under __duckdb_trigger_body_row_<uuid>, each a LogicalTrigger
// node at bind time; by the time the plan is optimized DuckDB has flattened them (NEW.col
// is a correlated column), so a row trigger's body is found as the INSERT, UPDATE or DELETE
// in there on the table its action names. A body that can't be matched refuses the
// statement (fail closed).

#pragma once

#include <duckdb.hpp>
#include <duckdb/catalog/catalog_entry/trigger_catalog_entry.hpp>
#include <duckdb/parser/parsed_data/create_trigger_info.hpp>
#include <optional>
#include <string>
#include <vector>

#include "authz/model.hpp"

namespace hardcharger::authz {

struct TriggerBody {
  // The operator the body starts at (a CTE's definition, or a row trigger's DML).
  const duckdb::LogicalOperator* root = nullptr;
  // Not a body: the writer's own INSERT / UPDATE / DELETE under a base CTE, to which the
  // binder added RETURNING * for the triggers' transition tables. The writer sees those
  // rows only when the statement had a RETURNING clause of its own (returning_to_user).
  bool base = false;
  bool returning_to_user = false;
  ObjectPath trigger;  // the owner record's path (TriggerOwnerPath)
  ObjectPath table;
  std::string describe;  // "trigger t on table c.s.t"
  // The user or role that owns the trigger: its body is checked with their rights.
  // Unset: the writer's rights (an administrator's trigger, or one without a record).
  std::optional<Principal> definer;
  std::string definer_name;
  // Why the body can't run at all (its owner is gone, or the trigger couldn't be matched).
  std::string problem;
};

using TriggerBodies = std::vector<TriggerBody>;

// Every trigger body in `plan`, in plan order. Runs inside the statement's transaction.
TriggerBodies FindTriggerBodies(duckdb::ClientContext& context, const Snapshot& snapshot,
                                duckdb::LogicalOperator& plan);

// The body (or base DML) rooted at `op`, if any.
const TriggerBody* TriggerBodyAt(const TriggerBodies& bodies, const duckdb::LogicalOperator& op);

// Is `op` a writer's DML whose RETURNING the binder added for the triggers alone?
bool ReturningAddedForTriggers(const TriggerBodies& bodies, const duckdb::LogicalOperator& op);

// Does any body in `bodies` run with an owner's rights, or refuse the statement?
bool HasDefinerBody(const TriggerBodies& bodies);

// The path a trigger's owner record is kept under: its table's path plus its name
// (triggers are named per table: two tables of one schema may each have a trigger t).
ObjectPath TriggerOwnerPath(const ObjectPath& table, const std::string& trigger_name);

// The hash an owner record keeps of a trigger's definition (what it fires on and does), so
// a stale record of an earlier trigger of the same name never applies to a new one. The
// same for the CREATE TRIGGER that makes it and for the trigger DuckDB keeps.
std::string TriggerDefinitionHash(const duckdb::CreateTriggerInfo& info);
std::string TriggerDefinitionHash(const duckdb::TriggerCatalogEntry& trigger);

}  // namespace hardcharger::authz
