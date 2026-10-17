// Instrumentation's writer: GizmoSQL 1.x's InstrumentationManagerTest.FailedWriteDoesNotAffectOtherWrites,
// against Hardcharger's Instrumentation (server/tests/test_instrumentation.py runs it).
#include <gtest/gtest.h>

#include <filesystem>

#include "duckdb_sql.hpp"
#include "duckdb_v2.hpp"
#include "instrumentation.hpp"
#include "runtime.hpp"

namespace {

struct Db {
  warpduckdb::Environment environment;
  warpduckdb::Instance instance;
  warpduckdb::Connection connection;
  Db() {
    warpduckdb::Call([&](auto* e) { return duckdb_v2_environment_create(environment.out(), e); });
    warpduckdb::Call([&](auto* e) {
      return duckdb_v2_instance_create_with_options(environment.get(), nullptr, nullptr, 0, instance.out(), e);
    });
    warpduckdb::Call([&](auto* e) { return duckdb_v2_connection_create(instance.get(), connection.out(), e); });
  }
};

}  // namespace

TEST(InstrumentationTest, FailedWriteDoesNotAffectOtherWrites) {
  const auto file = std::filesystem::temp_directory_path() / ("hc_instr_" + std::to_string(::getpid()) + ".db");
  std::filesystem::remove(file);
  hardcharger::Config config;
  config.admin_password = "x";
  config.enable_instrumentation = true;
  config.instrumentation_db_path = file.string();
  hardcharger::Runtime runtime(config, "00000000-0000-0000-0000-0000000000aa");
  Db db;
  {
    hardcharger::Instrumentation instrumentation(runtime, db.instance.get(), "v2.0.0");
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.EnqueueForTesting("INSERT INTO _hardcharger_instr.main.no_such_table VALUES (1)");
    instrumentation.EnqueueForTesting(
        "UPDATE _hardcharger_instr.main.instances SET instance_tag = '{\"after\": \"failure\"}' "
        "WHERE instance_id = '00000000-0000-0000-0000-0000000000aa'");
    instrumentation.Stop();  // writes everything queued
  }
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(),
                                  "SELECT instance_tag->>'after' FROM _hardcharger_instr.main.instances "
                                  "WHERE instance_id = '00000000-0000-0000-0000-0000000000aa'"),
            "failure");
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(),
                                  "SELECT status || '/' || stop_reason FROM _hardcharger_instr.main.instances"),
            "stopped/graceful");
  warpduckdb::RunSql(db.connection.get(), "DETACH _hardcharger_instr");
  std::filesystem::remove(file);
}
