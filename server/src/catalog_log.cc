#include "catalog_log.hpp"

#include <algorithm>
#include <boost/json.hpp>
#include <cctype>
#include <chrono>
#include <filesystem>

#include "duckdb_sql.hpp"
#include "platform.hpp"
#include "runtime.hpp"
#include "sql_text.hpp"

namespace hardcharger {

namespace {

// The log table: the fields people filter on are columns,
// the rest of a line's fields ride along in `fields`. No key: the table is append-only.
// {J} is the JSON type (VARCHAR on PostgreSQL, where DuckDB writes the text as is).
constexpr const char* kTableSql =
    "CREATE TABLE IF NOT EXISTS {T} (log_time TIMESTAMP WITH TIME ZONE NOT NULL, level VARCHAR NOT NULL, "
    "instance_id UUID, cluster_id UUID, session_id UUID, username VARCHAR, role VARCHAR, peer VARCHAR, "
    "component VARCHAR, trace_id VARCHAR, span_id VARCHAR, pid INTEGER, tid VARCHAR, source_file VARCHAR, "
    "source_line INTEGER, func VARCHAR, message VARCHAR, fields {J})";

// Time-range queries and retention deletes stay fast. DuckLake can't index.
constexpr const char* kIndexedColumns[] = {"log_time",   "level",    "instance_id", "cluster_id",
                                           "session_id", "username", "component",   "trace_id"};

constexpr const char* kColumns =
    "log_time, level, instance_id, cluster_id, session_id, username, role, peer, component, trace_id, span_id, pid, "
    "tid, source_file, source_line, func, message, fields";

// Batches the writer sends as one INSERT, and lines it holds before it drops new ones.
constexpr std::size_t kBatchRows = 512;
constexpr std::size_t kMaxQueued = 100000;

std::string Replace(std::string text, std::string_view from, std::string_view to) {
  for (auto at = text.find(from); at != std::string::npos; at = text.find(from, at + to.size())) {
    text.replace(at, from.size(), to);
  }
  return text;
}

std::string_view LevelName(LogLevel level) {
  switch (level) {
    case LogLevel::kDebug: return "DEBUG";
    case LogLevel::kInfo: return "INFO";
    case LogLevel::kWarning: return "WARN";
    case LogLevel::kError: return "ERROR";
  }
  return "INFO";
}

bool IsUuid(std::string_view text) {
  if (text.size() != 36) return false;
  for (std::size_t i = 0; i < text.size(); i++) {
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? text[i] != '-' : !std::isxdigit(static_cast<unsigned char>(text[i]))) return false;
  }
  return true;
}

std::string Text(std::string_view value) { return value.empty() ? "NULL" : SqlLiteral(value); }

// One record's row. A field the table has a column for goes there; an id that isn't a
// UUID stays in `fields` rather than failing the row (a sink must not lose lines).
std::string Row(const LogRecord& record) {
  std::string instance_id, cluster_id, session_id, username, role, peer, component, trace_id, span_id;
  boost::json::object rest;
  for (const auto& [key, value] : record.fields) {
    auto id = [&](std::string& column) {
      if (IsUuid(value)) {
        column = value;
      } else {
        rest[key] = value;
      }
    };
    if (key == "instance_id") {
      id(instance_id);
    } else if (key == "cluster_id") {
      id(cluster_id);
    } else if (key == "session_id") {
      id(session_id);
    } else if (key == "user" || key == "username") {
      username = value;
    } else if (key == "role") {
      role = value;
    } else if (key == "peer") {
      peer = value;
    } else if (key == "component") {
      component = value;
    } else if (key == "trace_id") {
      trace_id = value;
    } else if (key == "span_id") {
      span_id = value;
    } else {
      rest[key] = value;
    }
  }
  return "(" + SqlTimestamp(record.time) + ", " + SqlLiteral(LevelName(record.level)) + ", " + Text(instance_id) +
         ", " + Text(cluster_id) + ", " + Text(session_id) + ", " + Text(username) + ", " + Text(role) + ", " +
         Text(peer) + ", " + Text(component) + ", " + Text(trace_id) + ", " + Text(span_id) + ", " +
         std::to_string(ProcessId()) + ", " + Text(record.thread) + ", NULL, NULL, NULL, " + SqlLiteral(record.message) +
         ", " + (rest.empty() ? std::string("NULL") : SqlLiteral(boost::json::serialize(rest))) + ")";
}

}  // namespace

std::string CatalogLog::InsertSql(const std::string& table, const std::vector<LogRecord>& records) {
  std::string sql = "INSERT INTO " + table + " (" + kColumns + ") VALUES ";
  for (std::size_t i = 0; i < records.size(); i++) {
    if (i > 0) sql += ", ";
    sql += Row(records[i]);
  }
  return sql;
}

CatalogLog::CatalogLog(Runtime& runtime, duckdb_v2_instance_handle instance)
    : runtime_(runtime),
      instance_(instance),
      external_(!runtime.config.log_catalog.empty()),
      catalog_(external_ ? runtime.config.log_catalog : kLogCatalog),
      schema_(runtime.config.log_schema.empty() ? "main" : runtime.config.log_schema),
      table_(SqlIdentifier(catalog_) + "." + SqlIdentifier(schema_) + ".logs") {
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance_, connection_.out(), err); });
}

CatalogLog::~CatalogLog() { Stop(); }

