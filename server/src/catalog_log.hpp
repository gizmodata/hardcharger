// Catalog logging: every log line the server writes also lands in a `logs` table, so
// logs can be queried with SQL next to the instrumentation tables.
//
// By default the table lives in its own DuckDB file (log_catalog_db_path, next to the
// database), attached as _hardcharger_logs. log_catalog names a catalog the init SQL
// attaches instead (DuckLake or PostgreSQL), which several servers may share. Clients
// can read the catalog (admins only) but never change it.
//
// Lines are queued as they are logged and written in batches by one background thread
// on its own connection; logging never waits on the catalog. When the queue is full
// (the catalog is slow or unreachable) new lines are dropped, counted and reported.
#pragma once

#include <duckdb_v2.h>

#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "duckdb_v2.hpp"
#include "logging.hpp"

namespace hardcharger {

struct Runtime;

// The default name of the log catalog.
inline constexpr const char* kLogCatalog = "_hardcharger_logs";

class CatalogLog {
 public:
  CatalogLog(Runtime& runtime, duckdb_v2_instance_handle instance);
  ~CatalogLog();

  // File mode: attaches the log file. Before the init SQL runs, so the init SQL can't
  // take the name.
  void Attach();
  // Creates the table (also in a catalog the init SQL attached), starts the writer and
  // becomes the logger's sink.
  void Start();
  // Stops taking lines, writes the ones queued and stops the writer.
  void Stop();

  const std::string& catalog() const { return catalog_; }
  const std::string& schema() const { return schema_; }

  // The INSERT for `records` (one row each), into `table`.
  static std::string InsertSql(const std::string& table, const std::vector<LogRecord>& records);

 private:
  void Add(const LogRecord& record);
  void Write(const std::vector<LogRecord>& batch);
  void WriterLoop();

  Runtime& runtime_;
  duckdb_v2_instance_handle instance_;
  const bool external_;  // a catalog the init SQL attached
  std::string catalog_;
  std::string schema_;
  std::string table_;
  warpduckdb::Connection connection_;

  std::mutex mutex_;
  std::condition_variable wake_;
  std::deque<LogRecord> queue_;
  uint64_t dropped_ = 0;
  bool stopping_ = false;
  std::thread writer_;
};

}  // namespace hardcharger
