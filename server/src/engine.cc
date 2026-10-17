#include "engine.hpp"

#include <arrow/record_batch.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "duckdb_sql.hpp"
#include "logging.hpp"
#include "settings.hpp"
#include "sql_functions.hpp"
#include "tokens.hpp"
#include "version.hpp"

namespace hardcharger {

namespace json = boost::json;
using warpsql::Error;
using warpsql::ErrorCode;

namespace {

thread_local Session* current_session = nullptr;

// Marks this thread as running `session`'s statement, for the SQL functions.
class SessionScope {
 public:
  explicit SessionScope(Session* session) : previous_(current_session) { current_session = session; }
  ~SessionScope() { current_session = previous_; }
  SessionScope(const SessionScope&) = delete;
  SessionScope& operator=(const SessionScope&) = delete;

 private:
  Session* previous_;
};

warpduckdb::DuckDBBackend::Options EngineOptions(const Config& config) {
  warpduckdb::DuckDBBackend::Options options;
  options.database = config.database.empty() ? ":memory:" : config.database;
  options.read_only = config.read_only;
  if (!config.memory_limit.empty()) {
    options.instance_options.emplace_back("memory_limit", config.memory_limit);
  }
  if (config.allow_unsigned_extensions) {
    options.instance_options.emplace_back("allow_unsigned_extensions", "true");
  }
  if (!config.storage_version.empty()) {
    options.attach_options.emplace_back("STORAGE_VERSION", config.storage_version);
  }
  options.result_batch_rows = static_cast<std::size_t>(config.result_batch_rows);
  return options;
}

// Statements a "readonly" principal may run. A strict allow-list until the authorization
// module (with DuckDB's own read-only analysis) lands.
bool ReadOnlyStatement(DUCKDB_V2_STATEMENT_TYPE type) {
  return type == DUCKDB_V2_STATEMENT_TYPE_SELECT || type == DUCKDB_V2_STATEMENT_TYPE_EXPLAIN;
}

std::string Abbreviate(std::string_view sql) {
  constexpr std::size_t kMax = 4000;
  return sql.size() <= kMax ? std::string(sql) : std::string(sql.substr(0, kMax)) + "…";
}

int64_t NowMicros() {
  return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch())
      .count();
}

Error KilledError() {
  return Error(ErrorCode::kSessionEnded,
               "This session has been killed by an administrator (reason: killed). Sign in again and open a new "
               "session to re-authenticate.");
}

// The JDBC-style metadata views 1.x drivers query (getIndexInfo, view DDL).
constexpr const char* kSystemViews = R"SQL(
CREATE VIEW {catalog}.main.gizmosql_index_info AS
WITH idx_src AS (
  SELECT di.database_name, di.schema_name, di.table_name, di.is_unique, di.index_name,
         -- DuckDB renders the indexed expressions as a list, quoting names that need it
         -- (['"label"'] for a keyword); the view returns plain column names.
         list_transform(str_split(trim(BOTH '[]' FROM di.expressions), ', '),
                        lambda x: regexp_replace(regexp_replace(trim(x), '^''(.*)''$', '\1'), '^"(.*)"$', '\1')) AS cols
  FROM duckdb_indexes() di
), constraint_src AS (
  SELECT dc.database_name, dc.schema_name, dc.table_name, TRUE AS is_unique,
         dc.constraint_name AS index_name, dc.constraint_column_names AS cols
  FROM duckdb_constraints() dc
  WHERE dc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
), all_src AS (
  SELECT * FROM idx_src UNION ALL SELECT * FROM constraint_src
)
SELECT database_name AS "TABLE_CAT", schema_name AS "TABLE_SCHEM", table_name AS "TABLE_NAME",
       NOT is_unique AS "NON_UNIQUE", CAST(NULL AS VARCHAR) AS "INDEX_QUALIFIER",
       index_name AS "INDEX_NAME", CAST(3 AS SMALLINT) AS "TYPE",
       idx::SMALLINT AS "ORDINAL_POSITION", cols[idx] AS "COLUMN_NAME",
       CAST('A' AS VARCHAR) AS "ASC_OR_DESC", CAST(NULL AS BIGINT) AS "CARDINALITY",
       CAST(NULL AS BIGINT) AS "PAGES", CAST(NULL AS VARCHAR) AS "FILTER_CONDITION"
FROM all_src, generate_series(1, len(cols)) AS t(idx);
CREATE VIEW {catalog}.main.gizmosql_view_definition AS
SELECT database_name AS "TABLE_CAT", schema_name AS "TABLE_SCHEM", view_name AS "TABLE_NAME",
       sql AS "VIEW_DEFINITION"
FROM duckdb_views();
)SQL";

