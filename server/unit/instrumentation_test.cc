// Instrumentation's writer: GizmoSQL 1.x's InstrumentationManagerTest.FailedWriteDoesNotAffectOtherWrites,
// against Hardcharger's Instrumentation, and the batching writer's own cases: rows of earlier
// batches updated in place, a full queue that drops and counts, the backlog written at
// shutdown, and the shutdown deadline (server/tests/test_instrumentation.py runs them).
// HARDCHARGER_TEST_UNIT_PG (a libpq conninfo) also runs the PostgreSQL case.
#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <format>
#include <mutex>
#include <thread>
#include <vector>

#include "duckdb_sql.hpp"
#include "duckdb_v2.hpp"
#include "instrumentation.hpp"
#include "logging.hpp"
#include "platform.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"

namespace {

using hardcharger::ExecutionRecord;
using hardcharger::Instrumentation;
using hardcharger::InstrumentationLimits;
using std::chrono::milliseconds;

constexpr const char* kInstance = "00000000-0000-0000-0000-0000000000aa";

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
  std::string Query(const std::string& sql) { return warpduckdb::QueryText(connection.get(), sql); }
};

// The log messages the writer wrote (it logs from its own thread).
class Captured {
 public:
  Captured() {
    hardcharger::SetLogCaptureForTesting([this](hardcharger::LogLevel, std::string_view message) {
      std::lock_guard lock(mutex_);
      messages_.emplace_back(message);
    });
  }
  ~Captured() { hardcharger::SetLogCaptureForTesting(nullptr); }
  bool Has(std::string_view text) {
    std::lock_guard lock(mutex_);
    for (const auto& m : messages_) {
      if (m.find(text) != std::string::npos) return true;
    }
    return false;
  }

 private:
  std::mutex mutex_;
  std::vector<std::string> messages_;
};

// An instrumentation file of the test's own, removed afterwards.
struct Fixture {
  std::filesystem::path file;
  hardcharger::Config config;
  std::unique_ptr<hardcharger::Runtime> runtime;
  Db db;
  explicit Fixture(const std::string& name) {
    file = std::filesystem::temp_directory_path() /
           std::format("hc_instr_{}_{}.db", name, hardcharger::ProcessId());
    std::filesystem::remove(file);
    std::filesystem::remove(file.string() + ".wal");
    config.admin_password = "x";
    config.enable_instrumentation = true;
    config.instrumentation_db_path = file.string();
    runtime = std::make_unique<hardcharger::Runtime>(config, kInstance);
  }
  ~Fixture() {
    try {
      warpduckdb::RunSql(db.connection.get(), "DETACH _hardcharger_instr");
    } catch (const std::exception&) {
    }
    std::filesystem::remove(file);
    std::filesystem::remove(file.string() + ".wal");
  }
  const hardcharger::InstrumentationStats& stats() const { return runtime->instrumentation_stats; }
  // Waits until the writer has written `events` events in all.
  void WaitWritten(uint64_t events) const {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (stats().written.load() < events && std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(milliseconds(5));
    }
    ASSERT_GE(stats().written.load(), events);
  }
};

hardcharger::StatementRecord Statement(const std::string& id, const std::string& session) {
  return {.statement_id = id,
          .session_id = session,
          .sql = "SELECT 1 AS " + id,
          .kind = "statement",
          .accepted = true,
          .error = "",
          .created = std::chrono::system_clock::now(),
          .query_tag = ""};
}

hardcharger::SessionRecord SessionRow(const std::string& id) {
  return {.session_id = id,
          .username = "alice",
          .role = "admin",
          .auth_method = "password",
          .connection_protocol = "http",
          .start = std::chrono::system_clock::now()};
}

// Its outcome 3 ms after its start, its last batch read at 9 ms.
void Finish(ExecutionRecord& r, int64_t rows) {
  r.end = r.start + milliseconds(3);
  r.fetch_start = r.start + milliseconds(4);
  r.fetch_end = r.cursor_close = r.start + milliseconds(9);
  r.rows = rows;
  r.status = "success";
}

}  // namespace

