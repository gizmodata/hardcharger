// Port of GizmoSQL 1.x tests/integration/test_interactive_client.cpp (115 cases), run by
// server/tests/test_interactive_client.py against a real hardcharger-server. The client
// classes keep their 1.x names and calls; FlightConnection is ChargewireConnection. Cases that
// differ from 1.x say how, at the case:
//   - TlsSkipVerifyWithMtlsCertIsRejected: re-expressed. 1.x refused --tls-skip-verify with
//     --mtls-cert because Arrow Flight dropped the client certificate when verification was
//     off; over Chargewire the certificate is sent either way, so the combination is allowed
//     (test_client_cli.py proves it end to end against an mTLS server).
//   - DotShow: no "edition:" line (Hardcharger has no editions); engine and arrow come
//     from Chargewire's server-info document.
//   - Every connection to the test server sets use_tls = false: TLS is the client's default
//     (1.x's was plaintext); the server under test is plain HTTP.
//   - URI cases (ParseConnectionURI*, BuildConnectionURI*): re-expressed. The scheme is
//     hardcharger://, with the Hardcharger ADBC driver's rules: HTTPS unless ?tls=false, port
//     31337, and tls the only parameter (1.x took username, useEncryption, ...; they are
//     flags). client_config_test.cc covers every form.
#include <gtest/gtest.h>

#include <atomic>
#include <cstdlib>
#include <sstream>
#include <string>
#include <chrono>
#include <thread>

#include <arrow/api.h>

#include "client_config.hpp"
#include "command_processor.hpp"
#include "output_renderer.hpp"
#include "shell_completer.hpp"
#include "shell_loop.hpp"
#include "signal_watcher.hpp"
#include "sql_processor.hpp"
#include "sql_quoting.hpp"
#include "chargewire_connection.hpp"

using namespace hardcharger::client;

#ifndef _WIN32
TEST(ClientSignalWatcherTest, RepeatedShutdownJoinsTheWaiterAndRestoresTheMask) {
  sigset_t before{};
  ASSERT_EQ(pthread_sigmask(SIG_BLOCK, nullptr, &before), 0);
  for (int attempt = 0; attempt < 100; ++attempt) {
    std::atomic<int> calls{0};
    {
      SignalWatcher watcher([&calls] { calls.fetch_add(1); });
    }
    EXPECT_EQ(calls.load(), 0) << "Shutdown's wake signal must not cancel a query";
  }
  sigset_t after{};
  ASSERT_EQ(pthread_sigmask(SIG_BLOCK, nullptr, &after), 0);
  EXPECT_EQ(sigismember(&before, SIGINT), sigismember(&after, SIGINT));
}

// A non-interactive shell starts background jobs with SIGINT ignored, and an ignored
// SIGINT is discarded: the shutdown's wake signal never reached sigwait(), and the client
// hung at exit (fix/cli-background-exit). With SIGINT ignored from the start there is no
// watcher to wake; ignored only later, shutdown still wakes and joins it. Either way the
// disposition the process had is left as it was.
namespace {
struct sigaction Disposition() {
  struct sigaction current {};
  sigaction(SIGINT, nullptr, &current);
  return current;
}
void SetDisposition(void (*handler)(int)) {
  struct sigaction action {};
  action.sa_handler = handler;
  sigemptyset(&action.sa_mask);
  sigaction(SIGINT, &action, nullptr);
}
}  // namespace

TEST(ClientSignalWatcherTest, ShutdownReturnsWhenSigintStartsIgnored) {
  const struct sigaction before = Disposition();
  SetDisposition(SIG_IGN);
  for (int attempt = 0; attempt < 5; ++attempt) {
    std::atomic<int> calls{0};
    {
      SignalWatcher watcher([&calls] { calls.fetch_add(1); });
      // Long enough for a waiter to be in sigwait(): one destroyed at once never got there,
      // and saw the stop flag instead (the hang needs a client that ran a while).
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    EXPECT_EQ(calls.load(), 0);
  }
  EXPECT_EQ(Disposition().sa_handler, SIG_IGN);
  sigaction(SIGINT, &before, nullptr);
}

TEST(ClientSignalWatcherTest, ShutdownReturnsWhenSigintIsIgnoredLater) {
  const struct sigaction before = Disposition();
  SetDisposition(SIG_DFL);
  for (int attempt = 0; attempt < 5; ++attempt) {
    std::atomic<int> calls{0};
    {
      SignalWatcher watcher([&calls] { calls.fetch_add(1); });
      std::this_thread::sleep_for(std::chrono::milliseconds(100));  // the waiter is in sigwait()
      SetDisposition(SIG_IGN);
    }
    EXPECT_EQ(calls.load(), 0) << "Shutdown's wake signal must not cancel a query";
    EXPECT_EQ(Disposition().sa_handler, SIG_IGN);
    SetDisposition(SIG_DFL);
  }
  sigaction(SIGINT, &before, nullptr);
}
#else
// Windows' watcher waits on a condition its destructor signals, whatever the console's
// Ctrl+C state; these are the POSIX cases' counterparts.
TEST(ClientSignalWatcherTest, ShutdownReturnsWhenSigintStartsIgnored) {
  SetConsoleCtrlHandler(nullptr, TRUE);  // Ctrl+C ignored, as a background start leaves it
  for (int attempt = 0; attempt < 20; ++attempt) {
    std::atomic<int> calls{0};
    { SignalWatcher watcher([&calls] { calls.fetch_add(1); }); }
    EXPECT_EQ(calls.load(), 0);
  }
  SetConsoleCtrlHandler(nullptr, FALSE);
}

TEST(ClientSignalWatcherTest, ShutdownReturnsWhenSigintIsIgnoredLater) {
  for (int attempt = 0; attempt < 20; ++attempt) {
    std::atomic<int> calls{0};
    {
      SignalWatcher watcher([&calls] { calls.fetch_add(1); });
      SetConsoleCtrlHandler(nullptr, TRUE);
    }
    EXPECT_EQ(calls.load(), 0);
    SetConsoleCtrlHandler(nullptr, FALSE);
  }
}

// Windows has no signal mask: the watcher's own thread must be joined, and nothing may
// run the callback, however often a watcher comes and goes.
TEST(ClientSignalWatcherTest, RepeatedShutdownJoinsTheWaiterAndRestoresTheMask) {
  for (int attempt = 0; attempt < 100; ++attempt) {
    std::atomic<int> calls{0};
    {
      SignalWatcher watcher([&calls] { calls.fetch_add(1); });
    }
    EXPECT_EQ(calls.load(), 0) << "Shutdown must not cancel a query";
  }
}
#endif

// ============================================================================
// Test Fixture - the hardcharger-server that server/tests/test_interactive_client.py
// started (with the 1.x fixture's tables) and named in HARDCHARGER_TEST_CLIENT_*.
// ============================================================================

class InteractiveClientFixture : public ::testing::Test {
 public:
  static std::string Env(const char* name) {
    const char* value = std::getenv(name);
    return value != nullptr ? value : "";
  }
  static int GetPort() { return std::atoi(Env("HARDCHARGER_TEST_CLIENT_PORT").c_str()); }
  static std::string GetUsername() { return Env("HARDCHARGER_TEST_CLIENT_USERNAME"); }
  static std::string GetPassword() { return Env("HARDCHARGER_TEST_CLIENT_PASSWORD"); }
  // There is a server to test against (the wrapper always starts one; run alone, the
  // binary fails rather than skipping).
  static bool IsServerReady() { return GetPort() > 0 && !GetUsername().empty(); }

 protected:
  ClientConfig MakeConfig() {
    ClientConfig config;
    config.host = "localhost";
    config.port = GetPort();
    config.use_tls = false;  // the test server is plain HTTP; TLS is the default
    config.username = GetUsername();
    config.password = GetPassword();
    config.password_provided = true;
    return config;
  }

  ChargewireConnection ConnectClient() {
    ChargewireConnection conn;
    auto status = conn.Connect(MakeConfig());
    EXPECT_TRUE(status.ok()) << "Connect failed: " << status.ToString();
    return conn;
  }
};

// ============================================================================
// ChargewireConnection Tests (1.x: ChargewireConnection)
// ============================================================================

TEST_F(InteractiveClientFixture, ConnectWithValidCredentials) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  ASSERT_TRUE(conn.IsConnected());
}

