// The plan check's refusal of a DuckLake call the pre-bind gate did not check (review finding
// 15). With the gate's walk reading every statement form and every macro body, no SQL a client
// sends reaches this refusal today: it is the guard for whatever the walk might miss, so it is
// tested here, below the gate. A session of a principal with catalog_access rules runs a
// statement straight on its DuckDB connection (as the gate never sees it), against a table
// function named like DuckLake's (the plan check knows a DuckLake call by its function's
// name). Disable built-in extension loading in this isolated database so the baseline and
// sanitizer builds' real DuckLake does not register the same function first. The server-side behaviour is in
// server/tests/test_tenant_isolation.py and server/tests/test_rbac_ducklake_functions.py.
#include <gtest/gtest.h>

#include <chrono>
#include <duckdb.hpp>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_transaction.hpp>
#include <duckdb/function/table_function.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/parser/parsed_data/create_table_function_info.hpp>
#include <string>

#include "authz/ducklake_calls.hpp"
#include "authz/engine.hpp"
#include "authz/enforcement.hpp"

namespace dd = duckdb;
using hardcharger::authz::AttachRbacSession;
using hardcharger::authz::CatalogAccessLevel;
using hardcharger::authz::CatalogRule;
using hardcharger::authz::DuckLakeUncheckedRefusal;
using hardcharger::authz::kRbacSessionStateName;
using hardcharger::authz::Principal;
using hardcharger::authz::RbacEngine;
using hardcharger::authz::RbacSessionState;
using hardcharger::authz::RegisterRbacEnforcement;

namespace {

struct Once : dd::GlobalTableFunctionState {
  bool done = false;
};

dd::unique_ptr<dd::FunctionData> Bind(dd::ClientContext&, dd::TableFunctionBindInput&,
                                      dd::vector<dd::LogicalType>& types, dd::vector<dd::Identifier>& names) {
  types.push_back(dd::LogicalType::BIGINT);
  names.emplace_back("snapshot_id");
  return nullptr;
}

dd::unique_ptr<dd::GlobalTableFunctionState> Init(dd::ClientContext&, dd::TableFunctionInitInput&) {
  return dd::make_uniq<Once>();
}

void Snapshots(dd::ClientContext&, dd::TableFunctionInput& input, dd::DataChunk& output) {
  auto& state = input.global_state->Cast<Once>();
  if (state.done) return;
  state.done = true;
  output.SetValue(0, 0, dd::Value::BIGINT(7));
  output.SetCardinality(1);
}

class DuckLakePlanCheck : public ::testing::Test {
 protected:
  void SetUp() override {
    config_.options.load_extensions = false;
    RegisterRbacEnforcement(config_);
    db_ = std::make_unique<dd::DuckDB>(nullptr, &config_);
    dd::TableFunction function(dd::Identifier("ducklake_snapshots"), {dd::LogicalType::VARCHAR}, Snapshots, Bind, Init);
    dd::CreateTableFunctionInfo info(function);
    auto& instance = *db_->instance;
    auto transaction = dd::CatalogTransaction::GetSystemTransaction(instance);
    dd::Catalog::GetSystemCatalog(instance).CreateTableFunction(transaction, info);
    connection_ = std::make_unique<dd::Connection>(*db_);

    RbacEngine::Options options;
    options.store_uri = "memory";  // rules only, as a server without a store runs
    options.poll_interval = std::chrono::milliseconds(0);
    auto engine = RbacEngine::Create(options);
    ASSERT_TRUE(engine.ok()) << engine.status().ToString();
    engine_ = *engine;
    Principal principal;
    principal.user_name = "app_token";
    principal.catalog_rules = std::vector<CatalogRule>{{"memory", CatalogAccessLevel::kWrite}};
    AttachRbacSession(*connection_->context, engine_, principal);
  }

  void TearDown() override {
    connection_.reset();
    db_.reset();
    if (engine_) engine_->Stop();
  }

  RbacSessionState& State() {
    auto state = connection_->context->registered_state->Get<RbacSessionState>(kRbacSessionStateName);
    EXPECT_TRUE(state);
    return *state;
  }

  dd::DBConfig config_;
  std::unique_ptr<dd::DuckDB> db_;
  std::unique_ptr<dd::Connection> connection_;
  std::shared_ptr<RbacEngine> engine_;
};

}  // namespace

// The refusal: the call's catalog is one the rules allow, but the gate checked no call.
TEST_F(DuckLakePlanCheck, ACallTheGateDidNotCheckIsRefused) {
  State().SetCheckedDuckLakeCalls({});
  auto result = connection_->Query("SELECT * FROM ducklake_snapshots('memory')");
  ASSERT_TRUE(result->HasError());
  EXPECT_NE(result->GetError().find(DuckLakeUncheckedRefusal("ducklake_snapshots")), std::string::npos)
      << result->GetError();
}

// A call the gate checked on another catalog, or another function on this one, lends it nothing.
TEST_F(DuckLakePlanCheck, OnlyTheCallTheGateCheckedRuns) {
  State().SetCheckedDuckLakeCalls({{"ducklake_snapshots", "other"}, {"ducklake_table_info", "memory"}});
  auto result = connection_->Query("SELECT * FROM ducklake_snapshots('memory')");
  ASSERT_TRUE(result->HasError());
  EXPECT_NE(result->GetError().find(DuckLakeUncheckedRefusal("ducklake_snapshots")), std::string::npos)
      << result->GetError();
}

// The twin: the same statement, with the call the gate checked, runs.
TEST_F(DuckLakePlanCheck, ACallTheGateCheckedRuns) {
  State().SetCheckedDuckLakeCalls({{"ducklake_snapshots", "memory"}});
  auto result = connection_->Query("SELECT * FROM ducklake_snapshots('memory')");
  ASSERT_FALSE(result->HasError()) << result->GetError();
  auto chunk = result->Fetch();
  ASSERT_TRUE(chunk);
  EXPECT_EQ(chunk->GetValue(0, 0).GetValue<int64_t>(), 7);
}
