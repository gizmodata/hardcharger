#include "engine.hpp"

#include <arrow/record_batch.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "auth.hpp"
#include "duckdb_sql.hpp"
#include "instrumentation.hpp"
#include "logging.hpp"
#include "platform.hpp"
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

// Where DuckDB keeps installed extensions and persistent secrets. DuckDB puts them under
// HOME (~/.duckdb/extensions, ~/.duckdb/stored_secrets) and, without HOME, can't INSTALL
// at all ("Can't find the home directory") and keeps secrets relative to the working
// directory. Services and containers often run without HOME, or with one the account
// can't write (a container run as a user with no account in the image gets HOME=/). So:
//  - HOME unset, the account's home (the password database) writable: DuckDB's
//    home_directory is that home, as '~' is for the sensitive-path guard;
//  - no writable home at all: the extension and secret directories go under the data
//    directory (the database file's directory; the working directory for an in-memory
//    database), in .duckdb/, as they would under a home.
// Init SQL can still move either (SET GLOBAL extension_directory = ...).
std::vector<std::pair<std::string, std::string>> DuckDBDirectories(const Config& config) {
  const std::string home = HomeDirectory();
  if (IsWritableDirectory(home)) {
    if (HomeFromEnvironment()) return {};
    Log(LogLevel::kInfo, "HOME is not set: DuckDB's home directory is the account's", {{"home_directory", home}});
    return {{"home_directory", home}};
  }
  std::error_code ec;
  const bool file = !config.database.empty() && config.database != ":memory:";
  std::filesystem::path data = file ? std::filesystem::absolute(PathFromUtf8(config.database), ec).parent_path()
                                    : std::filesystem::current_path(ec);
  if (!IsWritableDirectory(data.string())) {
    Log(LogLevel::kWarning, "no writable home or data directory: INSTALL and persistent secrets fail until init SQL "
                            "sets extension_directory and secret_directory to a writable directory",
        {{"home", home}, {"data_directory", data.string()}});
  }
  const std::string extensions = (data / ".duckdb" / "extensions").string();
  const std::string secrets = (data / ".duckdb" / "stored_secrets").string();
  Log(LogLevel::kInfo, "no writable home directory: DuckDB's extensions and persistent secrets are kept in the data "
                       "directory",
      {{"home", home}, {"extension_directory", extensions}, {"secret_directory", secrets}});
  return {{"extension_directory", extensions}, {"secret_directory", secrets}};
}

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
  // The extensions the server needs are built in (scripts/duckdb-extensions.cmake) and
  // loaded at startup. Nothing else is downloaded or loaded behind a statement's back:
  // another extension takes an explicit INSTALL and LOAD.
  options.instance_options.emplace_back("autoinstall_known_extensions", "false");
  options.instance_options.emplace_back("autoload_known_extensions", "false");
  for (auto& option : DuckDBDirectories(config)) options.instance_options.push_back(std::move(option));
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

// SQL as logs and instrumentation keep it: shortened, and never with the credentials
// of CREATE SECRET or CREATE / ALTER USER ... PASSWORD (the catalog logs and
// instrumentation tables are durable, and admins read them).
std::string Abbreviate(std::string_view sql) { return LoggableSql(sql); }

int64_t NowMicros() {
  return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch())
      .count();
}

Error KilledError() {
  return Error::SessionEnded(warpsql::reason::kKilled,
                             "This session has been killed by an administrator (reason: killed). Sign in again "
                             "and open a new session to re-authenticate.");
}

// JDBC-style metadata views (getIndexInfo, view DDL).
constexpr const char* kSystemViews = R"SQL(
CREATE VIEW {catalog}.main.hardcharger_index_info AS
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
CREATE VIEW {catalog}.main.hardcharger_view_definition AS
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

// The rows of `rows` whose first column (a catalog name) is none of `catalogs`.
std::shared_ptr<arrow::RecordBatchReader> WithoutCatalogs(std::shared_ptr<arrow::RecordBatchReader> rows,
                                                          const std::vector<std::string>& catalogs) {
  auto schema = rows->schema();
  arrow::RecordBatchVector kept;
  for (;;) {
    auto next = rows->Next();
    if (!next.ok()) throw Error(ErrorCode::kExecutionError, next.status().message());
    auto batch = *next;
    if (batch == nullptr) break;
    const auto names = std::static_pointer_cast<arrow::StringArray>(batch->column(0));
    int64_t run_start = 0;
    for (int64_t i = 0; i <= batch->num_rows(); i++) {
      const bool drop = i < batch->num_rows() && names->IsValid(i) &&
                        std::find(catalogs.begin(), catalogs.end(), names->GetView(i)) != catalogs.end();
      if (drop || i == batch->num_rows()) {
        if (i > run_start) kept.push_back(batch->Slice(run_start, i - run_start));
        run_start = i + 1;
      }
    }
  }
  auto reader = arrow::RecordBatchReader::Make(std::move(kept), schema);
  if (!reader.ok()) throw Error(ErrorCode::kExecutionError, reader.status().message());
  return *reader;
}

