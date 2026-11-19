# Gives DuckDB's binder a "name scope" for stored expressions, and one hook that answers the
# function and type lookups made under it. Hardcharger's access control uses it so that a
# view's query, a column default, a CHECK constraint, a generated column, a trigger's body and
# a policy find every function, macro, table macro and type in ONE place fixed by the object
# they belong to (its schema, else DuckDB's own), never by the session that happens to read or
# write: not its search path, its current catalog, its temporary schema, nor a method call
# (x.f()) of a function that is not DuckDB's own. (src/authz/name_scope.cc; the design is in
# its header.)
#
# What it changes, all of it inert while the hook is null (it is unless access control is on):
#
#   catalog_entry_retriever.hpp/.cpp  CatalogEntryRetriever carries an optional scope (a schema
#       and what it belongs to); Inherit copies it, so child binders have their parent's.
#       HardchargerNameScopeGuard sets one for a C++ scope. The hook's declaration.
#   catalog.cpp                 Catalog::GetEntry(retriever, ...) asks the hook first when the
#       retriever has a scope.
#   bind_basetableref.cpp       a view's binder gets the view's scope (reading a view).
#   bind_insert/update/merge_into.cpp   column defaults are bound under their table's scope,
#       also where a default is written into the statement (VALUES (..., DEFAULT); the columns
#       an INSERT ... ON CONFLICT leaves out).
#   bind_create_table.cpp, table_catalog_entry.cpp   Binder::BindConstraints(table), the CHECKs
#       of a table being written, and TableCatalogEntry::BindUpdateConstraints, which binds them
#       again to learn which columns an UPDATE must carry, bind under the table's scope.
#   bind_context.cpp, binder.hpp/.cpp, expression_binder.cpp, column_qualifier.cpp
#       a generated column's expression, which the binder splices into the reading statement's
#       own expression, is wrapped in a marker call that carries its table's scope to wherever
#       it is bound. The marker's name holds a random id registered in the statement's binder
#       state; no SQL can name one.
#   column_qualifier.cpp        under a scope, x.f() falls back to f(x) only for DuckDB's own f.
#   bind_macro_expression.cpp   the binder that qualifies a macro's body has the caller's scope.
#   bind_trigger_expansion.cpp, bind_cte_node.cpp   a trigger's body (statement and row
#       triggers) is bound under its table's scope.
#   data_table.cpp              generated columns checked at append, and defaults filled in by
#       an append of some columns, are bound under their table's scope (at execution).
#   table_index_list.cpp        an index's expressions, bound when first needed after a start.
#   bind_create.cpp             under a scope, a nested schema path without a catalog is in the
#       scope's catalog.
#
# A source that no longer matches is a build error, never a silent skip: without the scope a
# stored expression would find its functions through the session again.
#
# Run as: cmake -DDUCKDB_SOURCE_DIR=<dir> -P patch-duckdb-name-scope.cmake

set(MARKER "hardcharger_scoped_lookup_hook")
file(READ "${DUCKDB_SOURCE_DIR}/src/include/duckdb/catalog/catalog_entry_retriever.hpp" already_content)
string(FIND "${already_content}" "${MARKER}" already)
if(NOT already EQUAL -1)
  message(STATUS "patch-duckdb-name-scope: already patched")
  return()
endif()

# Replaces `anchor`, which must occur exactly once in the file, by `replacement`.
function(hc_edit relative anchor replacement)
  set(path "${DUCKDB_SOURCE_DIR}/${relative}")
  file(READ "${path}" content)
  string(FIND "${content}" "${anchor}" first_at)
  string(FIND "${content}" "${anchor}" last_at REVERSE)
  if(first_at EQUAL -1 OR NOT first_at EQUAL last_at)
    message(FATAL_ERROR "patch-duckdb-name-scope: an anchor in ${relative} was not found exactly once; "
                        "update server/scripts/patch-duckdb-name-scope.cmake for this DuckDB. Anchor:\n${anchor}")
  endif()
  string(REPLACE "${anchor}" "${replacement}" content "${content}")
  file(WRITE "${path}" "${content}")
