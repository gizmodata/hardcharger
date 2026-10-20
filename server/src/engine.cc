#include "engine.hpp"

#include <arrow/record_batch.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <regex>
#include <set>
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
#include "version.hpp"
#include "warpsql/server.hpp"

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

// Statements that read: what may run on the server's own catalogs.
bool ReadOnlyStatement(DUCKDB_V2_STATEMENT_TYPE type) {
  return type == DUCKDB_V2_STATEMENT_TYPE_SELECT || type == DUCKDB_V2_STATEMENT_TYPE_EXPLAIN;
}

// PRAGMAs that only read (DuckDB runs each as a query of the pragma_* table function).
bool IsReadingPragma(const Piece& piece) {
  const auto& t = piece.tokens;
  if (t.size() < 2 || !t[0].IsWord("PRAGMA")) return false;
  if (t.size() > 2 && t[2].IsOp("=")) return false;  // PRAGMA name = value is a SET
  static const std::set<std::string> kReading = {
      "table_info", "show",     "show_tables", "show_tables_expanded", "show_databases", "database_list",
      "database_size", "storage_info", "metadata_info", "version", "platform", "functions", "collations",
      "user_agent"};
  return kReading.contains(t[1].Value());
}

// The kinds of statement a "readonly" principal may run, as GizmoSQL 1.x allowed them: reads,
// and statements that change only its own session (transactions, SET VARIABLE, session
// settings, USE, reading PRAGMAs). What each of them really does is then checked by DuckDB's
// own analysis (authz::BindReadOnlySession): a write anywhere in a SELECT, a SET VARIABLE's
// value or a transaction is refused all the same. Everything else (CREATE, temporary objects
// included; DROP, ALTER, INSERT, UPDATE, DELETE, MERGE, COPY, EXPORT, IMPORT, ATTACH, DETACH,
// INSTALL, LOAD, CHECKPOINT, VACUUM, CALL, SQL-level PREPARE / EXECUTE) is refused.
bool ReadOnlySessionMayRun(duckdb_v2_connection_handle connection, const Piece& piece,
                           DUCKDB_V2_STATEMENT_TYPE type) {
  switch (type) {
    case DUCKDB_V2_STATEMENT_TYPE_SELECT:
    case DUCKDB_V2_STATEMENT_TYPE_EXPLAIN:
    case DUCKDB_V2_STATEMENT_TYPE_TRANSACTION:   // BEGIN, COMMIT, ROLLBACK, ABORT, END
    case DUCKDB_V2_STATEMENT_TYPE_VARIABLE_SET:  // SET VARIABLE, RESET VARIABLE
      return true;
    case DUCKDB_V2_STATEMENT_TYPE_SET:  // SET / RESET of a setting, USE
      return authz::ReadOnlySessionMaySet(connection, piece.text);
    case DUCKDB_V2_STATEMENT_TYPE_PRAGMA:
      return IsReadingPragma(piece);
    default:
      return false;
  }
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
-- The SQL standard's privilege views, with its column names, over access control's table
-- functions: in a schema of their own, since DuckDB's information_schema can't be
-- extended, and named so they never shadow it (whatever the session's catalog).
CREATE SCHEMA {catalog}.hardcharger_information_schema;
CREATE VIEW {catalog}.hardcharger_information_schema.table_privileges AS {table_privileges};
CREATE VIEW {catalog}.hardcharger_information_schema.column_privileges AS
WITH tp AS ({table_privileges}),
cols AS (SELECT database_name AS c, schema_name AS s, table_name AS t, column_name AS col
         FROM duckdb_columns() WHERE NOT internal)
SELECT tp.grantor, tp.grantee, cols.c AS table_catalog, cols.s AS table_schema, cols.t AS table_name,
       cols.col AS column_name, tp.privilege_type, tp.is_grantable
FROM tp JOIN cols ON cols.c = tp.table_catalog AND cols.s = tp.table_schema AND cols.t = tp.table_name
WHERE tp.privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
UNION
SELECT g.grantor, g.grantee, cols.c, cols.s, cols.t, cols.col, g.privilege,
       CASE WHEN g.with_grant_option THEN 'YES' ELSE 'NO' END
FROM hardcharger_grants() g
JOIN cols ON lower(cols.c) = lower(g.catalog_name) AND lower(cols.s) = lower(g.schema_name)
         AND lower(cols.t) = lower(g.object_name) AND lower(cols.col) = lower(g.column_name)
WHERE g.column_name IS NOT NULL;
CREATE VIEW {catalog}.hardcharger_information_schema.role_table_grants AS
SELECT * FROM ({table_privileges})
WHERE grantee IN (SELECT name FROM hardcharger_roles() WHERE is_active);
CREATE VIEW {catalog}.hardcharger_information_schema.applicable_roles AS
SELECT member AS grantee, role AS role_name, CASE WHEN admin_option THEN 'YES' ELSE 'NO' END AS is_grantable
FROM hardcharger_role_members() WHERE member_type IN ('USER', 'ROLE');
CREATE VIEW {catalog}.hardcharger_information_schema.enabled_roles AS
SELECT name AS role_name FROM hardcharger_roles() WHERE is_active;
)SQL";

// The SQL standard's TABLE_PRIVILEGES from hardcharger_grants(): one row per privilege on
// each table or view that exists, a schema's or catalog's grant expanded to every table and
// view in it, ALL to the four table privileges. Ownership, column-level and system grants
// are not table privileges (column-level ones are in column_privileges).
constexpr const char* kTablePrivileges = R"SQL(
WITH objects AS (
  SELECT database_name AS c, schema_name AS s, table_name AS t FROM duckdb_tables() WHERE NOT internal
  UNION ALL
  SELECT database_name, schema_name, view_name FROM duckdb_views() WHERE NOT internal
), grants AS (
  SELECT * FROM (
    SELECT g.*, unnest(CASE WHEN g.privilege = 'ALL' THEN ['SELECT', 'INSERT', 'UPDATE', 'DELETE']
                            ELSE [g.privilege] END) AS privilege_type
    FROM hardcharger_grants() g
    WHERE g.object_type IN ('CATALOG', 'SCHEMA', 'TABLE', 'VIEW') AND g.column_name IS NULL)
  WHERE privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
)
SELECT DISTINCT g.grantor, g.grantee, o.c AS table_catalog, o.s AS table_schema, o.t AS table_name,
       g.privilege_type, CASE WHEN g.with_grant_option THEN 'YES' ELSE 'NO' END AS is_grantable,
       'NO' AS with_hierarchy
FROM grants g
JOIN objects o ON lower(o.c) = lower(g.catalog_name)
 AND (g.object_type = 'CATALOG'
      OR (g.object_type = 'SCHEMA' AND (lower(o.s) = lower(g.schema_name)
                                        OR starts_with(lower(o.s), lower(g.schema_name) || '.')))
      OR (g.object_type IN ('TABLE', 'VIEW') AND lower(o.s) = lower(g.schema_name)
          AND lower(o.t) = lower(g.object_name)))
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
  // `recorded`: in instrumentation; `ids`: the log fields naming it.
  Execution(Session& session, StatementKind kind, std::string sql, AdmissionSlot slot,
            std::chrono::steady_clock::time_point started, ExecutionRecord record, bool recorded, LogFields ids,
            std::string profile)
      : session_(session),
        kind_(kind),
        sql_(std::move(sql)),
        slot_(std::move(slot)),
        started_(started),
        record_(std::move(record)),
        recorded_(recorded),
        ids_(std::move(ids)),
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
    if (status == "cancelled") record_.cancel_reason = session_.cancel_reason();
    slot_.Release();
    session_.FinishRunning();
    Runtime& runtime = session_.runtime();
    if (status == "ok" && runtime.config.print_queries) {
      LogFields fields = {{"session_id", session_.id()}, {"user", session_.principal().name}};
      fields.insert(fields.end(), ids_.begin(), ids_.end());
      fields.insert(fields.end(), {{"kind", "sql"}, {"status", "success"}, {"is_internal", "false"}, {"sql", sql_}});
      session_.LogQuery(LogLevel::kInfo, "Client SQL command execution succeeded", fields);
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
  Instrumentation* Instr() const { return recorded_ ? session_.runtime().instrumentation : nullptr; }

  Session& session_;
  const StatementKind kind_;
  const std::string sql_;
  AdmissionSlot slot_;
  const std::chrono::steady_clock::time_point started_;
  ExecutionRecord record_;
  const bool recorded_;
  const LogFields ids_;
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

std::shared_ptr<arrow::RecordBatch> Session::AccessTableRows(authz::AccessTable table,
                                                             const std::optional<std::string>& user_name) const {
  if (!rbac_ || !guard_) return authz::EmptyAccessTable(table);
  return guard_->AccessTableRows(table, user_name);
}

// ---- Backend ----

Backend::Backend(Runtime& runtime, std::shared_ptr<authz::AccessControl> access)
    : DuckDBBackend(EngineOptions(runtime.config)),
      runtime_(runtime),
      access_(std::move(access)),
      sensitive_paths_(std::make_shared<SensitivePaths>(
          runtime.config.block_sensitive_paths, runtime.config.sensitive_paths,
          std::vector<std::string>{runtime.config.tls_key, runtime.config.tls_cert, runtime.config.mtls_ca_cert,
                                   runtime.config.init_sql_commands_file, runtime.config.config_file})),
      private_files_(std::make_shared<PrivateFiles>()) {
  // Before anything else runs on the instance: from here on every plan of a bound
  // session is checked. (The server's own connections below, and those of
  // instrumentation, catalog logging and the health check, carry no principal, so the
  // check leaves them alone.)
  if (access_) access_->InstallEnforcement(instance());
  // And no statement opens the server's own files: a DuckDB access-control store, and
  // the files this instance attaches (each added once it is attached, below); nor, once
  // startup is over (below), a sensitive path.
  if (access_) private_files_->Add(access_->store_file(), "the access-control store", /*attached=*/false);
  authz::InstallFileGuard(instance(), private_files_, sensitive_paths_);
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
  runtime_.instance_info =
      CollectInstanceInfo(runtime_.config, runtime_.instance_id, duckdb_version(), runtime_.started_at);
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
  sensitive_paths_->AddSecretDirectory(
      warpduckdb::QueryText(connection.get(), "SELECT current_setting('secret_directory')"));
  // From here on the file system refuses the sensitive paths to every statement. The
  // startup above (the server's own attaches and the operator's init SQL) is the
  // operator's configuration, as it always was: an init SQL may read a mounted secret.
  sensitive_paths_->Activate();
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
  warpduckdb::RunSql(connection.get(), Replace(Replace(kSystemViews, "{table_privileges}", kTablePrivileges), "{catalog}",
                                               "_hardcharger_system_build"));
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
  // Hardcharger's own keys take its prefix (Warp SQL §2.7).
  info["hardcharger_instance_id"] = runtime_.instance_id;
  info["hardcharger_access_control"] = access_ != nullptr && !access_->catalog_rules_only();
  return info;
}

std::unique_ptr<warpsql::EngineSession> Backend::NewSession(const warpsql::Principal& principal,
                                                          const std::string& session_id) {
  const bool capped = runtime_.sessions.Admit(principal);
  try {
    return std::make_unique<Session>(runtime_, *sensitive_paths_, instance(), principal, session_id, capped,
                                     access_.get());
  } catch (...) {
    runtime_.sessions.Unadmit(capped);
    throw;
  }
}

// ---- Session ----

Session::Session(Runtime& runtime, const SensitivePaths& sensitive_paths, duckdb_v2_instance_handle instance,
                 warpsql::Principal principal, std::string id, bool capped,
                 authz::AccessControl* access)
    : DuckDBSession(instance, static_cast<std::size_t>(runtime.config.result_batch_rows)),
      runtime_(runtime),
      sensitive_paths_(sensitive_paths),
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
  if (principal_.role == "readonly") authz::BindReadOnlySession(connection(), principal_.name);
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

namespace {

// DuckDB 2.0 drops a view's own catalog from the names in its query (BindCreateViewInfo:
// RemotePushdownOptimizer::StripCatalogName, so the view keeps working under another
// alias), and binds what is left with the ambiguity check for a two-part name. In a
// catalog that has a schema of its own name, CREATE VIEW x.x.v AS SELECT * FROM x.x.t
// then fails as "Ambiguous reference to catalog or schema "x"", though the name was fully
// qualified (DuckDB 1.5 accepts it). DuckDB's message says to qualify the name, which it
// already is; this says what really happens and what works instead.
void ExplainViewAmbiguity(std::string_view sql, const Error& error) {
  const std::string_view message = error.what();
  if (message.find("Ambiguous reference to catalog or schema") == std::string_view::npos) return;
  std::string lowered(sql.substr(0, 512));
  std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char c) { return std::tolower(c); });
  static const std::regex kCreateView(R"(^\s*create\s+(or\s+replace\s+)?(temp\s+|temporary\s+)?view\b)");
  if (!std::regex_search(lowered, kCreateView)) return;
  throw Error(error.code(),
              std::string(message) +
                  "\nHardcharger: DuckDB 2.0 drops the view's own catalog from the names in its query (x.x.t is "
                  "read as x.t), so in a catalog that has a schema with the catalog's name, a fully qualified "
                  "name is ambiguous too. Create the view in another catalog (CREATE VIEW memory.main.v AS SELECT "
                  "* FROM x.x.t) or use a schema with another name.",
              error.sqlstate());
}

}  // namespace