TEST(InstrumentationTest, LiveMonitorDoesNotDependOnHistoryWrites) {
  Fixture f("live_monitor");
  Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0", {.max_queued_bytes = 1});
  instrumentation.Attach();
  instrumentation.Start();
  ExecutionRecord active;
  active.execution_id = active.statement_id = "q_old_active";
  active.start = std::chrono::system_clock::now() - std::chrono::hours(3);
  active.sql = "SELECT old_active";
  instrumentation.ExecutionStarted(active);
  ASSERT_GT(f.stats().dropped.load(), 0);
  // No time window or completion cap may evict work still active.
  for (int i = 0; i < 2050; ++i) {
    ExecutionRecord finished;
    finished.execution_id = finished.statement_id = "q_finished_" + std::to_string(i);
    finished.start = std::chrono::system_clock::now();
    Finish(finished, 1);
    instrumentation.ExecutionFinished(finished);
  }
  auto records = instrumentation.MonitorRecords();
  EXPECT_EQ(records.size(), 2049);
  EXPECT_TRUE(std::any_of(records.begin(), records.end(), [](const auto& r) { return r.execution_id == "q_old_active"; }));
  active.end = active.fetch_start = std::chrono::system_clock::now();
  active.rows = 42;
  instrumentation.ExecutionProgress(active);
  records = instrumentation.MonitorRecords();
  const auto found = std::find_if(records.begin(), records.end(), [](const auto& r) { return r.execution_id == "q_old_active"; });
  ASSERT_NE(found, records.end());
  EXPECT_EQ(found->rows, 42);
  EXPECT_FALSE(found->fetch_end);
  instrumentation.Stop();
}

TEST(InstrumentationTest, FailedWriteDoesNotAffectOtherWrites) {
  Fixture f("failed");
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0");
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.EnqueueForTesting("INSERT INTO _hardcharger_instr.main.no_such_table VALUES (1)");
    instrumentation.EnqueueForTesting(
        "UPDATE _hardcharger_instr.main.instances SET instance_tag = '{\"after\": \"failure\"}' "
        "WHERE instance_id = '00000000-0000-0000-0000-0000000000aa'");
    instrumentation.Stop();  // writes everything queued
  }
  EXPECT_EQ(f.db.Query("SELECT instance_tag->>'after' FROM _hardcharger_instr.main.instances "
                       "WHERE instance_id = '00000000-0000-0000-0000-0000000000aa'"),
            "failure");
  EXPECT_EQ(f.db.Query("SELECT status || '/' || stop_reason FROM _hardcharger_instr.main.instances"),
            "stopped/graceful");
}

// A bad record in a batch loses only itself: the batch is retried in halves.
TEST(InstrumentationTest, ABadRecordInABatchLosesOnlyItself) {
  Fixture f("badrecord");
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0");
    instrumentation.SetWriteDelayForTesting(milliseconds(200));  // so the records share one batch
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.RecordSessionStarted(SessionRow("s_bad"));
    for (int i = 0; i < 20; i++) {
      auto statement = Statement(std::format("q_bad{:02}", i), "s_bad");
      if (i == 7) statement.query_tag = "{not json";  // fails its ::JSON cast
      instrumentation.RecordStatement(statement);
    }
    instrumentation.Stop();
  }
  EXPECT_EQ(f.db.Query("SELECT count(*) FROM _hardcharger_instr.main.sql_statements"), "19");
  EXPECT_EQ(f.db.Query("SELECT count(*) FROM _hardcharger_instr.main.sql_statements WHERE statement_id = 'q_bad07'"),
            "0");
  EXPECT_EQ(f.stats().failed.load(), 1u);
}