endfunction()
function(hc_after relative anchor addition)
  hc_edit("${relative}" "${anchor}" "${anchor}${addition}")
endfunction()
function(hc_before relative anchor addition)
  hc_edit("${relative}" "${anchor}" "${addition}${anchor}")
endfunction()

# ---- 1. The scope, on the retriever ----
set(RETRIEVER_HPP "src/include/duckdb/catalog/catalog_entry_retriever.hpp")
hc_after("${RETRIEVER_HPP}" "using catalog_entry_callback_t = std::function<void(CatalogEntry &)>;\n" [==[

class StandardEntry;

//! Hardcharger: where the functions, macros and types a stored expression names are found (a view's
//! query, a column default, a CHECK constraint, a generated column, a trigger's body, a policy): this
//! schema and DuckDB's own, whatever the session. server/scripts/patch-duckdb-name-scope.cmake.
struct HardchargerNameScope {
	HardchargerNameScope(SchemaCatalogEntry &schema_p, string kind_p, string object_p)
	    : schema(schema_p), kind(std::move(kind_p)), object(std::move(object_p)) {
	}
	//! The scope of a table's or view's own expressions (null while no hook is set)
	static shared_ptr<const HardchargerNameScope> Of(const StandardEntry &entry, const char *kind);

	//! The prefix of the marker call that carries a scope with a spliced expression (Binder::HardchargerScoped)
	static constexpr const char *MARKER = "__hardcharger_name_scope_";

	SchemaCatalogEntry &schema;
	//! What the expression belongs to, for messages: "table", "view", "trigger", "policy"; and its name
	string kind;
	string object;
};

//! Answers a lookup made under a scope. Returns false for a lookup that is not its to answer (DuckDB's
//! own lookup follows); true with the entry (or null, where the caller takes null for "not found").
//! Null unless Hardcharger sets it.
extern bool (*hardcharger_scoped_lookup_hook)(ClientContext &context, const HardchargerNameScope &scope,
                                              const EntryLookupInfo &lookup_info, OnEntryNotFound on_entry_not_found,
                                              optional_ptr<CatalogEntry> &result);
]==])
hc_after("${RETRIEVER_HPP}" "\tvoid SetCallback(catalog_entry_callback_t callback);\n" [==[
	//! Hardcharger: the name scope of the stored expression this retriever binds for (null: none)
	const shared_ptr<const HardchargerNameScope> &GetNameScope() const {
		return name_scope;
	}
	//! Sets it, and returns the one it had
	shared_ptr<const HardchargerNameScope> SetNameScope(shared_ptr<const HardchargerNameScope> scope);
]==])
hc_edit("${RETRIEVER_HPP}" "\toptional_ptr<BoundAtClause> at_clause;\n};\n" [==[
	optional_ptr<BoundAtClause> at_clause;
	shared_ptr<const HardchargerNameScope> name_scope;
};

//! Hardcharger: binds under `scope` until it goes out of (C++) scope; a null scope changes nothing
class HardchargerNameScopeGuard {
public:
	HardchargerNameScopeGuard(CatalogEntryRetriever &retriever_p, shared_ptr<const HardchargerNameScope> scope)
	    : retriever(retriever_p), active(scope != nullptr) {
		if (active) {
			previous = retriever.SetNameScope(std::move(scope));
		}
	}
	~HardchargerNameScopeGuard() {
		if (active) {
			retriever.SetNameScope(std::move(previous));
		}
	}
	HardchargerNameScopeGuard(const HardchargerNameScopeGuard &) = delete;
	HardchargerNameScopeGuard &operator=(const HardchargerNameScopeGuard &) = delete;

private:
	CatalogEntryRetriever &retriever;
	bool active;
	shared_ptr<const HardchargerNameScope> previous;
};
]==])