TEST_F(InteractiveClientFixture, LiteralQuotingPreservesSqlLookingText) {
  auto conn = ConnectClient();
  for (const std::string value : {"O'Reilly", "'; DROP TABLE test_data; --",
                                  "/* comment */ ; café 東京", "back\\slash'quote"}) {
    SCOPED_TRACE(value);
    auto result = conn.ExecuteQuery("SELECT " + QuoteSqlLiteral(value));
    ASSERT_TRUE(result.ok()) << result.status().ToString();
    ASSERT_EQ(result->table->num_rows(), 1);
    auto scalar = result->table->column(0)->GetScalar(0);
    ASSERT_TRUE(scalar.ok());
    EXPECT_EQ((*scalar)->ToString(), value);
  }
  auto intact = conn.ExecuteQuery("SELECT count(*) FROM test_data");
  ASSERT_TRUE(intact.ok()) << intact.status().ToString();
  auto count = intact->table->column(0)->GetScalar(0);
  ASSERT_TRUE(count.ok());
  EXPECT_EQ((*count)->ToString(), "3");
}

TEST_F(InteractiveClientFixture, TableMetadataQuotesIdentifiersAndKeepsTablesIntact) {
  auto conn = ConnectClient();
  const std::string table_name = "quoted\"'; DROP TABLE test_data; --é";
  const auto identifier = QuoteSqlIdentifier(table_name);
  auto created =
      conn.ExecuteUpdate("CREATE TEMP TABLE " + identifier + " AS SELECT 42 AS id");
  ASSERT_TRUE(created.ok()) << created.status().ToString();
  auto read = conn.ExecuteQuery("SELECT * FROM " + identifier);
  ASSERT_TRUE(read.ok()) << read.status().ToString();
  const auto uploaded = conn.UploadLastResult(read->table, table_name);
  ASSERT_TRUE(uploaded.ok()) << uploaded.ToString();
  auto config = MakeConfig();
  std::ostringstream output;
  config.output_stream = &output;
  CommandProcessor processor(conn, config);
  EXPECT_EQ(processor.Process(".tables"), CommandResult::OK);
  auto intact = conn.ExecuteQuery("SELECT count(*) FROM test_data");
  ASSERT_TRUE(intact.ok()) << intact.status().ToString();
  auto count = intact->table->column(0)->GetScalar(0);
  ASSERT_TRUE(count.ok());
  EXPECT_EQ((*count)->ToString(), "3");
  auto round_trip = conn.ExecuteQuery("SELECT id FROM " + identifier);
  ASSERT_TRUE(round_trip.ok()) << round_trip.status().ToString();
  EXPECT_EQ(round_trip->table->num_rows(), 1);
}

TEST_F(InteractiveClientFixture, CancellationCanRaceWithDisconnectAndReconnect) {
  ASSERT_TRUE(IsServerReady());
  auto conn = ConnectClient();
  std::atomic<bool> stop{false};
  std::thread canceller([&] {
    while (!stop.load()) {
      conn.SendCancelToServer();
      std::this_thread::yield();
    }
  });
  for (int attempt = 0; attempt < 20; ++attempt) {
    conn.Disconnect();
    const auto status = conn.Connect(MakeConfig());
    EXPECT_TRUE(status.ok()) << status.ToString();
    if (!status.ok()) break;
  }
  stop.store(true);
  canceller.join();
  conn.Disconnect();
  EXPECT_FALSE(conn.IsConnected());
}

// 1.x: --tls-skip-verify with --mtls-cert was refused, because Arrow Flight's gRPC
// transport silently dropped the client certificate when server verification was off.
// Chargewire's client presents the certificate either way, so the combination is allowed:
// Connect gets past the configuration check and fails only on the files themselves
// (which don't exist here), naming the certificate. test_client_cli.py connects with both
// to a real mTLS server.
TEST(ChargewireConnectionConfig, TlsSkipVerifyWithMtlsCertIsRejected) {
  ClientConfig config;
  config.host = "localhost";
  config.port = 31337;
  config.use_tls = true;
  config.tls_skip_verify = true;
  config.mtls_cert = "/tmp/does-not-need-to-exist.crt";
  config.mtls_key = "/tmp/does-not-need-to-exist.key";

  ChargewireConnection conn;
  auto status = conn.Connect(config);
  ASSERT_FALSE(status.ok()) << "The certificate files don't exist";
  EXPECT_EQ(status.message().find("cannot be combined"), std::string::npos)
      << "The combination is allowed over Chargewire: " << status.message();
  EXPECT_NE(status.message().find("/tmp/does-not-need-to-exist.crt"), std::string::npos)
      << "The error names the missing certificate: " << status.message();
}

// Sanity check: --tls-skip-verify alone (no mTLS) must still be permitted —
// the validation should only fire when both are present.
TEST(ChargewireConnectionConfig, TlsSkipVerifyAloneIsPermittedAtConfigCheck) {
  ClientConfig config;
  config.host = "127.0.0.1";
  config.port = 1;  // unreachable port — Connect will fail at network step
  config.use_tls = true;
  config.tls_skip_verify = true;
  // No mtls_cert / mtls_key set.

  ChargewireConnection conn;
  auto status = conn.Connect(config);
  // We expect it to fail (network), but NOT with the validation error.
  EXPECT_EQ(status.message().find("--tls-skip-verify cannot be combined"),
            std::string::npos)
      << "Validation should not fire without mTLS flags: " << status.message();
}

TEST_F(InteractiveClientFixture, ConnectWithInvalidCredentials) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  ClientConfig config;
  config.host = "localhost";
  config.port = GetPort();
  config.use_tls = false;  // the test server is plain HTTP; TLS is the default
  config.username = "wronguser";
  config.password = "wrongpass";
  config.password_provided = true;

  ChargewireConnection conn;
  auto status = conn.Connect(config);
  ASSERT_FALSE(status.ok()) << "Should fail with invalid credentials";
}

TEST_F(InteractiveClientFixture, ExecuteSelectQuery) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 42 AS answer");
  ASSERT_TRUE(result.ok()) << "Query failed: " << result.status().ToString();

  auto table = result->table;
  ASSERT_EQ(table->num_rows(), 1);
  ASSERT_EQ(table->num_columns(), 1);
  ASSERT_EQ(table->schema()->field(0)->name(), "answer");
}

TEST_F(InteractiveClientFixture, ExecuteSelectMultipleRows) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT * FROM test_data ORDER BY id");
  ASSERT_TRUE(result.ok()) << "Query failed: " << result.status().ToString();

  auto table = result->table;
  ASSERT_EQ(table->num_rows(), 3);
  ASSERT_EQ(table->num_columns(), 3);
  ASSERT_EQ(table->schema()->field(0)->name(), "id");
  ASSERT_EQ(table->schema()->field(1)->name(), "name");
  ASSERT_EQ(table->schema()->field(2)->name(), "value");
}

TEST_F(InteractiveClientFixture, ExecuteSelectEmptyTable) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT * FROM empty_table");
  ASSERT_TRUE(result.ok()) << "Query failed: " << result.status().ToString();

  auto table = result->table;
  ASSERT_EQ(table->num_rows(), 0);
  ASSERT_EQ(table->num_columns(), 1);
}

TEST_F(InteractiveClientFixture, ExecuteUpdateInsert) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteUpdate(
      "INSERT INTO test_data VALUES (4, 'Dave', 40.0)");
  ASSERT_TRUE(result.ok()) << "Update failed: " << result.status().ToString();

  // Verify the insert
  auto query_result = conn.ExecuteQuery(
      "SELECT COUNT(*) AS cnt FROM test_data WHERE name = 'Dave'");
  ASSERT_TRUE(query_result.ok());
  auto table = query_result->table;
  ASSERT_EQ(table->num_rows(), 1);

  std::string count_val = GetCellValue(table->column(0), 0, "NULL");
  ASSERT_EQ(count_val, "1");
}

TEST_F(InteractiveClientFixture, ExecuteInvalidSQL) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT * FROM nonexistent_table_xyz");
  ASSERT_FALSE(result.ok()) << "Invalid query should fail";
}

TEST_F(InteractiveClientFixture, ExecuteCreateAndDrop) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  // CREATE TABLE
  auto create_result = conn.ExecuteUpdate(
      "CREATE TABLE temp_test (a INTEGER, b VARCHAR)");
  ASSERT_TRUE(create_result.ok())
      << "CREATE TABLE failed: " << create_result.status().ToString();

  // INSERT
  auto insert_result = conn.ExecuteUpdate(
      "INSERT INTO temp_test VALUES (1, 'hello')");
  ASSERT_TRUE(insert_result.ok());

  // SELECT
  auto query_result = conn.ExecuteQuery("SELECT * FROM temp_test");
  ASSERT_TRUE(query_result.ok());
  ASSERT_EQ(query_result->table->num_rows(), 1);

  // DROP
  auto drop_result = conn.ExecuteUpdate("DROP TABLE temp_test");
  ASSERT_TRUE(drop_result.ok());
}