std::string Replace(std::string text, std::string_view from, std::string_view to) {
  for (std::size_t at = text.find(from); at != std::string::npos; at = text.find(from, at + to.size())) {
    text.replace(at, from.size(), to);
  }
  return text;
}

// Rows of one tracked statement: when they are all read (or the reader goes away), the
// statement's slot is given back and its outcome counted.
//
// A read that fails because the session was killed (or the statement cancelled) throws
// the Warp SQL error that says so: the SDK's reading loop reports a warpsql::Error as it
// is, where a plain Arrow failure would become an execution_error.
class TrackedReader : public arrow::RecordBatchReader {
 public:
  TrackedReader(std::shared_ptr<arrow::RecordBatchReader> rows, std::function<void(std::string_view, std::string_view)> done,
                std::function<void(const arrow::Status&)> translate)
      : rows_(std::move(rows)), done_(std::move(done)), translate_(std::move(translate)) {}
  ~TrackedReader() override { Finish("cancelled", ""); }

  std::shared_ptr<arrow::Schema> schema() const override { return rows_->schema(); }
  arrow::Status ReadNext(std::shared_ptr<arrow::RecordBatch>* batch) override {
    auto status = rows_->ReadNext(batch);
    if (!status.ok()) {
      const std::string_view error_class = ClassifyStatementError(status.message());
      Finish(error_class == "cancelled" ? "cancelled" : "error", error_class);
      if (translate_) translate_(status);
    } else if (*batch == nullptr) {
      Finish("ok", "");
    }
    return status;
  }
  arrow::Status Close() override { return rows_->Close(); }

 private:
  void Finish(std::string_view status, std::string_view error_class) {
    if (done_) std::exchange(done_, nullptr)(status, error_class);
  }

  std::shared_ptr<arrow::RecordBatchReader> rows_;
  std::function<void(std::string_view, std::string_view)> done_;
  std::function<void(const arrow::Status&)> translate_;
};

}  // namespace

Session* CurrentSession() { return current_session; }

// ---- Backend ----

Backend::Backend(Runtime& runtime)
    : DuckDBBackend(EngineOptions(runtime.config)),
      runtime_(runtime),
      sensitive_paths_(runtime.config.block_sensitive_paths, runtime.config.sensitive_paths,
                       {runtime.config.tls_key, runtime.config.tls_cert, runtime.config.mtls_ca_cert,
                        runtime.config.license_key_file, runtime.config.init_sql_commands_file,
                        runtime.config.config_file}) {
  warpduckdb::Connection connection;
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance(), connection.out(), err); });
  RegisterSqlFunctions(connection.get(), runtime_);
  AttachSystemCatalog();
  RunInitSql();
  // Init SQL may move DuckDB's secret directory; wherever it ends up is protected.
  sensitive_paths_.AddSecretDirectory(
      warpduckdb::QueryText(connection.get(), "SELECT current_setting('secret_directory')"));
}

Backend::~Backend() {
  StopHealthCheck();
  if (!system_catalog_dir_.empty()) {
    std::error_code ec;
    std::filesystem::remove_all(system_catalog_dir_, ec);
  }
}

void Backend::AttachSystemCatalog() {
  // DuckDB can't make an in-memory catalog read-only once it holds the views, so they
  // are written to a small private file that is then attached READ_ONLY: every write to
  // the system catalog is refused by DuckDB itself, for every user.
  std::string base = std::filesystem::temp_directory_path().string() + "/hardcharger-system-XXXXXX";
  if (mkdtemp(base.data()) == nullptr) throw std::runtime_error("cannot create a directory for the system catalog");
  system_catalog_dir_ = base;
  const std::string file = system_catalog_dir_ + "/system.duckdb";
  warpduckdb::Connection connection;
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance(), connection.out(), err); });
  const std::string quoted = "'" + Replace(file, "'", "''") + "'";
  warpduckdb::RunSql(connection.get(), "ATTACH " + quoted + " AS _hardcharger_system_build (READ_WRITE)");
  warpduckdb::RunSql(connection.get(), Replace(kSystemViews, "{catalog}", "_hardcharger_system_build"));
  warpduckdb::RunSql(connection.get(), "DETACH _hardcharger_system_build");
  warpduckdb::RunSql(connection.get(), "ATTACH " + quoted + " AS " + kSystemCatalog + " (READ_ONLY)");
}