set(RETRIEVER_CPP "src/catalog/catalog_entry_retriever.cpp")
hc_after("${RETRIEVER_CPP}" "#include \"duckdb/main/database_manager.hpp\"\n" "#include \"duckdb/catalog/standard_entry.hpp\"\n")
hc_after("${RETRIEVER_CPP}" "namespace duckdb {\n" [==[

// Hardcharger (server/scripts/patch-duckdb-name-scope.cmake)
bool (*hardcharger_scoped_lookup_hook)(ClientContext &context, const HardchargerNameScope &scope,
                                       const EntryLookupInfo &lookup_info, OnEntryNotFound on_entry_not_found,
                                       optional_ptr<CatalogEntry> &result) = nullptr;

shared_ptr<const HardchargerNameScope> HardchargerNameScope::Of(const StandardEntry &entry, const char *kind) {
	if (!hardcharger_scoped_lookup_hook) {
		return nullptr;
	}
	return make_shared_ptr<HardchargerNameScope>(entry.schema, kind, entry.name.GetIdentifierName());
}

shared_ptr<const HardchargerNameScope>
CatalogEntryRetriever::SetNameScope(shared_ptr<const HardchargerNameScope> scope) {
	auto previous = std::move(name_scope);
	name_scope = std::move(scope);
	return previous;
}
]==])
hc_after("${RETRIEVER_CPP}" "\tthis->at_clause = parent.at_clause;\n" "\tthis->name_scope = parent.name_scope;\n")

# ---- 2. The hook, where every lookup through a retriever ends up ----
hc_before("src/catalog/catalog.cpp" "\tauto result = TryLookupEntryAcrossCatalogs(retriever, lookup_info, if_not_found);\n" [==[
	if (hardcharger_scoped_lookup_hook && retriever.GetNameScope()) {
		// Hardcharger: a stored expression's functions and types are found under its scope
		optional_ptr<CatalogEntry> hc_entry;
		if (hardcharger_scoped_lookup_hook(retriever.GetContext(), *retriever.GetNameScope(), lookup_info, if_not_found,
		                                   hc_entry)) {
			return hc_entry;
		}
	}
]==])

# ---- 3. Views ----
hc_after("src/planner/binder/tableref/bind_basetableref.cpp"
         "view_binder->entry_retriever.SetSearchPath(std::move(view_search_path));\n" [==[
		// Hardcharger: the view's functions and types are found in the view's schema, whoever reads it
		view_binder->entry_retriever.SetNameScope(HardchargerNameScope::Of(view_catalog_entry, "view"));
]==])

# ---- 4. Column defaults (INSERT, UPDATE, MERGE) ----
foreach(dml insert update merge_into)
  hc_edit("src/planner/binder/statement/bind_${dml}.cpp"
          "\tBindDefaultValues(table.GetColumns(), ${dml}->bound_defaults, catalog_name.GetIdentifierName(),\n\t                  schema_name.GetIdentifierName());\n"
          "\t{\n\t\t// Hardcharger: a default's functions and types are found in its table's schema\n\t\tHardchargerNameScopeGuard hc_scope(entry_retriever, HardchargerNameScope::Of(table, \"table\"));\n\t\tBindDefaultValues(table.GetColumns(), ${dml}->bound_defaults, catalog_name.GetIdentifierName(),\n\t\t                  schema_name.GetIdentifierName());\n\t}\n")
endforeach()

# A default written into the statement itself (VALUES (..., DEFAULT), in an INSERT and in a
# MERGE's INSERT; the columns an INSERT ... ON CONFLICT with a column list leaves out): a parsed
# copy, bound with the statement, so it carries its table's scope in a marker (section 6).
hc_edit("src/planner/binder/statement/bind_insert.cpp"
        "\t\t\tTryReplaceDefaultExpression(expr_list.values[list_idx][col_idx], column);\n" [==[
			auto &hc_value = expr_list.values[list_idx][col_idx];
			const bool hc_default =
			    hc_value->GetExpressionType() == ExpressionType::VALUE_DEFAULT && column.HasDefaultValue();
			TryReplaceDefaultExpression(hc_value, column);
			if (hc_default) {
				// Hardcharger: the column's default, written into the statement: still its table's names
				hc_value = HardchargerScoped(std::move(hc_value), HardchargerNameScope::Of(table, "table"));
			}
]==])
hc_after("src/planner/binder/statement/bind_insert.cpp" "\t\t\t\t\t\texpr = column.DefaultValue().Copy();\n" [==[
						// Hardcharger: bound with the statement's source: still its table's names
						expr = HardchargerScoped(std::move(expr), HardchargerNameScope::Of(table, "table"));
]==])
hc_edit("src/planner/binder/statement/bind_merge_into.cpp"
        "\t\t\tTryReplaceDefaultExpression(action.expressions[i], column);\n" [==[
			const bool hc_default =
			    action.expressions[i]->GetExpressionType() == ExpressionType::VALUE_DEFAULT && column.HasDefaultValue();
			TryReplaceDefaultExpression(action.expressions[i], column);
			if (hc_default) {
				// Hardcharger: the column's default, written into the statement: still its table's names
				action.expressions[i] =
				    HardchargerScoped(std::move(action.expressions[i]), HardchargerNameScope::Of(table, "table"));
			}
]==])

