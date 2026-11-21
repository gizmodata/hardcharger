// DuckLake functions for catalog_access principals and store users (authz/ducklake_calls.hpp):
// the classification GizmoSQL 1.42.3 applies, checked against DuckDB 2.0's DuckLake. The
// server-side behaviour is in server/tests/test_tenant_isolation.py and
// server/tests/test_rbac_ducklake_functions.py.
#include <gtest/gtest.h>

#include <string>

#include "authz/ducklake_calls.hpp"

using hardcharger::authz::DuckLakeAccess;
using hardcharger::authz::DuckLakeBoundFunctions;
using hardcharger::authz::DuckLakeFunctionAccess;
using hardcharger::authz::IsDuckLakeCatalogMethod;

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