void Backend::RunInitSql() {
  const Config& config = runtime_.config;
  std::string sql = config.init_sql_commands;
  if (!config.init_sql_commands_file.empty()) {
    std::ifstream in(config.init_sql_commands_file);
    if (!in) {
      throw std::runtime_error("cannot read init_sql_commands_file " + config.init_sql_commands_file);
    }
    std::stringstream contents;
    contents << in.rdbuf();
    sql += "\n" + contents.str();
  }
  if (sql.find_first_not_of(" \t\r\n;") == std::string::npos) {
    return;
  }
  warpduckdb::Connection connection;
  warpduckdb::Call([&](auto* err) {
    return duckdb_v2_connection_create(instance(), connection.out(), err);
  });
  try {
    warpduckdb::RunSql(connection.get(), sql);
  } catch (const Error& error) {
    throw std::runtime_error(std::string("init SQL failed: ") + error.what());
  }
  Log(LogLevel::kInfo, "init SQL ran");
}

void Backend::StartHealthCheck() {
  const Config& config = runtime_.config;
  health_ = std::make_unique<HealthChecker>(instance(), config.health_check_query,
                                            std::chrono::seconds(config.health_check_interval_seconds),
                                            std::chrono::seconds(config.health_check_staleness_seconds));
  health_->Start();
  runtime_.health = health_.get();
}

void Backend::StopHealthCheck() {
  if (health_) health_->Stop();
}

void Backend::CheckHealth() {
  if (health_) {
    health_->Check();
  } else {
    DuckDBBackend::CheckHealth();
  }
}

warpsql::ServerIdentity Backend::Identity() {
  return {"Hardcharger", std::string(kVersion) + " (DuckDB " + duckdb_version() + ")"};
}

json::object Backend::ServerInfo() {
  json::object info = DuckDBBackend::ServerInfo();
  info["edition"] = runtime_.license.Edition();
  info["instance_id"] = runtime_.instance_id;
  return info;
}

std::unique_ptr<warpsql::EngineSession> Backend::NewSession(const warpsql::Principal& principal) {
  const bool capped = runtime_.sessions.Admit(principal);
  try {
    return std::make_unique<Session>(runtime_, sensitive_paths_, instance(), principal, capped);
  } catch (...) {
    runtime_.sessions.Unadmit(capped);
    throw;
  }
}

// ---- Session ----

Session::Session(Runtime& runtime, const SensitivePaths& sensitive_paths, duckdb_v2_instance_handle instance,
                 warpsql::Principal principal, bool capped)
    : DuckDBSession(instance, static_cast<std::size_t>(runtime.config.result_batch_rows)),
      runtime_(runtime),
      sensitive_paths_(sensitive_paths),
      principal_(std::move(principal)),
      id_(RandomUuid()),
      started_at_us_(NowMicros()) {
  runtime_.sessions.Add(this, capped);
  runtime_.metrics.CountSessionOpened();
  LogAt(runtime_.session_log_level, LogLevel::kInfo, "session opened",
        {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}});
}

Session::~Session() {
  runtime_.sessions.Remove(this);
  LogAt(runtime_.session_log_level, LogLevel::kInfo, "session closed",
        {{"session_id", id_}, {"user", principal_.name}, {"reason", killed_ ? "killed" : "closed"}});
}

void Session::CheckAlive() const {
  if (killed_) throw KilledError();
}

void Session::Kill() {
  killed_ = true;
  interrupted_ = true;
  DuckDBSession::Interrupt();
  runtime_.admission.WakeWaiters();
  runtime_.metrics.CountSessionReaped();
  Log(LogLevel::kInfo, "session killed", {{"session_id", id_}, {"user", principal_.name}});
}

void Session::Interrupt() {
  interrupted_ = true;
  runtime_.admission.WakeWaiters();
  DuckDBSession::Interrupt();
}

Session::Settings Session::settings() const {
  std::lock_guard lock(settings_mutex_);
  return settings_;
}

void Session::UpdateSettings(const std::function<void(Settings&)>& update) {
  std::lock_guard lock(settings_mutex_);
  update(settings_);
}

void Session::LogQuery(LogLevel level, std::string_view message, const LogFields& fields) const {
  // The statement log level (SET [GLOBAL] gizmosql.query_log_level) decides alone, so a
  // session that asks for DEBUG gets it whatever the logger's own level.
  LogAt(QueryLogLevel(), level, message, fields, true);
}