# ---- 5. CHECK constraints of a table being written ----
hc_after("src/planner/binder/statement/bind_create_table.cpp"
         "vector<unique_ptr<BoundConstraint>> Binder::BindConstraints(const TableCatalogEntry &table) {\n" [==[
	// Hardcharger: a CHECK's functions and types are found in its table's schema, whoever writes
	HardchargerNameScopeGuard hc_scope(entry_retriever, HardchargerNameScope::Of(table, "table"));
]==])
# An UPDATE binds the CHECKs once more, to learn which columns it must carry for them: under the
# same scope, or the two bindings disagree about the columns (DuckDB then fails the statement
# with a fatal error that invalidates the database).
hc_before("src/catalog/catalog_entry/table_catalog_entry.cpp"
          "\tauto bound_constraints = binder.BindConstraints(constraints, name, GetColumns());\n" [==[
	// Hardcharger: as Binder::BindConstraints(table) binds them (patch-duckdb-name-scope.cmake)
	HardchargerNameScopeGuard hc_scope(binder.EntryRetriever(), HardchargerNameScope::Of(*this, "table"));
]==])

# ---- 6. Generated columns: a marker carries the scope with the spliced expression ----
set(BINDER_HPP "src/include/duckdb/planner/binder.hpp")
hc_edit("${BINDER_HPP}" "\tIdentifier trigger_creation_name;\n};\n" [==[
	Identifier trigger_creation_name;
	//! Hardcharger: the name scopes of this statement's spliced expressions, by their marker's name
	//! (Binder::HardchargerScoped), and of query nodes bound with one (a trigger's body)
	identifier_map_t<shared_ptr<const HardchargerNameScope>> hardcharger_marked_scopes;
	reference_map_t<QueryNode, shared_ptr<const HardchargerNameScope>> hardcharger_node_scopes;
};
]==])
hc_after("${BINDER_HPP}" "\tCatalogEntryRetriever &EntryRetriever() {\n\t\treturn entry_retriever;\n\t}\n" [==[
	//! Hardcharger: `expr` wrapped in a marker call, so that wherever it is bound in this statement its
	//! functions and types are found under `scope` (a generated column's expression is spliced into the
	//! reading statement's own). A null scope returns `expr` as it is.
	unique_ptr<ParsedExpression> HardchargerScoped(unique_ptr<ParsedExpression> expr,
	                                               shared_ptr<const HardchargerNameScope> scope);
	//! The scope a marker call of this statement carries; null for any other function
	shared_ptr<const HardchargerNameScope> HardchargerScopeOf(const FunctionExpression &function);
]==])

set(BINDER_CPP "src/planner/binder.cpp")
hc_after("${BINDER_CPP}" "#include \"duckdb/parser/expression/star_expression.hpp\"\n"
         "#include \"duckdb/parser/expression/function_expression.hpp\"\n#include <random>\n")
