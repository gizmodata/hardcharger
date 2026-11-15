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
//
// Chains. The statement a trigger fires on is the one INSERT, UPDATE, DELETE or merge in
// its base CTE's definition outside any CTE's definition there: a WITH of the statement's
// own, and the alias the binder gives a transition table, put CTE nodes above it. A body
// that writes a table with statement triggers of its own is bound as that table's whole
// chain (its base CTE, the nested bodies beside it), which is then the body: the nested
// bodies are checked beneath it, with their owners' rights or, for an administrator's
// trigger, the rights of the body that fired it. INSERT ... ON CONFLICT is a merge in the
// plan and fires the INSERT triggers. A CTE the writer names like one of the binder's gets
// nothing from it: the statement in it is still expanded by the binder, so it is never the
// one write its "base" would need, and the statement is refused.
//
// Names in a body. DuckDB keeps a trigger's action as it was written and binds it again in
// every statement that fires it, in the WRITER's session. A table, view or sequence the
// action names without its catalog and schema is therefore looked up with the writer's
// search path, the writer's temporary objects first: a writer with a temporary table named
// like the table an audit trigger writes had the audit row land in its own table, and one
// named like the table a user's trigger writes read there what the trigger's owner may read.
// So, in an access-controlled session, each name in a body must be, for the writer's
// session, the object it is for the trigger's own table (TriggerBodyNameProblem), whoever
// made the trigger and whoever writes, in chains too (each body against its own trigger's
// table). What a name is for the trigger's table ("canonical"; DuckDB lets a body name
// objects of the table's own catalog only):
//   - written in full (catalog, schemas, name): that object;
//   - without a catalog: the schema path it gives, in the table's catalog (and
//     "catalog.name": the catalog's default schema, main);
//   - bare: in the table's own schema, else in the catalog's default schema, main. One that
//     is in both is nobody's to choose: refused, for every writer, until the trigger's owner
//     names it in full (taking the table's schema would let whoever may create a table there
//     take over a name that meant main's).
// The statement is refused when the writer's session resolves a name to anything else (a
// temporary object, one reached by USE or the search path, another catalog's), and also
// when the body's plan reaches a table, view or sequence that no name in the action stands
// for (a macro's, a default's temporary sequence), or the names can't be read: fail closed.
// A name of a CTE or of a transition table is no table's. Functions, macros and types are
// not checked here: a session can make none that shadows one a body uses (temporary macros
// are refused, and a macro, sequence or type named like one a trigger uses is the
// administrator's alone to create: macros.cc). An administrator's own statements are not
// checked: it is the writer this guards against.
//
// New triggers don't depend on any of that: CREATE [OR REPLACE] TRIGGER under access
// control stores the action with its tables, views and sequences named in full, as the
// creator's session resolved them (QualifyTriggerNames), and the owner record's hash is of
// what is stored. The init SQL's triggers too.

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
  // The trigger whose action the body is; several when a row trigger's body could be any
  // of them.
  std::vector<const duckdb::TriggerCatalogEntry*> entries;
};

using TriggerBodies = std::vector<TriggerBody>;

// Every trigger body in `plan`, in plan order. Runs inside the statement's transaction.
TriggerBodies FindTriggerBodies(duckdb::ClientContext& context, const Snapshot& snapshot,
                                duckdb::LogicalOperator& plan);

// The body (or base DML) rooted at `op`, if any.
const TriggerBody* TriggerBodyAt(const TriggerBodies& bodies, const duckdb::LogicalOperator& op);

// Is `op` a writer's DML whose RETURNING the binder added for the triggers alone?
bool ReturningAddedForTriggers(const TriggerBodies& bodies, const duckdb::LogicalOperator& op);

// Why the statement that fires the trigger of `body` must not run in this session: a name
// in the body is not, for this session, what it is for the trigger's own table ("Names in a
// body", above). In words for a refusal; empty when every name is. Runs inside the
// statement's transaction.
std::string TriggerBodyNameProblem(duckdb::ClientContext& context, const TriggerBodies& bodies, const TriggerBody& body);

// CREATE [OR REPLACE] TRIGGER on `table`: writes into the action of `info` the full names of
// the tables, views and sequences it names, as this session resolves them, so that the
// trigger DuckDB stores depends on no session's search path. Leaves alone what it cannot
// resolve (a CTE's name, a transition table's). False (and the action as it was) when the
// action can't be read to the end.
bool QualifyTriggerNames(duckdb::ClientContext& context, const ObjectPath& table, duckdb::CreateTriggerInfo& info);

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