LogLevel Session::QueryLogLevel() const {
  std::lock_guard lock(settings_mutex_);
  return settings_.query_log_level.value_or(runtime_.query_log_level.load());
}

std::optional<std::chrono::milliseconds> Session::DefaultStatementTimeout() {
  std::optional<int64_t> seconds;
  {
    std::lock_guard lock(settings_mutex_);
    seconds = settings_.query_timeout;
  }
  return std::chrono::milliseconds(1000 * seconds.value_or(runtime_.query_timeout_seconds.load()));
}

DUCKDB_V2_STATEMENT_TYPE Session::Authorize(const Piece& piece) {
  const DUCKDB_V2_STATEMENT_TYPE type = StatementType(connection(), piece.text);
  const Config& config = runtime_.config;
  CheckStatement({connection(), admin(), config.block_unredacted_secrets, &sensitive_paths_}, piece, type);
  if (type == DUCKDB_V2_STATEMENT_TYPE_DETACH) {
    for (const Token& token : piece.tokens) {
      if (token.Value() == kSystemCatalog) {
        throw Error(ErrorCode::kPermissionDenied,
                    std::string("Cannot DETACH ") + kSystemCatalog + ": it is the server's system catalog", "42501");
      }
    }
  }
  if (principal_.role == "readonly" && !ReadOnlyStatement(type)) {
    throw Error(ErrorCode::kPermissionDenied,
                "User '" + principal_.name + "' has a readonly session and cannot run statements that modify state.",
                "42501");
  }
  return type;
}

void Session::NoteTransaction(const Piece& piece) {
  const auto& t = piece.tokens;
  if (t.empty()) return;
  if (t[0].IsWord("BEGIN") || t[0].IsWord("START")) {
    transaction_ = Transaction::kOpen;
  } else if (t[0].IsWord("COMMIT") || t[0].IsWord("END") || t[0].IsWord("ROLLBACK") || t[0].IsWord("ABORT")) {
    transaction_ = manual_commit_ ? Transaction::kOpen : Transaction::kNone;
  }
}

warpsql::StatementOutcome Session::RunCommand(const Piece& piece) {
  if (auto setting = ParseSettingCommand(piece)) {
    ApplySetting(*this, *setting);
    return {};
  }
  const std::string target = ParseKillSession(piece).value_or("");
  if (!runtime_.license.Has("kill_session")) {
    throw Error(ErrorCode::kPermissionDenied, LicenseRequired("kill_session", "KILL SESSION"));
  }
  if (!admin()) {
    throw Error(ErrorCode::kPermissionDenied, "Only admin users can execute KILL SESSION", "42501");
  }
  if (target.empty()) {
    throw Error(ErrorCode::kSqlError, "KILL SESSION needs a session id: KILL SESSION '<uuid>'", "42601");
  }
  switch (runtime_.sessions.Kill(target, *this)) {
    case SessionRegistry::KillResult::kKilled:
      Log(LogLevel::kInfo, "KILL SESSION", {{"target", target}, {"user", principal_.name}, {"session_id", id_}});
      return {};
    case SessionRegistry::KillResult::kOwnSession:
      throw Error(ErrorCode::kSqlError, "Cannot kill your own session", "42P01");
    case SessionRegistry::KillResult::kNotFound:
      break;
  }
  throw Error(ErrorCode::kSqlError, "Session '" + target + "' not found", "42704");
}