// Without access control, its statements get a clear error instead of DuckDB's syntax error.
void RefuseAccessControlStatement(std::string_view sql) {
  if (auto kind = authz::AccessControlStatementKind(sql)) {
    throw Error(ErrorCode::kSqlError,
                "Hardcharger access control is not enabled on this server, so " + *kind +
                    " is not available. (Server operators: set rbac_store_uri to turn it on)");
  }
}

}  // namespace

// One execution of a statement, from its queue slot to its result being let go: gives
// the slot back, counts it in metrics, logs its success and records its timeline in
// instrumentation, each exactly once.
class Execution {
 public:
  // `profile`: where DuckDB writes this execution's profile, when one is captured.
  Execution(Session& session, StatementKind kind, std::string sql, AdmissionSlot slot,
            std::chrono::steady_clock::time_point started, ExecutionRecord record, std::string profile)
      : session_(session),
        kind_(kind),
        sql_(std::move(sql)),
        slot_(std::move(slot)),
        started_(started),
        record_(std::move(record)),
        profile_(std::move(profile)) {
    if (auto* instrumentation = Instr()) {
      if (record_.enqueue_time) {
        instrumentation->ExecutionAdmitted(record_);
      } else {
        instrumentation->ExecutionStarted(record_);
      }
    }
  }
  ~Execution() {
    End("cancelled", "cancelled", "the result was abandoned");
    Close();
  }

  // The engine has the outcome (rows to read, or the update done).
  void Produced() { record_.end = std::chrono::system_clock::now(); }
  void Batch(int64_t rows) {
    if (!record_.fetch_start) record_.fetch_start = std::chrono::system_clock::now();
    record_.rows += rows;
  }
  // status: ok, error or cancelled.
  void End(std::string_view status, std::string_view error_class, std::string_view error) {
    if (ended_) return;
    ended_ = true;
    const auto now = std::chrono::system_clock::now();
    if (!record_.end) record_.end = now;
    record_.fetch_end = now;
    record_.status = status == "ok" ? "success" : std::string(status);
    record_.error = std::string(error);
    slot_.Release();
    session_.FinishRunning();
    Runtime& runtime = session_.runtime();
    if (status == "ok" && runtime.config.print_queries) {
      session_.LogQuery(LogLevel::kInfo, "Client SQL command execution succeeded",
                        {{"session_id", session_.id()}, {"user", session_.principal().name}, {"kind", "sql"},
                         {"status", "success"}, {"is_internal", "false"}, {"sql", sql_}});
    }
    runtime.metrics.RecordStatement(kind_, status, error_class,
                                    std::chrono::duration<double>(std::chrono::steady_clock::now() - started_).count());
  }
  // The result was let go (read to its end, abandoned, or an update's outcome taken).
  void Close() {
    if (closed_) return;
    closed_ = true;
    record_.cursor_close = std::chrono::system_clock::now();
    if (!profile_.empty()) {
      // DuckDB writes the profile when the query ends, which it has once the result is let go.
      std::ifstream in(profile_);
      std::stringstream contents;
      contents << in.rdbuf();
      record_.query_profile = contents.str();
      std::error_code ec;
      std::filesystem::remove(profile_, ec);
    }
    if (auto* instrumentation = Instr()) instrumentation->ExecutionFinished(record_);
  }

 private:
  Instrumentation* Instr() const { return record_.execution_id.empty() ? nullptr : session_.runtime().instrumentation; }

  Session& session_;
  const StatementKind kind_;
  const std::string sql_;
  AdmissionSlot slot_;
  const std::chrono::steady_clock::time_point started_;
  ExecutionRecord record_;
  const std::string profile_;
  bool ended_ = false;
  bool closed_ = false;
};

namespace {

// Rows of one tracked statement, driving its Execution.
//
// A read that fails because the session was killed (or the statement cancelled) throws
// the Warp SQL error that says so: the SDK's reading loop reports a warpsql::Error as it
// is, where a plain Arrow failure would become an execution_error.
class TrackedReader : public arrow::RecordBatchReader {
 public:
  TrackedReader(std::shared_ptr<arrow::RecordBatchReader> rows, std::shared_ptr<Execution> execution,
                std::function<void(const arrow::Status&)> translate)
      : execution_(std::move(execution)), rows_(std::move(rows)), translate_(std::move(translate)) {}

