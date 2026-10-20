// hardcharger-embedded-smoke: the embedded server (hardcharger_embedded.h) as an app uses
// it. Starts a server on a database file in a directory of its own, signs in, runs
// SELECT 42, loads a CSV into a table, stops it, starts it again on the same file and
// finds the table, and checks the refusals: a second server while one runs, bad options.
// Exits 0 when everything held. ios/scripts/build-server-ios.sh runs it on the Mac and in
// the iOS simulator (`xcrun simctl spawn`):
//
//   hardcharger-embedded-smoke <scratch directory>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <arrow/array.h>
#include <arrow/scalar.h>

#include "hardcharger_embedded.h"
#include "warpsql/client.hpp"

namespace {

int failures = 0;

void Check(bool ok, const std::string& what) {
  std::cout << (ok ? "ok    " : "FAIL  ") << what << std::endl;
  if (!ok) failures++;
}

hardcharger_server* Start(const std::vector<std::string>& args, std::string* error = nullptr) {
  std::vector<const char*> argv;
  for (const auto& a : args) argv.push_back(a.c_str());
  hardcharger_server* server = nullptr;
  char message[512] = {};
  const int rc = hardcharger_server_start(static_cast<int>(argv.size()), argv.data(), &server, message, sizeof(message));
  if (error) *error = message;
  if (rc != 0) std::cout << "      start: " << rc << " " << message << std::endl;
  return server;
}

int64_t Answer(const std::shared_ptr<arrow::Table>& table) {
  auto column = table->column(0)->chunk(0);
  auto scalar = column->GetScalar(0).ValueOrDie();
  return std::stoll(scalar->ToString());
}

std::string Run(hardcharger_console* console, const std::string& input) {
  char* text = hardcharger_console_run(console, input.c_str(), nullptr);
  std::string out = text ? text : "";
  hardcharger_console_free_text(text);
  std::cout << "      > " << input << "\n" << out;
  return out;
}

bool Complete(const std::string& input, int expected_statements = -1) {
  int statements = 0;
  const bool complete = hardcharger_console_input_complete(input.c_str(), &statements) == 1;
  return complete && (expected_statements < 0 || statements == expected_statements);
}

// The console (the `hardcharger` client in the app): when input is ready, and what it prints.
void ConsoleChecks(int port, const std::string& password) {
  Check(!Complete("SELECT\n  42\n  AS answer"), "console: a 3-line SELECT without ';' waits");
  Check(Complete("SELECT\n  42\n  AS answer;", 1), "console: ... and is ready at the ';'");
  Check(!Complete("SELECT 'a;b'"), "console: a ';' in a string ends nothing");
  Check(!Complete("SELECT \"a;b\""), "console: nor in a quoted name");
  Check(!Complete("SELECT 1 -- ;"), "console: nor in a line comment");
  Check(!Complete("SELECT /* ; */ 1"), "console: nor in a block comment");
  Check(!Complete("CREATE MACRO m() AS $$ SELECT 1; $$"), "console: nor in a dollar quote");
  Check(Complete("SELECT 1; SELECT 2;", 2), "console: two statements in one input");
  Check(Complete(".tables", 0), "console: a dot command needs no ';'");
  Check(!Complete("   "), "console: whitespace is nothing to run");

  hardcharger_console_options options{};
  options.host = "127.0.0.1";
  options.port = port;
  options.username = "hardcharger";
  options.password = password.c_str();
  hardcharger_console* console = nullptr;
  char error[512] = {};
  Check(hardcharger_console_open(&options, &console, error, sizeof(error)) == 0, std::string("console: opens ") + error);
  if (!console) return;
  char* prompt = hardcharger_console_prompt(console, 0);
  Check(std::string(prompt) == "smoke.main> ", std::string("console: the prompt names catalog and schema (") + prompt + ")");
  hardcharger_console_free_text(prompt);
  const std::string answer = Run(console, "SELECT 42 AS answer;");
  Check(answer.find("answer") != std::string::npos && answer.find("42") != std::string::npos &&
            answer.find("1 row") != std::string::npos,
        "console: SELECT 42 AS answer renders a table and its footer");
  Check(Run(console, ".tables").find("people") != std::string::npos, "console: .tables lists people");
  const std::string two = Run(console, "SELECT 1 AS one;\nSELECT 'x' AS two;");
  Check(two.find("one") != std::string::npos && two.find("two") != std::string::npos, "console: two statements both run");
  const std::string failing = Run(console, "SELECT 1 AS first;\nSELECT * FROM nowhere;\nSELECT 3 AS third;");
  Check(failing.find("Error") != std::string::npos && failing.find("third") == std::string::npos &&
            failing.find("SELECT * FROM nowhere;") != std::string::npos,
        "console: stops at the failing statement, which the echo names");
  Check(Run(console, ".mode csv\nSELECT 42 AS answer;").find("answer\n42") != std::string::npos, "console: .mode csv");
  hardcharger_console_close(console);
}

}  // namespace