void CatalogLog::Attach() {
  if (external_) return;
  std::string path = runtime_.config.log_catalog_db_path;
  if (path.empty()) {
    // Next to the database file; in the working directory for an in-memory database.
    const auto& database = runtime_.config.database;
    const bool file = !database.empty() && database != ":memory:";
    path = (file ? std::filesystem::path(database).parent_path() / "hardcharger_logs.db"
                 : std::filesystem::path("hardcharger_logs.db"))
               .string();
  }
  // READ_WRITE: an instance opened read-only would otherwise open it read-only too.
  warpduckdb::RunSql(connection_.get(),
                     "ATTACH " + SqlLiteral(path) + " AS " + SqlIdentifier(catalog_) + " (READ_WRITE)");
}

void CatalogLog::Start() {
  const std::string type = warpduckdb::QueryText(
      connection_.get(), "SELECT type FROM duckdb_databases() WHERE database_name = " + SqlLiteral(catalog_));
  if (type.empty()) {
    throw std::runtime_error("log_catalog '" + catalog_ + "' is not attached: attach it in the init SQL");
  }
  if (type == "postgres") {
    // Native DDL through the postgres extension, so PostgreSQL gets its own indexes;
    // then DuckDB rereads the catalog.
    const std::string table = SqlIdentifier(schema_) + ".logs";
    std::vector<std::string> ddl = {"CREATE SCHEMA IF NOT EXISTS " + SqlIdentifier(schema_),
                                    Replace(Replace(kTableSql, "{T}", table), "{J}", "VARCHAR")};
    for (const char* column : kIndexedColumns) {
      ddl.push_back(std::string("CREATE INDEX IF NOT EXISTS idx_logs_") + column + " ON " + table + "(" + column + ")");
    }
    for (const auto& command : ddl) {
      warpduckdb::RunSql(connection_.get(),
                         "CALL postgres_execute(" + SqlLiteral(catalog_) + ", " + SqlLiteral(command) + ")");
    }
    warpduckdb::RunSql(connection_.get(), "CALL postgres_clear_cache()");
  } else {
    if (schema_ != "main") {
      warpduckdb::RunSql(connection_.get(),
                         "CREATE SCHEMA IF NOT EXISTS " + SqlIdentifier(catalog_) + "." + SqlIdentifier(schema_));
    }
    warpduckdb::RunSql(connection_.get(), Replace(Replace(kTableSql, "{T}", table_), "{J}", "JSON"));
    if (type != "ducklake") {
      for (const char* column : kIndexedColumns) {
        warpduckdb::RunSql(connection_.get(), std::string("CREATE INDEX IF NOT EXISTS idx_logs_") + column + " ON " +
                                                  table_ + "(" + column + ")");
      }
    }
  }
  writer_ = std::thread([this] { WriterLoop(); });
  SetLogSink([this](const LogRecord& record) { Add(record); });
  Log(LogLevel::kInfo, "catalog logging on", {{"catalog", catalog_}, {"schema", schema_}, {"backend", type}});
}

void CatalogLog::Stop() {
  // No line reaches Add once the sink is unset; then the writer drains what is queued.
  SetLogSink(nullptr);
  {
    std::lock_guard lock(mutex_);
    if (!writer_.joinable() || stopping_) return;
    stopping_ = true;
  }
  wake_.notify_all();
  writer_.join();
}

void CatalogLog::Add(const LogRecord& record) {
  {
    std::lock_guard lock(mutex_);
    if (stopping_) return;
    if (queue_.size() >= kMaxQueued) {
      dropped_++;
      return;
    }
    queue_.push_back(record);
  }
  wake_.notify_one();
}

void CatalogLog::Write(const std::vector<LogRecord>& batch) {
  try {
    warpduckdb::RunSql(connection_.get(), InsertSql(table_, batch));
    return;
  } catch (const std::exception& e) {
    Log(LogLevel::kWarning, "catalog log batch write failed; retrying line by line",
        {{"lines", std::to_string(batch.size())}, {"error", e.what()}});
  }
  // One bad line must not lose the batch.
  std::size_t failed = 0;
  for (const auto& record : batch) {
    try {
      warpduckdb::RunSql(connection_.get(), InsertSql(table_, {record}));
    } catch (const std::exception&) {
      failed++;
    }
  }
  if (failed > 0) {
    Log(LogLevel::kWarning, "catalog log lines lost: their writes failed", {{"lines", std::to_string(failed)}});
  }
}

void CatalogLog::WriterLoop() {
  // This thread's own lines (its write failures) go to the output only.
  LogSinkExclusion exclusion;
  uint64_t reported_drops = 0;
  auto reported_at = std::chrono::steady_clock::time_point();
  std::unique_lock lock(mutex_);
  for (;;) {
    wake_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
    if (queue_.empty() && stopping_) return;
    std::vector<LogRecord> batch;
    while (!queue_.empty() && batch.size() < kBatchRows) {
      batch.push_back(std::move(queue_.front()));
      queue_.pop_front();
    }
    const uint64_t dropped = dropped_;
    lock.unlock();
    Write(batch);
    // At most once a minute: a catalog that stays down must not flood the output.
    if (dropped > reported_drops && std::chrono::steady_clock::now() - reported_at >= std::chrono::minutes(1)) {
      reported_at = std::chrono::steady_clock::now();
      Log(LogLevel::kWarning, "catalog log queue full: lines dropped",
          {{"dropped_total", std::to_string(dropped)}, {"queue_limit", std::to_string(kMaxQueued)}});
      reported_drops = dropped;
    }
    lock.lock();
  }
}

}  // namespace hardcharger