TEST_F(InteractiveClientFixture, GetTables) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.GetTables();
  ASSERT_TRUE(result.ok()) << "GetTables failed: " << result.status().ToString();

  auto table = *result;
  ASSERT_GT(table->num_rows(), 0) << "Should have at least one table";
  ASSERT_GT(table->num_columns(), 0);
}

TEST_F(InteractiveClientFixture, GetCatalogs) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.GetCatalogs();
  ASSERT_TRUE(result.ok()) << "GetCatalogs failed: " << result.status().ToString();

  auto table = *result;
  ASSERT_GT(table->num_rows(), 0) << "Should have at least one catalog";
}

TEST_F(InteractiveClientFixture, GetDbSchemas) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.GetDbSchemas();
  ASSERT_TRUE(result.ok()) << "GetDbSchemas failed: " << result.status().ToString();

  auto table = *result;
  ASSERT_GT(table->num_rows(), 0) << "Should have at least one schema";
}

TEST_F(InteractiveClientFixture, MultipleQueriesOnSameConnection) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  for (int i = 0; i < 10; ++i) {
    auto result = conn.ExecuteQuery("SELECT " + std::to_string(i) + " AS val");
    ASSERT_TRUE(result.ok()) << "Query " << i
                             << " failed: " << result.status().ToString();
    ASSERT_EQ(result->table->num_rows(), 1);
  }
}

TEST_F(InteractiveClientFixture, NullValues) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT NULL AS n, 42 AS v");
  ASSERT_TRUE(result.ok());
  auto table = result->table;
  ASSERT_EQ(table->num_rows(), 1);

  std::string null_val = GetCellValue(table->column(0), 0, "NULL");
  ASSERT_EQ(null_val, "NULL");

  std::string int_val = GetCellValue(table->column(1), 0, "NULL");
  ASSERT_EQ(int_val, "42");
}

// ============================================================================
// Output Renderer Tests (using real query results from server)
// ============================================================================

TEST_F(InteractiveClientFixture, BoxRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS a, 'hello' AS b");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.output_mode = OutputMode::BOX;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Box renderer uses Unicode box-drawing characters
  EXPECT_NE(output.find("\u250c"), std::string::npos) << "Should have top-left corner";
  EXPECT_NE(output.find("\u2518"), std::string::npos) << "Should have bottom-right corner";
  EXPECT_NE(output.find("a"), std::string::npos) << "Should have column name 'a'";
  EXPECT_NE(output.find("hello"), std::string::npos) << "Should have value 'hello'";
}

TEST_F(InteractiveClientFixture, TableRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS a, 'hello' AS b");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::TABLE, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("+"), std::string::npos) << "Should have ASCII borders";
  EXPECT_NE(output.find("|"), std::string::npos) << "Should have column separators";
  EXPECT_NE(output.find("a"), std::string::npos);
  EXPECT_NE(output.find("hello"), std::string::npos);
}

TEST_F(InteractiveClientFixture, CsvRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT * FROM test_data ORDER BY id LIMIT 2");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::CSV, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should have header line and data lines
  EXPECT_NE(output.find("id,name,value"), std::string::npos)
      << "Should have CSV headers";
  EXPECT_NE(output.find("Alice"), std::string::npos);
  EXPECT_NE(output.find("Bob"), std::string::npos);
}

TEST_F(InteractiveClientFixture, JsonRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS x, 'y' AS name");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::JSON, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("["), std::string::npos) << "Should be JSON array";
  EXPECT_NE(output.find("]"), std::string::npos);
  EXPECT_NE(output.find("\"x\""), std::string::npos);
  EXPECT_NE(output.find("\"name\""), std::string::npos);
  EXPECT_NE(output.find("\"y\""), std::string::npos);
}

TEST_F(InteractiveClientFixture, JsonLinesRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT id, name FROM test_data ORDER BY id LIMIT 2");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::JSONLINES, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Each line should be a valid JSON object
  EXPECT_NE(output.find("\"Alice\""), std::string::npos);
  EXPECT_NE(output.find("\"Bob\""), std::string::npos);
  // No opening bracket (not an array)
  EXPECT_EQ(output.find("["), std::string::npos);
}

TEST_F(InteractiveClientFixture, MarkdownRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS col1, 2 AS col2");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::MARKDOWN, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("| col1"), std::string::npos);
  EXPECT_NE(output.find("| col2"), std::string::npos);
  EXPECT_NE(output.find("| ---"), std::string::npos) << "Should have separator row";
}

TEST_F(InteractiveClientFixture, LineRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS alpha, 'test' AS beta");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::LINE, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("alpha = "), std::string::npos);
  EXPECT_NE(output.find("beta = test"), std::string::npos);
}

TEST_F(InteractiveClientFixture, HtmlRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS x");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::HTML, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("<table>"), std::string::npos);
  EXPECT_NE(output.find("</table>"), std::string::npos);
  EXPECT_NE(output.find("<th>x</th>"), std::string::npos);
}

TEST_F(InteractiveClientFixture, LatexRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS x");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::LATEX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("\\begin{tabular}"), std::string::npos);
  EXPECT_NE(output.find("\\end{tabular}"), std::string::npos);
}

TEST_F(InteractiveClientFixture, InsertRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT id, name FROM test_data ORDER BY id LIMIT 1");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.insert_table = "my_table";
  auto renderer = CreateRenderer(OutputMode::INSERT, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("INSERT INTO my_table VALUES("), std::string::npos);
  EXPECT_NE(output.find("'Alice'"), std::string::npos);
}

TEST_F(InteractiveClientFixture, TabsRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS a, 2 AS b");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::TABS, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("a\tb"), std::string::npos) << "Headers tab-separated";
}

TEST_F(InteractiveClientFixture, TrashRendererProducesNoOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT * FROM test_data");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::TRASH, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  ASSERT_EQ(out.str(), "") << "Trash renderer should produce no output";
}

TEST_F(InteractiveClientFixture, RendererNoHeaders) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS col1");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.show_headers = false;
  auto renderer = CreateRenderer(OutputMode::CSV, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should NOT have header row
  EXPECT_EQ(output.find("col1"), std::string::npos)
      << "Should not have headers when disabled";
  // Should still have data
  EXPECT_NE(output.find("1"), std::string::npos);
}

TEST_F(InteractiveClientFixture, RendererCustomNullValue) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT NULL AS n");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.null_value = "(nil)";
  auto renderer = CreateRenderer(OutputMode::CSV, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("(nil)"), std::string::npos)
      << "Should use custom null value";
}

TEST_F(InteractiveClientFixture, RendererMultipleRows) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT * FROM test_data ORDER BY id");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("Alice"), std::string::npos);
  EXPECT_NE(output.find("Bob"), std::string::npos);
  EXPECT_NE(output.find("Charlie"), std::string::npos);
}

// ============================================================================
// SQL Processor Tests (unit tests, no server needed)
// ============================================================================

TEST_F(InteractiveClientFixture, SqlProcessorSimpleStatement) {
  SqlProcessor proc;
  ASSERT_TRUE(proc.AccumulateLine("SELECT 1;"));
  EXPECT_EQ(proc.GetStatement(), "SELECT 1");
}

TEST_F(InteractiveClientFixture, SqlProcessorMultiLine) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT"));
  ASSERT_TRUE(proc.IsAccumulating());
  ASSERT_TRUE(proc.AccumulateLine("  1;"));
  EXPECT_EQ(proc.GetStatement(), "SELECT\n  1");
}

TEST_F(InteractiveClientFixture, SqlProcessorSingleQuotedSemicolon) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT 'hello;world'"));
  ASSERT_TRUE(proc.IsAccumulating());
  ASSERT_TRUE(proc.AccumulateLine(";"));
  EXPECT_EQ(proc.GetStatement(), "SELECT 'hello;world'");
}

TEST_F(InteractiveClientFixture, SqlProcessorDoubleQuotedSemicolon) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT \"col;name\""));
  ASSERT_TRUE(proc.AccumulateLine(";"));
  EXPECT_EQ(proc.GetStatement(), "SELECT \"col;name\"");
}