warpsql::StatementOutcome Session::Guarded(std::string_view sql, bool has_parameters,
                                           const std::function<warpsql::StatementOutcome()>& run) {
  try {
    return guard_ ? guard_->Execute(sql, has_parameters, run) : run();
  } catch (const Error& error) {
    ExplainViewAmbiguity(sql, error);
    throw;
  }
}

void Session::FinishRunning() {
  if (--running_ <= 0) {
    std::lock_guard lock(activity_mutex_);
    activity_.statement.clear();
    activity_.statement_started_us = 0;
  }
}

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
  // What it runs was stopped by the cancel KILL SESSION runs first (CancelSessionWork):
  // this only marks the session ended, so its requests say so.
  killed_ = true;
  runtime_.admission.WakeWaiters();
  runtime_.metrics.CountSessionReaped();
  Log(LogLevel::kInfo, "session killed", {{"session_id", id_}, {"user", principal_.name}});
}

void Session::Interrupt() {
  interrupted_ = true;
  runtime_.admission.WakeWaiters();
  DuckDBSession::Interrupt();
}

void Session::Cancel(std::string_view reason) {
  {
    std::lock_guard lock(activity_mutex_);
    if (activity_.cancel_reason.empty()) activity_.cancel_reason = std::string(reason);
  }
  Interrupt();
}