// An execution whose start was written by an earlier batch is updated in place, through
// every phase (queued, admitted, finished), as is its session; within one batch the same
// events fold into one row. Either way the row ends the same.
TEST(InstrumentationTest, RowsOfEarlierBatchesAreUpdatedAndOneBatchFoldsThem) {
  for (const bool separate : {true, false}) {
    SCOPED_TRACE(separate ? "a batch per event" : "one batch");
    Fixture f(separate ? "update" : "fold");
    ExecutionRecord r;
    std::string start, enqueue;
    {
      Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0");
      if (!separate) instrumentation.SetWriteDelayForTesting(milliseconds(300));
      instrumentation.Attach();
      instrumentation.Start();
      uint64_t events = 0;
      auto step = [&] {
        if (separate) f.WaitWritten(++events);
      };
      const auto session = SessionRow("s_one");
      instrumentation.RecordSessionStarted(session);
      step();
      instrumentation.RecordStatement(Statement("q_one", "s_one"));
      step();
      r.execution_id = r.statement_id = "q_one";
      r.enqueue_time = r.start = std::chrono::system_clock::now();
      instrumentation.ExecutionQueued(r);
      step();
      r.start = *r.enqueue_time + milliseconds(25);
      instrumentation.ExecutionAdmitted(r);
      step();
      Finish(r, 42);
      r.query_profile = R"({"latency": 0.01})";
      instrumentation.ExecutionFinished(r);
      step();
      instrumentation.RecordSessionEnded("s_one", session.start, "closed");
      step();
      instrumentation.Stop();
    }
    const std::string p = "_hardcharger_instr.main.";
    EXPECT_EQ(f.db.Query("SELECT concat_ws('|', status, rows_fetched, duration_ms, total_duration_ms, "
                         "execution_start_time = " + hardcharger::SqlTimestamp(r.start) + ", enqueue_time = " +
                         hardcharger::SqlTimestamp(*r.enqueue_time) + ", cursor_close_time = " +
                         hardcharger::SqlTimestamp(*r.cursor_close) + ", query_profile->>'latency', "
                         "date_diff('millisecond', execution_start_time, execution_end_time) = duration_ms, "
                         "date_diff('millisecond', execution_start_time, fetch_end_time) = total_duration_ms) FROM " +
                         p + "sql_executions"),
              "success|42|3|9|true|true|true|0.01|true|true");
    EXPECT_EQ(f.db.Query("SELECT count(*) FROM " + p + "sql_executions"), "1");
    EXPECT_EQ(f.db.Query("SELECT status || '/' || stop_reason || '/' || (stop_time IS NOT NULL) FROM " + p +
                         "sessions"),
              "closed/closed/true");
    EXPECT_EQ(f.db.Query("SELECT count(*) FROM " + p + "execution_details WHERE status = 'success' AND "
                         "queue_wait_ms = 25"),
              "1");
  }
}

// Recording never waits on a slow catalog: past the queue's limit new records are dropped,
// counted (hardcharger_server_info(), /metrics) and reported, and the rest are written.
TEST(InstrumentationTest, AFullQueueDropsCountsAndReportsNewRecords) {
  Fixture f("full");
  Captured log;
  InstrumentationLimits limits;
  limits.max_queued_bytes = 64 * 1024;
  constexpr int kStatements = 2000;
  std::chrono::steady_clock::duration recording{};
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0", limits);
    instrumentation.SetWriteDelayForTesting(milliseconds(300));
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.RecordSessionStarted(SessionRow("s_full"));
    const auto started = std::chrono::steady_clock::now();
    for (int i = 0; i < kStatements; i++) {
      instrumentation.RecordStatement(Statement(std::format("q_f{:05}", i), "s_full"));
    }
    recording = std::chrono::steady_clock::now() - started;
    EXPECT_GT(f.stats().dropped.load(), 0u);
    EXPECT_GT(f.stats().queued.load(), 0);
    instrumentation.Stop();
  }
  // 2000 records against a writer that sleeps 300 ms a batch: recording didn't wait for it.
  EXPECT_LT(std::chrono::duration_cast<milliseconds>(recording).count(), 250);
  const uint64_t dropped = f.stats().dropped.load();
  const uint64_t written = f.stats().written.load();
  EXPECT_EQ(written + dropped, static_cast<uint64_t>(kStatements) + 1);
  EXPECT_EQ(f.db.Query("SELECT count(*) FROM _hardcharger_instr.main.sql_statements"),
            std::to_string(written - 1));  // the session row is one of them
  EXPECT_EQ(f.stats().queued.load(), 0);
  EXPECT_TRUE(log.Has("instrumentation queue full: records dropped"));
  EXPECT_TRUE(log.Has("instrumentation stopped"));
}