  std::shared_ptr<arrow::Schema> schema() const override { return rows_->schema(); }
  arrow::Status ReadNext(std::shared_ptr<arrow::RecordBatch>* batch) override {
    auto status = rows_->ReadNext(batch);
    if (!status.ok()) {
      const std::string_view error_class = ClassifyStatementError(status.message());
      execution_->End(error_class == "cancelled" ? "cancelled" : "error", error_class, status.message());
      if (translate_) translate_(status);
    } else if (*batch == nullptr) {
      execution_->End("ok", "", "");
    } else {
      execution_->Batch((*batch)->num_rows());
    }
    return status;
  }
  arrow::Status Close() override { return rows_->Close(); }

 private:
  // Declared before rows_, so the DuckDB result is gone (its query ended, its profile
  // written) before the execution closes.
  std::shared_ptr<Execution> execution_;
  std::shared_ptr<arrow::RecordBatchReader> rows_;
  std::function<void(const arrow::Status&)> translate_;
};

}  // namespace

Session* CurrentSession() { return current_session; }

// ---- Backend ----

Backend::Backend(Runtime& runtime, std::shared_ptr<authz::AccessControl> access)
    : DuckDBBackend(EngineOptions(runtime.config)),
      runtime_(runtime),
      access_(std::move(access)),
      sensitive_paths_(runtime.config.block_sensitive_paths, runtime.config.sensitive_paths,
                       {runtime.config.tls_key, runtime.config.tls_cert, runtime.config.mtls_ca_cert,
                        runtime.config.init_sql_commands_file,
                        runtime.config.config_file}),
      private_files_(std::make_shared<PrivateFiles>()) {
  // Before anything else runs on the instance: from here on every plan of a bound
  // session is checked. (The server's own connections below, and those of
  // instrumentation, catalog logging and the health check, carry no principal, so the
  // check leaves them alone.)
  if (access_) access_->InstallEnforcement(instance());
  // And no statement opens the server's own files: a DuckDB access-control store, and
  // the files this instance attaches (each added once it is attached, below).
  if (access_) private_files_->Add(access_->store_file(), "the access-control store", /*attached=*/false);
  authz::InstallPrivateFileGuard(instance(), private_files_);
  warpduckdb::Connection connection;
  warpduckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance(), connection.out(), err); });
  // An attached file is registered under the path DuckDB recorded for it, which its
  // storage opens it (and its WAL) by: DuckDB makes a relative one absolute.
  const auto add_attached = [&](const std::string& catalog_sql, const std::string& given, const char* category) {
    std::string path;
    try {
      path = warpduckdb::QueryText(connection.get(),
                                   "SELECT path FROM duckdb_databases() WHERE database_name = " + catalog_sql);
    } catch (const std::exception&) {
    }
    private_files_->Add(path.empty() ? given : path, category, /*attached=*/true);
    if (!path.empty() && path != given) private_files_->Add(given, category, /*attached=*/true);
  };
  const auto& database = runtime_.config.database;
  if (!database.empty() && database != ":memory:") {
    add_attached("current_database()", database, "the server's database");
  }
  RegisterSqlFunctions(connection.get(), runtime_);
  AttachSystemCatalog();
  if (runtime_.config.enable_instrumentation) {
    instrumentation_ = std::make_unique<Instrumentation>(runtime_, instance(), duckdb_version());
    instrumentation_->Attach();
  }
  if (runtime_.config.enable_catalog_logging) {
    catalog_log_ = std::make_unique<CatalogLog>(runtime_, instance());
    catalog_log_->Attach();
  }
  if (instrumentation_ && !instrumentation_->file().empty()) {
    add_attached(SqlLiteral(instrumentation_->catalog()), instrumentation_->file(), "the instrumentation database");
  }
  if (catalog_log_ && !catalog_log_->file().empty()) {
    add_attached(SqlLiteral(catalog_log_->catalog()), catalog_log_->file(), "the log database");
  }
  RunInitSql();
  // Both after the init SQL, which may attach their catalogs.
  if (catalog_log_) {
    catalog_log_->Start();
    runtime_.catalog_log = catalog_log_.get();
  }
  if (instrumentation_) {
    instrumentation_->Start();
    runtime_.instrumentation = instrumentation_.get();
  }
  // Access control hides the server's own catalogs from everyone but the administrator.
  if (access_) {
    std::vector<std::string> hidden;
    if (instrumentation_) hidden.push_back(instrumentation_->catalog());
    if (catalog_log_) hidden.push_back(catalog_log_->catalog());
    access_->SetHiddenCatalogs(std::move(hidden));
  }
  // Init SQL may move DuckDB's secret directory; wherever it ends up is protected.
  sensitive_paths_.AddSecretDirectory(
      warpduckdb::QueryText(connection.get(), "SELECT current_setting('secret_directory')"));
}

Backend::~Backend() {
  StopHealthCheck();
  StopInstrumentation();
  if (catalog_log_) {
    runtime_.catalog_log = nullptr;
    catalog_log_->Stop();
  }
  if (!system_catalog_dir_.empty()) {
    std::error_code ec;
    std::filesystem::remove_all(system_catalog_dir_, ec);
  }
}