TEST_F(InteractiveClientFixture, SqlProcessorLineComment) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT 1 -- this is a comment;"));
  ASSERT_TRUE(proc.IsAccumulating());
  ASSERT_TRUE(proc.AccumulateLine(";"));
}

TEST_F(InteractiveClientFixture, SqlProcessorBlockComment) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT /* comment with ; */"));
  ASSERT_TRUE(proc.AccumulateLine("1;"));
}

TEST_F(InteractiveClientFixture, SqlProcessorMultiLineBlockComment) {
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("SELECT /*"));
  ASSERT_FALSE(proc.AccumulateLine("  comment with ;"));
  ASSERT_FALSE(proc.AccumulateLine("*/ 1"));
  ASSERT_TRUE(proc.AccumulateLine(";"));
}

TEST_F(InteractiveClientFixture, SqlProcessorDollarQuotedSemicolon) {
  // A function body in $$...$$ or $tag$...$tag$ keeps its semicolons.
  SqlProcessor proc;
  ASSERT_FALSE(proc.AccumulateLine("CREATE MACRO m() AS $$ SELECT 1; SELECT 2;"));
  ASSERT_TRUE(proc.IsAccumulating());
  ASSERT_TRUE(proc.AccumulateLine("$$;"));
  EXPECT_EQ(proc.GetStatement(), "CREATE MACRO m() AS $$ SELECT 1; SELECT 2;\n$$");

  SqlProcessor tagged;
  ASSERT_FALSE(tagged.AccumulateLine("SELECT $body$ a; $$ b; $body$"));
  ASSERT_TRUE(tagged.AccumulateLine(";"));
  EXPECT_EQ(tagged.GetStatement(), "SELECT $body$ a; $$ b; $body$");
}

TEST_F(InteractiveClientFixture, SqlProcessorDollarParameterIsNoQuote) {
  // $1 is a parameter and a$b$ part of a name, not the start of a dollar quote.
  SqlProcessor proc;
  ASSERT_TRUE(proc.AccumulateLine("SELECT $1, a$b$ FROM t;"));
  EXPECT_EQ(proc.GetStatement(), "SELECT $1, a$b$ FROM t");
}

TEST_F(InteractiveClientFixture, SqlProcessorReset) {
  SqlProcessor proc;
  proc.AccumulateLine("SELECT");
  ASSERT_TRUE(proc.IsAccumulating());
  proc.Reset();
  ASSERT_FALSE(proc.IsAccumulating());
}

TEST_F(InteractiveClientFixture, SqlProcessorIsUpdateStatement) {
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("INSERT INTO t VALUES(1)"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("  UPDATE t SET x=1"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("DELETE FROM t"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("CREATE TABLE t (x INT)"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("DROP TABLE t"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("ALTER TABLE t ADD y INT"));
  EXPECT_TRUE(SqlProcessor::IsUpdateStatement("USE my_catalog"));
  EXPECT_FALSE(SqlProcessor::IsUpdateStatement("SELECT * FROM t"));
  EXPECT_FALSE(SqlProcessor::IsUpdateStatement("  SELECT 1"));
  EXPECT_FALSE(SqlProcessor::IsUpdateStatement("WITH cte AS (SELECT 1) SELECT * FROM cte"));
}

// ============================================================================
// Command Processor Tests (using real server connection)
// ============================================================================

TEST_F(InteractiveClientFixture, DotCommandIsDotCommand) {
  EXPECT_TRUE(CommandProcessor::IsDotCommand(".quit"));
  EXPECT_TRUE(CommandProcessor::IsDotCommand("  .help"));
  EXPECT_TRUE(CommandProcessor::IsDotCommand(".tables"));
  EXPECT_FALSE(CommandProcessor::IsDotCommand("SELECT 1"));
  EXPECT_FALSE(CommandProcessor::IsDotCommand(""));
  EXPECT_FALSE(CommandProcessor::IsDotCommand("   "));
}

TEST_F(InteractiveClientFixture, DotQuit) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".quit"), CommandResult::EXIT);
  EXPECT_EQ(proc.Process(".exit"), CommandResult::EXIT);
}

TEST_F(InteractiveClientFixture, DotHelp) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".help"), CommandResult::OK);
  EXPECT_NE(out.str().find(".quit"), std::string::npos)
      << "Help should mention .quit";
  EXPECT_NE(out.str().find(".tables"), std::string::npos);
}

TEST_F(InteractiveClientFixture, DotTables) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".tables"), CommandResult::OK);
  // Output should contain table metadata
  EXPECT_FALSE(out.str().empty()) << ".tables should produce output";
}

TEST_F(InteractiveClientFixture, DotCatalogs) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".catalogs"), CommandResult::OK);
  EXPECT_FALSE(out.str().empty()) << ".catalogs should produce output";
}

TEST_F(InteractiveClientFixture, DotMode) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  proc.Process(".mode csv");
  EXPECT_EQ(config.output_mode, OutputMode::CSV);

  proc.Process(".mode json");
  EXPECT_EQ(config.output_mode, OutputMode::JSON);

  proc.Process(".mode box");
  EXPECT_EQ(config.output_mode, OutputMode::BOX);

  proc.Process(".mode markdown");
  EXPECT_EQ(config.output_mode, OutputMode::MARKDOWN);
}

TEST_F(InteractiveClientFixture, DotHeaders) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_TRUE(config.show_headers);
  proc.Process(".headers off");
  EXPECT_FALSE(config.show_headers);
  proc.Process(".headers on");
  EXPECT_TRUE(config.show_headers);
}

TEST_F(InteractiveClientFixture, DotTimer) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_FALSE(config.show_timer);
  proc.Process(".timer on");
  EXPECT_TRUE(config.show_timer);
  proc.Process(".timer off");
  EXPECT_FALSE(config.show_timer);
}

TEST_F(InteractiveClientFixture, DotNullvalue) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_EQ(config.null_value, "NULL");
  proc.Process(".nullvalue <nil>");
  EXPECT_EQ(config.null_value, "<nil>");
}

TEST_F(InteractiveClientFixture, DotSeparator) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  proc.Process(".separator , \\n");
  EXPECT_EQ(config.separator_col, ",");
}

TEST_F(InteractiveClientFixture, DotEcho) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_FALSE(config.echo);
  proc.Process(".echo on");
  EXPECT_TRUE(config.echo);
}

TEST_F(InteractiveClientFixture, DotBail) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_FALSE(config.bail_on_error);
  proc.Process(".bail on");
  EXPECT_TRUE(config.bail_on_error);
}

TEST_F(InteractiveClientFixture, DotShow) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  proc.Process(".show");
  std::string output = out.str();

  // Section headers
  EXPECT_NE(output.find("--- Server ---"), std::string::npos);
  EXPECT_NE(output.find("--- Session ---"), std::string::npos);
  EXPECT_NE(output.find("--- Settings ---"), std::string::npos);
  // Server section
  EXPECT_NE(output.find("version:"), std::string::npos)
      << "Should show server version";
  // 1.x: "edition:" (Hardcharger has no editions).
  EXPECT_NE(output.find("instance_id:"), std::string::npos)
      << "Should show instance_id";
  EXPECT_NE(output.find("engine:"), std::string::npos)
      << "Should show engine (from SqlInfo)";
  EXPECT_NE(output.find("duckdb"), std::string::npos)
      << "Engine should contain 'duckdb'";
  EXPECT_NE(output.find("arrow:"), std::string::npos)
      << "Should show Arrow version";
  // Session section
  EXPECT_NE(output.find("host:"), std::string::npos);
  EXPECT_NE(output.find("port:"), std::string::npos);
  EXPECT_NE(output.find("session_id:"), std::string::npos)
      << "Should show session_id";
  EXPECT_NE(output.find("role:"), std::string::npos)
      << "Should show role";
  EXPECT_NE(output.find("catalog:"), std::string::npos)
      << "Should show current catalog";
  EXPECT_NE(output.find("schema:"), std::string::npos)
      << "Should show current schema";
  // Settings section
  EXPECT_NE(output.find("mode:"), std::string::npos);
  EXPECT_NE(output.find("headers:"), std::string::npos);
}

TEST_F(InteractiveClientFixture, UnknownDotCommand) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".nonexistent"), CommandResult::ERROR);
}

// ============================================================================
// Client Config Tests (unit tests)
// ============================================================================

