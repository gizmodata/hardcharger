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
// every statement that fires it, inside the WRITER's statement and session. A table, view or
// sequence the action names without its catalog is therefore looked up with the writer's
// current catalog and search path, the writer's temporary objects first (the temporary
// catalog's schema is main, so a temporary audit_log is found before main.audit_log), and a
// bare name is looked up as a CTE before the catalog: a WITH of the writer's statement (or of a
// trigger body that fires this one, which binds it inside itself) is in scope in the body. A
// writer could so have an audit row land in its own table, feed an audit trigger rows of its
// choosing, or have a user's trigger, which runs with its owner's rights, read or write what the
// writer chose. So, in an access-controlled session, each name in a body must be, for the
// writer's session, the object it is for the trigger's own table (TriggerBodyNameProblem),
// whoever made the trigger and whoever writes, in chains too (each body against its own
// trigger's table). An administrator's own statements are checked only in the bodies of users'
// triggers they fire (those run with their owners' rights; an administrator's own trigger
// fired by an administrator is the administrator's to shadow). What a name is for the trigger's
// table ("canonical"; DuckDB lets a body name objects of the table's own catalog only):
//   - written in full (catalog, schemas, name): that object;
//   - without a catalog: the schema path it gives, in the table's catalog (and
//     "catalog.name": the catalog's default schema, main);
//   - bare: in the table's own schema, else in the catalog's default schema, main. One that
//     is in both is nobody's to choose: refused, for every writer, until the trigger's owner
//     names it with its schema (taking the table's schema would let whoever may create a table
//     there take over a name that meant main's). Only the administrator may create a table or
//     view that would put a bare name a trigger uses in both (TriggerNameRefusal).
// A bare name in the scope of a CTE of the body's own (DuckDB's scoping: a node's CTEs are
// seen by its body and by the CTEs after them in the same WITH, a CTE's own definition looks
// further out, a recursive CTE sees itself) or of a transition table is that, no table's.
// The statement is refused when:
//   - the writer's session resolves a name to anything else (a temporary object, one reached
//     by USE or the search path, another catalog's), saying what to do;
//   - the body (or a macro it calls) reads a CTE defined outside the body: in the bound plan
//     every CTE reference is a LogicalCTERef with its CTE's index, so one whose CTE is neither
//     inside the body nor one the binder made for the triggers (a base's rows, the place row
//     triggers sit, DuckDB's own "__duckdb_" CTEs for correlated subqueries, which no name in
//     the body or its macros gives) is a WITH from outside standing in (TriggerBody::outer_ctes);
//   - a macro the body calls is not, in this session, the one its name is for the table, or a
//     name in the macro's own text fails the same checks (table macros are bound where they are
//     called, in the writer's session, and see its CTEs);
//   - the body's plan reaches a table, view or sequence that no name in the action or in its
//     macros stands for (a default's temporary sequence), or the names can't be read (a
//     sequence, or a query_table() table, named by something other than a plain string):
//     fail closed.
// The types a cast names are held to the same rule (a user's type made before its name was
// pinned, earlier in the session's search path, would change what the cast does).
//
// A table's CHECK constraints and generated columns are kept as written too, and DuckDB binds
// them again in the session of each statement that writes the table (CHECK) or reads a
// generated column, inside a trigger's body as anywhere. Their macros (and those macros'
// names) and cast types are held to the same rule against the table they belong to: for every
// table a statement writes or scans outside a view's query (StatementTableExpressionProblem),
// and for every table a trigger's body reaches (TriggerBodyNameProblem: an administrator's
// statement too, for a user's trigger). CREATE TABLE stores the macros they call with their
// schema (QualifyTableExpressions), and a macro or type named like one a table's default,
// CHECK or generated column calls is the administrator's alone to create (macros.cc). Column
// defaults are bound with the table's own search path by DuckDB, so no session decides them.
// A row trigger's action may have a WITH of its own, which DuckDB binds above the action's
// INSERT, UPDATE or DELETE: the body starts at that WITH, so its definitions are checked with
// the trigger's rights and names like the rest of the body.
//
// New triggers don't depend on the search path within their catalog: CREATE [OR REPLACE]
// TRIGGER under access control stores the action with its tables, views and sequences named
// with their schema, as the creator's session resolved them (QualifyTriggerNames), and the
// owner record's hash is of what is stored; the init SQL's triggers too. Not with their
// catalog (Philip, 2.14.0): a catalog's name is the database file's or the ATTACH alias, and
// renaming either must not break writes. The price: a writer's temporary object named like an
// object of main a trigger names, or a session whose current catalog is not the trigger
// table's, is refused rather than served.
// Carried columns. To feed the triggers their rows (transition tables, OLD and NEW), the
// binder adds RETURNING * to the writer's UPDATE, DELETE or merge, which makes the scan
// that locates the rows read every column of the table. Reading a column that way is no
// read of the writer's: a writer needs SELECT on what its own statement reads (WHERE, a
// SET expression, a join, a subquery), as it would without the triggers. A column of that
// scan is "carried", and asks no SELECT of the writer, only when every use of it in the
// whole plan is one of the binder's own hand-overs to the write operator:
//
//   DELETE   an entry of the DELETE's own expression list that is the bare column
//            (bind_delete.cpp: the row id, then the columns of the row it returns);
//   UPDATE   the entry of the UPDATE's own expression list for a column that is that very
//            column, bare ("c = c", which the binder adds for every column the statement
//            did not set: bind_update.cpp, BindUpdateConstraints), or a column of the OLD
//            row image the UPDATE takes by place (LogicalUpdate::old_row_columns);
//   merge    (INSERT ... ON CONFLICT DO UPDATE) the same "c = c" of an update action.
//
// A column is still itself where an operator gives it on unchanged: an entry of a
// projection that is the bare column, and a materialized CTE's column for every scan of
// that CTE (DuckDB keeps the outer rows of a correlated subquery in CTEs of its own). Those
// are followed, and the uses of the column under its new names count as uses of it.
//
// Anything else is a read and needs SELECT:
//   - a column used in a filter, a join condition, a SET or RETURNING expression, an
//     aggregate, a subquery (also where an operator names it in a correlated-column list
//     rather than an expression), and a column of the scan that nothing uses;
//   - a column the write itself can tell of: one in a key or an index of the table (a
//     constraint error names a key's value, and DuckDB runs an UPDATE that returns its
//     rows as a delete and an insert, so a foreign key that refers to the row stops it),
//     and for an UPDATE a column of a CHECK constraint over several columns (a failed
//     CHECK tells of the ones the statement did not set), or of one that calls anything but
//     DuckDB's own functions or casts to anything but its own types (a macro can raise an
//     error that names the value: UsesOnlyBuiltIns);
//   - every column, when the plan is not laid out as this expects. What it expects: the
//     write takes its row ids from one plain scan of the written table (no filter pushed
//     into it); every operator beneath the write is one whose uses of columns this file
//     knows how to list (ListsItsColumnUses), with no expression it can't look into; from
//     each operator that has the column, only filters and joins (which hand a column on as
//     itself) come before the one that takes it (a projection or an aggregate, which give
//     on only what their expressions name, a materialized CTE's definition, or the
//     write); the write's rows go into the base CTE through a projection of bare columns
//     (so no generated column is computed from them here) and nowhere else; and the
//     writer's statement has no RETURNING of its own.
// So a carried value can only end up written back where it was, or in the base CTE's rows.
//
// Who reads those rows. A scan of the base CTE is "blind" when the operator right above it
// is a projection or an aggregate and no expression anywhere names a column of the scan
// that comes from the write: the binder's own SELECT count_star() FROM base, which gives
// the writer the number of rows changed and no value. Every other scan of the base CTE is
// a row reader: a transition table (REFERENCING OLD/NEW TABLE, an alias CTE in the
// trigger's body) or the rows a row trigger's OLD and NEW come from. (The rows that feed
// OLD and NEW sit beside the row triggers' bodies, not inside one: each row trigger's body
// answers for them. DuckDB makes every row trigger's body name OLD or NEW.) None of this
// trusts the CTE's name: a CTE the writer names like a base is held to the same rules.
//
// Who answers for a row reader. Where the rights the write's scan was checked with hold
// SELECT on the carried columns, nothing was waived and nobody else needs to (as it was
// before there were carried columns). Where they don't, somebody must answer for the
// columns the writer was let off, and it is one of three:
//
//   1. An administrator, for a trigger an administrator made. Such a trigger VOUCHES for
//      its body's reads of the rows of the statement that fires it: the writer is not
//      asked for SELECT because of them. The administrator wrote the body and so chose
//      where the rows go; an administrator who puts a row-copying trigger on a table
//      chooses who can see those rows. Only these reads are vouched for: everything else
//      the body does (the tables it writes, other tables it reads) is checked with the
//      writer's rights, as ever. A trigger vouches when, and only when, its owner record
//      is a system one (written when an administrator's own session created or replaced
//      the trigger, under access control) AND that record's definition hash is the hash
//      of the trigger as it is now (TriggerBody::administrators). So a user's trigger an
//      administrator replaced vouches (the record is the administrator's, of the new
//      definition), and nothing a user can do makes a trigger vouch: a trigger a user
//      creates or replaces gets a user's record; one a user drops and recreates, the
//      same; DuckDB renames no table that has a trigger (and a rename moves a table's
//      trigger records as they are); a trigger on a temporary table has no record at
//      all, and DuckDB has no temporary trigger on another table. A trigger with NO
//      record does not vouch, though it too runs with the writer's rights: nobody
//      recorded who made it (the init SQL's DuckDB statements run outside access control,
//      and a database file comes with whatever triggers it had, also one a user was let
//      ATTACH). Nor does a record that no longer matches the definition (a user's or a
//      system one: the trigger was changed outside access control). Those are case 3.
//   2. The trigger's owner, for a user's trigger: the owner needs SELECT on those
//      columns (checked with the owner's rights, like the rest of the body).
//   3. Otherwise the writer: SELECT, as before there were carried columns. That is a
//      reader in the writer's own statement, and a trigger nobody recorded.
//
// Cases 1 and 2 rest on the body doing what its maker wrote, whoever fires it: the names
// in every body (and in the macros it calls) are held against the writer's session before any
// of this, and no WITH from outside the body stands in for what it reads ("Names in a body",
// above), so a writer cannot have the rows land in a table of its own or the body read rows of
// its choosing. A table with keys, indexes or a CHECK over several columns, and one with
// generated columns, still takes the writer's SELECT on those columns; the refusal says why.