int main(int argc, char** argv) {
  const std::filesystem::path dir =
      argc > 1 ? std::filesystem::path(argv[1]) : std::filesystem::temp_directory_path() / "hardcharger-smoke";
  std::filesystem::remove_all(dir);
  std::filesystem::create_directories(dir);
  const std::string database = (dir / "smoke.db").string();
  const std::string csv = (dir / "people.csv").string();
  std::ofstream(csv) << "id,name\n1,Ada\n2,Grace\n3,Linus\n";
  const std::string password = "smoke-test-only";  // a throwaway server on 127.0.0.1

  std::cout << "Hardcharger " << hardcharger_embedded_version() << ", DuckDB " << hardcharger_embedded_duckdb_version()
            << ", Warp SQL " << hardcharger_embedded_warp_sql_version() << ", web UI '"
            << hardcharger_embedded_web_ui_version() << "'" << std::endl;
  const std::vector<std::string> run = {"hardcharger-server",
                                        "--host=127.0.0.1",
                                        "--port=0",
                                        "--database=" + database,
                                        "--admin-password=" + password,
                                        "--log-file=" + (dir / "server.log").string(),
                                        "--graceful-shutdown=false"};

  for (int round = 1; round <= 2; round++) {
    const auto started = std::chrono::steady_clock::now();
    hardcharger_server* server = Start(run);
    Check(server != nullptr, "round " + std::to_string(round) + ": the server starts");
    if (!server) break;
    const int port = hardcharger_server_port(server);
    Check(port > 0, "it reports the port it bound (" + std::to_string(port) + ")");
    std::cout << "      ready in "
              << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count()
              << " ms" << std::endl;
    Check(hardcharger_server_is_running(server) == 1, "it is running");

    std::string second_error;
    Check(Start(run, &second_error) == nullptr && second_error.find("already running") != std::string::npos,
          "a second server is refused while one runs");

    try {
      warpsql::client::Client client({.host = "127.0.0.1", .port = static_cast<uint16_t>(port)});
      client.SignInWithPassword("hardcharger", password);
      auto session = client.CreateSession();
      Check(Answer(session->Execute("SELECT 42 AS answer").reader->ToTable()) == 42, "SELECT 42 answers 42");
      // The extensions built into DuckDB (server/scripts/duckdb-extensions.cmake and DuckDB's
      // own four) are registered: an app links them from an archive (embedding.cc).
      const auto linked = Answer(session->Execute(
          "SELECT count(*) FROM duckdb_extensions() WHERE install_mode = 'STATICALLY_LINKED' AND extension_name IN "
          "('parquet', 'json', 'icu', 'core_functions', 'autocomplete', 'tpch', 'httpfs', 'ducklake', "
          "'postgres_scanner')").reader->ToTable());
      Check(linked == 9, "the nine built-in extensions are there (" + std::to_string(linked) + ")");
      if (round == 1) {
        auto created = session->Execute("CREATE TABLE people AS SELECT * FROM read_csv('" + csv + "')");
        if (created.reader) created.reader->ToTable();  // a statement without rows has no reader
      }
      Check(Answer(session->Execute("SELECT count(*) FROM people").reader->ToTable()) == 3,
            round == 1 ? "a CSV loads into a table" : "the table is still there after a restart");
      session->Close();
    } catch (const std::exception& e) {
      Check(false, std::string("queries: ") + e.what());
    }

    if (round == 1) ConsoleChecks(port, password);

    hardcharger_server_request_stop(server, 0);
    char error[256] = {};
    const int status = hardcharger_server_wait(server, error, sizeof(error));
    hardcharger_server_free(server);
    Check(status == 0, "it stops cleanly (status " + std::to_string(status) + (error[0] ? std::string(", ") + error : "") + ")");
  }

  std::string bad;
  Check(Start({"hardcharger-server", "--port", "not-a-port"}, &bad) == nullptr && !bad.empty(),
        "bad options are refused, with the reason (" + bad + ")");

  std::cout << (failures == 0 ? "PASS" : "FAILED") << std::endl;
  return failures == 0 ? 0 : 1;
}
