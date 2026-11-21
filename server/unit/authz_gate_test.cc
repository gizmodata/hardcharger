// The access-control gate before binding (authz/gate.hpp): a statement of a kind it has no rule
// for is refused, never left to DuckDB's binder; the kinds it lets through to the plan check are
// not refused for their kind. The server-side behaviour is in server/tests/test_bind_time_checks.py.
#include <gtest/gtest.h>

#include <duckdb.hpp>
#include <duckdb/parser/statement/logical_plan_statement.hpp>
#include <string>

#include "authz/duckdb_internal.hpp"
#include "authz/gate.hpp"

namespace dd = duckdb;
using hardcharger::authz::GatedCategory;

// No SQL text parses into a kind without a rule (every kind the parser makes has one), so the
// test hands the gate one directly: a logical plan, as a client API (not SQL) could.
TEST(AuthzGate, AStatementKindWithoutARuleIsRefused) {
  dd::LogicalPlanStatement plan(nullptr);
  const auto category = GatedCategory(plan, nullptr);
  ASSERT_TRUE(category.has_value());
  EXPECT_EQ(category->rfind("a statement of a kind access control has no rule for (", 0), 0u) << *category;
}

// The twins: each kind the gate leaves to the plan check by name, parsed from SQL.
TEST(AuthzGate, KindsLeftToThePlanCheckAreNotRefusedForTheirKind) {
  for (const char* sql : {"BEGIN", "COMMIT", "ROLLBACK", "ANALYZE", "VACUUM", "ALTER TABLE t ADD COLUMN y INTEGER",
                          "COPY FROM DATABASE a TO b", "UPDATE EXTENSIONS", "SELECT 1", "SET VARIABLE v = 1"}) {
    auto parser = hardcharger::authz::ddb::StandaloneParser();
    parser.ParseQuery(sql);
    ASSERT_FALSE(parser.statements.empty()) << sql;
    for (auto& statement : parser.statements) {
      EXPECT_FALSE(GatedCategory(*statement, nullptr).has_value()) << sql;
    }
  }
}