warpsql::StatementOutcome Session::Track(StatementKind kind, std::string_view sql,
                                         const std::function<warpsql::StatementOutcome()>& work) {
  const auto started = std::chrono::steady_clock::now();
  AdmissionSlot slot;
  AdmissionController& queue = runtime_.admission;
  if (queue.Limit() > 0) {
    const auto mine = settings();
    // Admins skip the queue unless they asked not to; nobody else can skip it.
    const bool bypass = admin() && mine.bypass_queue.value_or(runtime_.config.admin_bypass_queue_default);
    if (bypass) runtime_.metrics.CountQueueBypass();
    try {
      slot = queue.Acquire(!bypass, mine.max_queue_wait.value_or(queue.DefaultMaxQueueWaitSeconds()),
                           [this] { return killed_.load() || interrupted_.load(); }, std::chrono::milliseconds(100));
    } catch (const AdmissionController::Refused& refused) {
      runtime_.metrics.CountQueueRejected();
      if (killed_) throw KilledError();
      if (refused.reason == AdmissionController::Refusal::kAborted) {
        throw Error(ErrorCode::kStatementCancelled, "INTERRUPT Error: the statement was cancelled while queued",
                    "57014");
      }
      throw Error(ErrorCode::kRateLimited, refused.message);
    }
    runtime_.metrics.ObserveQueueWait(
        std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
  }

  running_++;
  auto shared_slot = std::make_shared<AdmissionSlot>(std::move(slot));
  auto done = [this, kind, started, shared_slot, logged = Abbreviate(sql)](std::string_view status,
                                                                           std::string_view error_class) {
    shared_slot->Release();
    running_--;
    if (status == "ok" && runtime_.config.print_queries) {
      LogQuery(LogLevel::kInfo, "Client SQL command execution succeeded",
               {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"status", "success"},
                {"is_internal", "false"}, {"sql", logged}});
    }
    runtime_.metrics.RecordStatement(kind, status, error_class,
                                     std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
  };
  warpsql::StatementOutcome outcome;
  try {
    outcome = work();
  } catch (const Error& error) {
    const std::string_view error_class = ClassifyStatementError(error.what());
    done(error.code() == ErrorCode::kStatementCancelled ? "cancelled" : "error", error_class);
    // A failed statement inside a transaction leaves it aborted until a ROLLBACK.
    if (transaction_ == Transaction::kOpen) transaction_ = Transaction::kAborted;
    LogQuery(LogLevel::kInfo, "statement failed",
          {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"status", "error"},
           {"error", error.what()}, {"sql", Abbreviate(sql)}});
    if (killed_) throw KilledError();
    throw;
  }
  if (outcome.rows) {
    outcome.rows = std::make_shared<TrackedReader>(std::move(outcome.rows), done, [this](const arrow::Status& status) {
      if (killed_) throw KilledError();
      if (interrupted_ && ClassifyStatementError(status.message()) == "cancelled") {
        throw Error(ErrorCode::kStatementCancelled, status.message(), "57014");
      }
    });
  } else {
    done("ok", "");
  }
  return outcome;
}

warpsql::StatementOutcome Session::Execute(std::string_view sql, const json::value& parameters) {
  CheckAlive();
  SessionScope scope(this);
  interrupted_ = false;
  if (sql.find('\0') != std::string_view::npos) {
    throw Error(ErrorCode::kSqlError, "SQL text must not contain a NUL byte", "22021");
  }
  const auto pieces = SplitStatements(connection(), sql);
  if (pieces.empty()) {
    return DuckDBSession::Execute(sql, parameters);  // DuckDB's own "no statement" answer
  }

  // Check every statement of a batch before running any of them, so a refused one
  // never leaves the others half-done.
  struct Step {
    const Piece* piece;
    DUCKDB_V2_STATEMENT_TYPE type = DUCKDB_V2_STATEMENT_TYPE_INVALID;
    bool command = false;
  };
  std::vector<Step> steps;
  try {
    for (const Piece& piece : pieces) {
      Step step{&piece};
      step.command = ParseSettingCommand(piece).has_value() || ParseKillSession(piece).has_value();
      if (!step.command) step.type = Authorize(piece);
      steps.push_back(step);
    }
  } catch (const Error& error) {
    // Refused or unparseable: a failed statement all the same, for metrics.
    runtime_.metrics.RecordStatement(StatementKind::kOther, "error", ClassifyStatementError(error.what()), 0);
    throw;
  }
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "statement",
          {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}, {"kind", "sql"},
           {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
  }

  if (steps.size() == 1 && !steps[0].command) {
    auto outcome = Track(ClassifyStatement(steps[0].type), sql, [&] { return DuckDBSession::Execute(sql, parameters); });
    if (steps[0].type == DUCKDB_V2_STATEMENT_TYPE_TRANSACTION) NoteTransaction(pieces[0]);
    return outcome;
  }
  if (!parameters.is_null() && steps.size() > 1) {
    throw Error(ErrorCode::kSqlError, "bind parameters need a single SQL statement");
  }
  warpsql::StatementOutcome outcome;
  for (std::size_t i = 0; i < steps.size(); i++) {
    const Step& step = steps[i];
    if (step.command) {
      outcome = RunCommand(*step.piece);
    } else if (step.type == DUCKDB_V2_STATEMENT_TYPE_TRANSACTION) {
      outcome = Track(StatementKind::kOther, step.piece->text,
                      [&] { return DuckDBSession::Execute(step.piece->text, parameters); });
      NoteTransaction(*step.piece);
    } else {
      outcome = Track(ClassifyStatement(step.type), step.piece->text,
                      [&] { return DuckDBSession::Execute(step.piece->text, parameters); });
    }
    if (i + 1 < steps.size() && outcome.rows) {
      // Every statement but the last runs to its end; its rows are not returned.
      for (;;) {
        auto next = outcome.rows->Next();
        if (!next.ok()) throw Error(ErrorCode::kExecutionError, next.status().message());
        if (*next == nullptr) break;
      }
      outcome.rows.reset();
    }
  }
  return outcome;
}