void Backend::AttachSystemCatalog() {
  // DuckDB can't make an in-memory catalog read-only once it holds the views, so they
  // are written to a small private file that is then attached READ_ONLY: every write to
  // the system catalog is refused by DuckDB itself, for every user.
  std::string base;
  try {
    base = MakePrivateTempDirectory("hardcharger-system-");
  } catch (const std::exception&) {
    throw std::runtime_error("cannot create a directory for the system catalog");
  }
  system_catalog_dir_ = base;
  runtime_.scratch_dir = base;
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

void Backend::StopInstrumentation() {
  if (instrumentation_) {
    instrumentation_->Stop();
    runtime_.instrumentation = nullptr;
  }
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
  info["instance_id"] = runtime_.instance_id;
  info["access_control"] = access_ != nullptr && !access_->catalog_rules_only();
  return info;
}

std::unique_ptr<warpsql::EngineSession> Backend::NewSession(const warpsql::Principal& principal,
                                                          const std::string& session_id) {
  const bool capped = runtime_.sessions.Admit(principal);
  try {
    return std::make_unique<Session>(runtime_, sensitive_paths_, *private_files_, instance(), principal, session_id,
                                     capped, access_.get());
  } catch (...) {
    runtime_.sessions.Unadmit(capped);
    throw;
  }
}

// ---- Session ----

Session::Session(Runtime& runtime, const SensitivePaths& sensitive_paths, const PrivateFiles& private_files,
                 duckdb_v2_instance_handle instance, warpsql::Principal principal, std::string id, bool capped,
                 authz::AccessControl* access)
    : DuckDBSession(instance, static_cast<std::size_t>(runtime.config.result_batch_rows)),
      runtime_(runtime),
      sensitive_paths_(sensitive_paths),
      private_files_(private_files),
      principal_(std::move(principal)),
      // A rules-only administrator without catalog_access rules is unchecked and runs no
      // access-control statements, so it needs no guard: binding it would only add the
      // plan check's per-statement work to an unrestricted session. One whose token
      // carries rules is held to them (as GizmoSQL 1.x does, whatever the role), so it is
      // bound. An RBAC administrator keeps its guard (it runs GRANT, CREATE USER, ...).
      access_controlled_(access != nullptr &&
                         !(access->catalog_rules_only() && principal_.role == "admin" &&
                           !IdentityOf(principal_).catalog_access)),
      rbac_(access != nullptr && !access->catalog_rules_only()),
      id_(std::move(id)),
      started_at_us_(NowMicros()) {
  // User connections are created in exactly one place, the DuckDBSession constructor,
  // and bound here right after it, before any statement runs on them (and before the
  // session is listed anywhere, so a refused one leaves nothing behind).
  if (access_controlled_) guard_ = access->Bind(connection(), IdentityOf(principal_));
  runtime_.sessions.Add(this, capped);
  runtime_.metrics.CountSessionOpened();
  if (runtime_.instrumentation != nullptr) runtime_.instrumentation->SessionStarted(*this);
  LogAt(runtime_.session_log_level, LogLevel::kInfo, "session opened",
        {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}});
}

Session::~Session() {
  // The guard may outlive this session briefly (the store's listener holds it while it
  // walks the sessions); it must not reach the connection once the base class closes it.
  if (guard_) guard_->Detach();
  runtime_.sessions.Remove(this);
  if (runtime_.instrumentation != nullptr) runtime_.instrumentation->SessionEnded(*this, killed_ ? "killed" : "closed");
  LogAt(runtime_.session_log_level, LogLevel::kInfo, "session closed",
        {{"session_id", id_}, {"user", principal_.name}, {"reason", killed_ ? "killed" : "closed"}});
}

void Session::CheckAlive() const {
  if (killed_) throw KilledError();
  if (!access_controlled_) return;
  if (!guard_) {
    // Fail closed: under access control, a session without a guard runs nothing.
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this request: the session is not bound to access control", "42501");
  }
  guard_->RequireLive();
}

void Session::CheckEnded() {
  // Safe from any thread: killed_ is atomic, the guard is set once and locks its own state.
  if (killed_) throw KilledError();
  if (guard_) guard_->RequireLive();
}

warpsql::StatementOutcome Session::Guarded(std::string_view sql, bool has_parameters,
                                           const std::function<warpsql::StatementOutcome()>& run) {
  return guard_ ? guard_->Execute(sql, has_parameters, run) : run();
}

void Session::FinishRunning() { running_--; }

