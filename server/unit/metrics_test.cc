// The metrics registry: ports the unit cases of GizmoSQL 1.x's
// tests/integration/test_metrics.cpp (MetricsRegistryTest.*) to Hardcharger's Metrics.
#include <gtest/gtest.h>

#include <thread>
#include <vector>

#include "metrics.hpp"

using hardcharger::ClassifyStatement;
using hardcharger::ClassifyStatementError;
using hardcharger::Metrics;
using hardcharger::Sample;
using hardcharger::StatementKind;

namespace {

double Value(const Metrics& metrics, const std::string& name, const hardcharger::Labels& labels = {}) {
  for (const auto& sample : metrics.Snapshot()) {
    if (sample.name == name && sample.labels == labels) return sample.value;
  }
  return -1;
}

}  // namespace

TEST(MetricsRegistryTest, UpdatesAreAtomicUnderContention) {
  Metrics metrics;
  metrics.Enable();
  std::vector<std::thread> threads;
  for (int t = 0; t < 20; t++) {
    threads.emplace_back([&] {
      for (int i = 0; i < 1000; i++) metrics.RecordStatement(StatementKind::kRead, "ok", "", 0.001);
    });
  }
  for (auto& thread : threads) thread.join();
  EXPECT_EQ(Value(metrics, "gizmosql_statements_total", {{"status", "ok"}}), 20000);
}

TEST(MetricsRegistryTest, NothingIsCountedWhileDisabled) {
  Metrics metrics;
  metrics.RecordStatement(StatementKind::kRead, "ok", "", 0.001);
  EXPECT_EQ(Value(metrics, "gizmosql_statements_total", {{"status", "ok"}}), 0);
}

TEST(MetricsRegistryTest, HistogramsAreCumulativeAndCountEveryOutcome) {
  Metrics metrics;
  metrics.Enable();
  metrics.RecordStatement(StatementKind::kRead, "ok", "", 0.001);
  metrics.RecordStatement(StatementKind::kRead, "error", "out_of_memory", 2.0);
  metrics.RecordStatement(StatementKind::kRead, "cancelled", "cancelled", 0.2);
  double last = 0;
  for (const auto& sample : metrics.Snapshot()) {
    if (sample.name == "gizmosql_statement_duration_seconds_bucket" && sample.labels.at("kind") == "read") {
      EXPECT_GE(sample.value, last);
      last = sample.value;
      if (sample.labels.at("le") == "0.005") EXPECT_EQ(sample.value, 1);
      if (sample.labels.at("le") == "+Inf") EXPECT_EQ(sample.value, 3);
    }
  }
  EXPECT_EQ(Value(metrics, "gizmosql_statement_duration_seconds_count", {{"kind", "read"}}), 3);
  EXPECT_EQ(Value(metrics, "gizmosql_statements_total", {{"status", "ok"}}), 1);
  EXPECT_EQ(Value(metrics, "gizmosql_statements_total", {{"status", "error"}}), 1);
  EXPECT_EQ(Value(metrics, "gizmosql_statements_total", {{"status", "cancelled"}}), 1);
  EXPECT_EQ(Value(metrics, "gizmosql_statement_errors_total", {{"class", "out_of_memory"}}), 1);
}

TEST(MetricsRegistryTest, SerializationEscapesLabelsAndHasOneTypePerFamily) {
  Metrics metrics;
  metrics.Enable();
  metrics.RecordStatement(StatementKind::kRead, "ok", "", 0.001);
  metrics.RecordStatement(StatementKind::kWrite, "ok", "", 0.001);
  metrics.AddCollector([](std::vector<Sample>& out) {
    out.push_back({"gizmosql_test_info", {{"version", "quote\"slash\\newline\n"}}, 1});
  });
  const std::string text = metrics.Render();
  EXPECT_NE(text.find("version=\"quote\\\"slash\\\\newline\\n\""), text.npos) << text;
  const std::string type = "# TYPE gizmosql_statement_duration_seconds histogram\n";
  const auto first = text.find(type);
  ASSERT_NE(first, text.npos) << text;
  EXPECT_EQ(text.find(type, first + 1), text.npos) << "two TYPE lines for one family";
  EXPECT_EQ(text.back(), '\n');
}

TEST(MetricsRegistryTest, ParsedStatementClassification) {
  EXPECT_EQ(ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE_SELECT), StatementKind::kRead);
  EXPECT_EQ(ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE_MERGE_INTO), StatementKind::kWrite);
  EXPECT_EQ(ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE_ATTACH), StatementKind::kDdl);
  EXPECT_EQ(ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE_CALL), StatementKind::kOther);
}

TEST(MetricsRegistryTest, ErrorClassification) {
  EXPECT_EQ(ClassifyStatementError("Out of Memory Error: allocation failed"), "out_of_memory");
  EXPECT_EQ(ClassifyStatementError("IO Error: disk full"), "io");
  EXPECT_EQ(ClassifyStatementError("Catalog Error: missing table"), "catalog");
  EXPECT_EQ(ClassifyStatementError("Parser Error: syntax error"), "syntax");
  EXPECT_EQ(ClassifyStatementError("Permission denied"), "permission");
  EXPECT_EQ(ClassifyStatementError("INTERRUPT Error: Interrupted!"), "cancelled");
  EXPECT_EQ(ClassifyStatementError("unknown"), "other");
}