// Stop() writes the whole backlog before it closes the instance's rows.
TEST(InstrumentationTest, ShutdownWritesTheBacklog) {
  Fixture f("drain");
  constexpr int kThreads = 8, kPerThread = 2500;
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0");
    instrumentation.SetWriteDelayForTesting(milliseconds(100));  // a backlog builds up
    instrumentation.Attach();
    instrumentation.Start();
    std::vector<std::thread> threads;
    for (int t = 0; t < kThreads; t++) {
      threads.emplace_back([&, t] {
        const std::string session = std::format("s_drain{}", t);
        instrumentation.RecordSessionStarted(SessionRow(session));
        for (int i = 0; i < kPerThread; i++) {
          const std::string id = std::format("q_d{}_{:05}", t, i);
          instrumentation.RecordStatement(Statement(id, session));
          ExecutionRecord r;
          r.execution_id = r.statement_id = id;
          r.start = std::chrono::system_clock::now();
          instrumentation.ExecutionStarted(r);
          Finish(r, 1);
          instrumentation.ExecutionFinished(r);
        }
      });
    }
    for (auto& thread : threads) thread.join();
    EXPECT_GT(f.stats().queued.load(), 0);  // still queued when Stop() begins
    instrumentation.Stop();
  }
  EXPECT_EQ(f.stats().dropped.load(), 0u);
  EXPECT_EQ(f.db.Query("SELECT count(*) || '/' || count(*) FILTER (status = 'success' AND rows_fetched = 1 AND "
                       "cursor_close_time IS NOT NULL) FROM _hardcharger_instr.main.sql_executions"),
            std::format("{0}/{0}", kThreads * kPerThread));
  // Then the closing sweep: the sessions this instance left open are closed.
  EXPECT_EQ(f.db.Query("SELECT string_agg(DISTINCT status || '/' || stop_reason, ',') FROM "
                       "_hardcharger_instr.main.sessions"),
            "closed/server_shutdown");
  EXPECT_EQ(f.db.Query("SELECT status || '/' || stop_reason FROM _hardcharger_instr.main.instances"),
            "stopped/graceful");
}

// A backlog the catalog can't take in time is dropped at the deadline, counted and said so;
// the instance's rows are closed all the same.
TEST(InstrumentationTest, ShutdownGivesUpOnTheBacklogAtItsDeadline) {
  Fixture f("deadline");
  Captured log;
  InstrumentationLimits limits;
  limits.max_batch_events = 10;
  limits.drain_deadline = milliseconds(500);
  std::chrono::steady_clock::duration stopping{};
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0", limits);
    instrumentation.SetWriteDelayForTesting(milliseconds(100));  // 100 records a second
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.RecordSessionStarted(SessionRow("s_late"));
    for (int i = 0; i < 1000; i++) instrumentation.RecordStatement(Statement(std::format("q_l{:04}", i), "s_late"));
    const auto started = std::chrono::steady_clock::now();
    instrumentation.Stop();
    stopping = std::chrono::steady_clock::now() - started;
  }
  EXPECT_LT(std::chrono::duration_cast<milliseconds>(stopping).count(), 3000);
  const uint64_t dropped = f.stats().dropped.load();
  EXPECT_GT(dropped, 500u);
  EXPECT_EQ(f.stats().written.load() + dropped, 1001u);
  EXPECT_TRUE(log.Has("instrumentation backlog not written by the shutdown deadline"));
  EXPECT_EQ(f.db.Query("SELECT status || '/' || stop_reason FROM _hardcharger_instr.main.instances"),
            "stopped/graceful");
}