void Session::BeginStatement() {
  interrupted_ = false;
  std::lock_guard lock(activity_mutex_);
  activity_.cancel_reason.clear();
}

std::string Session::cancel_reason() const {
  std::lock_guard lock(activity_mutex_);
  return activity_.cancel_reason;
}

Session::Activity Session::activity() const {
  std::lock_guard lock(activity_mutex_);
  return activity_;
}

std::string Session::tenant() const { return guard_ ? guard_->tenant_name() : std::string(); }

Session::Settings Session::settings() const {
  std::lock_guard lock(settings_mutex_);
  return settings_;
}

void Session::SetQueryTag(const json::object* tag) {
  request_query_tag_ = tag == nullptr ? std::string() : json::serialize(*tag);
}

void Session::SetStatementId(std::string_view id) { request_statement_id_ = std::string(id); }

std::string Session::RequestStatementId() const {
  return request_statement_id_.empty() ? warpsql::NewId("x") : request_statement_id_;
}

std::string Session::StatementQueryTag() const {
  return request_query_tag_.empty() ? settings().query_tag : request_query_tag_;
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
  CheckStatement({connection(), admin() || rbac_, config.block_unredacted_secrets, &sensitive_paths_}, piece, type);
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
  if (principal_.role == "readonly" && !ReadOnlySessionMayRun(connection(), piece, executed) && !pivot) {
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
  std::string wanted = target;
  std::transform(wanted.begin(), wanted.end(), wanted.begin(), [](unsigned char c) { return std::tolower(c); });
  switch (runtime_.sessions.Find(wanted, *this)) {
    case SessionRegistry::Lookup::kOwnSession:
      // 55000 (object not in prerequisite state): the session exists, it just can't be the
      // one asking. (It was 42P01, undefined table, which named the wrong problem.)
      throw Error(ErrorCode::kSqlError, "A session can't kill itself; disconnect (or close the connection) instead.",
                  "55000");
    case SessionRegistry::Lookup::kNotFound:
    case SessionRegistry::Lookup::kHidden:  // (an unrestricted administrator sees every session)
      throw Error(ErrorCode::kSqlError, "Session '" + target + "' not found", "42704");
    case SessionRegistry::Lookup::kVisible:
      break;
  }
  // Everything hardcharger_cancel() does (one implementation), then the end: the session's
  // requests get session_ended (killed), and once its work has stopped it is torn down.
  CancelSessionWork(runtime_, wanted, warpsql::SessionCancelReason::kKilled);
  if (runtime_.sessions.Kill(wanted, *this) != SessionRegistry::KillResult::kKilled) {
    throw Error(ErrorCode::kSqlError, "Session '" + target + "' not found", "42704");  // it closed meanwhile
  }
  if (runtime_.server != nullptr) runtime_.server->EndSession(wanted, KilledError());
  Log(LogLevel::kInfo, "KILL SESSION", {{"target", wanted}, {"user", principal_.name}, {"session_id", id_}});
  return {};
}

warpsql::StatementOutcome Session::Track(StatementKind kind, std::string_view sql, const ExecutionIds& ids,
                                         const std::function<warpsql::StatementOutcome()>& work,
                                         std::string_view shown) {
  const auto started = std::chrono::steady_clock::now();
  Instrumentation* const instrumentation = ids.statement.empty() ? nullptr : runtime_.instrumentation;
  ExecutionRecord record;
  record.execution_id = ids.execution;
  record.statement_id = ids.statement;
  LogFields id_fields = {{"statement_id", ids.execution}};
  if (ids.prepared) id_fields.push_back({"prepared_id", ids.statement});
  // What hardcharger_active_connections() shows, from the moment it waits in the queue.
  // (The redaction runs once: the execution's log text is the same.)
  std::string abbreviated = Abbreviate(sql);
  std::string statement_text = shown.empty() ? abbreviated : std::string(shown);
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
    if (!bypass) {
      std::lock_guard lock(activity_mutex_);
      activity_.queued_since_us = NowMicros();
      activity_.statement = statement_text;
    }
    struct Unqueued {
      Session& session;
      ~Unqueued() {
        std::lock_guard lock(session.activity_mutex_);
        session.activity_.queued_since_us = 0;
        if (session.running_ <= 0) session.activity_.statement.clear();
      }
    };
    try {
      Unqueued unqueued{*this};
      slot = queue.Acquire(!bypass, mine.max_queue_wait.value_or(queue.DefaultMaxQueueWaitSeconds()),
                           [this] { return killed_.load() || interrupted_.load(); }, std::chrono::milliseconds(100));
    } catch (const AdmissionController::Refused& refused) {
      runtime_.metrics.CountQueueRejected();
      const bool cancelled = killed_ || refused.reason == AdmissionController::Refusal::kAborted;
      if (record.enqueue_time) {
        record.end = record.cursor_close = std::chrono::system_clock::now();
        record.status = cancelled ? "cancelled" : "error";
        record.error = cancelled ? "cancelled while queued" : refused.message;
        if (cancelled) record.cancel_reason = cancel_reason();
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
  {
    std::lock_guard lock(activity_mutex_);
    activity_.statement = std::move(statement_text);
    activity_.statement_started_us = NowMicros();
  }
  // The execution clock starts when it leaves the queue.
  if (instrumentation != nullptr) record.start = std::chrono::system_clock::now();
  std::string profile;
  if (instrumentation != nullptr) profile = PrepareProfile(record.execution_id);
  auto execution = std::make_shared<Execution>(*this, kind, std::move(abbreviated), std::move(slot), started,
                                               std::move(record), instrumentation != nullptr, id_fields,
                                               std::move(profile));
  warpsql::StatementOutcome outcome;
  try {
    outcome = work();
  } catch (const Error& error) {
    const std::string_view error_class = ClassifyStatementError(error.what());
    const std::string logged_error = LoggableError(sql, error.what());
    execution->End(error.code() == ErrorCode::kStatementCancelled ? "cancelled" : "error", error_class, logged_error);
    LogFields fields = {{"session_id", id_}, {"user", principal_.name}};
    fields.insert(fields.end(), id_fields.begin(), id_fields.end());
    fields.insert(fields.end(), {{"kind", "sql"}, {"status", "error"}, {"error", logged_error}, {"sql", Abbreviate(sql)}});
    LogQuery(LogLevel::kInfo, "statement failed", fields);
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
      // A refusal raised while the rows are read (a function's, such as hardcharger_cancel()
      // naming someone else's session) is the permission_denied it is, not an execution error.
      if (status.message().starts_with("Permission Error: ")) {
        throw Error(ErrorCode::kPermissionDenied, status.message(), "42501");
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
  BeginStatement();
  if (sql.find('\0') != std::string_view::npos) {
    throw Error(ErrorCode::kSqlError, "SQL text must not contain a NUL byte", "22021");
  }
  if (!rbac_) RefuseAccessControlStatement(sql);
  const bool has_parameters = !parameters.is_null();
  // The statement's Warp SQL id (q_...): its sql_statements row, and its execution's.
  const std::string statement_id = RequestStatementId();
  // An access-control statement (GRANT, CREATE USER, ...) is the guard's, text and all:
  // it runs one at a time, and the guard refuses anything riding along.
  if (rbac_ && authz::LooksLikeAccessControlStatement(sql)) {
    if (runtime_.instrumentation != nullptr) {
      runtime_.instrumentation->StatementCreated(*this, statement_id, Abbreviate(sql), "statement", true, "");
    }
    if (runtime_.config.print_queries) {
      LogQuery(LogLevel::kInfo, "statement",
               {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role},
                {"statement_id", statement_id}, {"kind", "sql"}, {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
    }
    return Track(StatementKind::kOther, Abbreviate(sql), {.statement = statement_id, .execution = statement_id}, [&] {
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
      runtime_.instrumentation->StatementCreated(*this, statement_id, Abbreviate(sql), "statement", false,
                                                 LoggableError(sql, error.what()));
    }
    throw;
  }
  if (runtime_.instrumentation != nullptr) {
    runtime_.instrumentation->StatementCreated(*this, statement_id, Abbreviate(sql), "statement", true, "");
  }
  if (runtime_.config.print_queries) {
    LogQuery(LogLevel::kInfo, "statement",
          {{"session_id", id_}, {"user", principal_.name}, {"role", principal_.role}, {"statement_id", statement_id},
           {"kind", "sql"}, {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
  }

  // One statement: its execution has the statement's id. Several: each is numbered by
  // its place in the script (q_....1, q_....2, ...), commands included.
  if (steps.size() == 1 && !steps[0].command) {
    auto outcome = Track(ClassifyStatement(steps[0].type), sql, {.statement = statement_id, .execution = statement_id}, [&] {
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
                      text, {.statement = statement_id, .execution = statement_id + "." + std::to_string(i + 1)}, [&] {
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
  // The prepared statement's Warp SQL id (p_...), what its record and log lines carry.
  const std::string prepared_id = RequestStatementId();
  auto record = [&](bool accepted, std::string_view error) {
    if (runtime_.instrumentation != nullptr) {
      runtime_.instrumentation->StatementCreated(*this, prepared_id, Abbreviate(sql), "prepared", accepted, error);
    }
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
               {{"session_id", id_}, {"user", principal_.name}, {"prepared_id", prepared_id}, {"kind", "sql"},
                {"is_internal", "false"}, {"sql", Abbreviate(sql)}});
    }
    prepared = guard_ ? guard_->Prepare(sql, [&] { return DuckDBSession::Prepare(sql); })
                      : DuckDBSession::Prepare(sql);
  } catch (const Error& error) {
    record(false, LoggableError(sql, error.what()));
    throw;
  }
  record(true, "");
  prepared_statement_ids_[prepared.get()] = prepared_id;
  prepared_sql_[prepared.get()] = Abbreviate(sql);
  return prepared;
}

warpsql::StatementOutcome Session::ExecutePrepared(warpsql::PreparedStatement& statement,
                                                   const warpsql::Parameters& parameters) {
  CheckAlive();
  SessionScope scope(this);
  BeginStatement();
  const auto id = prepared_statement_ids_.find(&statement);
  const auto text = prepared_sql_.find(&statement);
  // Each execution is its request's statement (q_...), of the prepared statement (p_...).
  return Track(StatementKind::kOther, "(prepared statement)",
               {.statement = id == prepared_statement_ids_.end() ? std::string() : id->second,
                .execution = RequestStatementId(),
                .prepared = true},
               [&] {
                 if (!guard_) return DuckDBSession::ExecutePrepared(statement, parameters);
                 const bool has_parameters = parameters.rows != nullptr || !parameters.json.is_null();
                 return guard_->ExecutePrepared(statement, has_parameters, [&](warpsql::PreparedStatement& inner) {
                   return DuckDBSession::ExecutePrepared(inner, parameters);
                 });
               },
               text == prepared_sql_.end() ? std::string_view() : std::string_view(text->second));
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
  BeginStatement();
  {
    std::lock_guard lock(activity_mutex_);
    std::string into;
    for (const std::string* part : {&target.catalog, &target.schema}) {
      if (!part->empty()) into += SqlIdentifier(*part) + ".";
    }
    activity_.statement = "(bulk ingest into " + into + SqlIdentifier(target.table) + ")";
    activity_.statement_started_us = NowMicros();
    activity_.ingesting = true;
  }
  struct Ingested {
    Session& session;
    ~Ingested() {
      std::lock_guard lock(session.activity_mutex_);
      session.activity_.ingesting = false;
      if (session.running_ <= 0) {
        session.activity_.statement.clear();
        session.activity_.statement_started_us = 0;
      }
    }
  } ingested_scope{*this};
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
  if (const auto* tag = options.if_contains("session_tag"); tag != nullptr && !errors.contains("session_tag")) {
    std::lock_guard lock(activity_mutex_);
    activity_.session_tag_option = tag->is_null() ? std::string() : json::serialize(*tag);
  }
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