#pragma once

#include <duckdb.hpp>
#include <duckdb/catalog/catalog_entry/trigger_catalog_entry.hpp>
#include <duckdb/parser/parsed_data/create_trigger_info.hpp>
#include <duckdb/parser/parsed_data/create_table_info.hpp>
#include <optional>
#include <set>
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
  // An administrator made this very trigger: its owner record is a system one whose
  // definition hash is the trigger's as it is now ("Who answers for a row reader", above).
  bool administrators = false;
  // CTEs defined outside the body that the body reads (by name): the firing statement's WITH,
  // or an enclosing trigger body's, bound where the body names a table ("Names in a body").
  std::vector<std::string> outer_ctes;
  // The trigger whose action the body is; several when a row trigger's body could be any
  // of them (then `administrators` holds for all of them).
  std::vector<const duckdb::TriggerCatalogEntry*> entries;

  // ---- carried columns (see above) ----
  // A base UPDATE, DELETE or merge whose plan is laid out as described above: the scan
  // of the written table that locates its rows (else nullptr, and nothing is carried), ...
  const duckdb::LogicalOperator* target_scan = nullptr;
  // ... the scan's columns the statement itself reads (SELECT, as without triggers), ...
  std::vector<std::string> read_columns;
  // (of those, the ones it reads only because a key, an index or a CHECK over several columns
  // of the table can tell their values: for the refusal's words) ...
  std::vector<std::string> told_columns;
  // ... its other columns, which only the triggers' rows need, ...
  std::vector<std::string> carried_columns;
  // ... and the scans of the base CTE that can read those rows with the rights in force
  // where they sit (transition tables).
  std::vector<const duckdb::LogicalOperator*> row_readers;
  // A row trigger's body: the base write (a base's root) whose rows its OLD and NEW are.
  const duckdb::LogicalOperator* reads_rows_of = nullptr;
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

