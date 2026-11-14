# An internal transaction can acquire changes during binding without being marked
# writable. Reject its commit through the ordinary rollback/cleanup path. Throwing
# an internal error here skips that cleanup and invalidates the whole database.
# Ordinary registered writes and read-only-session enforcement are unchanged.
set(TRANSACTION_FILE "${DUCKDB_SOURCE_DIR}/src/transaction/duck_transaction_manager.cpp")
file(READ "${TRANSACTION_FILE}" CONTENT)

set(before [=[				throw InternalException("Attempting to commit a transaction that is read-only but has made changes - "
				                        "this should not be possible");]=])
set(after [=[				// Hardcharger: reject this commit without invalidating the database.
				error.Merge(ErrorData(TransactionException(
				    "Cannot commit changes made by statement binding in an unregistered write transaction")));
				t_lock.unlock();
				RollbackTransaction(transaction);
				return error;]=])

string(FIND "${CONTENT}" "${after}" patched)
if(NOT patched EQUAL -1)
  message(STATUS "patch-duckdb-binding-commit: already patched")
  return()
endif()
string(FIND "${CONTENT}" "${before}" first)
string(FIND "${CONTENT}" "${before}" last REVERSE)
if(first EQUAL -1 OR NOT first EQUAL last)
  message(FATAL_ERROR "patch-duckdb-binding-commit: commit guard not found exactly once; update this patch for DuckDB")
endif()
string(REPLACE "${before}" "${after}" CONTENT "${CONTENT}")
file(WRITE "${TRANSACTION_FILE}" "${CONTENT}")
message(STATUS "patch-duckdb-binding-commit: invalid writes roll back without invalidating the database")
