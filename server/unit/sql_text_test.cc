// Statement splitting and the commands Hardcharger handles before DuckDB parses SQL.
// Ports the splitter cases of GizmoSQL 1.x's tests/integration/test_init_sql_splitting.cpp
// (6 unit tests) to SplitStatements(), which splits with DuckDB's own tokenizer; the
// server case of that file is in server/tests/test_init_sql_splitting.py.
#include <gtest/gtest.h>

#include <string>
#include <vector>

#include "duckdb_v2.hpp"
#include "sql_text.hpp"

using hardcharger::ParseKillSession;
using hardcharger::ParseSettingCommand;
using hardcharger::SettingCommand;
using hardcharger::SplitStatements;
using Statements = std::vector<std::string>;

namespace {

class SqlText : public ::testing::Test {
 protected:
  static void SetUpTestSuite() {
    chargewire_duckdb::Call([](auto* e) { return duckdb_v2_environment_create(environment_.out(), e); });
    chargewire_duckdb::Call([](auto* e) {
      return duckdb_v2_instance_create_with_options(environment_.get(), nullptr, nullptr, 0, instance_.out(), e);
    });
    chargewire_duckdb::Call([](auto* e) { return duckdb_v2_connection_create(instance_.get(), connection_.out(), e); });
  }
  static void TearDownTestSuite() {
    connection_.reset();
    instance_.reset();
    environment_.reset();
  }

  Statements Split(const std::string& sql) {
    Statements out;
    for (const auto& piece : SplitStatements(connection_.get(), sql)) out.emplace_back(piece.text);
    return out;
  }
  std::optional<SettingCommand> Setting(const std::string& sql) {
    sql_ = sql;
    auto pieces = SplitStatements(connection_.get(), sql_);
    if (pieces.size() != 1) return std::nullopt;
    return ParseSettingCommand(pieces[0]);
  }
  std::optional<std::string> Kill(const std::string& sql) {
    sql_ = sql;
    auto pieces = SplitStatements(connection_.get(), sql_);
    if (pieces.size() != 1) return std::nullopt;
    return ParseKillSession(pieces[0]);
  }

  static inline chargewire_duckdb::Environment environment_;
  static inline chargewire_duckdb::Instance instance_;
  static inline chargewire_duckdb::Connection connection_;
  std::string sql_;
};

}  // namespace

TEST_F(SqlText, SplitsOnTopLevelSemicolons) {
  EXPECT_EQ(Split("SELECT 1; SELECT 2;"), (Statements{"SELECT 1", "SELECT 2"}));
  EXPECT_EQ(Split("  SELECT 1  \n;\n SELECT 2"), (Statements{"SELECT 1", "SELECT 2"}));
  EXPECT_EQ(Split(";;  ;"), Statements{});
  EXPECT_EQ(Split(""), Statements{});
}

TEST_F(SqlText, SemicolonInStringLiteralDoesNotSplit) {
  EXPECT_EQ(Split("SELECT 'a;b'; SELECT 'it''s;x'"), (Statements{"SELECT 'a;b'", "SELECT 'it''s;x'"}));
  EXPECT_EQ(Split(R"(SELECT E'a\';b'; SELECT 2)"), (Statements{R"(SELECT E'a\';b')", "SELECT 2"}));
}

TEST_F(SqlText, SemicolonInQuotedIdentifierDoesNotSplit) {
  EXPECT_EQ(Split(R"(CREATE TABLE "a;b" (x INT); SELECT 1)"), (Statements{R"(CREATE TABLE "a;b" (x INT))", "SELECT 1"}));
  EXPECT_EQ(Split(R"(SELECT 1 AS "q""; x")"), (Statements{R"(SELECT 1 AS "q""; x")"}));
}

TEST_F(SqlText, SemicolonInCommentsDoesNotSplit) {
  // Comments around a statement are not part of its text; comments inside it are.
  EXPECT_EQ(Split("SELECT 1; -- setup; done\nSELECT 2"), (Statements{"SELECT 1", "SELECT 2"}));
  EXPECT_EQ(Split("SELECT /* a; b */ 1; SELECT 2"), (Statements{"SELECT /* a; b */ 1", "SELECT 2"}));
  EXPECT_EQ(Split("SELECT /* x /* y; */ z; */ 1"), (Statements{"SELECT /* x /* y; */ z; */ 1"}));
  EXPECT_EQ(Split("SELECT 1; -- the end;"), (Statements{"SELECT 1"}));
  EXPECT_EQ(Split("/* only; a comment */"), Statements{});
}

TEST_F(SqlText, SemicolonInDollarQuotedStringDoesNotSplit) {
  EXPECT_EQ(Split("SELECT $$a;b$$; SELECT 2"), (Statements{"SELECT $$a;b$$", "SELECT 2"}));
  EXPECT_EQ(Split("SELECT $fn$ x; $$ y; $fn$; SELECT 2"), (Statements{"SELECT $fn$ x; $$ y; $fn$", "SELECT 2"}));
  // $1 / $name parameters are not dollar quotes.
  EXPECT_EQ(Split("SELECT $1; SELECT $name; SELECT 3"), (Statements{"SELECT $1", "SELECT $name", "SELECT 3"}));
}

TEST_F(SqlText, UnterminatedQuoteRunsToTheEnd) {
  EXPECT_EQ(Split("SELECT 'oops; SELECT 2"), (Statements{"SELECT 'oops; SELECT 2"}));
}

TEST_F(SqlText, SettingCommands) {
  auto set = Setting("SET hardcharger.query_timeout = 5");
  ASSERT_TRUE(set);
  EXPECT_EQ(set->name, "query_timeout");
  EXPECT_EQ(set->value, "5");
  EXPECT_EQ(set->scope, SettingCommand::Scope::kAutomatic);

  set = Setting("set global HardCharger.Query_Log_Level to 'DEBUG'");
  ASSERT_TRUE(set);
  EXPECT_EQ(set->name, "query_log_level");
  EXPECT_EQ(set->value, "DEBUG");
  EXPECT_EQ(set->scope, SettingCommand::Scope::kGlobal);

  set = Setting("SET SESSION hardcharger.query_tag = '{\"a\": 1}'");
  ASSERT_TRUE(set);
  EXPECT_EQ(set->value, "{\"a\": 1}");
  EXPECT_EQ(set->scope, SettingCommand::Scope::kSession);

  set = Setting("RESET hardcharger.query_timeout");
  ASSERT_TRUE(set);
  EXPECT_TRUE(set->reset);

  EXPECT_FALSE(Setting("SET memory_limit = '1GB'"));
  EXPECT_FALSE(Setting("SET search_path = 'hardcharger.main'"));
}

TEST_F(SqlText, KillSessionCommands) {
  const std::string id = "1b4e28ba-2fa1-11d2-883f-0016d3cca427";
  EXPECT_EQ(Kill("KILL SESSION '" + id + "'"), id);
  EXPECT_EQ(Kill("kill session \"" + id + "\""), id);
  EXPECT_EQ(Kill("Kill Session " + id), id);
  EXPECT_EQ(Kill("KILL SESSION '" + id + "';"), id);
  EXPECT_EQ(Kill("KILL SESSION"), std::string());
  EXPECT_FALSE(Kill("SELECT 'KILL SESSION x'"));
}
