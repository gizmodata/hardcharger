// DuckLake functions for catalog_access principals (authz/ducklake_calls.hpp): the
// classification GizmoSQL 1.42.3 applies, checked against DuckDB 2.0's DuckLake, and the token
// count the gate compares with the calls its walk inspected. The server-side behaviour is
// in server/tests/test_tenant_isolation.py.
#include <gtest/gtest.h>

#include <string>

#include "authz/ducklake_calls.hpp"

using hardcharger::authz::CountDuckLakeCallTokens;
using hardcharger::authz::DuckLakeAccess;
using hardcharger::authz::DuckLakeBoundFunctions;
using hardcharger::authz::DuckLakeFunctionAccess;
using hardcharger::authz::IsDuckLakeCatalogMethod;
using hardcharger::authz::LooksLikeDuckLakeCall;

TEST(DuckLakeCalls, InspectionNeedsReadAccess) {
  for (const char* name : {"ducklake_snapshots", "ducklake_table_info", "ducklake_list_files",
                           "ducklake_table_insertions", "ducklake_table_deletions", "ducklake_table_changes",
                           "ducklake_settings", "ducklake_options", "ducklake_current_snapshot",
                           "ducklake_last_committed_snapshot", "ducklake_branches", "ducklake_current_branch",
                           "ducklake_branch_table"}) {
    EXPECT_EQ(DuckLakeFunctionAccess(name), DuckLakeAccess::kRead) << name;
  }
}

TEST(DuckLakeCalls, MaintenanceNeedsWriteAccess) {
  for (const char* name : {"ducklake_merge_adjacent_files", "ducklake_expire_snapshots", "ducklake_cleanup_old_files",
                           "ducklake_delete_orphaned_files", "ducklake_flush_inlined_data",
                           "ducklake_rewrite_data_files", "ducklake_set_option", "ducklake_set_commit_message",
                           // Branch functions of customer builds other than the three that look.
                           "ducklake_create_branch", "ducklake_merge_branch", "ducklake_drop_branch"}) {
    EXPECT_EQ(DuckLakeFunctionAccess(name), DuckLakeAccess::kWrite) << name;
  }
}

TEST(DuckLakeCalls, PathFunctionsAreTheAdministrators) {
  for (const char* name : {"ducklake_add_data_files", "ducklake_scan", "ducklake_commit"}) {
    EXPECT_EQ(DuckLakeFunctionAccess(name), DuckLakeAccess::kAdministrator) << name;
  }
}

TEST(DuckLakeCalls, AnUnknownFunctionFailsClosed) {
  EXPECT_EQ(DuckLakeFunctionAccess("ducklake_something_new"), DuckLakeAccess::kUnknown);
  EXPECT_EQ(DuckLakeFunctionAccess("snapshots"), std::nullopt);
  EXPECT_EQ(DuckLakeFunctionAccess("read_parquet"), std::nullopt);
}

TEST(DuckLakeCalls, CatalogMethods) {
  for (const char* name : {"snapshots", "table_info", "options", "settings", "current_snapshot",
                           "last_committed_snapshot", "merge_adjacent_files", "set_option", "set_commit_message",
                           "table_changes", "table_insertions", "table_deletions"}) {
    EXPECT_TRUE(IsDuckLakeCatalogMethod(name)) << name;
    EXPECT_TRUE(DuckLakeFunctionAccess(std::string("ducklake_") + name).has_value()) << name;
  }
  EXPECT_FALSE(IsDuckLakeCatalogMethod("range"));
  EXPECT_EQ(DuckLakeBoundFunctions("ducklake_table_changes"),
            (std::vector<std::string>{"ducklake_table_insertions", "ducklake_table_deletions"}));
  EXPECT_EQ(DuckLakeBoundFunctions("ducklake_snapshots"), (std::vector<std::string>{"ducklake_snapshots"}));
}

TEST(DuckLakeCalls, TokenCount) {
  std::string first;
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT * FROM ducklake_snapshots('a')", &first), 1);
  EXPECT_EQ(first, "ducklake_snapshots");
  first.clear();
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT * FROM a.snapshots(), lake.main.settings()", &first), 2);
  EXPECT_EQ(first, "ducklake_snapshots");
  // Quoted, upper case, a comment before the parenthesis: still a call.
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT * FROM \"DuckLake_Snapshots\" /* x */ ('a')"), 1);
  EXPECT_EQ(CountDuckLakeCallTokens("SET VARIABLE v = (SELECT count(*) FROM ducklake_snapshots('a'))"), 1);
  // Not calls: a string, a comment, a name without a parenthesis, an unqualified method.
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT 'ducklake_snapshots(' AS s -- ducklake_snapshots('a')"), 0);
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT ducklake_snapshots FROM t"), 0);
  EXPECT_EQ(CountDuckLakeCallTokens("SELECT * FROM snapshots()"), 0);
  // A macro's own name is no call (GizmoSQL 6a458ca7); its body still counts.
  EXPECT_EQ(CountDuckLakeCallTokens("CREATE OR REPLACE MACRO myschema.settings() AS 42"), 0);
  EXPECT_EQ(CountDuckLakeCallTokens("CREATE FUNCTION main.snapshots() AS TABLE SELECT 1 AS x"), 0);
  EXPECT_EQ(CountDuckLakeCallTokens("CREATE MACRO IF NOT EXISTS ducklake_x(a) AS a + 1"), 0);
  EXPECT_EQ(CountDuckLakeCallTokens("CREATE MACRO s.settings() AS TABLE SELECT * FROM lake.snapshots()"), 1);
}

TEST(DuckLakeCalls, LooksLikeACall) {
  EXPECT_TRUE(LooksLikeDuckLakeCall({"ducklake_snapshots"}));
  EXPECT_TRUE(LooksLikeDuckLakeCall({"lake", "snapshots"}));
  EXPECT_TRUE(LooksLikeDuckLakeCall({"lake", "main", "Settings"}));
  EXPECT_FALSE(LooksLikeDuckLakeCall({"snapshots"}));
  EXPECT_FALSE(LooksLikeDuckLakeCall({"lake", "range"}));
}
