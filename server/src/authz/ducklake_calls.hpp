// DuckLake's table functions for a non-administrator: a catalog_access principal (a `user` token, or an
// administrator's token, with catalog_access rules), as GizmoSQL 1.42.3 checks them.
//
// A DuckLake function names the catalog it works on as its first argument
// (ducklake_snapshots('lake')), or is called through one of the catalog's own macros
// (lake.snapshots(), CALL lake.merge_adjacent_files()), which binds the same function
// with the catalog filled in. DuckDB does not report that argument as a read or write of
// the catalog, so the check reads it: inspection needs read access to the catalog,
// maintenance and option changes need write access. The pre-bind gate checks each call
// as written (its catalog must be a string literal) and remembers it; the plan check
// checks each bound call again and refuses one the statement did not name itself
// (inside a view or macro, or anywhere the gate's walk does not reach).
//
// A store user (RBAC) needs EXECUTE on the function, as on any table function, and on the
// catalog the call names a privilege granted ON CATALOG: SELECT to inspect, MAINTAIN to
// maintain, with the same pre-bind and plan checks. Its administrator-only and unknown
// DuckLake functions are what they were: table functions an EXECUTE grant opens.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "authz/types.hpp"

namespace hardcharger::authz {

enum class DuckLakeAccess {
  kRead,           // inspection: needs read access to the catalog
  kWrite,          // maintenance, options, commit messages, branches: needs write access
  kAdministrator,  // registers arbitrary files, or its first argument is not a catalog
  kUnknown,        // a ducklake_* function access control does not know: refused
};

// For a function named ducklake_* (lower case): what it needs; nullopt for any other name.
std::optional<DuckLakeAccess> DuckLakeFunctionAccess(const std::string& name_lower);

// Is `name_lower` one of the macros a DuckLake catalog defines (lake.snapshots(),
// lake.set_option(...)), which call ducklake_<name>('lake', ...)?
bool IsDuckLakeCatalogMethod(const std::string& name_lower);

// The DuckLake table functions a call of `function` binds (ducklake_table_changes is a
// macro over ducklake_table_insertions and ducklake_table_deletions).
std::vector<std::string> DuckLakeBoundFunctions(const std::string& function_lower);

// What a store user's call needs on the catalog it names, granted ON CATALOG (besides
// EXECUTE on the function): SELECT to inspect, MAINTAIN to maintain.
Privilege DuckLakeCatalogPrivilege(DuckLakeAccess access);

// Refusals, worded the same by the gate and the plan check.
std::string DuckLakeExecuteRefusal(const std::string& function);
// `what`: "ducklake_x()", or what else runs DuckLake's maintenance ("CHECKPOINT", ...).
std::string DuckLakeCatalogPrivilegeRefusal(const std::string& what, const std::string& catalog,
                                            DuckLakeAccess access, const std::string& user);
std::string DuckLakeAdministratorRefusal(const std::string& function);
std::string DuckLakeUnknownRefusal(const std::string& function);
std::string DuckLakeWrappedRefusal(const std::string& function, const std::string& wrapper);
std::string DuckLakeComputedCatalogRefusal(const std::string& function);
std::string DuckLakeUncheckedRefusal(const std::string& function);


}  // namespace hardcharger::authz