std::string Session::PrepareProfile(const std::string& execution_id) {
  const int mode = settings().capture_query_profile.value_or(runtime_.capture_query_profile.load());
  if (mode == 0 && profile_mode_ == 0) return {};
  // The server's own settings on the session's connection: not the user's to check
  // (profiling_output names a file, which only an administrator may write).
  auto internal = [&](const std::string& sql) {
    if (guard_) {
      guard_->RunInternal([&] { warpduckdb::RunSql(connection(), sql); });
    } else {
      warpduckdb::RunSql(connection(), sql);
    }
  };
  if (mode != profile_mode_) {
    // DuckDB's profiler, per connection: JSON written to a file of the server's own.
    if (mode == 0) {
      internal("PRAGMA disable_profiling");
    } else {
      internal(std::string("SET enable_profiling = 'json'; SET profiling_mode = '") +
               (mode == 2 ? "detailed" : "standard") + "'");
    }
    profile_mode_ = mode;
  }
  if (mode == 0 || runtime_.scratch_dir.empty()) return {};
  const std::string path = runtime_.scratch_dir + "/profile-" + execution_id + ".json";
  internal("SET profiling_output = '" + path + "'");
  return path;
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
  // The statement log level (SET [GLOBAL] hardcharger.query_log_level) decides alone, so a
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
  // Under access control, what a store user may reach outside the database (files, URLs,
  // ATTACH, ...) is the access-control gate's to decide, by its system privileges; the
  // rest of the statement guard (secrets, sensitive paths) holds for everyone.
  CheckStatement({connection(), admin() || rbac_, config.block_unredacted_secrets, &sensitive_paths_, &private_files_},
                 piece, type);
  if (type == DUCKDB_V2_STATEMENT_TYPE_DETACH) {
    for (const Token& token : piece.tokens) {
      if (token.Value() == kSystemCatalog) {
        throw Error(ErrorCode::kPermissionDenied,
                    std::string("Cannot DETACH ") + kSystemCatalog + ": it is the server's system catalog", "42501");
      }
    }
  }
  // What runs decides what is a write (EXPLAIN ANALYZE INSERT ... inserts).
  const DUCKDB_V2_STATEMENT_TYPE executed = ExecutedType(connection(), piece, type);
  for (const auto& managed : ManagedCatalogs()) GuardManagedCatalog(piece, executed, managed);
  // catalog_access rules (per-catalog permissions) are access control's to
  // enforce, on each plan (rules-only access control, or under RBAC, where they grant
  // nothing).
  // PIVOT without an IN list runs as a group (a temporary enum of the values, then the
  // query): it reads, and the enum is the session's own.
  const bool pivot = executed == DUCKDB_V2_STATEMENT_TYPE_MULTI && !piece.tokens.empty() &&
                     (piece.tokens[0].IsWord("PIVOT") || piece.tokens[0].IsWord("UNPIVOT"));
  if (principal_.role == "readonly" && !ReadOnlyStatement(executed) && !pivot) {
    throw Error(ErrorCode::kPermissionDenied,
                "User '" + principal_.name + "' has a readonly session and cannot run statements that modify state.",
                "42501");
  }
  return type;
}

std::vector<Session::ManagedCatalog> Session::ManagedCatalogs() const {
  std::vector<ManagedCatalog> out;
  if (runtime_.instrumentation != nullptr) out.push_back({runtime_.instrumentation->catalog(), "instrumentation"});
  if (runtime_.catalog_log != nullptr) out.push_back({runtime_.catalog_log->catalog(), "log"});
  return out;
}

// The instrumentation and log catalogs belong to the server: only admins may read them,
// nobody may change them, detach them, make them current or put them on the search path.
// Without the statement's catalogs (the v2 C API doesn't expose them) the check reads
// the tokens: a statement naming the catalog is checked, and as it can be neither
// current nor on the search path, an unqualified name can't reach it.
void Session::GuardManagedCatalog(const Piece& piece, DUCKDB_V2_STATEMENT_TYPE type,
                                  const ManagedCatalog& managed) const {
  const std::string& catalog = managed.name;
  const std::string what = "the " + managed.label + " catalog '" + catalog + "'";
  std::string lowered = catalog;
  std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char c) { return std::tolower(c); });
  bool named = false;
  for (const Token& token : piece.tokens) {
    const bool text = token.Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL);
    if (token.Value() == lowered || (text && token.Value().find(catalog) != std::string::npos)) named = true;
  }
  if (!named) return;
  if (!admin()) {
    throw Error(ErrorCode::kPermissionDenied, "Access denied: Only administrators can read " + what + ".", "42501");
  }
  if (type == DUCKDB_V2_STATEMENT_TYPE_DETACH) {
    throw Error(ErrorCode::kPermissionDenied, "Cannot DETACH " + what + ".", "42501");
  }
  const auto& t = piece.tokens;
  const bool use = !t.empty() && t[0].IsWord("USE");
  const bool search_path = !t.empty() && (t[0].IsWord("SET") || t[0].IsWord("PRAGMA")) &&
                           std::any_of(t.begin(), t.end(), [](const Token& x) { return x.Value() == "search_path"; });
  if (use || search_path || !ReadOnlyStatement(type)) {
    throw Error(ErrorCode::kPermissionDenied, "Cannot modify " + what + ": it is read-only (Access denied).",
                "42501");
  }
}