// Text that isn't valid UTF-8 (a statement cut at 4000 bytes can split a character) or
// holds NUL is stored with U+FFFD in place of each bad byte, not lost.
TEST(InstrumentationTest, InvalidUtf8IsStoredReplaced) {
  Fixture f("utf8");
  {
    Instrumentation instrumentation(*f.runtime, f.db.instance.get(), "v2.0.0");
    instrumentation.Attach();
    instrumentation.Start();
    instrumentation.RecordSessionStarted(SessionRow("s_utf8"));
    auto statement = Statement("q_utf8", "s_utf8");
    statement.sql = std::string("SELECT 'caf\xC3\xA9 \xE2\x80", 16) + std::string(1, '\0') + "' \"q\" \\ \n\t'it''s'";
    instrumentation.RecordStatement(statement);
    instrumentation.Stop();
  }
  EXPECT_EQ(f.db.Query("SELECT sql_text FROM _hardcharger_instr.main.sql_statements"),
            "SELECT 'caf\xC3\xA9 \xEF\xBF\xBD\xEF\xBF\xBD\xEF\xBF\xBD' \"q\" \\ \n\t'it''s'");
  EXPECT_EQ(f.stats().failed.load(), 0u);
}

// PostgreSQL, the production backend: the same rows, through its own SQL, and a record
// whose parent was never written (dropped from a full queue) is left out instead of
// failing its batch on the foreign key.
TEST(InstrumentationTest, PostgresWritesUpdatesAndSkipsOrphans) {
  const char* conninfo = std::getenv("HARDCHARGER_TEST_UNIT_PG");
  if (conninfo == nullptr) GTEST_SKIP() << "HARDCHARGER_TEST_UNIT_PG is not set";
  hardcharger::Config config;
  config.admin_password = "x";
  config.enable_instrumentation = true;
  config.instrumentation_catalog = "pg_unit";
  config.instrumentation_schema = std::format("hc_unit_{}", hardcharger::ProcessId());
  hardcharger::Runtime runtime(config, kInstance);
  Db db;
  warpduckdb::RunSql(db.connection.get(), "INSTALL postgres; LOAD postgres");
  warpduckdb::RunSql(db.connection.get(),
                     "ATTACH " + hardcharger::SqlLiteral(conninfo) + " AS pg_unit (TYPE postgres)");
  const std::string drop = "CALL postgres_execute('pg_unit', 'DROP SCHEMA IF EXISTS " + config.instrumentation_schema +
                           " CASCADE')";
  warpduckdb::RunSql(db.connection.get(), drop);
  ExecutionRecord kept;
  {
    Instrumentation instrumentation(runtime, db.instance.get(), "v2.0.0");
    instrumentation.Start();
    instrumentation.RecordSessionStarted(SessionRow("s_pg"));
    instrumentation.RecordStatement(Statement("q_pg", "s_pg"));
    kept.execution_id = kept.statement_id = "q_pg";
    kept.start = std::chrono::system_clock::now();
    instrumentation.ExecutionStarted(kept);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (runtime.instrumentation_stats.written.load() < 3 && std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(milliseconds(5));
    }
    Finish(kept, 7);
    instrumentation.ExecutionFinished(kept);  // an update of the row written above
    // Orphans: a statement of a session never recorded, an execution of a statement never recorded.
    instrumentation.RecordStatement(Statement("q_orphan", "s_never"));
    ExecutionRecord orphan;
    orphan.execution_id = orphan.statement_id = "q_never";
    orphan.start = std::chrono::system_clock::now();
    instrumentation.ExecutionStarted(orphan);
    instrumentation.Stop();
  }
  warpduckdb::RunSql(db.connection.get(), "CALL postgres_clear_cache()");
  const std::string p = "pg_unit." + config.instrumentation_schema + ".";
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(),
                                  "SELECT concat_ws('|', status, rows_fetched, duration_ms, total_duration_ms, "
                                  "execution_start_time = " + hardcharger::SqlTimestamp(kept.start) + ") FROM " + p +
                                      "sql_executions"),
            "success|7|3|9|true");
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(), "SELECT count(*) FROM " + p + "sql_statements"), "1");
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(), "SELECT count(*) FROM " + p + "sql_executions"), "1");
  EXPECT_EQ(runtime.instrumentation_stats.failed.load(), 0u);
  warpduckdb::RunSql(db.connection.get(), drop);
}
