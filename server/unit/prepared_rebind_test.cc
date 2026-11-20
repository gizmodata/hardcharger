// Every run of a prepared statement is planned, and checked, again (finding 17): the session
// state never lets DuckDB skip the re-plan, also not for a statement at the address of one it
// checked earlier, which the allocator can hand out again.

#include <gtest/gtest.h>

#include <chrono>
#include <duckdb.hpp>
#include <duckdb/main/client_context_state.hpp>
#include <duckdb/main/prepared_statement_data.hpp>
#include <new>

#include "authz/engine.hpp"
#include "authz/enforcement.hpp"

namespace dd = duckdb;
using hardcharger::authz::AttachRbacSession;
using hardcharger::authz::CatalogAccessLevel;
using hardcharger::authz::CatalogRule;
using hardcharger::authz::kRbacSessionStateName;
using hardcharger::authz::Principal;
using hardcharger::authz::RbacEngine;
using hardcharger::authz::RbacSessionState;
using hardcharger::authz::RegisterRbacEnforcement;

namespace {

class PreparedRebind : public ::testing::Test {
 protected:
  void SetUp() override {
    RegisterRbacEnforcement(config_);
    db_ = std::make_unique<dd::DuckDB>(nullptr, &config_);
    connection_ = std::make_unique<dd::Connection>(*db_);
    RbacEngine::Options options;
    options.store_uri = "memory";
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

  dd::RebindQueryInfo AskRebind(dd::PreparedStatementData& prepared) {
    dd::identifier_map_t<dd::BoundParameterData> values;
    dd::BindPreparedStatementCallbackInfo info{prepared, values};
    return State().OnRebindPreparedStatement(*connection_->context, info, dd::RebindQueryInfo::DO_NOT_REBIND);
  }

  dd::DBConfig config_;
  std::unique_ptr<dd::DuckDB> db_;
  std::unique_ptr<dd::Connection> connection_;
  std::shared_ptr<RbacEngine> engine_;
};

}  // namespace

TEST_F(PreparedRebind, AStatementAtAReusedAddressIsPlannedAgain) {
  alignas(dd::PreparedStatementData) unsigned char storage[sizeof(dd::PreparedStatementData)];
  // A statement checked and finalized at this address, as every statement a session runs is.
  auto* first = new (storage) dd::PreparedStatementData(dd::StatementType::SELECT_STATEMENT);
  State().NoteChecked(engine_->snapshot()->version(), {}, false);
  State().OnFinalizePrepare(*connection_->context, *first, dd::PreparedStatementMode::PREPARE_AND_EXECUTE);
  first->~PreparedStatementData();
  // Another statement the allocator puts at the same address, at the same snapshot version.
  auto* second = new (storage) dd::PreparedStatementData(dd::StatementType::SELECT_STATEMENT);
  EXPECT_EQ(AskRebind(*second), dd::RebindQueryInfo::ATTEMPT_TO_REBIND);
  second->~PreparedStatementData();
}

TEST_F(PreparedRebind, EveryRunIsPlannedAgain) {
  auto prepared = std::make_shared<dd::PreparedStatementData>(dd::StatementType::SELECT_STATEMENT);
  State().NoteChecked(engine_->snapshot()->version(), {}, false);
  State().OnFinalizePrepare(*connection_->context, *prepared, dd::PreparedStatementMode::PREPARE_AND_EXECUTE);
  EXPECT_EQ(AskRebind(*prepared), dd::RebindQueryInfo::ATTEMPT_TO_REBIND);
  EXPECT_EQ(AskRebind(*prepared), dd::RebindQueryInfo::ATTEMPT_TO_REBIND);
}