TEST_F(InteractiveClientFixture, OutputModeRoundTrip) {
  // Test all modes can be converted to string and back
  std::vector<OutputMode> modes = {
      OutputMode::TABLE, OutputMode::BOX,    OutputMode::CSV,
      OutputMode::TABS,  OutputMode::JSON,   OutputMode::JSONLINES,
      OutputMode::MARKDOWN, OutputMode::LINE, OutputMode::LIST,
      OutputMode::HTML,  OutputMode::LATEX,  OutputMode::INSERT,
      OutputMode::QUOTE, OutputMode::ASCII,  OutputMode::TRASH,
  };

  for (auto mode : modes) {
    std::string name = OutputModeToString(mode);
    ASSERT_FALSE(name.empty()) << "Mode should have a string name";

    auto parsed = OutputModeFromString(name);
    ASSERT_TRUE(parsed.has_value()) << "Should parse back: " << name;
    EXPECT_EQ(*parsed, mode) << "Round-trip failed for: " << name;
  }
}

TEST_F(InteractiveClientFixture, OutputModeFromStringCaseInsensitive) {
  EXPECT_EQ(*OutputModeFromString("CSV"), OutputMode::CSV);
  EXPECT_EQ(*OutputModeFromString("csv"), OutputMode::CSV);
  EXPECT_EQ(*OutputModeFromString("Csv"), OutputMode::CSV);
}

TEST_F(InteractiveClientFixture, OutputModeFromStringInvalid) {
  EXPECT_FALSE(OutputModeFromString("invalid").has_value());
  EXPECT_FALSE(OutputModeFromString("").has_value());
}

// ============================================================================
// End-to-end query + render integration tests
// ============================================================================

TEST_F(InteractiveClientFixture, QueryAndRenderCSV) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT id, name FROM test_data ORDER BY id LIMIT 2");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.output_mode = OutputMode::CSV;
  auto renderer = CreateRenderer(OutputMode::CSV, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);

  // Parse CSV output
  std::istringstream iss(out.str());
  std::string line;

  std::getline(iss, line);
  EXPECT_EQ(line, "id,name") << "CSV header row";

  std::getline(iss, line);
  EXPECT_EQ(line, "1,Alice") << "CSV first data row";

  std::getline(iss, line);
  EXPECT_EQ(line, "2,Bob") << "CSV second data row";
}

TEST_F(InteractiveClientFixture, QueryAndRenderJSON) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 42 AS num, 'hi' AS str");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::JSON, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string json = out.str();

  // Verify JSON structure
  EXPECT_NE(json.find("\"num\": 42"), std::string::npos)
      << "Numeric value should not be quoted";
  EXPECT_NE(json.find("\"str\": \"hi\""), std::string::npos)
      << "String value should be quoted";
}

TEST_F(InteractiveClientFixture, QuoteRendererOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 'it''s' AS val");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::QUOTE, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("'val'"), std::string::npos)
      << "Header should be SQL-quoted";
}

// ============================================================================
// Disconnected State Tests
// ============================================================================

TEST_F(InteractiveClientFixture, IsNotConnectedBeforeConnect) {
  ChargewireConnection conn;
  ASSERT_FALSE(conn.IsConnected());
}

TEST_F(InteractiveClientFixture, DisconnectResetsConnection) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  ASSERT_TRUE(conn.IsConnected());

  conn.Disconnect();
  ASSERT_FALSE(conn.IsConnected());
}

TEST_F(InteractiveClientFixture, ReconnectAfterDisconnect) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  ASSERT_TRUE(conn.IsConnected());

  conn.Disconnect();
  ASSERT_FALSE(conn.IsConnected());

  auto status = conn.Connect(MakeConfig());
  ASSERT_TRUE(status.ok()) << "Reconnect failed: " << status.ToString();
  ASSERT_TRUE(conn.IsConnected());

  // Verify it works after reconnect
  auto result = conn.ExecuteQuery("SELECT 1 AS val");
  ASSERT_TRUE(result.ok());
  ASSERT_EQ(result->table->num_rows(), 1);
}

TEST_F(InteractiveClientFixture, DotCommandsRequireConnectionTables) {
  ChargewireConnection conn;
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".tables"), CommandResult::ERROR);
}

TEST_F(InteractiveClientFixture, DotCommandsRequireConnectionSchema) {
  ChargewireConnection conn;
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".schema"), CommandResult::ERROR);
}

TEST_F(InteractiveClientFixture, DotCommandsRequireConnectionCatalogs) {
  ChargewireConnection conn;
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  EXPECT_EQ(proc.Process(".catalogs"), CommandResult::ERROR);
}

TEST_F(InteractiveClientFixture, DotConnectEstablishesConnection) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  ChargewireConnection conn;
  ASSERT_FALSE(conn.IsConnected());

  auto config = MakeConfig();
  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  std::string connect_cmd = ".connect localhost " +
                            std::to_string(GetPort()) + " " +
                            GetUsername() + " " + GetPassword();
  auto result = proc.Process(connect_cmd);
  EXPECT_EQ(result, CommandResult::OK);
  EXPECT_TRUE(conn.IsConnected());

  // Verify queries work after .connect
  auto query_result = conn.ExecuteQuery("SELECT 42 AS answer");
  ASSERT_TRUE(query_result.ok());
  ASSERT_EQ(query_result->table->num_rows(), 1);
}

TEST_F(InteractiveClientFixture, DotConnectShowsConnected) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  ChargewireConnection conn;
  auto config = MakeConfig();
  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  // .show before connecting
  proc.Process(".show");
  std::string output = out.str();
  EXPECT_NE(output.find("connected: no"), std::string::npos);

  // Connect
  out.str("");
  std::string connect_cmd = ".connect localhost " +
                            std::to_string(GetPort()) + " " +
                            GetUsername() + " " + GetPassword();
  proc.Process(connect_cmd);

  // .show after connecting
  out.str("");
  proc.Process(".show");
  output = out.str();
  EXPECT_NE(output.find("connected: yes"), std::string::npos);
}

TEST_F(InteractiveClientFixture, DotConnectInvalidArgs) {
  ChargewireConnection conn;
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  // Too few arguments
  EXPECT_EQ(proc.Process(".connect"), CommandResult::ERROR);
  EXPECT_EQ(proc.Process(".connect localhost"), CommandResult::ERROR);
  EXPECT_EQ(proc.Process(".connect localhost 31337"), CommandResult::ERROR);

  // Invalid port
  EXPECT_EQ(proc.Process(".connect localhost abc user"), CommandResult::ERROR);
}

TEST_F(InteractiveClientFixture, DotConnectViaURI) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  ChargewireConnection conn;
  ASSERT_FALSE(conn.IsConnected());

  auto config = MakeConfig();
  // Override password so we don't get a prompt
  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  // Use positional format with password (since we can't prompt in test)
  std::string connect_cmd = ".connect localhost " +
                            std::to_string(GetPort()) + " " +
                            GetUsername() + " " + GetPassword();
  proc.Process(connect_cmd);
  ASSERT_TRUE(conn.IsConnected());

  // Disconnect and reconnect via URI (positional args include password)
  conn.Disconnect();
  ASSERT_FALSE(conn.IsConnected());

  // Re-connect with positional (since URI would prompt for password)
  connect_cmd = ".connect localhost " +
                std::to_string(GetPort()) + " " +
                GetUsername() + " " + GetPassword();
  auto result = proc.Process(connect_cmd);
  EXPECT_EQ(result, CommandResult::OK);
  EXPECT_TRUE(conn.IsConnected());
}

// ============================================================================
// Non-Interactive Exit Code Tests (regression: query failures must return != 0)
// ============================================================================

TEST_F(InteractiveClientFixture, NonInteractiveReturnsNonZeroOnInvalidSQL) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  config.command = "SELECT * FROM nonexistent_table_xyz";

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  int exit_code = RunNonInteractive(conn, cmd_proc, sql_proc, config);
  EXPECT_EQ(exit_code, 1) << "Invalid SQL in -c mode should return exit code 1";
}

TEST_F(InteractiveClientFixture, NonInteractiveReturnsZeroOnValidSQL) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  config.command = "SELECT 42 AS answer";

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  int exit_code = RunNonInteractive(conn, cmd_proc, sql_proc, config);
  EXPECT_EQ(exit_code, 0) << "Valid SQL should return exit code 0";
}

TEST_F(InteractiveClientFixture, NonInteractiveReturnsNonZeroWhenDisconnected) {
  ChargewireConnection conn;
  ASSERT_FALSE(conn.IsConnected());

  auto config = MakeConfig();
  std::ostringstream out;
  config.output_stream = &out;
  config.command = "SELECT 1";

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  int exit_code = RunNonInteractive(conn, cmd_proc, sql_proc, config);
  EXPECT_EQ(exit_code, 1)
      << "SQL on disconnected client should return exit code 1";
}