hc_before("${BINDER_CPP}" "//! Create a binder whose catalog search path is anchored to the table's catalog+schema\n" [==[
// Hardcharger (server/scripts/patch-duckdb-name-scope.cmake)
unique_ptr<ParsedExpression> Binder::HardchargerScoped(unique_ptr<ParsedExpression> expr,
                                                       shared_ptr<const HardchargerNameScope> scope) {
	if (!scope || !expr) {
		return expr;
	}
	// A name no SQL can give: an id drawn here, kept in this statement's binder state only.
	std::random_device random;
	string name = HardchargerNameScope::MARKER;
	for (idx_t i = 0; i < 4; i++) {
		auto word = static_cast<uint32_t>(random());
		for (idx_t nibble = 0; nibble < 8; nibble++, word >>= 4) {
			name += "0123456789abcdef"[word & 0xF];
		}
	}
	Identifier marker(name);
	global_binder_state->hardcharger_marked_scopes[marker] = std::move(scope);
	auto alias = expr->GetAlias();
	auto location = expr->GetQueryLocation();
	vector<unique_ptr<ParsedExpression>> children;
	children.push_back(std::move(expr));
	auto wrapped = make_uniq<FunctionExpression>(marker, std::move(children));
	wrapped->SetAlias(alias);
	wrapped->SetQueryLocation(location);
	return std::move(wrapped);
}

shared_ptr<const HardchargerNameScope> Binder::HardchargerScopeOf(const FunctionExpression &function) {
	if (!function.FunctionName().StartsWith(HardchargerNameScope::MARKER) || function.GetArguments().size() != 1) {
		return nullptr;
	}
	auto entry = global_binder_state->hardcharger_marked_scopes.find(function.FunctionName());
	if (entry == global_binder_state->hardcharger_marked_scopes.end()) {
		return nullptr;
	}
	return entry->second;
}

]==])

hc_after("src/planner/bind_context.cpp"
         "\tauto result = table_binding.ExpandGeneratedColumn(column_name);\n\tresult->SetAlias(column_name);\n" [==[
	// Hardcharger: the expression is bound as part of the reading statement; its functions and types
	// are still found in its table's schema
	if (auto hc_entry = table_binding.GetStandardEntry()) {
		result = binder.HardchargerScoped(std::move(result), HardchargerNameScope::Of(*hc_entry, "table"));
	}
]==])

hc_after("src/planner/expression_binder.cpp" "\t\tauto &function = expr_ref.Cast<FunctionExpression>();\n" [==[
		if (auto hc_scope = binder.HardchargerScopeOf(function)) {
			// Hardcharger: a generated column's expression, bound under its table's name scope
			HardchargerNameScopeGuard hc_guard(binder.EntryRetriever(), std::move(hc_scope));
			return BindExpression(function.GetArgumentsMutable()[0].GetExpressionMutable(), depth, root_expression);
		}
]==])

set(QUALIFIER_CPP "src/planner/column_qualifier.cpp")
hc_before("${QUALIFIER_CPP}" "\t\tif (!ExpressionBinder::IsUnnestFunction(function.FunctionName())) {\n\t\t\tQualifyFunction(function);\n\t\t}\n" [==[
		if (auto hc_scope = binder.HardchargerScopeOf(function)) {
			// Hardcharger: a generated column's expression, qualified under its table's name scope
			HardchargerNameScopeGuard hc_guard(binder.EntryRetriever(), std::move(hc_scope));
			QualifyColumnNames(function.GetArgumentsMutable()[0].GetExpressionMutable(), lambda_params, false);
			return;
		}
]==])
# ---- 7. Method calls ----
hc_after("${QUALIFIER_CPP}" "\t\t// we could not find the function - bail\n\t\treturn nullptr;\n\t}\n" [==[
	if (hardcharger_scoped_lookup_hook && binder.EntryRetriever().GetNameScope() &&
	    !func->ParentCatalog().IsSystemCatalog()) {
		// Hardcharger: inside a stored expression only DuckDB's own functions are called as value.f()
		return nullptr;
	}
]==])

# ---- 8. Macros: the binder that qualifies a macro's body has the caller's scope ----
hc_after("src/planner/binder/expression/bind_macro_expression.cpp"
         "\tdummy_binder->macro_binding = new_macro_binding.get();\n"
         "\tdummy_binder->EntryRetriever().SetNameScope(binder.EntryRetriever().GetNameScope()); // Hardcharger\n")

