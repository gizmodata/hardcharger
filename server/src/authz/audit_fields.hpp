// What an access-control statement is about, for its audit record: the users and roles it
// names (with their store ids), the privilege or membership it changes, and the object,
// tenant, policy, group or token it concerns. Read from the parsed statement, so a refused
// statement is described as fully as one that ran, and never from its text: a password, a
// token or an attribute's value is in no field.
#pragma once

#include <string>
#include <utility>
#include <vector>

#include "authz/ddl.hpp"
#include "authz/model.hpp"

namespace hardcharger::authz {

// Fields for the audit record of `command`:
//   target, target_ids       the users and roles it is about ("user alice, role dba") and
//                            their ids in the store, in the same order ("?" for one the
//                            store does not have)
//   privilege                the privileges granted or revoked ("SYSTEM ADMIN", "SELECT (a), INSERT")
//   membership, membership_ids  the roles granted or revoked as membership, and their ids
//   object                   what the privileges are on ("TABLE crm.users", "LOCATION 's3://b/'")
//   option                   "WITH ADMIN OPTION", "GRANT OPTION FOR", ...
//   change                   what ALTER USER changes ("password", "disabled", "attribute region")
//   tenant, catalog, policy, group, issuer, token, owner   as the statement names them
// A user or role is looked up in `before` (the store as the statement found it), then in
// `after` (as it left it: what it created).
std::vector<std::pair<std::string, std::string>> AuditFieldsOf(const RbacCommand& command, const Snapshot& before,
                                                               const Snapshot& after);

}  // namespace hardcharger::authz