TEST_F(InteractiveClientFixture, NonInteractivePartialFailureReturnsNonZero) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  // First statement succeeds, second fails
  config.command = "SELECT 1; SELECT * FROM no_such_table_abc;";

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  int exit_code = RunNonInteractive(conn, cmd_proc, sql_proc, config);
  EXPECT_EQ(exit_code, 1)
      << "Partial failure in -c mode should return exit code 1";
}

TEST_F(InteractiveClientFixture, NonInteractiveBailStopsOnFirstError) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  config.bail_on_error = true;
  // First statement fails — second should NOT execute
  config.command =
      "SELECT * FROM no_such_table_abc; "
      "CREATE TABLE bail_test_marker (x INT);";

  SqlProcessor sql_proc;
  CommandProcessor cmd_proc(conn, config);

  int exit_code = RunNonInteractive(conn, cmd_proc, sql_proc, config);
  EXPECT_EQ(exit_code, 1);

  // Verify the second statement did NOT execute
  auto result = conn.ExecuteQuery(
      "SELECT COUNT(*) AS cnt FROM information_schema.tables "
      "WHERE table_name = 'bail_test_marker'");
  ASSERT_TRUE(result.ok());
  std::string count_val = GetCellValue(result->table->column(0), 0, "NULL");
  EXPECT_EQ(count_val, "0")
      << "Second statement should not have executed due to --bail";
}

// ============================================================================
// URI Parsing Tests (unit tests)
// ============================================================================

TEST_F(InteractiveClientFixture, ParseConnectionURIBasic) {
  ClientConfig config;
  ASSERT_TRUE(ParseConnectionURI("hardcharger://myhost:12345", config));
  EXPECT_EQ(config.host, "myhost");
  EXPECT_EQ(config.port, 12345);
  EXPECT_TRUE(config.use_tls);  // re-expressed: HTTPS unless ?tls=false (1.x: plaintext)
}

// Re-expressed: 1.x URIs carried username, useEncryption and authType; Hardcharger's take
// only tls, as the ADBC driver's do, and refuse the rest by name.
TEST_F(InteractiveClientFixture, ParseConnectionURIWithParams) {
  ClientConfig config;
  EXPECT_FALSE(ParseConnectionURI(
      "hardcharger://server.example.com:31337?username=admin&useEncryption=true&authType=external", config));
  ASSERT_TRUE(ParseConnectionURI("hardcharger://server.example.com:31337?tls=false", config));
  EXPECT_EQ(config.host, "server.example.com");
  EXPECT_EQ(config.port, 31337);
  EXPECT_FALSE(config.use_tls);
}

TEST_F(InteractiveClientFixture, ParseConnectionURINoPort) {
  ClientConfig config;
  config.port = 1;
  ASSERT_TRUE(ParseConnectionURI("hardcharger://myhost", config));
  EXPECT_EQ(config.host, "myhost");
  EXPECT_EQ(config.port, 31337);  // re-expressed: the scheme's default port, not the previous one
}

// Re-expressed: the TLS roots and verification are --tls-roots and --tls-skip-verify.
TEST_F(InteractiveClientFixture, ParseConnectionURITLSParams) {
  ClientConfig config;
  EXPECT_FALSE(ParseConnectionURI(
      "hardcharger://host:443?useEncryption=true&disableCertificateVerification=true&tlsRoots=/path/to/ca.pem",
      config));
  ASSERT_TRUE(ParseConnectionURI("hardcharger://host:443?tls=true", config));
  EXPECT_TRUE(config.use_tls);
  EXPECT_EQ(config.port, 443);
}

TEST_F(InteractiveClientFixture, BuildConnectionURIBasic) {
  ClientConfig config;
  config.host = "localhost";
  config.port = 31337;
  config.username = "admin";

  // Re-expressed: no credentials in the URI; TLS is the default, and plain HTTP says ?tls=false.
  EXPECT_EQ(BuildConnectionURI(config), "hardcharger://localhost:31337");
  config.use_tls = false;
  EXPECT_EQ(BuildConnectionURI(config), "hardcharger://localhost:31337?tls=false");
}

TEST_F(InteractiveClientFixture, BuildConnectionURIWithTLS) {
  ClientConfig config;
  config.host = "server.example.com";
  config.port = 443;
  config.username = "admin";
  config.use_tls = true;
  config.auth_type_external = true;

  std::string uri = BuildConnectionURI(config);
  EXPECT_EQ(uri, "hardcharger://server.example.com:443");
  ClientConfig back;
  ASSERT_TRUE(ParseConnectionURI(uri, back));
  EXPECT_TRUE(back.use_tls);
  EXPECT_EQ(back.port, 443);
}

// ============================================================================
// Result Rendering: Row/Column Truncation, Types, Alignment
// ============================================================================

TEST_F(InteractiveClientFixture, MaxRowsTruncatesOutput) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  // Generate a table with 100 rows
  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 100) t(x)");
  ASSERT_TRUE(result.ok()) << result.status().ToString();
  ASSERT_EQ(result->table->num_rows(), 100);

  ClientConfig config;
  config.max_rows = 10;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);

  EXPECT_EQ(render_result.rows_rendered, 10);
  EXPECT_EQ(render_result.columns_rendered, 1);

  // Count data rows in the output (lines containing │ digit │)
  std::istringstream iss(out.str());
  std::string line;
  int data_rows = 0;
  while (std::getline(iss, line)) {
    // Data rows start with │ and contain a number
    if (line.find("\xe2\x94\x82") == 0 &&
        line.find("x") == std::string::npos &&
        line.find("\xe2\x94\x80") == std::string::npos) {
      // Not header, not border — it's a data row
      ++data_rows;
    }
  }
  // Should have exactly 10 data rows (+ 1 type row = 11 rows with │ prefix)
  // But let's just verify the render_result directly (more reliable)
  EXPECT_EQ(render_result.rows_rendered, 10);
}

TEST_F(InteractiveClientFixture, MaxRowsZeroShowsAllRows) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 50) t(x)");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_rows = 0;  // unlimited
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);

  EXPECT_EQ(render_result.rows_rendered, 50);
  EXPECT_EQ(render_result.columns_rendered, 1);
}

TEST_F(InteractiveClientFixture, MaxWidthTruncatesColumnValues) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT 'abcdefghijklmnopqrstuvwxyz' AS long_value");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_width = 20;  // Very narrow
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should contain ellipsis character (U+2026, UTF-8: E2 80 A6)
  EXPECT_NE(output.find("\xe2\x80\xa6"), std::string::npos)
      << "Should contain ellipsis for truncated values";
  EXPECT_EQ(render_result.columns_rendered, 1);
}

TEST_F(InteractiveClientFixture, MaxWidthOmitsColumnsWhenTooNarrow) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT 1 AS col1, 2 AS col2, 3 AS col3, 4 AS col4, "
      "5 AS col5, 6 AS col6, 7 AS col7, 8 AS col8");
  ASSERT_TRUE(result.ok());
  ASSERT_EQ(result->table->num_columns(), 8);

  ClientConfig config;
  config.max_width = 30;  // Very narrow for 8 columns
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);

  EXPECT_LT(render_result.columns_rendered, 8)
      << "Should omit some columns when terminal is too narrow";
  EXPECT_GT(render_result.columns_rendered, 0);
}

TEST_F(InteractiveClientFixture, DotMaxrowsSetsConfig) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  EXPECT_EQ(config.max_rows, 0);  // default
  proc.Process(".maxrows 100");
  EXPECT_EQ(config.max_rows, 100);
  proc.Process(".maxrows 0");
  EXPECT_EQ(config.max_rows, 0);
}

TEST_F(InteractiveClientFixture, DotMaxrowsShowsValue) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  config.max_rows = 42;

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  proc.Process(".maxrows");
  EXPECT_NE(out.str().find("42"), std::string::npos);
}

TEST_F(InteractiveClientFixture, DotMaxwidthSetsConfig) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  CommandProcessor proc(conn, config);

  proc.Process(".maxwidth 200");
  EXPECT_EQ(config.max_width, 200);

  // .maxwidth 0 resets to terminal width (which is > 0)
  proc.Process(".maxwidth 0");
  EXPECT_GT(config.max_width, 0);
}

