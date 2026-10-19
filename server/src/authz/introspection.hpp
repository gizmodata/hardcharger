// Access-control introspection: the rows of hardcharger_grants(), hardcharger_role_members(),
// hardcharger_roles(), hardcharger_users(), hardcharger_policies() and
// hardcharger_effective_privileges(), read from the snapshot the server already holds (no
// store round trip), and only those the viewer may see:
//
//  - the administrator without catalog_access rules: everything;
//  - a store user: grants to the roles it holds (directly, inherited, or through its sign-in
//    token's groups), the memberships that make it hold them, those roles, its own user row,
//    and the policies on tables it can see; never another user's grants;
//  - an administrator's token with catalog_access rules: the grants, ownerships and
//    policies on objects inside the catalogs its rules reach, the roles those grants name,
//    and no users or memberships (as hardcharger_sessions() shows it only its own session).
#pragma once

#include <arrow/api.h>

#include <memory>
#include <optional>
#include <string>

#include "authz/authz.hpp"

namespace hardcharger::authz {

class Snapshot;
struct Principal;

// The rows of `table` that `viewer` may see. `user_name` is
// hardcharger_effective_privileges()'s argument (nullopt: the viewer itself). Throws
// warpsql::Error when the viewer may not ask (another user's privileges).
std::shared_ptr<arrow::RecordBatch> AccessTableRows(AccessTable table, const Snapshot& snapshot,
                                                    const Principal& viewer,
                                                    const std::optional<std::string>& user_name);

}  // namespace hardcharger::authz
