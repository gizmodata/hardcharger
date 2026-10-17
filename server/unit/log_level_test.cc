// Component log thresholds: ports the contract of GizmoSQL 1.x's
// tests/integration/test_log_level_filtering.cpp (14 cases) to Hardcharger's LogAt().
// 1.x's macros map as follows: GIZMOSQL_LOGKV_DYNAMIC_AT(threshold, level) and
// ..._SESSION_DYNAMIC_AT are LogAt(threshold, level); ..._SESSION_DYNAMIC (which
// ignores the logger's threshold) is LogAt(threshold, level, ..., true).
#include <gtest/gtest.h>

#include <string>
#include <vector>

#include "logging.hpp"

using hardcharger::ConfigureLogging;
using hardcharger::LogAt;
using hardcharger::LogLevel;

namespace {

struct Entry {
  LogLevel level;
  std::string message;
};

class LogLevelFilteringTest : public ::testing::Test {
 protected:
  void SetUp() override {
    hardcharger::SetLogCaptureForTesting([this](LogLevel level, std::string_view message) {
      entries_.push_back({level, std::string(message)});
    });
    Logger("debug");
  }
  void TearDown() override { hardcharger::SetLogCaptureForTesting(nullptr); }
  void Logger(const char* level) { ConfigureLogging(level, "text"); }

  std::vector<Entry> entries_;
};

using SessionLogLevelFilteringTest = LogLevelFilteringTest;

}  // namespace

// ---- Component thresholds (1.x: GIZMOSQL_LOGKV_DYNAMIC_AT, auth logging) ----

TEST_F(LogLevelFilteringTest, DynamicAt_InfoMessageSuppressedWhenThresholdIsError) {
  LogAt(LogLevel::kError, LogLevel::kInfo, "info message");
  EXPECT_TRUE(entries_.empty());
}

TEST_F(LogLevelFilteringTest, DynamicAt_InfoMessageShownWhenThresholdIsInfo) {
  LogAt(LogLevel::kInfo, LogLevel::kInfo, "info message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kInfo);
}

TEST_F(LogLevelFilteringTest, DynamicAt_InfoMessageShownWhenThresholdIsDebug) {
  LogAt(LogLevel::kDebug, LogLevel::kInfo, "info message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kInfo) << "shown at its own level, never promoted or demoted";
}

TEST_F(LogLevelFilteringTest, DynamicAt_ErrorMessageShownWhenThresholdIsError) {
  LogAt(LogLevel::kError, LogLevel::kError, "error message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kError);
}

TEST_F(LogLevelFilteringTest, DynamicAt_InfoSuppressedByOverallLoggerThreshold) {
  Logger("warning");
  LogAt(LogLevel::kDebug, LogLevel::kInfo, "info message");
  EXPECT_TRUE(entries_.empty());
}

// ---- Session thresholds (1.x: GIZMOSQL_LOGKV_SESSION_DYNAMIC_AT, query logging) ----

TEST_F(SessionLogLevelFilteringTest, SessionDynamicAt_DebugMessageSuppressedWhenThresholdIsInfo) {
  LogAt(LogLevel::kInfo, LogLevel::kDebug, "debug message");
  EXPECT_TRUE(entries_.empty());
}

TEST_F(SessionLogLevelFilteringTest, SessionDynamicAt_InfoMessageShownWhenThresholdIsInfo) {
  LogAt(LogLevel::kInfo, LogLevel::kInfo, "info message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kInfo);
}

TEST_F(SessionLogLevelFilteringTest, SessionDynamicAt_InfoMessageNotPromotedWhenThresholdIsDebug) {
  LogAt(LogLevel::kDebug, LogLevel::kInfo, "info message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kInfo);
}

TEST_F(SessionLogLevelFilteringTest, SessionDynamicAt_WarningSuppressedWhenThresholdIsError) {
  LogAt(LogLevel::kError, LogLevel::kWarning, "warning message");
  EXPECT_TRUE(entries_.empty());
}

TEST_F(SessionLogLevelFilteringTest, SessionDynamicAt_ErrorShownWhenThresholdIsWarning) {
  LogAt(LogLevel::kWarning, LogLevel::kError, "error message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kError);
}

// ---- A session's own threshold decides alone (1.x: GIZMOSQL_LOGKV_SESSION_DYNAMIC) ----

TEST_F(SessionLogLevelFilteringTest, SessionDynamic_ShownAtNaturalSeverity) {
  LogAt(LogLevel::kDebug, LogLevel::kInfo, "info message", {}, true);
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kInfo);
}

TEST_F(SessionLogLevelFilteringTest, SessionDynamic_IgnoresLoggerThreshold) {
  Logger("error");
  LogAt(LogLevel::kDebug, LogLevel::kDebug, "debug message", {}, true);
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kDebug);
}

// ---- Both thresholds ----

TEST_F(LogLevelFilteringTest, BothThresholdsMustBeSatisfied) {
  Logger("error");
  LogAt(LogLevel::kInfo, LogLevel::kWarning, "warning message");
  EXPECT_TRUE(entries_.empty());
}

TEST_F(LogLevelFilteringTest, WarningShownWhenBothThresholdsMet) {
  Logger("warning");
  LogAt(LogLevel::kInfo, LogLevel::kWarning, "warning message");
  ASSERT_EQ(entries_.size(), 1u);
  EXPECT_EQ(entries_[0].level, LogLevel::kWarning);
}
