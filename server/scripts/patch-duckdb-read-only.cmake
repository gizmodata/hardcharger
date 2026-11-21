# Two additions to DuckDB that the read-only sessions of the adapter are built on
# (src/duckdb/duckdb_read_only.hpp). Both act where DuckDB does the thing itself, so they
# hold wherever a call was written: in the client's statement, in a view or macro someone
# else defined, in a view that is bound only when a catalog function runs.
#
# 1. A sequence's use is registered as a write of its database before its value changes
#    (sequence_catalog_entry.cpp). DuckDB evaluates nextval/setval wherever they stand, also
#    while it binds a statement (a table function's argument), and registered the write only
#    when a statement was executed. With the registration at the use, DuckDB's own rules
#    apply there: a READ ONLY transaction refuses it, a transaction that has written another
#    database refuses it, and a statement that is only prepared leaves a transaction that
#    can commit. Temporary sequences are exempt, as temporary objects are in DuckDB.
#
# 2. A hook before any table function's bind step (bind_table_function.cpp): a null function
#    pointer called with the context and the function's name before bind, bind_replace or
#    bind_operator runs. The adapter's guard sets it and refuses, for a read-only session,
#    a function that is on none of its lists. Null unless set: DuckDB behaves as before.
#
# A source that no longer matches is a build error, never a silent skip.
#
# Run as: cmake -DDUCKDB_SOURCE_DIR=<dir> -P patch-duckdb-read-only.cmake

function(replace_once FILE_PATH BEFORE AFTER MARKER WHAT)
  file(READ "${FILE_PATH}" CONTENT)
  string(FIND "${CONTENT}" "${MARKER}" patched)
  if(NOT patched EQUAL -1)
    message(STATUS "patch-duckdb-read-only: ${WHAT}: already patched")
    return()
  endif()
  string(FIND "${CONTENT}" "${BEFORE}" first)
  string(FIND "${CONTENT}" "${BEFORE}" last REVERSE)
  if(first EQUAL -1 OR NOT first EQUAL last)
    message(FATAL_ERROR "patch-duckdb-read-only: ${WHAT}: not found exactly once in ${FILE_PATH}; update this patch for DuckDB")
  endif()
  string(REPLACE "${BEFORE}" "${AFTER}" CONTENT "${CONTENT}")
  file(WRITE "${FILE_PATH}" "${CONTENT}")
  message(STATUS "patch-duckdb-read-only: ${WHAT}: patched")
endfunction()

# ---- 1. sequences ----
set(SEQUENCE_FILE "${DUCKDB_SOURCE_DIR}/src/catalog/catalog_entry/sequence_catalog_entry.cpp")

set(before [=[int64_t SequenceCatalogEntry::NextValue(DuckTransaction &transaction) {
	lock_guard<mutex> seqlock(state->lock);
]=])
set(after [=[// Chargewire: the use of a sequence is a write of its database. DuckDB is told before the
// value changes, where it would otherwise learn of it only when a statement is executed: a
// READ ONLY transaction, a database attached read-only and a transaction that has written
// another database refuse here, with nothing changed.
static void ChargewireRegisterSequenceWrite(SequenceCatalogEntry &sequence, DuckTransaction &transaction) {
	if (sequence.temporary || !transaction.IsReadOnly()) {
		return;
	}
	auto context = transaction.context.lock();
	if (!context) {
		return;
	}
	auto &database = sequence.ParentCatalog().GetAttached();
	if (database.IsReadOnly()) {
		throw TransactionException("Cannot change sequence \"%s\" - database \"%s\" is attached in read-only mode",
		                           sequence.name, database.GetName());
	}
	MetaTransaction::Get(*context).ModifyDatabase(database, DatabaseModificationType::SEQUENCE);
}

int64_t SequenceCatalogEntry::NextValue(DuckTransaction &transaction) {
	ChargewireRegisterSequenceWrite(*this, transaction);
	lock_guard<mutex> seqlock(state->lock);
]=])
replace_once("${SEQUENCE_FILE}" "${before}" "${after}" "ChargewireRegisterSequenceWrite(SequenceCatalogEntry" "sequence registration (nextval)")

set(before [=[int64_t SequenceCatalogEntry::SetValue(DuckTransaction &transaction, int64_t value, bool is_called) {
	{
]=])
set(after [=[int64_t SequenceCatalogEntry::SetValue(DuckTransaction &transaction, int64_t value, bool is_called) {
	ChargewireRegisterSequenceWrite(*this, transaction); // (setval)
	{
]=])
replace_once("${SEQUENCE_FILE}" "${before}" "${after}" "ChargewireRegisterSequenceWrite(*this, transaction); // (setval)" "sequence registration (setval)")

set(before [=[namespace duckdb {
]=])
set(after [=[// Chargewire: for ChargewireRegisterSequenceWrite
#include "duckdb/catalog/catalog.hpp"
#include "duckdb/common/enums/database_modification_type.hpp"
#include "duckdb/main/attached_database.hpp"
#include "duckdb/main/client_context.hpp"
#include "duckdb/transaction/meta_transaction.hpp"

namespace duckdb {
]=])
replace_once("${SEQUENCE_FILE}" "${before}" "${after}" "// Chargewire: for ChargewireRegisterSequenceWrite" "sequence registration (includes)")

# ---- 2. the hook before a table function's bind ----
set(BIND_FILE "${DUCKDB_SOURCE_DIR}/src/planner/binder/tableref/bind_table_function.cpp")

set(before [=[	table_function.SetCallArguments(parameters, named_parameters);
	if (table_function.bind || table_function.bind_replace || table_function.bind_operator) {
]=])
set(after [=[	table_function.SetCallArguments(parameters, named_parameters);
	if (chargewire_bind_table_function_hook) {
		chargewire_bind_table_function_hook(context, table_function.GetName());
	}
	if (table_function.bind || table_function.bind_replace || table_function.bind_operator) {
]=])
replace_once("${BIND_FILE}" "${before}" "${after}" "chargewire_bind_table_function_hook(context, table_function.GetName());" "table function bind hook (call)")

set(before [=[namespace duckdb {
]=])
set(after [=[namespace duckdb {

// Chargewire: called before the bind step of every table function, whoever wrote the call
// (null unless set). It may throw to refuse the function.
void (*chargewire_bind_table_function_hook)(ClientContext &context, const Identifier &function_name) = nullptr;
]=])
replace_once("${BIND_FILE}" "${before}" "${after}" "void (*chargewire_bind_table_function_hook)" "table function bind hook (definition)")