std::unique_ptr<warpsql::PreparedStatement> Session::Prepare(std::string_view sql) {
  CheckAlive();
  SessionScope scope(this);
  if (sql.find('\0') != std::string_view::npos) {
    throw Error(ErrorCode::kSqlError, "SQL text must not contain a NUL byte", "22021");
  }
  const auto pieces = SplitStatements(connection(), sql);
  if (pieces.size() == 1) {
    if (ParseSettingCommand(pieces[0]) || ParseKillSession(pieces[0])) {
      throw Error(ErrorCode::kSqlError, "SET gizmosql.* and KILL SESSION can't be prepared; run them directly");
    }
    Authorize(pieces[0]);
  }
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "prepare",
          {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"is_internal", "false"},
           {"sql", Abbreviate(sql)}});
  }
  return DuckDBSession::Prepare(sql);
}

warpsql::StatementOutcome Session::ExecutePrepared(warpsql::PreparedStatement& statement,
                                                   const warpsql::Parameters& parameters) {
  CheckAlive();
  SessionScope scope(this);
  interrupted_ = false;
  return Track(StatementKind::kOther, "(prepared statement)",
               [&] { return DuckDBSession::ExecutePrepared(statement, parameters); });
}

int64_t Session::Ingest(const warpsql::IngestTarget& target, std::shared_ptr<arrow::RecordBatchReader> rows) {
  CheckAlive();
  SessionScope scope(this);
  if (principal_.role == "readonly") {
    throw Error(ErrorCode::kPermissionDenied,
                "User '" + principal_.name + "' has a readonly session and cannot ingest data.", "42501");
  }
  const LogFields where = {{"session_id", id_},       {"user", principal_.name},  {"kind", "ingest"},
                           {"catalog", target.catalog}, {"schema", target.schema}, {"table", target.table},
                           {"flight_method", "ingest"}};
  auto with = [&](LogFields extra) {
    LogFields fields = where;
    fields.insert(fields.end(), extra.begin(), extra.end());
    return fields;
  };
  const auto started = std::chrono::steady_clock::now();
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "ingest", with({{"status", "attempt"}}));
  }
  const int64_t ingested = DuckDBSession::Ingest(target, std::move(rows));
  if (runtime_.config.print_queries) {
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started);
    LogQuery(LogLevel::kInfo, "ingest",
          with({{"status", "success"}, {"rows_ingested", std::to_string(ingested)},
                {"duration_ms", std::to_string(ms.count())}}));
  }
  return ingested;
}

std::shared_ptr<arrow::RecordBatchReader> Session::Metadata(warpsql::MetadataKind kind,
                                                            const warpsql::MetadataFilter& filter) {
  CheckAlive();
  SessionScope scope(this);
  if (runtime_.config.print_queries) {
    // Metadata requests run SQL of the server's own: logged only at DEBUG.
    LogQuery(LogLevel::kDebug, "metadata",
             {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"is_internal", "true"},
              {"metadata", std::to_string(static_cast<int>(kind))}});
  }
  return DuckDBSession::Metadata(kind, filter);
}

warpsql::OptionErrors Session::SetOptions(const json::object& options) {
  CheckAlive();
  SessionScope scope(this);
  auto errors = DuckDBSession::SetOptions(options);
  if (const auto* autocommit = options.if_contains("autocommit");
      autocommit != nullptr && autocommit->is_bool() && !errors.contains("autocommit")) {
    manual_commit_ = !autocommit->as_bool();
    transaction_ = manual_commit_ ? Transaction::kOpen : Transaction::kNone;
  }
  return errors;
}

void Session::Commit() {
  CheckAlive();
  DuckDBSession::Commit();
  transaction_ = manual_commit_ ? Transaction::kOpen : Transaction::kNone;
}

void Session::Rollback() {
  CheckAlive();
  DuckDBSession::Rollback();
  transaction_ = manual_commit_ ? Transaction::kOpen : Transaction::kNone;
}

}  // namespace hardcharger
