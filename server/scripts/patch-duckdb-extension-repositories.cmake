# Adding an extension repository trusts a new signing key, whose extensions then run inside the
# server. DuckDB 2.0.0 refused every new repository until allow_extension_repositories was set
# ('undecided', its default). DuckDB 2.0.1 replaced 'undecided' with 'hosts', which lets
# CREATE EXTENSION REPOSITORY <host> (no prefix: the host names the prefix and the keys) add one
# with no such decision. Hardcharger keeps 2.0.0's rule: under the default, a repository named
# by its host is refused like one given by prefix, until an administrator sets
# allow_extension_repositories to 'allowed' (or 'hosts_only', which allows only host
# repositories). 'forbidden', existing repositories and DROP are unchanged.
set(MANAGER_FILE "${DUCKDB_SOURCE_DIR}/src/main/extension/extension_repository_manager.cpp")
file(READ "${MANAGER_FILE}" CONTENT)

set(before [=[	if (access == ExtensionRepositoryAccess::ALLOWED || (from_host && access != ExtensionRepositoryAccess::FORBIDDEN)) {
		return;
	}
	string message;
	if (access == ExtensionRepositoryAccess::FORBIDDEN) {]=])
set(after [=[	// Hardcharger: under the default 'hosts', a repository named by its host needs the same explicit
	// opt-in as one given by prefix (DuckDB 2.0.0's rule).
	if (access == ExtensionRepositoryAccess::ALLOWED || (from_host && access == ExtensionRepositoryAccess::HOSTS_ONLY)) {
		return;
	}
	string message;
	if (access == ExtensionRepositoryAccess::HOSTS) {
		message = StringUtil::Format(
		    "Cannot create extension repository '%s': adding a repository trusts a new signing key, and this server "
		    "adds none until an administrator allows it. Run SET GLOBAL allow_extension_repositories = 'allowed' "
		    "(or 'hosts_only', for repositories named by their host only) first; the setting cannot be loosened "
		    "again while the server runs.",
		    name);
	} else if (access == ExtensionRepositoryAccess::FORBIDDEN) {]=])

string(FIND "${CONTENT}" "${after}" patched)
if(NOT patched EQUAL -1)
  message(STATUS "patch-duckdb-extension-repositories: already patched")
  return()
endif()
string(FIND "${CONTENT}" "${before}" first)
string(FIND "${CONTENT}" "${before}" last REVERSE)
if(first EQUAL -1 OR NOT first EQUAL last)
  message(FATAL_ERROR "patch-duckdb-extension-repositories: the repository opt-in was not found exactly once; update this patch for DuckDB")
endif()
string(REPLACE "${before}" "${after}" CONTENT "${CONTENT}")
file(WRITE "${MANAGER_FILE}" "${CONTENT}")
message(STATUS "patch-duckdb-extension-repositories: a repository named by its host needs allow_extension_repositories set first")