TEST_F(InteractiveClientFixture, NonInteractiveModeNoTruncation) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 100) t(x)");
  ASSERT_TRUE(result.ok());

  // Simulate non-interactive: max_rows=0, max_width=0
  ClientConfig config;
  config.max_rows = 0;
  config.max_width = 0;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);

  EXPECT_EQ(render_result.rows_rendered, 100)
      << "Non-interactive should show all rows";
  EXPECT_EQ(render_result.columns_rendered, 1);
}

TEST_F(InteractiveClientFixture, BoxRendererShowsColumnTypes) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 42 AS num, 'hello' AS str");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should show friendly type names below column names
  EXPECT_NE(output.find("int"), std::string::npos)
      << "Should show integer type";
  EXPECT_NE(output.find("varchar"), std::string::npos)
      << "Should show varchar type";
}

TEST_F(InteractiveClientFixture, BoxRendererRightAlignsNumbers) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT 1 AS x, 1000 AS y");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // For right-aligned numbers, padding should come before the value.
  // The value "1" in column "x" (width >= 1) should have leading spaces.
  // Look for pattern: spaces followed by "1" followed by space and border
  // In the "y" column with value "1000", since header "y" is only 1 char,
  // the width is driven by "1000" (4 chars). So "y" header has right-padding.
  // The value "1000" should be right-aligned (no padding needed since it fills width).
  // Let's just verify the output is valid
  EXPECT_NE(output.find("1000"), std::string::npos);
  EXPECT_NE(output.find("x"), std::string::npos);
  EXPECT_NE(output.find("y"), std::string::npos);
}

TEST_F(InteractiveClientFixture, TableRendererShowsColumnTypes) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 42 AS num, 'hello' AS str");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::TABLE, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  EXPECT_NE(output.find("int"), std::string::npos)
      << "Should show integer type";
  EXPECT_NE(output.find("varchar"), std::string::npos)
      << "Should show varchar type";
}

TEST_F(InteractiveClientFixture, ShowSettingsIncludesMaxRowsMaxWidth) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();
  config.max_rows = 40;
  config.max_width = 120;

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  proc.Process(".show");
  std::string output = out.str();

  EXPECT_NE(output.find("maxrows:"), std::string::npos);
  EXPECT_NE(output.find("40"), std::string::npos);
  EXPECT_NE(output.find("maxwidth:"), std::string::npos);
  EXPECT_NE(output.find("120"), std::string::npos);
}

TEST_F(InteractiveClientFixture, FooterShowsTruncationInfo) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 20) t(x)");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_rows = 5;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // In-table footer should show truncation info inside the box
  EXPECT_TRUE(render_result.footer_rendered)
      << "Box renderer should set footer_rendered";
  EXPECT_NE(output.find("5 shown"), std::string::npos)
      << "In-table footer should show truncation info";
  EXPECT_NE(output.find("20 rows"), std::string::npos)
      << "In-table footer should show total rows";
}

TEST_F(InteractiveClientFixture, BoxRendererSplitDisplayShowsDotRows) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 100) t(x)");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_rows = 10;  // >= 4 triggers split display
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should have middle dot character (U+00B7, UTF-8: C2 B7)
  EXPECT_NE(output.find("\xc2\xb7"), std::string::npos)
      << "Split display should show dot rows";

  // Should show first few rows and last few rows
  EXPECT_NE(output.find("1"), std::string::npos) << "Should show first row";
  EXPECT_NE(output.find("100"), std::string::npos) << "Should show last row";

  EXPECT_EQ(render_result.rows_rendered, 10);
  EXPECT_TRUE(render_result.footer_rendered);
}

TEST_F(InteractiveClientFixture, BoxRendererInTableFooter) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS a, 'hello' AS b");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // In-table footer should appear for all box results (even non-truncated)
  EXPECT_TRUE(render_result.footer_rendered);
  EXPECT_NE(output.find("1 row"), std::string::npos)
      << "In-table footer should show row count";
  EXPECT_NE(output.find("2 columns"), std::string::npos)
      << "In-table footer should show column count";

  // Merged bottom border: └───────┘ (no internal dividers)
  // Find the last line containing └
  auto last_bl = output.rfind("\u2514");
  ASSERT_NE(last_bl, std::string::npos);
  // The merged bottom should NOT contain ┴ (bottom-tee)
  auto after_bl = output.substr(last_bl);
  EXPECT_EQ(after_bl.find("\u2534"), std::string::npos)
      << "Merged bottom border should not have internal dividers";
}

TEST_F(InteractiveClientFixture, CsvRendererNoFooterRendered) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery("SELECT 1 AS x");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::CSV, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);

  EXPECT_FALSE(render_result.footer_rendered)
      << "CSV renderer should not set footer_rendered";
}

TEST_F(InteractiveClientFixture, BoxRendererFooterWrapsOnNarrowTable) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  // Single narrow column — footer text must wrap to fit
  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 100) t(x)");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_rows = 10;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // Footer should show row count and "(N shown)" on separate lines
  EXPECT_TRUE(render_result.footer_rendered);
  // because column "x" is narrow (1 char + type "int64" = 5 chars)
  EXPECT_NE(output.find("100 rows"), std::string::npos)
      << "Should show total row count in footer";
  EXPECT_NE(output.find("(10 shown)"), std::string::npos)
      << "Should show truncation indicator in footer";

  // Single column — should NOT show "1 column" (DuckDB-style)
  EXPECT_EQ(output.find("1 column"), std::string::npos)
      << "Should not show '1 column' for single-column results";
}

TEST_F(InteractiveClientFixture, TableRendererSplitDisplay) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result =
      conn.ExecuteQuery("SELECT x FROM generate_series(1, 50) t(x)");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  config.max_rows = 8;
  auto renderer = CreateRenderer(OutputMode::TABLE, config);

  std::ostringstream out;
  auto render_result = renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should have middle dot character for split
  EXPECT_NE(output.find("\xc2\xb7"), std::string::npos)
      << "Split display should show dot rows in table mode";

  // Should show bottom rows (50 should appear)
  EXPECT_NE(output.find("50"), std::string::npos)
      << "Should show last row in split display";

  EXPECT_EQ(render_result.rows_rendered, 8);
  EXPECT_TRUE(render_result.footer_rendered);
  EXPECT_NE(output.find("8 shown"), std::string::npos);
}

// ============================================================================
// Tab Completion Tests
// ============================================================================

TEST_F(InteractiveClientFixture, CompleterReturnsTableNames) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;
  auto completions = completer.Complete("select * from test_d", ctx_len);

  bool found = false;
  for (const auto& c : completions) {
    if (c.text() == "test_data") found = true;
  }
  EXPECT_TRUE(found) << "Should complete table name 'test_data'";
  EXPECT_GT(ctx_len, 0);
}

TEST_F(InteractiveClientFixture, CompleterReturnsSqlKeywords) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;
  auto completions = completer.Complete("sel", ctx_len);

  bool found = false;
  for (const auto& c : completions) {
    if (c.text() == "select") found = true;
  }
  EXPECT_TRUE(found) << "Should complete SQL keyword 'select'";
}

TEST_F(InteractiveClientFixture, CompleterReturnsDotCommands) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;
  auto completions = completer.Complete(".ta", ctx_len);

  bool found = false;
  for (const auto& c : completions) {
    if (c.text() == ".tables") found = true;
  }
  EXPECT_TRUE(found) << "Should complete dot command '.tables'";
}

TEST_F(InteractiveClientFixture, CompleterCasePreservation) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;

  // Uppercase prefix -> uppercase completions
  auto upper = completer.Complete("SEL", ctx_len);
  bool found_upper = false;
  for (const auto& c : upper) {
    if (c.text() == "SELECT") found_upper = true;
  }
  EXPECT_TRUE(found_upper) << "Uppercase prefix should get uppercase completion";

  // Lowercase prefix -> lowercase completions
  auto lower = completer.Complete("sel", ctx_len);
  bool found_lower = false;
  for (const auto& c : lower) {
    if (c.text() == "select") found_lower = true;
  }
  EXPECT_TRUE(found_lower) << "Lowercase prefix should get lowercase completion";
}

TEST_F(InteractiveClientFixture, CompleterHintSingleMatch) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;
  replxx::Replxx::Color color = replxx::Replxx::Color::DEFAULT;

  // ".refr" should uniquely match ".refresh"
  auto hints = completer.Hint(".refr", ctx_len, color);
  ASSERT_EQ(hints.size(), 1);
  EXPECT_EQ(hints[0], ".refresh");
  EXPECT_EQ(color, replxx::Replxx::Color::GRAY);
}

