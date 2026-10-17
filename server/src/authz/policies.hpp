// Row policies and column masking under access control.
//
// After a store user's statement passed the access check, its bound plan is
// rewritten before DuckDB optimizes it: every scan of a table with row
// policies gets a filter with the combined predicate (permissive policies OR-ed,
// restrictive ones AND-ed, nothing applicable = no rows), and every use of a
// masked column is replaced by the masking expression, so filters, joins,
// sorts and aggregates see the masked value too. Context functions in policy
// SQL (the user, their roles, their attributes) become literals first; DuckDB
// never evaluates them. Writing to a table with policies is refused for now.

#pragma once

#include <map>
#include <set>
#include <string>

#include <arrow/result.h>
#include <arrow/status.h>
#include <duckdb.hpp>

#include "authz/model.hpp"

namespace duckdb {
class ParsedExpression;
}

namespace hardcharger::authz {

// Who a policy is evaluated for.
struct PolicyContext {
  std::string user_name;
  std::set<std::string> role_names;  // folded
  std::map<std::string, std::string> attributes;

  static PolicyContext For(const Snapshot& snapshot, const Principal& principal);
};

// Check policy SQL before it is stored: one expression, only the shapes a
// policy may use (no subqueries, parameters, variables, settings or windows),
// context functions with a constant argument. A masking body may refer to its
// parameter and nothing else of the table.
arrow::Status ValidatePolicySql(const std::string& sql,
                                const std::string& mask_parameter = "");

// The policy SQL parsed, with context functions replaced by literals for
// `context` and (for a masking body) the parameter by `parameter_value`.
arrow::Result<duckdb::unique_ptr<duckdb::ParsedExpression>> PreparePolicyExpression(
    const std::string& sql, const PolicyContext& context,
    const std::string& mask_parameter = "",
    const duckdb::ParsedExpression* parameter_value = nullptr);

// Rewrite a store user's checked plan for the row and masking policies that
// apply to them. Throws a PermissionException for a statement it cannot
// rewrite safely.
void ApplyPolicies(duckdb::ClientContext& context, const Snapshot& snapshot,
                   const Principal& principal,
                   duckdb::unique_ptr<duckdb::LogicalOperator>& plan);

}  // namespace hardcharger::authz
