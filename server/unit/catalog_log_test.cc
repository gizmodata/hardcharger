// Catalog logging's writer: lines logged while it runs land in the logs table, with
// their fields in the right columns (server/tests/test_catalog_logging.py runs it).
#include <gtest/gtest.h>

#include <filesystem>

#include "catalog_log.hpp"
#include "duckdb_sql.hpp"
#include "duckdb_v2.hpp"
#include "logging.hpp"
#include "platform.hpp"
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

std::string Query(Db& db, const std::string& sql) { return warpduckdb::QueryText(db.connection.get(), sql); }

}  // namespace

TEST(CatalogLogTest, LinesLandInTheirColumns) {
  const auto file = std::filesystem::temp_directory_path() / ("hc_logs_" + std::to_string(hardcharger::ProcessId()) + ".db");
  std::filesystem::remove(file);
  hardcharger::Config config;
  config.admin_password = "x";
  config.enable_catalog_logging = true;
  config.log_catalog_db_path = file.string();
  hardcharger::Runtime runtime(config, "00000000-0000-0000-0000-0000000000aa");
  hardcharger::ConfigureLogging("debug", "text", "-");
  hardcharger::SetDefaultLogFields({{"instance_id", "00000000-0000-0000-0000-0000000000aa"}});
  Db db;
  {
    hardcharger::CatalogLog log(runtime, db.instance.get());
    log.Attach();
    log.Start();
    hardcharger::Log(hardcharger::LogLevel::kWarning, "it's a probe",
                     {{"session_id", "s_abcdefghijklmnopqrstuvwxyz234567"},  // a Warp SQL session id
                      {"user", "alice"},
                      {"role", "admin"},
                      {"kind", "sql"}});
    // A cluster id that isn't a UUID: kept in fields, not lost and not failing the row.
    hardcharger::Log(hardcharger::LogLevel::kDebug, "odd id probe", {{"cluster_id", "nope"}});
    log.Stop();  // writes everything queued
  }
  hardcharger::SetDefaultLogFields({});
  hardcharger::ConfigureLogging("info", "text");
  const std::string row =
      Query(db, "SELECT concat_ws('|', level, instance_id, session_id, username, role, fields->>'kind', "
                "pid IS NOT NULL, tid IS NOT NULL, log_time > now() - INTERVAL 1 MINUTE) "
                "FROM _hardcharger_logs.main.logs WHERE message = 'it''s a probe'");
  EXPECT_EQ(row,
            "WARN|00000000-0000-0000-0000-0000000000aa|s_abcdefghijklmnopqrstuvwxyz234567|alice|admin|sql|true|true|"
            "true");
  EXPECT_EQ(Query(db, "SELECT concat_ws('|', level, cluster_id IS NULL, fields->>'cluster_id') "
                      "FROM _hardcharger_logs.main.logs WHERE message = 'odd id probe'"),
            "DEBUG|true|nope");
  // Nothing logged after Stop reaches the table.
  hardcharger::Log(hardcharger::LogLevel::kInfo, "after stop");
  EXPECT_EQ(Query(db, "SELECT count(*) FROM _hardcharger_logs.main.logs WHERE message = 'after stop'"), "0");
  warpduckdb::RunSql(db.connection.get(), "DETACH _hardcharger_logs");
  std::filesystem::remove(file);
}