// CREATE [OR REPLACE] TRIGGER on `table`: writes into the action of `info` the schema of each
// table, view and sequence it names (not the catalog: a database file renamed, or attached
// under another name, keeps its triggers), as this session resolves them, so that the
// trigger DuckDB stores depends on no session's search path within its catalog. Leaves alone
// what it cannot resolve (a CTE's name where that CTE is in scope, a transition table's). A
// reason the trigger can't be stored (in words for a refusal; the action as it was) when the
// action can't be read to the end, or names what could never be checked when it fires (a
// sequence by something other than a plain string, query() or query_table() of one that is
// not a table's name); empty when it can.
std::string QualifyTriggerNames(duckdb::ClientContext& context, const ObjectPath& table, duckdb::CreateTriggerInfo& info);

// Definition time (name_scope.hpp): why a stored expression being created must not be stored as
// written: a function, macro or type it names (or one a macro it calls names) is, in this
// session, something its name scope `catalog`.`schema_path` will not find when the expression
// is bound (another schema's by the search path, a temporary one, a method-style call of a
// macro). In words for a refusal that say what to write; "" when every name is the same.
// `what`: "view c.s.v". `aliases`: CTE names in scope around `node` (transition tables).
std::string StoredNamesProblem(duckdb::ClientContext& context, const std::string& catalog,
                               const std::vector<std::string>& schema_path, const std::string& what,
                               const duckdb::QueryNode* node,
                               const std::vector<const duckdb::ParsedExpression*>& expressions,
                               const std::set<std::string>& aliases = {});