# ---- 9. Trigger bodies ----
set(TRIGGER_CPP "src/planner/binder/query_node/bind_trigger_expansion.cpp")
hc_before("${TRIGGER_CPP}" "\tauto chain = Bind(*outer);\n" [==[
	if (auto hc_scope = HardchargerNameScope::Of(table, "trigger")) {
		// Hardcharger: each trigger's body (every CTE of the chain but the writer's own statement) finds
		// its functions and types in the trigger's table's schema, whoever fires it
		for (auto &hc_cte : outer->cte_map.map) {
			if (hc_cte.second->is_trigger_generated && !(hc_cte.first == base_cte_name)) {
				global_binder_state->hardcharger_node_scopes[*hc_cte.second->query_node] = hc_scope;
			}
		}
	}
]==])
hc_after("${TRIGGER_CPP}" "\t\tauto child_binder = Binder::CreateBinder(context, this);\n" [==[
		child_binder->EntryRetriever().SetNameScope(HardchargerNameScope::Of(table, "trigger")); // Hardcharger
]==])
hc_after("src/planner/binder/query_node/bind_cte_node.cpp" "BoundStatement Binder::BindNode(QueryNode &node) {\n" [==[
	if (hardcharger_scoped_lookup_hook) {
		// Hardcharger: a query node registered with a name scope (a trigger's body) is bound under it
		auto hc_scope = global_binder_state->hardcharger_node_scopes.find(node);
		if (hc_scope != global_binder_state->hardcharger_node_scopes.end()) {
			entry_retriever.SetNameScope(hc_scope->second);
		}
	}
]==])

# ---- 10. Bound at execution: generated columns at append, defaults of a partial append ----
set(DATA_TABLE_CPP "src/storage/data_table.cpp")
hc_after("${DATA_TABLE_CPP}" "\t\tauto binder = Binder::CreateBinder(context);\n\t\tphysical_index_set_t bound_columns;\n"
         "\t\tbinder->EntryRetriever().SetNameScope(HardchargerNameScope::Of(table, \"table\")); // Hardcharger\n")
hc_after("${DATA_TABLE_CPP}" "\tauto binder = Binder::CreateBinder(context);\n\tConstantBinder default_binder(*binder, context, \"DEFAULT value\");\n"
         "\tbinder->EntryRetriever().SetNameScope(HardchargerNameScope::Of(table, \"table\")); // Hardcharger\n")

# ---- 11. Index expressions, bound when the index is first needed ----
hc_after("src/storage/table_index_list.cpp" "\t\t// Create a binder to bind this index.\n\t\tauto binder = Binder::CreateBinder(context);\n"
         "\t\tbinder->EntryRetriever().SetNameScope(HardchargerNameScope::Of(table, \"table\")); // Hardcharger\n")

# ---- 12. A qualifier's catalog ----
set(CREATE_CPP "src/planner/binder/statement/bind_create.cpp")
hc_edit("${CREATE_CPP}" "QualifiedName Binder::BindTableName(const QualifiedName &name) {\n\tCatalogEntryRetriever retriever(context);\n" [==[
QualifiedName Binder::BindTableName(const QualifiedName &name) {
	if (hardcharger_scoped_lookup_hook && entry_retriever.GetNameScope()) {
		// Hardcharger: under a name scope, with the binder's own retriever (which has the scope)
		return BindTableName(entry_retriever, name);
	}
	CatalogEntryRetriever retriever(context);
]==])
hc_edit("${CREATE_CPP}" "\tauto catalog = path.front();\n\tif (IsInvalidCatalog(catalog)) {\n" [==[
	auto catalog = path.front();
	if (IsInvalidCatalog(catalog) && hardcharger_scoped_lookup_hook && retriever.GetNameScope()) {
		// Hardcharger: under a name scope a nested schema path is in the scope's catalog, not the session's
		catalog = retriever.GetNameScope()->schema.ParentCatalog().GetName();
	}
	if (IsInvalidCatalog(catalog)) {
]==])

message(STATUS "patch-duckdb-name-scope: name scopes and the scoped lookup hook added to ${DUCKDB_SOURCE_DIR}")