warpsql::StatementOutcome Session::RunCommand(const Piece& piece) {
  if (auto setting = ParseSettingCommand(piece)) {
    ApplySetting(*this, *setting);
    return {};
  }
  const std::string target = ParseKillSession(piece).value_or("");
  if (!admin()) {
    throw Error(ErrorCode::kPermissionDenied, "Only admin users can execute KILL SESSION", "42501");
  }
  // A restricted administrator (an admin token with catalog_access rules) sees only its
  // own session, so it has no other session to name; killing is for the unrestricted
  // administrator.
  if (!full_admin()) {
    throw Error(ErrorCode::kPermissionDenied,
                "KILL SESSION needs an administrator without catalog_access rules", "42501");
  }
  if (target.empty()) {
    throw Error(ErrorCode::kSqlError, "KILL SESSION needs a session id: KILL SESSION '<session id>' (HARDCHARGER_CURRENT_SESSION(), hardcharger_sessions())", "42601");
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

warpsql::StatementOutcome Session::Track(StatementKind kind, std::string_view sql, const std::string& statement_id,
                                         const std::function<warpsql::StatementOutcome()>& work) {
  const auto started = std::chrono::steady_clock::now();
  Instrumentation* const instrumentation = statement_id.empty() ? nullptr : runtime_.instrumentation;
  ExecutionRecord record;
  if (instrumentation != nullptr) {
    record.execution_id = RandomUuid();
    record.statement_id = statement_id;
  }
  AdmissionSlot slot;
  AdmissionController& queue = runtime_.admission;
  if (queue.Limit() > 0) {
    const auto mine = settings();
    // Admins skip the queue unless they asked not to; nobody else can skip it.
    const bool bypass = admin() && mine.bypass_queue.value_or(runtime_.config.admin_bypass_queue_default);
    if (bypass) runtime_.metrics.CountQueueBypass();
    if (!bypass && instrumentation != nullptr) {
      // Recorded while it waits ('queued'), so a monitor sees what is queued.
      record.enqueue_time = record.start = std::chrono::system_clock::now();
      instrumentation->ExecutionQueued(record);
    }
    try {
      slot = queue.Acquire(!bypass, mine.max_queue_wait.value_or(queue.DefaultMaxQueueWaitSeconds()),
                           [this] { return killed_.load() || interrupted_.load(); }, std::chrono::milliseconds(100));
    } catch (const AdmissionController::Refused& refused) {
      runtime_.metrics.CountQueueRejected();
      const bool cancelled = killed_ || refused.reason == AdmissionController::Refusal::kAborted;
      if (record.enqueue_time) {
        record.end = record.cursor_close = std::chrono::system_clock::now();
        record.status = cancelled ? "cancelled" : "error";
        record.error = cancelled ? "cancelled while queued" : refused.message;
        instrumentation->ExecutionFinished(record);
      }
      if (killed_) throw KilledError();
      if (cancelled) {
        throw Error(ErrorCode::kStatementCancelled, "INTERRUPT Error: the statement was cancelled while queued",
                    "57014");
      }
      throw Error(ErrorCode::kRateLimited, refused.message);
    }
    runtime_.metrics.ObserveQueueWait(
        std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
  }

  running_++;
  // The execution clock starts when it leaves the queue.
  if (instrumentation != nullptr) record.start = std::chrono::system_clock::now();
  std::string profile;
  if (!record.execution_id.empty()) profile = PrepareProfile(record.execution_id);
  auto execution = std::make_shared<Execution>(*this, kind, Abbreviate(sql), std::move(slot), started, std::move(record),
                                               std::move(profile));
  warpsql::StatementOutcome outcome;
  try {
    outcome = work();
  } catch (const Error& error) {
    const std::string_view error_class = ClassifyStatementError(error.what());
    const std::string logged_error = LoggableError(sql, error.what());
    execution->End(error.code() == ErrorCode::kStatementCancelled ? "cancelled" : "error", error_class, logged_error);
    LogQuery(LogLevel::kInfo, "statement failed",
             {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"status", "error"},
              {"error", logged_error}, {"sql", Abbreviate(sql)}});
    if (killed_) throw KilledError();
    throw;
  }
  execution->Produced();
  if (outcome.rows) {
    outcome.rows = std::make_shared<TrackedReader>(std::move(outcome.rows), std::move(execution),
                                                   [this](const arrow::Status& status) {
      if (killed_) throw KilledError();
      if (interrupted_ && ClassifyStatementError(status.message()) == "cancelled") {
        throw Error(ErrorCode::kStatementCancelled, status.message(), "57014");
      }
    });
  } else {
    execution->End("ok", "", "");
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
  if (!rbac_) RefuseAccessControlStatement(sql);
  const bool has_parameters = !parameters.is_null();
  // An access-control statement (GRANT, CREATE USER, ...) is the guard's, text and all:
  // it runs one at a time, and the guard refuses anything riding along.
  if (rbac_ && authz::LooksLikeAccessControlStatement(sql)) {
    const std::string statement_id =
        runtime_.instrumentation != nullptr
            ? runtime_.instrumentation->StatementCreated(*this, Abbreviate(sql), "statement", true, "")
            : std::string();
    if (runtime_.config.print_queries) {
      LogQuery(LogLevel::kInfo, "statement",
               {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}, {"kind", "sql"},
                {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
    }
    return Track(StatementKind::kOther, Abbreviate(sql), statement_id, [&] {
      return Guarded(sql, has_parameters, [&] { return DuckDBSession::Execute(sql, parameters); });
    });
  }
  const auto pieces = SplitStatements(connection(), sql);
  if (pieces.empty()) {
    return Guarded(sql, has_parameters, [&] { return DuckDBSession::Execute(sql, parameters); });
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
      if (!step.command && rbac_ && authz::LooksLikeAccessControlStatement(piece.text)) {
        // As DuckDB sees the batch: one it can't parse fails as the syntax error it is;
        // one it would take for something else (SHOW GRANTS, RESET ROLE) is refused.
        StatementType(connection(), piece.text);
        throw Error(ErrorCode::kSqlError,
                    "Hardcharger access control: access-control statements run on their own, "
                    "not in a batch with other statements",
                    "42601");
      }
      if (!step.command) step.type = Authorize(piece);
      steps.push_back(step);
    }
  } catch (const Error& error) {
    // Refused or unparseable: a failed statement all the same, for metrics and the record.
    runtime_.metrics.RecordStatement(StatementKind::kOther, "error", ClassifyStatementError(error.what()), 0);
    if (runtime_.instrumentation != nullptr) {
      runtime_.instrumentation->StatementCreated(*this, Abbreviate(sql), "statement", false, LoggableError(sql, error.what()));
    }
    throw;
  }
  const std::string statement_id =
      runtime_.instrumentation != nullptr
          ? runtime_.instrumentation->StatementCreated(*this, Abbreviate(sql), "statement", true, "")
          : std::string();
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "statement",
          {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}, {"kind", "sql"},
           {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
  }

  if (steps.size() == 1 && !steps[0].command) {
    auto outcome = Track(ClassifyStatement(steps[0].type), sql, statement_id, [&] {
      return Guarded(sql, has_parameters, [&] { return DuckDBSession::Execute(sql, parameters); });
    });
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
    } else {
      const std::string_view text = step.piece->text;
      outcome = Track(step.type == DUCKDB_V2_STATEMENT_TYPE_TRANSACTION ? StatementKind::kOther
                                                                       : ClassifyStatement(step.type),
                      text, statement_id, [&] {
                        return Guarded(text, has_parameters,
                                       [&] { return DuckDBSession::Execute(text, parameters); });
                      });
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
  auto record = [&](bool accepted, std::string_view error) {
    return runtime_.instrumentation != nullptr
               ? runtime_.instrumentation->StatementCreated(*this, Abbreviate(sql), "prepared", accepted, error)
               : std::string();
  };
  std::unique_ptr<warpsql::PreparedStatement> prepared;
  try {
    if (!rbac_) RefuseAccessControlStatement(sql);
    const bool access_control = rbac_ && authz::LooksLikeAccessControlStatement(sql);
    if (pieces.size() == 1 && !access_control) {
      if (ParseSettingCommand(pieces[0]) || ParseKillSession(pieces[0])) {
        throw Error(ErrorCode::kSqlError, "SET hardcharger.* and KILL SESSION can't be prepared; run them directly");
      }
      Authorize(pieces[0]);
    }
    if (runtime_.config.print_queries) {
      LogQuery(LogLevel::kInfo, "prepare",
               {{"session_id", id_}, {"user", principal_.name}, {"kind", "sql"}, {"is_internal", "false"},
                {"sql", Abbreviate(sql)}});
    }
    prepared = guard_ ? guard_->Prepare(sql, [&] { return DuckDBSession::Prepare(sql); })
                      : DuckDBSession::Prepare(sql);
  } catch (const Error& error) {
    record(false, LoggableError(sql, error.what()));
    throw;
  }
  prepared_statement_ids_[prepared.get()] = record(true, "");
  return prepared;
}

warpsql::StatementOutcome Session::ExecutePrepared(warpsql::PreparedStatement& statement,
                                                   const warpsql::Parameters& parameters) {
  CheckAlive();
  SessionScope scope(this);
  interrupted_ = false;
  const auto id = prepared_statement_ids_.find(&statement);
  return Track(StatementKind::kOther, "(prepared statement)",
               id == prepared_statement_ids_.end() ? std::string() : id->second, [&] {
                 if (!guard_) return DuckDBSession::ExecutePrepared(statement, parameters);
                 const bool has_parameters = parameters.rows != nullptr || !parameters.json.is_null();
                 return guard_->ExecutePrepared(statement, has_parameters, [&](warpsql::PreparedStatement& inner) {
                   return DuckDBSession::ExecutePrepared(inner, parameters);
                 });
               });
}

int64_t Session::Ingest(const warpsql::IngestTarget& target, std::shared_ptr<arrow::RecordBatchReader> rows) {
  CheckAlive();
  SessionScope scope(this);
  if (principal_.role == "readonly") {
    throw Error(ErrorCode::kPermissionDenied,
                "User '" + principal_.name + "' has a readonly session and cannot ingest data.", "42501");
  }
  const LogFields where = {{"session_id", id_},       {"user", principal_.name},  {"kind", "ingest"},
                           {"catalog", target.catalog}, {"schema", target.schema}, {"table", target.table}};
  auto with = [&](LogFields extra) {
    LogFields fields = where;
    fields.insert(fields.end(), extra.begin(), extra.end());
    return fields;
  };
  const auto started = std::chrono::steady_clock::now();
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "ingest", with({{"status", "attempt"}}));
  }
  // Under access control: one INSERT or CREATE TABLE AS on this session's connection,
  // checked like any other.
  const int64_t ingested =
      guard_ ? guard_->Ingest(target, [&] { return DuckDBSession::Ingest(target, std::move(rows)); })
             : DuckDBSession::Ingest(target, std::move(rows));
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
  auto rows = guard_ ? guard_->Metadata(
                          kind, filter,
                          [&](const warpsql::MetadataFilter& narrowed) { return DuckDBSession::Metadata(kind, narrowed); },
                          [&](std::shared_ptr<arrow::RecordBatchReader> tables) {
                            return TablesWithSchemas(std::move(tables));
                          })
                     : DuckDBSession::Metadata(kind, filter);
  // The server's own catalogs are invisible to non-admins in the catalog, schema and
  // table lists (their first column is the catalog).
  const bool listing = kind == warpsql::MetadataKind::kCatalogs || kind == warpsql::MetadataKind::kDbSchemas ||
                       kind == warpsql::MetadataKind::kTables;
  const auto managed = ManagedCatalogs();
  if (admin() || managed.empty() || !listing) return rows;
  std::vector<std::string> hidden;
  for (const auto& catalog : managed) hidden.push_back(catalog.name);
  return WithoutCatalogs(std::move(rows), hidden);
}

warpsql::OptionErrors Session::SetOptions(const json::object& options) {
  CheckAlive();
  SessionScope scope(this);
  if (const auto* catalog = options.if_contains("catalog"); catalog && catalog->is_string()) {
    for (const auto& managed : ManagedCatalogs()) {
      if (catalog->as_string() != managed.name) continue;
      json::object rest = options;
      rest.erase("catalog");
      auto errors = DuckDBSession::SetOptions(rest);
      errors.emplace("catalog", Error(ErrorCode::kPermissionDenied,
                                      "the " + managed.label + " catalog can't be the session's catalog (read access "
                                      "to catalog '" + managed.name + "' is for administrators querying it by name)"));
      return errors;
    }
  }
  auto errors = DuckDBSession::SetOptions(options);
  if (guard_) {
    // A restricted user's {"catalog": "other_tenants_db"} says no more than a missing
    // catalog would: mask only an error that names a catalog it may not see, and keep
    // every other error (unknown option, bad value) as the SDK reported it.
    for (auto& [name, error] : errors) {
      const Error hidden = guard_->ForTenant(error);
      if (std::string_view(hidden.what()) != std::string_view(error.what())) {
        error = Error(ErrorCode::kInvalidOptionValue, hidden.what(), hidden.sqlstate());
      }
    }
  }
  return errors;
}

json::object Session::Options() {
  CheckAlive();
  return DuckDBSession::Options();
}

void Session::Commit() {
  CheckAlive();
  DuckDBSession::Commit();
}

void Session::Rollback() {
  CheckAlive();
  DuckDBSession::Rollback();
}

// The SDK's DuckDB session follows the transaction statements that ran and asks DuckDB
// whether an open transaction is aborted (DuckDB forgives a syntax error, not a missing
// table); the SDK reads this after every call that ran work in the session, for the
// session document (Warp SQL §5.2), and the metrics read the copy kept here.
warpsql::TransactionState Session::Transaction() {
  const warpsql::TransactionState state = DuckDBSession::Transaction();
  transaction_ = state;
  return state;
}

}  // namespace hardcharger