// The same for a new table's CHECK constraints, generated columns and column defaults (after
// QualifyTableExpressions), and for a new trigger's action.
std::string TableStoredNamesProblem(duckdb::ClientContext& context, const ObjectPath& table,
                                    const duckdb::CreateTableInfo& info);
std::string TriggerStoredNamesProblem(duckdb::ClientContext& context, const ObjectPath& table,
                                      const duckdb::CreateTriggerInfo& info);

// CREATE TABLE / ALTER TABLE: the macros a CHECK constraint, a generated column or a column
// default calls, as this session resolves them, given their schema, so that the definition
// says what its creator meant and its name scope finds it (name_scope.hpp). A convenience:
// what this leaves as written is found under the scope or refused, never by a session.
void QualifyTableExpressions(duckdb::ClientContext& context, const std::string& catalog,
                             duckdb::CreateTableInfo& info);
void QualifyTableExpression(duckdb::ClientContext& context, const std::string& catalog,
                            duckdb::unique_ptr<duckdb::ParsedExpression>& expression);

// Does `expression` call DuckDB's own functions, and cast to its own types, only? (Nothing a
// user could create or a session's search path could choose.)
bool UsesOnlyBuiltIns(duckdb::ClientContext& context, const duckdb::ParsedExpression& expression);
bool BuiltInType(duckdb::ClientContext& context, const duckdb::ParsedExpression& expression, int depth);

// CREATE TABLE / CREATE VIEW (or a rename) of `path` by a user other than the administrator:
// why not, when a trigger of that catalog from before names the object bare, in its table's
// schema or main, and this would put one of that name in both (the trigger would then be
// refused for every writer, "Names in a body"). Nothing when it may.
std::optional<std::string> TriggerNameRefusal(duckdb::ClientContext& context, const ObjectPath& path);
// The base whose target scan `scan` is, when its columns were told apart (carried columns).
const TriggerBody* BaseScannedBy(const TriggerBodies& bodies, const duckdb::LogicalOperator& scan);

// A base UPDATE, DELETE or merge (with no RETURNING of the writer's) on `table`, if any: the
// statement writes a table with triggers.
const TriggerBody* BaseWriting(const TriggerBodies& bodies, const duckdb::TableCatalogEntry& table);

// The base whose rows the CTE scan `scan` can read (a row reader of it), if any.
const TriggerBody* BaseReadBy(const TriggerBodies& bodies, const duckdb::LogicalOperator& scan);

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