TEST_F(InteractiveClientFixture, CompleterHintMultipleNoHint) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);
  int ctx_len = 0;
  replxx::Replxx::Color color = replxx::Replxx::Color::DEFAULT;

  // ".s" matches .schema, .separator, .shell, .show — no single hint
  auto hints = completer.Hint(".s", ctx_len, color);
  EXPECT_TRUE(hints.empty()) << "Multiple matches should produce no hint";
}

TEST_F(InteractiveClientFixture, CompleterCacheInvalidation) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  ShellCompleter completer(conn);

  // Populate cache
  int ctx_len = 0;
  const auto cached = completer.Complete("select * from test_", ctx_len);
  ASSERT_FALSE(cached.empty()) << "Populate the metadata cache before creating a table";

  // Create a new table
  auto create_result = conn.ExecuteUpdate(
      "CREATE TABLE completer_test_xyz (a INTEGER)");
  ASSERT_TRUE(create_result.ok());

  // Before invalidation: cache still has old data
  auto before = completer.Complete("select * from completer_test_", ctx_len);
  bool found_before = false;
  for (const auto& c : before) {
    if (c.text() == "completer_test_xyz") found_before = true;
  }
  EXPECT_FALSE(found_before) << "The existing cache should not include the new table";

  // Invalidate and retry
  completer.InvalidateCache();
  auto after = completer.Complete("select * from completer_test_", ctx_len);
  bool found_after = false;
  for (const auto& c : after) {
    if (c.text() == "completer_test_xyz") found_after = true;
  }
  EXPECT_TRUE(found_after) << "After invalidation, new table should appear";

  // Cleanup
  EXPECT_TRUE(conn.ExecuteUpdate("DROP TABLE completer_test_xyz").ok());
}

TEST_F(InteractiveClientFixture, CompleterDisconnectedGraceful) {
  ChargewireConnection conn;
  ASSERT_FALSE(conn.IsConnected());

  ShellCompleter completer(conn);
  int ctx_len = 0;

  // Should not crash; returns keywords only
  auto completions = completer.Complete("sel", ctx_len);
  bool found = false;
  for (const auto& c : completions) {
    if (c.text() == "select") found = true;
  }
  EXPECT_TRUE(found) << "Should return keywords even when disconnected";

  // Table names should be empty (no connection)
  auto table_completions = completer.Complete("select * from te", ctx_len);
  // Should only have keywords, not table names
  for (const auto& c : table_completions) {
    EXPECT_NE(c.text(), "test_data")
        << "Should not have table names when disconnected";
  }
}

// ============================================================================
// Display Polish Tests (Centered Headers, Friendly Type Names)
// ============================================================================

TEST_F(InteractiveClientFixture, BoxRendererCentersColumnNames) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT 1 AS a, 'hello_world' AS long_name");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Parse the header line for 'a' column — should be centered
  // In the output, find the line containing column names
  std::istringstream iss(output);
  std::string line;
  while (std::getline(iss, line)) {
    // Look for the line containing " a " (the column header for 'a')
    // If 'a' is centered in a wider column, it should have spaces on both sides
    if (line.find("long_name") != std::string::npos) {
      // This is the header line — "long_name" should be centered
      // Find the position of "long_name" — it should have spaces on both sides
      auto pos = line.find("long_name");
      ASSERT_NE(pos, std::string::npos);
      // Check that there is at least one space before "long_name" (centering)
      EXPECT_EQ(line[pos - 1], ' ') << "Column name should have padding";
      break;
    }
  }
}

TEST_F(InteractiveClientFixture, BoxRendererFriendlyTypeNames) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();

  auto result = conn.ExecuteQuery(
      "SELECT 42::BIGINT AS num, 'hello' AS str, "
      "3.14::DOUBLE AS dbl, DATE '2026-01-01' AS dt");
  ASSERT_TRUE(result.ok());

  ClientConfig config;
  auto renderer = CreateRenderer(OutputMode::BOX, config);

  std::ostringstream out;
  renderer->Render(*result->table, out);
  std::string output = out.str();

  // Should show friendly type names
  EXPECT_NE(output.find("bigint"), std::string::npos)
      << "Should show 'bigint' not 'int64'";
  EXPECT_NE(output.find("varchar"), std::string::npos)
      << "Should show 'varchar' not 'string'";
  EXPECT_NE(output.find("double"), std::string::npos)
      << "Should show 'double'";
  EXPECT_NE(output.find("date"), std::string::npos)
      << "Should show 'date' not 'date32[day]'";

  // Should NOT show raw Arrow type names
  EXPECT_EQ(output.find("int64"), std::string::npos)
      << "Should not show raw Arrow type 'int64'";
  EXPECT_EQ(output.find("string"), std::string::npos)
      << "Should not show raw Arrow type 'string'";
  EXPECT_EQ(output.find("date32"), std::string::npos)
      << "Should not show raw Arrow type 'date32[day]'";
}

TEST_F(InteractiveClientFixture, FriendlyTypeNameMappings) {
  // Unit tests for the FriendlyTypeName function
  EXPECT_EQ(FriendlyTypeName(arrow::utf8()), "varchar");
  EXPECT_EQ(FriendlyTypeName(arrow::int8()), "tinyint");
  EXPECT_EQ(FriendlyTypeName(arrow::int16()), "smallint");
  EXPECT_EQ(FriendlyTypeName(arrow::int32()), "integer");
  EXPECT_EQ(FriendlyTypeName(arrow::int64()), "bigint");
  EXPECT_EQ(FriendlyTypeName(arrow::uint8()), "utinyint");
  EXPECT_EQ(FriendlyTypeName(arrow::uint16()), "usmallint");
  EXPECT_EQ(FriendlyTypeName(arrow::uint32()), "uinteger");
  EXPECT_EQ(FriendlyTypeName(arrow::uint64()), "ubigint");
  EXPECT_EQ(FriendlyTypeName(arrow::float32()), "float");
  EXPECT_EQ(FriendlyTypeName(arrow::float64()), "double");
  EXPECT_EQ(FriendlyTypeName(arrow::boolean()), "boolean");
  EXPECT_EQ(FriendlyTypeName(arrow::date32()), "date");
  EXPECT_EQ(FriendlyTypeName(arrow::binary()), "blob");
  EXPECT_EQ(FriendlyTypeName(arrow::null()), "null");
  EXPECT_EQ(FriendlyTypeName(arrow::decimal128(15, 2)), "decimal(15,2)");
  EXPECT_EQ(FriendlyTypeName(arrow::timestamp(arrow::TimeUnit::MICRO)),
            "timestamp");
  EXPECT_EQ(
      FriendlyTypeName(arrow::timestamp(arrow::TimeUnit::MICRO, "UTC")),
      "timestamptz");
  EXPECT_EQ(FriendlyTypeName(arrow::duration(arrow::TimeUnit::MICRO)),
            "interval");
}

// ============================================================================
// SqlProcessor::IsDdlStatement Tests
// ============================================================================

TEST_F(InteractiveClientFixture, SqlProcessorIsDdlStatement) {
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("CREATE TABLE t (x INT)"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("  DROP TABLE t"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("ALTER TABLE t ADD y INT"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("ATTACH ':memory:'"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("DETACH db"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("USE my_catalog"));
  EXPECT_TRUE(SqlProcessor::IsDdlStatement("  use main"));
  EXPECT_FALSE(SqlProcessor::IsDdlStatement("SELECT * FROM t"));
  EXPECT_FALSE(SqlProcessor::IsDdlStatement("INSERT INTO t VALUES(1)"));
  EXPECT_FALSE(SqlProcessor::IsDdlStatement("UPDATE t SET x=1"));
  EXPECT_FALSE(SqlProcessor::IsDdlStatement("DELETE FROM t"));
}

// ============================================================================
// .refresh Dot Command Test
// ============================================================================

TEST_F(InteractiveClientFixture, DotRefreshCommand) {
  ASSERT_TRUE(IsServerReady()) << "Server not ready";
  auto conn = ConnectClient();
  auto config = MakeConfig();

  std::ostringstream out;
  config.output_stream = &out;
  CommandProcessor proc(conn, config);

  bool callback_called = false;
  proc.SetRefreshCallback([&callback_called]() { callback_called = true; });

  auto result = proc.Process(".refresh");
  EXPECT_EQ(result, CommandResult::OK);
  EXPECT_TRUE(callback_called) << "Refresh callback should be called";
  EXPECT_NE(out.str().find("Schema cache refreshed"), std::string::npos);
}
