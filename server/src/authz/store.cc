#include "authz/store.hpp"

#include <openssl/rand.h>

#include <algorithm>
#include <boost/json.hpp>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <duckdb.hpp>
#include <duckdb/common/file_open_flags.hpp>
#include <duckdb/common/local_file_system.hpp>
#include <duckdb/common/virtual_file_system.hpp>
#include <filesystem>
#include <mutex>
#include <random>
#include <regex>
#include <set>
#include <string_view>
#include <thread>

#include "authz/authz.hpp"
#include "authz/duckdb_internal.hpp"
#include "redact.hpp"

#ifndef _WIN32
#include <sys/stat.h>
#endif

namespace hardcharger::authz {

namespace dd = duckdb;

// The store's DuckDB connection, which only a thread of its own uses, so that a store that
// stops answering holds no caller longer than the store timeout. A PostgreSQL that is
// paused, overloaded or cut off without a reset leaves libpq waiting in recv(), and
// nothing in DuckDB can interrupt that. So a caller that has waited the timeout is told
// the store is unavailable, and the statement is left to this thread. Whatever it was part
// of is then cancelled: when the statement returns, the thread rolls back the open
// transaction before it runs anything else, so a change a caller was told failed never
// commits (a COMMIT already sent may still land, and the caller is told so). The thread
// only ever holds what a statement owns (its SQL and values), never a caller's data, and
// keeps the DuckDB instance alive for as long as it runs.
class StoreLink {
 public:
  using Work = std::function<dd::unique_ptr<dd::QueryResult>(dd::Connection&)>;

  StoreLink(std::unique_ptr<dd::DuckDB> db, std::chrono::milliseconds timeout)
      : state_(std::make_shared<State>()), timeout_(timeout) {
    state_->db = std::move(db);
    state_->conn = std::make_unique<dd::Connection>(*state_->db);
    thread_ = std::thread([state = state_] { Loop(*state); });
  }

  ~StoreLink() {
    std::unique_lock lock(state_->mutex);
    state_->stopping = true;
    state_->cv.notify_all();
    // A thread still waiting on a store that hangs is left behind: it owns all it uses.
    const bool finished =
        state_->cv.wait_for(lock, timeout_, [this] { return state_->finished; });
    lock.unlock();
    if (finished) {
      thread_.join();
    } else {
      thread_.detach();
    }
  }

  StoreLink(const StoreLink&) = delete;
  StoreLink& operator=(const StoreLink&) = delete;

  std::chrono::milliseconds timeout() const { return timeout_; }

  // Whether a statement has run for the whole timeout (one a caller gave up on included):
  // the store doesn't answer.
  bool Hung() const {
    std::lock_guard lock(state_->mutex);
    return state_->busy && std::chrono::steady_clock::now() - state_->busy_since >= timeout_;
  }

  // Runs `work` on the store's thread, waiting at most the timeout. Ok: its result.
  arrow::Result<dd::unique_ptr<dd::QueryResult>> Call(Work work) {
    auto task = std::make_shared<Task>();
    task->work = std::move(work);
    std::unique_lock lock(state_->mutex);
    const auto now = std::chrono::steady_clock::now();
    // A statement that has hung for the whole timeout already: no use queueing behind it.
    if (state_->busy && now - state_->busy_since >= timeout_) return TimedOut();
    state_->queue.push_back(task);
    state_->cv.notify_all();
    if (!state_->cv.wait_until(lock, now + timeout_, [&] { return task->done; })) {
      task->abandoned = true;
      state_->rollback_pending = true;  // whatever transaction it was part of is cancelled
      state_->cv.notify_all();
      return TimedOut();
    }
    if (!task->error.empty()) {
      return arrow::Status::IOError("Hardcharger access-control store: statement failed: " + task->error);
    }
    return std::move(task->result);
  }

  arrow::Status TimedOut() const {
    const auto ms = timeout_.count();
    std::string seconds = std::to_string(ms / 1000);
    if (ms % 1000 != 0) seconds += "." + std::to_string(ms % 1000 / 100);
    return arrow::Status::IOError("Hardcharger ", kStoreUnavailable, ": timed out after ", seconds,
                                  " s (rbac_store_timeout_seconds)");
  }

 private:
  struct Task {
    Work work;
    bool done = false;
    bool abandoned = false;
    dd::unique_ptr<dd::QueryResult> result;
    std::string error;
  };
  struct State {
    std::unique_ptr<dd::DuckDB> db;
    std::unique_ptr<dd::Connection> conn;
    std::mutex mutex;
    std::condition_variable cv;
    std::deque<std::shared_ptr<Task>> queue;
    bool stopping = false;
    bool finished = false;
    bool rollback_pending = false;
    bool busy = false;  // running a statement (since busy_since)
    std::chrono::steady_clock::time_point busy_since;
    ~State() {
      conn.reset();
      db.reset();
    }
  };

  static void Loop(State& state) {
    std::unique_lock lock(state.mutex);
    while (true) {
      state.cv.wait(lock, [&] { return state.stopping || state.rollback_pending || !state.queue.empty(); });
      if (state.rollback_pending) {
        // A caller gave up on a statement of this connection's transaction: undo it
        // before anything else runs (outside a transaction this only errs).
        state.rollback_pending = false;
        state.busy = true;
        state.busy_since = std::chrono::steady_clock::now();
        lock.unlock();
        try {
          (void)state.conn->Query("ROLLBACK");
        } catch (const std::exception&) {
        }
        lock.lock();
        state.busy = false;
        continue;
      }
      if (state.stopping) break;
      auto task = std::move(state.queue.front());
      state.queue.pop_front();
      if (task->abandoned) continue;
      state.busy = true;
      state.busy_since = std::chrono::steady_clock::now();
      lock.unlock();
      dd::unique_ptr<dd::QueryResult> result;
      std::string error;
      try {
        result = task->work(*state.conn);
      } catch (const std::exception& e) {
        error = e.what();
        if (error.empty()) error = "unknown error";
      }
      lock.lock();
      state.busy = false;
      if (!task->abandoned) {
        task->result = std::move(result);
        task->error = std::move(error);
        task->done = true;
        state.cv.notify_all();
      } else {
        lock.unlock();
        result.reset();
        lock.lock();
      }
    }
    state.finished = true;
    state.cv.notify_all();
  }

  std::shared_ptr<State> state_;
  std::chrono::milliseconds timeout_;
  std::thread thread_;
};

bool IsStoreUnavailable(const arrow::Status& status) {
  return !status.ok() && status.message().find(kStoreUnavailable) != std::string::npos;
}

namespace {

namespace compat = hardcharger::authz::ddb;

constexpr const char* kSchema = "rbac_store.hardcharger_rbac";

std::string T(const char* table) { return std::string(kSchema) + "." + table; }

std::string Lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
  return text;
}

// libpq's and the postgres scanner's ways of saying the server is not there (as
// opposed to refusing a statement).
bool LooksUnreachable(const std::string& message) {
  for (const char* text :
       {"Unable to connect to Postgres", "Connection refused", "could not connect to server",
        "connection to server", "server closed the connection", "no connection to the server",
        "terminating connection", "could not receive data from server", "could not send data to server",
        "SSL SYSCALL error", "SSL connection has been closed", "timeout expired", "Connection timed out",
        "canceling statement due to statement timeout", "Operation timed out", "Network is unreachable",
        "No route to host", "could not translate host name"}) {
    if (message.find(text) != std::string::npos) return true;
  }
  return false;
}

arrow::Status StoreError(const std::string& what, const std::string& detail) {
  if (LooksUnreachable(detail)) {
    return arrow::Status::IOError(std::string("Hardcharger ") + kStoreUnavailable + ": " + what + ": " + detail);
  }
  return arrow::Status::IOError("Hardcharger access-control store: " + what + ": " + detail);
}

// A statement's error as Call() reports it, refiled by StoreError.
arrow::Status Refile(const arrow::Status& status, const std::string& what) {
  constexpr std::string_view kPrefix = "Hardcharger access-control store: statement failed: ";
  const std::string& message = status.message();
  if (message.rfind(kPrefix, 0) != 0) return status;  // a timeout, already filed
  return StoreError(what, message.substr(kPrefix.size()));
}

arrow::Result<dd::unique_ptr<dd::QueryResult>> Query(StoreLink& conn, const std::string& sql) {
  auto result = conn.Call([sql](dd::Connection& c) -> dd::unique_ptr<dd::QueryResult> { return c.Query(sql); });
  if (!result.ok()) return Refile(result.status(), "statement failed");
  return result;
}

arrow::Status Exec(StoreLink& conn, const std::string& sql) {
  ARROW_ASSIGN_OR_RAISE(auto result, Query(conn, sql));
  if (result->HasError()) return StoreError("statement failed", result->GetError());
  return arrow::Status::OK();
}

// A store query's rows. Cells are read through one row view of the result: DuckDB's
// ColumnDataCollection::GetValue builds a view of every row for each cell it reads, which
// made reading the store quadratic in its rows (5,000 owners: 1.5 s for one load, and each
// CREATE TABLE loaded twice).
class StoreRows {
 public:
  explicit StoreRows(dd::unique_ptr<dd::QueryResult> result)
      : result_(std::move(result)), rows_(result_->Collection().GetRows()) {}
  dd::idx_t size() const { return rows_.size(); }
  dd::Value GetValue(dd::idx_t col, dd::idx_t row) const { return rows_.GetValue(col, row); }

 private:
  dd::unique_ptr<dd::QueryResult> result_;
  dd::ColumnDataRowCollection rows_;
};

dd::idx_t RowCount(const StoreRows& rows) { return rows.size(); }

arrow::Result<std::unique_ptr<StoreRows>> Run(StoreLink& conn, const std::string& sql,
                                              dd::vector<dd::Value> params = {}) {
  auto result = conn.Call([sql, params = std::move(params)](dd::Connection& c) mutable -> dd::unique_ptr<dd::QueryResult> {
    auto stmt = c.Prepare(sql);
    if (stmt->HasError()) throw std::runtime_error("prepare failed: " + stmt->GetError());
    auto out = compat::ExecuteMaterialized(*stmt, params);
    if (out->HasError()) throw std::runtime_error(out->GetError());
    return out;
  });
  if (!result.ok()) return Refile(result.status(), "statement failed");
  return std::make_unique<StoreRows>(std::move(*result));
}

dd::Value Text(const std::string& s) { return dd::Value(s); }
dd::Value OptText(const std::optional<std::string>& s) {
  return s ? dd::Value(*s) : dd::Value(dd::LogicalType::VARCHAR);
}
dd::Value Bool(bool b) { return dd::Value::BOOLEAN(b); }

// String lists are stored as JSON arrays (["a","b"]).
std::string ToJson(const std::vector<std::string>& values) {
  boost::json::array array;
  for (const auto& value : values) array.emplace_back(value);
  return boost::json::serialize(array);
}
std::vector<std::string> FromJson(const std::string& text) {
  if (text.empty()) return {};
  boost::system::error_code ec;
  const auto parsed = boost::json::parse(text, ec);
  if (ec || !parsed.is_array()) return {};
  std::vector<std::string> out;
  for (const auto& v : parsed.as_array()) {
    if (v.is_string()) out.emplace_back(v.as_string());
  }
  return out;
}

std::string GetString(const StoreRows& result, dd::idx_t col, dd::idx_t row) {
  auto v = result.GetValue(col, row);
  return v.IsNull() ? std::string() : v.ToString();
}
std::optional<std::string> GetOptString(const StoreRows& result, dd::idx_t col, dd::idx_t row) {
  auto v = result.GetValue(col, row);
  if (v.IsNull()) return std::nullopt;
  return v.ToString();
}
bool GetBool(const StoreRows& result, dd::idx_t col, dd::idx_t row) {
  auto v = result.GetValue(col, row);
  return !v.IsNull() && v.GetValue<bool>();
}

std::string ScopeName(GrantScope scope) {
  switch (scope) {
    case GrantScope::kObject:
      return "object";
    case GrantScope::kSchema:
      return "schema";
    case GrantScope::kCatalog:
      return "catalog";
  }
  return "object";
}
GrantScope ScopeFrom(const std::string& s) {
  if (s == "schema") return GrantScope::kSchema;
  if (s == "catalog") return GrantScope::kCatalog;
  return GrantScope::kObject;
}

std::optional<Privilege> PrivilegeFrom(const std::string& name) {
  for (auto p : {Privilege::kSelect, Privilege::kInsert, Privilege::kUpdate, Privilege::kDelete,
                 Privilege::kCreateTable, Privilege::kCreateView, Privilege::kCreateSchema,
                 Privilege::kDropTable, Privilege::kDropView, Privilege::kDropSchema,
                 Privilege::kAlterTable, Privilege::kAlterView, Privilege::kAll}) {
    if (PrivilegeName(p) == name) return p;
  }
  return std::nullopt;
}

std::string ObjectTypeName(ObjectType t) {
  switch (t) {
    case ObjectType::kTable:
      return "table";
    case ObjectType::kView:
      return "view";
    case ObjectType::kSchema:
      return "schema";
  }
  return "table";
}
ObjectType ObjectTypeFrom(const std::string& s) {
  if (s == "view") return ObjectType::kView;
  if (s == "schema") return ObjectType::kSchema;
  return ObjectType::kTable;
}

std::string OwnerKindName(OwnerKind k) {
  switch (k) {
    case OwnerKind::kSystem:
      return "system";
    case OwnerKind::kUser:
      return "user";
    case OwnerKind::kRole:
      return "role";
  }
  return "system";
}
OwnerKind OwnerKindFrom(const std::string& s) {
  if (s == "user") return OwnerKind::kUser;
  if (s == "role") return OwnerKind::kRole;
  return OwnerKind::kSystem;
}

bool IsRetryableConflict(const std::string& message) {
  return message.find("could not serialize") != std::string::npos ||
         message.find("onflict") != std::string::npos ||  // DuckDB write-write conflicts
         message.find("deadlock detected") != std::string::npos;
}

bool LooksLikePostgres(const std::string& uri) {
  return uri.rfind("postgresql://", 0) == 0 || uri.rfind("postgres://", 0) == 0 ||
         uri.find("dbname=") != std::string::npos || uri.find("host=") != std::string::npos;
}

// The connection string with bounds on every way libpq can wait, each unless the string
// sets it already: connect_timeout (the whole connection setup, sign-in included), TCP
// keepalives and tcp_user_timeout (a peer that vanished without a reset; Linux), and the
// session's statement_timeout (a statement the server is slow to finish, lock waits
// included). The store's own deadline (StoreLink) covers what none of these can see,
// such as a paused server whose kernel still acknowledges.
std::string WithTimeouts(const std::string& uri, std::chrono::milliseconds timeout) {
  const int64_t seconds = std::max<int64_t>(2, (timeout.count() + 999) / 1000);  // libpq's minimum is 2
  const int64_t idle = std::max<int64_t>(1, seconds / 2);
  const std::vector<std::pair<std::string, std::string>> wanted = {
      {"connect_timeout", std::to_string(seconds)},
      {"keepalives", "1"},
      {"keepalives_idle", std::to_string(idle)},
      {"keepalives_interval", std::to_string(std::max<int64_t>(1, idle / 2))},
      {"keepalives_count", "3"},
      {"tcp_user_timeout", std::to_string(timeout.count())},
  };
  const std::string statement_timeout = "-c statement_timeout=" + std::to_string(timeout.count());
  const bool url = uri.rfind("postgresql://", 0) == 0 || uri.rfind("postgres://", 0) == 0;
  std::string out = uri;
  if (url) {
    // libpq allows '?' in the user info: the query starts at the first '?' after it.
    const auto scheme_end = uri.find("://") + 3;
    auto start = scheme_end;
    if (const auto at = uri.rfind('@'); at != std::string::npos && at >= scheme_end) start = at + 1;
    const auto query = uri.find('?', start);
    std::set<std::string> present;
    if (query != std::string::npos) {
      std::string rest = uri.substr(query + 1);
      for (std::size_t i = 0; i <= rest.size();) {
        auto amp = rest.find('&', i);
        if (amp == std::string::npos) amp = rest.size();
        const std::string pair = rest.substr(i, amp - i);
        present.insert(pair.substr(0, pair.find('=')));
        i = amp + 1;
      }
    }
    char separator = query == std::string::npos ? '?' : '&';
    if (separator == '&' && (out.back() == '&' || out.back() == '?')) separator = 0;
    auto append = [&](const std::string& key, const std::string& value) {
      if (present.contains(key)) return;
      if (separator != 0) out += separator;
      out += key + "=" + value;
      separator = '&';
    };
    for (const auto& [key, value] : wanted) append(key, value);
    std::string encoded;  // percent-encoded for the query string
    for (char c : statement_timeout) {
      if (c == ' ') encoded += "%20";
      else if (c == '=') encoded += "%3D";
      else encoded += c;
    }
    append("options", encoded);
    return out;
  }
  auto has = [&](const std::string& key) {
    return std::regex_search(uri, std::regex("(^|\\s)" + key + "\\s*="));
  };
  for (const auto& [key, value] : wanted) {
    if (!has(key)) out += " " + key + "=" + value;
  }
  if (!has("options")) out += " options='" + statement_timeout + "'";
  return out;
}

std::string Quote(const std::string& text) {
  std::string quoted = "'";
  for (char c : text) {
    if (c == '\'') quoted += '\'';
    quoted += c;
  }
  return quoted + "'";
}

// A DuckDB store's files (the database, its WAL, and the WAL's checkpoint and recovery
// copies) hold password hashes, so whatever the store's own instance creates is created
// 0600, whatever the process's umask. (DuckDB's private flag refuses an existing file,
// so it is asked for only when the file is new.)
class PrivateLocalFileSystem : public dd::LocalFileSystem {
 public:
  dd::unique_ptr<dd::FileHandle> OpenFile(const std::string& path, dd::FileOpenFlags flags,
                                          dd::optional_ptr<dd::FileOpener> opener) override {
    if ((flags.CreateFileIfNotExists() || flags.OverwriteExistingFile()) && !FileExists(path, opener)) {
      try {
        return dd::LocalFileSystem::OpenFile(path, flags | dd::FileFlags::FILE_FLAGS_PRIVATE, opener);
      } catch (const std::exception&) {
        if (!FileExists(path, opener)) throw;  // not a race with another open
      }
    }
    return dd::LocalFileSystem::OpenFile(path, flags, opener);
  }
};

#ifndef _WIN32
// The store's file holds password hashes and grants: no one but the server's user may
// read it, and anyone else who could write it could grant themselves anything.
arrow::Status CheckPrivate(const std::filesystem::path& file) {
  struct stat st {};
  if (::stat(file.c_str(), &st) != 0) return arrow::Status::OK();  // not there (yet)
  if (!S_ISREG(st.st_mode)) {
    return arrow::Status::Invalid("Hardcharger access control: the DuckDB store ", file.string(),
                                  " is not a regular file");
  }
  if ((st.st_mode & (S_IRWXG | S_IRWXO)) != 0) {
    char mode[8];
    std::snprintf(mode, sizeof(mode), "%04o", static_cast<unsigned>(st.st_mode & 07777));
    return arrow::Status::Invalid(
        "Hardcharger access control: the DuckDB store ", file.string(), " has mode ", mode,
        ", so other users of this host can read or change it; it holds password hashes and "
        "grants. Restrict it to the server's user (chmod 600 '",
        file.string(), "') and start again");
  }
  return arrow::Status::OK();
}
#endif

// The DuckDB store's file: absolute, its directory there, and (POSIX) private if it
// exists already.
arrow::Result<std::string> PrepareStoreFile(const std::string& given) {
  namespace fs = std::filesystem;
  std::error_code ec;
  fs::path file = fs::absolute(fs::path(given), ec);
  if (ec) return arrow::Status::Invalid("Hardcharger access control: no DuckDB store file '", given, "'");
  file = file.lexically_normal();
  if (!fs::is_directory(file.parent_path(), ec)) {
    return arrow::Status::Invalid("Hardcharger access control: the DuckDB store's directory ",
                                  file.parent_path().string(), " does not exist");
  }
#ifndef _WIN32
  for (const char* suffix : {"", ".wal", ".wal.checkpoint", ".wal.recovery"}) {
    ARROW_RETURN_NOT_OK(CheckPrivate(fs::path(file.string() + suffix)));
  }
#endif
  return file.string();
}

}  // namespace

// ---------------------------------------------------------------------------

std::string DuckDBStoreFile(const std::string& store_uri) { return RbacStore::DuckDBFile(store_uri); }

std::string StoreConnectionWithTimeouts(const std::string& uri, std::chrono::milliseconds timeout) {
  return WithTimeouts(uri, timeout);
}

std::string RbacStore::DuckDBFile(const std::string& uri) {
  if (uri.rfind("duckdb:", 0) == 0) return uri.substr(7);
  std::string lower = uri;
  std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return std::tolower(c); });
  const std::string suffix = ".duckdb";
  if (lower.size() > suffix.size() && lower.compare(lower.size() - suffix.size(), suffix.size(), suffix) == 0 &&
      !LooksLikePostgres(uri)) {
    return uri;
  }
  return {};
}

arrow::Result<std::unique_ptr<RbacStore>> RbacStore::Open(const StoreOptions& options) {
  std::unique_ptr<RbacStore> store(new RbacStore());
  store->options_ = options;
  if (store->options_.timeout.count() <= 0) store->options_.timeout = std::chrono::milliseconds(10000);
  ARROW_RETURN_NOT_OK(store->Redacted(store->OpenStore()));
  return store;
}

arrow::Status RbacStore::OpenStore() {
  const std::string& uri = options_.uri;
  const bool duckdb_file = uri.rfind("duckdb:", 0) == 0 || !DuckDBFile(uri).empty();
  if (duckdb_file) {
    const std::string file = DuckDBFile(uri);
    if (file.empty()) {
      return arrow::Status::Invalid("Hardcharger access control: duckdb: needs a file (duckdb:/path/to/rbac.duckdb)");
    }
    ARROW_ASSIGN_OR_RAISE(file_path_, PrepareStoreFile(file));
  }
  dd::DBConfig config;
  if (duckdb_file) {
    config.file_system = dd::make_uniq<dd::VirtualFileSystem>(dd::make_uniq<PrivateLocalFileSystem>());
  }
  // The store is small: nothing spills to a temporary directory (by default a .tmp in
  // the working directory, which the serving instance can read).
  config.SetOptionByName("temp_directory", dd::Value(""));
  // The private instance must not pick up secrets from the host's DuckDB
  // profile (a default PostgreSQL secret there would change where the store
  // connects), so it gets an empty secret directory of its own.
  std::error_code ec;
  auto secret_dir = std::filesystem::temp_directory_path(ec) /
                    ("hardcharger-rbac-store-" + StoreTransaction::NewId());
  if (ec || !std::filesystem::create_directory(secret_dir, ec) || ec) {
    return StoreError("could not start", "no private secret directory");
  }
  std::filesystem::permissions(secret_dir, std::filesystem::perms::owner_all,
                               std::filesystem::perm_options::replace, ec);
  secret_dir_ = secret_dir.string();
  config.SetOptionByName("secret_directory", dd::Value(secret_dir_));
  try {
    link_ = std::make_shared<StoreLink>(std::make_unique<dd::DuckDB>(nullptr, &config), options_.timeout);
  } catch (const std::exception& e) {
    return StoreError("could not start", e.what());
  }
  auto& conn = *link_;

  if (uri == "memory") {
    description_ = "in-memory (tests only)";
    ARROW_RETURN_NOT_OK(Exec(conn, "ATTACH ':memory:' AS rbac_store"));
  } else if (duckdb_file) {
    description_ = "DuckDB file " + file_path_ + " (one server only)";
    ARROW_ASSIGN_OR_RAISE(auto attach, Query(conn, "ATTACH " + Quote(file_path_) + " AS rbac_store"));
    if (attach->HasError()) {
      std::string error = attach->GetError();
      if (auto nl = error.find('\n'); nl != std::string::npos) error.resize(nl);
      // DuckDB's file lock (Windows: the file's sharing mode): another process has it open.
      if (error.find("Could not set lock") != std::string::npos ||
          error.find("being used by another process") != std::string::npos) {
        return StoreError("the DuckDB store " + file_path_ + " is in use by another process",
                          error +
                              ". A DuckDB store serves a single server; for several replicas, use a "
                              "PostgreSQL store (rbac_store_uri: postgresql://...)");
      }
      return StoreError("could not open the DuckDB store " + file_path_, error);
    }
  } else if (LooksLikePostgres(uri)) {
    description_ = "PostgreSQL";
    ARROW_ASSIGN_OR_RAISE(auto load, Query(conn, "LOAD postgres"));
    if (load->HasError()) {
      ARROW_RETURN_NOT_OK(Exec(conn, "INSTALL postgres"));
      ARROW_RETURN_NOT_OK(Exec(conn, "LOAD postgres"));
    }
    connect_uri_ = WithTimeouts(uri, options_.timeout);
    // The connection string carries credentials: never log it (Redacted() takes it out of
    // every error, as given and as rewritten).
    ARROW_ASSIGN_OR_RAISE(auto attach, Query(conn, "ATTACH " + Quote(connect_uri_) + " AS rbac_store (TYPE postgres)"));
    if (attach->HasError()) {
      std::string error = attach->GetError();
      if (auto nl = error.find('\n'); nl != std::string::npos) error.resize(nl);
      return StoreError("could not connect to the PostgreSQL store", error);
    }
  } else {
    return arrow::Status::Invalid(
        "Hardcharger access control: the store must be a PostgreSQL connection string "
        "(postgresql://user@host/db or 'host=... dbname=...') or a DuckDB file "
        "(duckdb:/path/to/rbac.duckdb)");
  }

  // Two replicas starting at once can race on CREATE ... IF NOT EXISTS.
  arrow::Status init;
  for (int attempt = 0; attempt < 5; ++attempt) {
    init = InitializeSchema();
    if (init.ok() || IsStoreUnavailable(init) || init.IsInvalid()) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(100 * (attempt + 1)));
  }
  ARROW_RETURN_NOT_OK(init);
  ARROW_RETURN_NOT_OK(CheckOwnership());
  CheckpointFile();
  return arrow::Status::OK();
}

std::string RbacStore::Redact(const std::string& text) const {
  return RedactCredentials(text, {connect_uri_, options_.uri});
}

arrow::Status RbacStore::Redacted(const arrow::Status& status) const {
  if (status.ok()) return status;
  return arrow::Status(status.code(), Redact(status.message()));
}

StoreTurn::~StoreTurn() {
  if (store_) store_->Unlock();
}

arrow::Status RbacStore::Lock(StoreTurn& turn) {
  std::unique_lock lock(turn_mutex_);
  const uint64_t ticket = next_ticket_++;
  // Checked at every handover, and between them often enough to notice a hang well within
  // the timeout. The callers ahead may take long (reading a large store is work of their
  // own, not the store's), but a statement outstanding for the whole timeout is a store that
  // doesn't answer, and the line behind it gives up.
  const auto check = std::clamp<std::chrono::milliseconds>(options_.timeout / 10, std::chrono::milliseconds(10),
                                                           std::chrono::milliseconds(250));
  while (serving_ != ticket) {
    if (link_->Hung()) {
      abandoned_.insert(ticket);
      return link_->TimedOut();
    }
    turn_cv_.wait_for(lock, check);
  }
  turn.store_ = this;
  return arrow::Status::OK();
}

void RbacStore::Unlock() {
  std::lock_guard lock(turn_mutex_);
  ++serving_;
  while (abandoned_.erase(serving_) > 0) ++serving_;
  turn_cv_.notify_all();
}

// A DuckDB store keeps nothing in its WAL past a write: each change is checkpointed into
// the database file at once. A WAL lost or replaced (DuckDB skips one it can't read, and
// with it every change since the last checkpoint: a REVOKE would come undone) then costs
// nothing. Writes are rare (access-control statements), so this is cheap. A failed
// checkpoint leaves the change in the WAL, committed: the next write tries again.
void RbacStore::CheckpointFile() {
  if (file_path_.empty()) return;
  (void)Query(*link_, "CHECKPOINT rbac_store");
}

RbacStore::~RbacStore() {
  link_.reset();
  if (!secret_dir_.empty()) {
    std::error_code ec;
    std::filesystem::remove_all(secret_dir_, ec);
  }
}

// The store says which cluster it serves (meta cluster_id, written by the first server
// with a cluster_id to open it): grants name catalogs, and another cluster's catalogs of
// the same names are other data. Replicas of one cluster share the store; another
// cluster is refused unless sharing is asked for (rbac_store_shared).
arrow::Status RbacStore::CheckOwnership() {
  auto& conn = *link_;
  auto read = [&]() -> arrow::Result<std::string> {
    ARROW_ASSIGN_OR_RAISE(auto r, Run(conn, "SELECT value FROM " + T("meta") + " WHERE key = 'cluster_id'"));
    return RowCount(*r) == 0 ? std::string() : GetString(*r, 0, 0);
  };
  ARROW_ASSIGN_OR_RAISE(cluster_id_, read());
  if (cluster_id_.empty() && !options_.cluster_id.empty()) {
    ARROW_RETURN_NOT_OK(Run(conn,
                            "INSERT INTO " + T("meta") +
                                " SELECT 'cluster_id', $1 WHERE NOT EXISTS (SELECT 1 FROM " + T("meta") +
                                " WHERE key = 'cluster_id')",
                            {Text(options_.cluster_id)})
                            .status());
    ARROW_ASSIGN_OR_RAISE(cluster_id_, read());  // another cluster may have been first
  }
  if (!cluster_id_.empty() && !options_.cluster_id.empty() && !options_.shared &&
      Lower(cluster_id_) != Lower(options_.cluster_id)) {
    return arrow::Status::Invalid(
        "Hardcharger access control: the access control store belongs to cluster ", cluster_id_,
        ", and this server's cluster_id is ", options_.cluster_id,
        ". Grants name catalogs, so another cluster's grants would apply to this cluster's "
        "catalogs of the same names: give each cluster a store of its own, or set "
        "rbac_store_shared=true to share users, roles and grants between the clusters");
  }
  return arrow::Status::OK();
}

arrow::Status RbacStore::InitializeSchema() {
  auto& conn = *link_;
  ARROW_RETURN_NOT_OK(Exec(conn, std::string("CREATE SCHEMA IF NOT EXISTS ") + kSchema));
  const std::string ddl[] = {
      "CREATE TABLE IF NOT EXISTS " + T("meta") +
          " (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("users") +
          " (user_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, name_key VARCHAR NOT "
          "NULL "
          "UNIQUE, password_hash VARCHAR, enabled BOOLEAN NOT NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("roles") +
          " (role_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, name_key VARCHAR NOT "
          "NULL "
          "UNIQUE, builtin BOOLEAN NOT NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("memberships") +
          " (member_kind VARCHAR NOT NULL, member_id VARCHAR NOT NULL, role_id VARCHAR "
          "NOT "
          "NULL, admin_option BOOLEAN NOT NULL, granted_by VARCHAR, PRIMARY KEY "
          "(member_kind, "
          "member_id, role_id))",
      "CREATE TABLE IF NOT EXISTS " + T("object_grants") +
          " (grant_id VARCHAR PRIMARY KEY, role_id VARCHAR NOT NULL, scope VARCHAR NOT "
          "NULL, "
          "catalog_name VARCHAR NOT NULL, schema_path VARCHAR NOT NULL, object_name "
          "VARCHAR "
          "NOT NULL, privilege VARCHAR NOT NULL, columns VARCHAR NOT NULL, grant_option "
          "BOOLEAN NOT NULL, granted_by VARCHAR)",
      "CREATE TABLE IF NOT EXISTS " + T("owners") +
          " (object_key VARCHAR PRIMARY KEY, catalog_name VARCHAR NOT NULL, schema_path "
          "VARCHAR NOT NULL, object_name VARCHAR NOT NULL, object_type VARCHAR NOT NULL, "
          "owner_kind VARCHAR NOT NULL, owner_id VARCHAR NOT NULL, definition_hash "
          "VARCHAR NOT "
          "NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("tenants") +
          " (tenant_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, name_key VARCHAR NOT "
          "NULL "
          "UNIQUE, default_catalog VARCHAR NOT NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("principal_tenants") +
          " (principal_kind VARCHAR NOT NULL, principal_id VARCHAR NOT NULL, tenant_id "
          "VARCHAR NOT "
          "NULL, PRIMARY KEY (principal_kind, principal_id))",
      "CREATE TABLE IF NOT EXISTS " + T("catalog_tenants") +
          " (catalog_key VARCHAR PRIMARY KEY, catalog_name VARCHAR NOT NULL, tenant_id "
          "VARCHAR NOT "
          "NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("group_mappings") +
          " (mapping_id VARCHAR PRIMARY KEY, issuer VARCHAR NOT NULL, group_name VARCHAR "
          "NOT "
          "NULL, role_id VARCHAR NOT NULL, UNIQUE (issuer, group_name, role_id))",
      "CREATE TABLE IF NOT EXISTS " + T("scoped_tokens") +
          " (token_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, name_key VARCHAR NOT "
          "NULL, "
          "user_id VARCHAR NOT NULL, parent_id VARCHAR NOT NULL, privileges VARCHAR NOT "
          "NULL, "
          "catalogs VARCHAR NOT NULL, expires_at VARCHAR NOT NULL, created_by VARCHAR "
          "NOT NULL, "
          "UNIQUE (user_id, name_key))",
      "CREATE TABLE IF NOT EXISTS " + T("user_attributes") +
          " (user_id VARCHAR NOT NULL, attribute VARCHAR NOT NULL, value VARCHAR NOT "
          "NULL, "
          "PRIMARY KEY (user_id, attribute))",
      "CREATE TABLE IF NOT EXISTS " + T("row_policies") +
          " (policy_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, table_key VARCHAR NOT "
          "NULL, "
          "catalog_name VARCHAR NOT NULL, schema_path VARCHAR NOT NULL, object_name "
          "VARCHAR NOT "
          "NULL, permissive BOOLEAN NOT NULL, role_ids VARCHAR NOT NULL, predicate "
          "VARCHAR NOT "
          "NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("masking_policies") +
          " (policy_id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, name_key VARCHAR NOT "
          "NULL "
          "UNIQUE, parameter VARCHAR NOT NULL, parameter_type VARCHAR NOT NULL, "
          "return_type "
          "VARCHAR NOT NULL, body VARCHAR NOT NULL)",
      "CREATE TABLE IF NOT EXISTS " + T("mask_bindings") +
          " (table_key VARCHAR NOT NULL, column_key VARCHAR NOT NULL, catalog_name "
          "VARCHAR NOT "
          "NULL, schema_path VARCHAR NOT NULL, object_name VARCHAR NOT NULL, column_name "
          "VARCHAR "
          "NOT NULL, policy_id VARCHAR NOT NULL, exempt_role_ids VARCHAR NOT NULL, "
          "PRIMARY KEY "
          "(table_key, column_key))",
      "CREATE TABLE IF NOT EXISTS " + T("system_privileges") +
          " (role_id VARCHAR NOT NULL, privilege VARCHAR NOT NULL, admin_option BOOLEAN "
          "NOT NULL, "
          "granted_by VARCHAR NOT NULL, PRIMARY KEY (role_id, privilege, granted_by))",
  };
  ARROW_RETURN_NOT_OK(Exec(conn, ddl[0]));  // meta, which says the schema's version
  {
    ARROW_ASSIGN_OR_RAISE(auto r, Run(conn, "SELECT value FROM " + T("meta") + " WHERE key = 'schema_version'"));
    if (RowCount(*r) == 1) {
      const std::string text = GetString(*r, 0, 0);
      int64_t version = 0;
      try {
        version = std::stoll(text);
      } catch (const std::exception&) {
        return arrow::Status::Invalid("Hardcharger access control: the store's schema version '", text,
                                      "' is not a number");
      }
      if (version > kStoreSchemaVersion) {
        return arrow::Status::Invalid(
            "Hardcharger access control: the access control store's schema is version ", version,
            ", newer than this server's (", kStoreSchemaVersion,
            "): a later Hardcharger has upgraded it. Run that version (or later), or give this "
            "server another store");
      }
    }
  }
  for (const auto& sql : ddl) ARROW_RETURN_NOT_OK(Exec(conn, sql));
  ARROW_RETURN_NOT_OK(Run(conn, "INSERT INTO " + T("meta") +
                                    " SELECT 'data_version', '1' WHERE NOT EXISTS (SELECT 1 FROM " +
                                    T("meta") + " WHERE key = 'data_version')")
                          .status());
  ARROW_RETURN_NOT_OK(
      Run(conn, "INSERT INTO " + T("meta") +
                    " SELECT 'schema_version', '" + std::to_string(kStoreSchemaVersion) +
                    "' WHERE NOT EXISTS (SELECT 1 FROM " + T("meta") +
                    " WHERE key = 'schema_version')")
          .status());
  ARROW_RETURN_NOT_OK(
      Run(conn,
          "INSERT INTO " + T("roles") +
              " SELECT $1, $2, $3, true WHERE NOT EXISTS (SELECT 1 FROM " + T("roles") +
              " WHERE role_id = $1)",
          {Text(kPublicRoleId), Text(kPublicRoleName), Text(FoldIdentifier(kPublicRoleName))})
          .status());
  return arrow::Status::OK();
}

arrow::Result<int64_t> RbacStore::CurrentVersion() {
  StoreTurn turn;
  ARROW_RETURN_NOT_OK(Lock(turn));
  auto result = Run(*link_, "SELECT value FROM " + T("meta") + " WHERE key = 'data_version'");
  if (!result.ok()) return Redacted(result.status());
  if (RowCount(**result) != 1) return StoreError("corrupt store", "no data version");
  return std::stoll(GetString(**result, 0, 0));
}

arrow::Result<std::shared_ptr<const Snapshot>> RbacStore::Load() {
  StoreTurn turn;
  ARROW_RETURN_NOT_OK(Lock(turn));
  return Redacted(LoadLocked());
}

arrow::Result<std::shared_ptr<const Snapshot>> RbacStore::LoadLocked() {
  auto& conn = *link_;
  ARROW_ASSIGN_OR_RAISE(auto version_result, Run(conn, "SELECT value FROM " + T("meta") +
                                                           " WHERE key = 'data_version'"));
  if (RowCount(*version_result) != 1) {
    return StoreError("corrupt store", "no data version");
  }
  const int64_t version = std::stoll(GetString(*version_result, 0, 0));

  std::vector<UserRecord> users;
  ARROW_ASSIGN_OR_RAISE(auto u, Run(conn, "SELECT user_id, name, password_hash, enabled FROM " +
                                              T("users") + " ORDER BY name_key"));
  for (dd::idx_t r = 0; r < RowCount(*u); ++r) {
    users.push_back(
        {GetString(*u, 0, r), GetString(*u, 1, r), GetOptString(*u, 2, r), GetBool(*u, 3, r), {}});
  }
  ARROW_ASSIGN_OR_RAISE(auto ua,
                        Run(conn, "SELECT user_id, attribute, value FROM " + T("user_attributes")));
  for (dd::idx_t r = 0; r < RowCount(*ua); ++r) {
    const std::string id = GetString(*ua, 0, r);
    for (auto& user : users) {
      if (user.id == id) user.attributes[GetString(*ua, 1, r)] = GetString(*ua, 2, r);
    }
  }

  std::vector<RoleRecord> roles;
  ARROW_ASSIGN_OR_RAISE(auto ro, Run(conn, "SELECT role_id, name, builtin FROM " + T("roles") +
                                               " ORDER BY name_key"));
  for (dd::idx_t r = 0; r < RowCount(*ro); ++r) {
    roles.push_back({GetString(*ro, 0, r), GetString(*ro, 1, r), GetBool(*ro, 2, r)});
  }

  std::vector<MembershipRecord> memberships;
  ARROW_ASSIGN_OR_RAISE(auto m, Run(conn,
                                    "SELECT member_kind, member_id, role_id, admin_option, "
                                    "granted_by FROM " +
                                        T("memberships")));
  for (dd::idx_t r = 0; r < RowCount(*m); ++r) {
    memberships.push_back(
        {GetString(*m, 0, r) == "role" ? PrincipalKind::kRole : PrincipalKind::kUser,
         GetString(*m, 1, r), GetString(*m, 2, r), GetBool(*m, 3, r), GetString(*m, 4, r)});
  }

  std::vector<ObjectGrantRecord> grants;
  ARROW_ASSIGN_OR_RAISE(
      auto g, Run(conn,
                  "SELECT grant_id, role_id, scope, catalog_name, schema_path, object_name, "
                  "privilege, columns, grant_option, granted_by FROM " +
                      T("object_grants") + " ORDER BY grant_id"));
  for (dd::idx_t r = 0; r < RowCount(*g); ++r) {
    auto privilege = PrivilegeFrom(GetString(*g, 6, r));
    if (!privilege) continue;  // written by a newer version; never widens access
    ObjectGrantRecord grant;
    grant.id = GetString(*g, 0, r);
    grant.role_id = GetString(*g, 1, r);
    grant.scope = ScopeFrom(GetString(*g, 2, r));
    grant.target.catalog = GetString(*g, 3, r);
    grant.target.schema_path = FromJson(GetString(*g, 4, r));
    grant.target.object_name = GetString(*g, 5, r);
    grant.privilege = *privilege;
    grant.columns = FromJson(GetString(*g, 7, r));
    grant.grant_option = GetBool(*g, 8, r);
    grant.granted_by = GetString(*g, 9, r);
    grants.push_back(std::move(grant));
  }

  std::vector<OwnerRecord> owners;
  ARROW_ASSIGN_OR_RAISE(
      auto o, Run(conn,
                  "SELECT catalog_name, schema_path, object_name, object_type, owner_kind, "
                  "owner_id, definition_hash FROM " +
                      T("owners")));
  for (dd::idx_t r = 0; r < RowCount(*o); ++r) {
    OwnerRecord owner;
    owner.object.catalog = GetString(*o, 0, r);
    owner.object.schema_path = FromJson(GetString(*o, 1, r));
    owner.object.object_name = GetString(*o, 2, r);
    owner.type = ObjectTypeFrom(GetString(*o, 3, r));
    owner.owner_kind = OwnerKindFrom(GetString(*o, 4, r));
    owner.owner_id = GetString(*o, 5, r);
    owner.definition_hash = GetString(*o, 6, r);
    owners.push_back(std::move(owner));
  }

  std::vector<SystemPrivilegeRecord> system_privileges;
  ARROW_ASSIGN_OR_RAISE(
      auto sp, Run(conn, "SELECT role_id, privilege, admin_option, granted_by FROM " +
                             T("system_privileges") + " ORDER BY role_id, privilege, granted_by"));
  for (dd::idx_t r = 0; r < RowCount(*sp); ++r) {
    auto privilege = SystemPrivilegeFromName(GetString(*sp, 1, r));
    if (!privilege) return StoreError("corrupt store", "unknown system privilege");
    system_privileges.push_back(
        {GetString(*sp, 0, r), *privilege, GetBool(*sp, 2, r), GetString(*sp, 3, r)});
  }

  std::vector<RowPolicyRecord> row_policies;
  ARROW_ASSIGN_OR_RAISE(auto rp,
                        Run(conn,
                            "SELECT policy_id, name, catalog_name, schema_path, object_name, "
                            "permissive, role_ids, predicate FROM " +
                                T("row_policies") + " ORDER BY policy_id"));
  for (dd::idx_t r = 0; r < RowCount(*rp); ++r) {
    RowPolicyRecord p;
    p.id = GetString(*rp, 0, r);
    p.name = GetString(*rp, 1, r);
    p.table = {GetString(*rp, 2, r), FromJson(GetString(*rp, 3, r)), GetString(*rp, 4, r)};
    p.permissive = GetBool(*rp, 5, r);
    p.role_ids = FromJson(GetString(*rp, 6, r));
    p.predicate = GetString(*rp, 7, r);
    row_policies.push_back(std::move(p));
  }
  std::vector<MaskingPolicyRecord> masking_policies;
  ARROW_ASSIGN_OR_RAISE(auto mp, Run(conn,
                                     "SELECT policy_id, name, parameter, parameter_type, "
                                     "return_type, body FROM " +
                                         T("masking_policies") + " ORDER BY name_key"));
  for (dd::idx_t r = 0; r < RowCount(*mp); ++r) {
    masking_policies.push_back({GetString(*mp, 0, r), GetString(*mp, 1, r), GetString(*mp, 2, r),
                                GetString(*mp, 3, r), GetString(*mp, 4, r), GetString(*mp, 5, r)});
  }
  std::vector<MaskBindingRecord> mask_bindings;
  ARROW_ASSIGN_OR_RAISE(auto mb, Run(conn,
                                     "SELECT catalog_name, schema_path, object_name, "
                                     "column_name, policy_id, exempt_role_ids FROM " +
                                         T("mask_bindings") + " ORDER BY table_key, column_key"));
  for (dd::idx_t r = 0; r < RowCount(*mb); ++r) {
    mask_bindings.push_back(
        {{GetString(*mb, 0, r), FromJson(GetString(*mb, 1, r)), GetString(*mb, 2, r)},
         GetString(*mb, 3, r),
         GetString(*mb, 4, r),
         FromJson(GetString(*mb, 5, r))});
  }

  std::vector<TenantRecord> tenants;
  ARROW_ASSIGN_OR_RAISE(auto tn, Run(conn, "SELECT tenant_id, name, default_catalog FROM " +
                                               T("tenants") + " ORDER BY name_key"));
  for (dd::idx_t r = 0; r < RowCount(*tn); ++r) {
    tenants.push_back({GetString(*tn, 0, r), GetString(*tn, 1, r), GetString(*tn, 2, r)});
  }
  std::vector<PrincipalTenantRecord> principal_tenants;
  ARROW_ASSIGN_OR_RAISE(auto pt, Run(conn, "SELECT principal_kind, principal_id, tenant_id FROM " +
                                               T("principal_tenants")));
  for (dd::idx_t r = 0; r < RowCount(*pt); ++r) {
    principal_tenants.push_back(
        {GetString(*pt, 0, r) == "role" ? PrincipalKind::kRole : PrincipalKind::kUser,
         GetString(*pt, 1, r), GetString(*pt, 2, r)});
  }
  std::vector<CatalogTenantRecord> catalog_tenants;
  ARROW_ASSIGN_OR_RAISE(auto ct, Run(conn, "SELECT catalog_name, tenant_id FROM " +
                                               T("catalog_tenants") + " ORDER BY catalog_key"));
  for (dd::idx_t r = 0; r < RowCount(*ct); ++r) {
    catalog_tenants.push_back({GetString(*ct, 0, r), GetString(*ct, 1, r)});
  }

  std::vector<GroupMappingRecord> group_mappings;
  ARROW_ASSIGN_OR_RAISE(
      auto gm, Run(conn, "SELECT mapping_id, issuer, group_name, role_id FROM " +
                             T("group_mappings") + " ORDER BY group_name, issuer, role_id"));
  for (dd::idx_t r = 0; r < RowCount(*gm); ++r) {
    group_mappings.push_back(
        {GetString(*gm, 0, r), GetString(*gm, 1, r), GetString(*gm, 2, r), GetString(*gm, 3, r)});
  }

  std::vector<ScopedTokenRecord> scoped_tokens;
  ARROW_ASSIGN_OR_RAISE(auto st, Run(conn,
                                     "SELECT token_id, name, user_id, parent_id, privileges, "
                                     "catalogs, expires_at, created_by FROM " +
                                         T("scoped_tokens") + " ORDER BY user_id, name_key"));
  for (dd::idx_t r = 0; r < RowCount(*st); ++r) {
    ScopedTokenRecord t;
    t.id = GetString(*st, 0, r);
    t.name = GetString(*st, 1, r);
    t.user_id = GetString(*st, 2, r);
    t.parent_id = GetString(*st, 3, r);
    for (const auto& p : FromJson(GetString(*st, 4, r))) {
      if (auto privilege = PrivilegeFrom(p)) t.privileges.push_back(*privilege);
    }
    t.catalogs = FromJson(GetString(*st, 5, r));
    t.expires_at = std::stoll(GetString(*st, 6, r));
    t.created_by = GetString(*st, 7, r);
    scoped_tokens.push_back(std::move(t));
  }

  return std::make_shared<const Snapshot>(
      version, std::move(users), std::move(roles), std::move(memberships), std::move(grants),
      std::move(owners), std::move(system_privileges), std::move(row_policies),
      std::move(masking_policies), std::move(mask_bindings), std::move(tenants),
      std::move(principal_tenants), std::move(catalog_tenants), std::move(group_mappings),
      std::move(scoped_tokens));
}

arrow::Status RbacStore::Mutate(
    const std::function<arrow::Status(StoreTransaction&, const Snapshot&)>& fn) {
  return Transact(
      [&](StoreTransaction& txn) -> arrow::Status {
        ARROW_ASSIGN_OR_RAISE(auto snapshot, LoadLocked());
        return fn(txn, *snapshot);
      },
      nullptr);
}

arrow::Result<int64_t> RbacStore::Write(const std::function<arrow::Status(StoreTransaction&)>& fn) {
  int64_t version = 0;
  ARROW_RETURN_NOT_OK(Transact(fn, &version));
  return version;
}

arrow::Status RbacStore::Transact(const std::function<arrow::Status(StoreTransaction&)>& fn,
                                  int64_t* version) {
  StoreTurn turn;
  ARROW_RETURN_NOT_OK(Lock(turn));
  auto& conn = *link_;
  arrow::Status last;
  // Replicas writing at once conflict on the version row; the loser retries.
  // Under steady contention a few attempts are not enough, so keep trying for
  // a while, with jitter so two replicas do not retry in lockstep.
  thread_local std::minstd_rand jitter(std::random_device{}());
  constexpr int kAttempts = 40;
  for (int attempt = 0; attempt < kAttempts; ++attempt) {
    ARROW_RETURN_NOT_OK(Redacted(Exec(conn, "BEGIN TRANSACTION")));
    auto body = [&]() -> arrow::Status {
      // Bump the version first: the row lock serializes writers on every replica.
      ARROW_RETURN_NOT_OK(Run(conn, "UPDATE " + T("meta") +
                                        " SET value = CAST(CAST(value AS BIGINT) + 1 AS VARCHAR) "
                                        "WHERE key = 'data_version'")
                              .status());
      if (version) {
        ARROW_ASSIGN_OR_RAISE(auto bumped,
                              Run(conn, "SELECT value FROM " + T("meta") + " WHERE key = 'data_version'"));
        if (RowCount(*bumped) != 1) return StoreError("corrupt store", "no data version");
        *version = std::stoll(GetString(*bumped, 0, 0));
      }
      StoreTransaction txn(*this, conn);
      ARROW_RETURN_NOT_OK(fn(txn));
      auto commit = Exec(conn, "COMMIT");
      if (!commit.ok() && commit.message().find("timed out after") != std::string::npos) {
        return arrow::Status::IOError(commit.message(),
                                      " while committing: the change may have been applied");
      }
      return commit;
    };
    last = Redacted(body());
    if (last.ok()) {
      CheckpointFile();
      return last;
    }
    // A store that did not answer is not asked to roll back: the store's thread does that
    // once the statement returns. Neither that nor an unreachable store is retried.
    if (last.message().find("timed out after") != std::string::npos) return last;
    (void)Query(conn, "ROLLBACK");
    if (IsStoreUnavailable(last) || !IsRetryableConflict(last.message())) return last;
    const int backoff_ms = std::min(10 * (attempt + 1), 200) + static_cast<int>(jitter() % 25);
    std::this_thread::sleep_for(std::chrono::milliseconds(backoff_ms));
  }
  return last;
}

// ---------------------------------------------------------------------------

std::string StoreTransaction::NewId() {
  unsigned char bytes[16];
  if (RAND_bytes(bytes, sizeof(bytes)) != 1) {
    // Fall back to a time-seeded id; uniqueness is still enforced by the store.
    auto now = std::chrono::steady_clock::now().time_since_epoch().count();
    for (size_t i = 0; i < sizeof(bytes); ++i)
      bytes[i] = static_cast<unsigned char>(now >> (i % 8));
  }
  bytes[6] = static_cast<unsigned char>((bytes[6] & 0x0f) | 0x40);
  bytes[8] = static_cast<unsigned char>((bytes[8] & 0x3f) | 0x80);
  static const char* kHex = "0123456789abcdef";
  std::string id;
  for (size_t i = 0; i < sizeof(bytes); ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) id += '-';
    id += kHex[bytes[i] >> 4];
    id += kHex[bytes[i] & 0x0f];
  }
  return id;
}

arrow::Status StoreTransaction::InsertUser(const UserRecord& user) {
  return Run(conn_, "INSERT INTO " + T("users") + " VALUES ($1, $2, $3, $4, $5)",
             {Text(user.id), Text(user.name), Text(FoldIdentifier(user.name)),
              OptText(user.password_hash), Bool(user.enabled)})
      .status();
}

arrow::Status StoreTransaction::UpdateUser(const UserRecord& user) {
  return Run(conn_,
             "UPDATE " + T("users") + " SET password_hash = $2, enabled = $3 WHERE user_id = $1",
             {Text(user.id), OptText(user.password_hash), Bool(user.enabled)})
      .status();
}

arrow::Status StoreTransaction::SetUserAttribute(const std::string& user_id,
                                                 const std::string& attribute,
                                                 const std::optional<std::string>& value) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("user_attributes") + " WHERE user_id = $1 AND attribute = $2",
          {Text(user_id), Text(attribute)})
          .status());
  if (!value) return arrow::Status::OK();
  return Run(conn_, "INSERT INTO " + T("user_attributes") + " VALUES ($1, $2, $3)",
             {Text(user_id), Text(attribute), Text(*value)})
      .status();
}

arrow::Status StoreTransaction::UpsertTenant(const TenantRecord& t) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("tenants") + " WHERE tenant_id = $1", {Text(t.id)}).status());
  return Run(conn_, "INSERT INTO " + T("tenants") + " VALUES ($1, $2, $3, $4)",
             {Text(t.id), Text(t.name), Text(FoldIdentifier(t.name)), Text(t.default_catalog)})
      .status();
}

arrow::Status StoreTransaction::DeleteTenant(const std::string& tenant_id) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("catalog_tenants") + " WHERE tenant_id = $1", {Text(tenant_id)})
          .status());
  ARROW_RETURN_NOT_OK(Run(conn_, "DELETE FROM " + T("principal_tenants") + " WHERE tenant_id = $1",
                          {Text(tenant_id)})
                          .status());
  return Run(conn_, "DELETE FROM " + T("tenants") + " WHERE tenant_id = $1", {Text(tenant_id)})
      .status();
}

arrow::Status StoreTransaction::SetPrincipalTenant(PrincipalKind kind, const std::string& id,
                                                   const std::string& tenant_id) {
  const std::string kind_name = kind == PrincipalKind::kRole ? "role" : "user";
  ARROW_RETURN_NOT_OK(Run(conn_,
                          "DELETE FROM " + T("principal_tenants") +
                              " WHERE principal_kind = $1 AND principal_id = $2",
                          {Text(kind_name), Text(id)})
                          .status());
  if (tenant_id.empty()) return arrow::Status::OK();
  return Run(conn_, "INSERT INTO " + T("principal_tenants") + " VALUES ($1, $2, $3)",
             {Text(kind_name), Text(id), Text(tenant_id)})
      .status();
}

arrow::Status StoreTransaction::SetCatalogTenant(const std::string& catalog,
                                                 const std::string& tenant_id) {
  ARROW_RETURN_NOT_OK(Run(conn_, "DELETE FROM " + T("catalog_tenants") + " WHERE catalog_key = $1",
                          {Text(FoldIdentifier(catalog))})
                          .status());
  if (tenant_id.empty()) return arrow::Status::OK();
  return Run(conn_, "INSERT INTO " + T("catalog_tenants") + " VALUES ($1, $2, $3)",
             {Text(FoldIdentifier(catalog)), Text(catalog), Text(tenant_id)})
      .status();
}

arrow::Status StoreTransaction::InsertScopedToken(const ScopedTokenRecord& t) {
  std::vector<std::string> privileges;
  for (auto p : t.privileges) privileges.push_back(PrivilegeName(p));
  return Run(conn_,
             "INSERT INTO " + T("scoped_tokens") + " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
             {Text(t.id), Text(t.name), Text(FoldIdentifier(t.name)), Text(t.user_id),
              Text(t.parent_id), Text(ToJson(privileges)), Text(ToJson(t.catalogs)),
              Text(std::to_string(t.expires_at)), Text(t.created_by)})
      .status();
}

arrow::Status StoreTransaction::DeleteScopedToken(const std::string& token_id) {
  return Run(conn_, "DELETE FROM " + T("scoped_tokens") + " WHERE token_id = $1", {Text(token_id)})
      .status();
}

arrow::Status StoreTransaction::InsertGroupMapping(const GroupMappingRecord& m) {
  return Run(conn_, "INSERT INTO " + T("group_mappings") + " VALUES ($1, $2, $3, $4)",
             {Text(m.id), Text(m.issuer), Text(m.group), Text(m.role_id)})
      .status();
}

arrow::Status StoreTransaction::DeleteGroupMapping(const std::string& mapping_id) {
  return Run(conn_, "DELETE FROM " + T("group_mappings") + " WHERE mapping_id = $1",
             {Text(mapping_id)})
      .status();
}

arrow::Status StoreTransaction::UpsertRowPolicy(const RowPolicyRecord& p) {
  ARROW_RETURN_NOT_OK(DeleteRowPolicy(p.id));
  return Run(conn_,
             "INSERT INTO " + T("row_policies") + " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
             {Text(p.id), Text(p.name), Text(ObjectKey(p.table)), Text(p.table.catalog),
              Text(ToJson(p.table.schema_path)), Text(p.table.object_name), Bool(p.permissive),
              Text(ToJson(p.role_ids)), Text(p.predicate)})
      .status();
}

arrow::Status StoreTransaction::DeleteRowPolicy(const std::string& policy_id) {
  return Run(conn_, "DELETE FROM " + T("row_policies") + " WHERE policy_id = $1", {Text(policy_id)})
      .status();
}

arrow::Status StoreTransaction::UpsertMaskingPolicy(const MaskingPolicyRecord& m) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("masking_policies") + " WHERE policy_id = $1", {Text(m.id)})
          .status());
  return Run(conn_, "INSERT INTO " + T("masking_policies") + " VALUES ($1, $2, $3, $4, $5, $6, $7)",
             {Text(m.id), Text(m.name), Text(FoldIdentifier(m.name)), Text(m.parameter),
              Text(m.parameter_type), Text(m.return_type), Text(m.body)})
      .status();
}

arrow::Status StoreTransaction::DeleteMaskingPolicy(const std::string& policy_id) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("mask_bindings") + " WHERE policy_id = $1", {Text(policy_id)})
          .status());
  return Run(conn_, "DELETE FROM " + T("masking_policies") + " WHERE policy_id = $1",
             {Text(policy_id)})
      .status();
}

arrow::Status StoreTransaction::SetMaskBinding(const MaskBindingRecord& b, bool remove) {
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("mask_bindings") + " WHERE table_key = $1 AND column_key = $2",
          {Text(ObjectKey(b.table)), Text(FoldIdentifier(b.column))})
          .status());
  if (remove) return arrow::Status::OK();
  return Run(conn_,
             "INSERT INTO " + T("mask_bindings") + " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
             {Text(ObjectKey(b.table)), Text(FoldIdentifier(b.column)), Text(b.table.catalog),
              Text(ToJson(b.table.schema_path)), Text(b.table.object_name), Text(b.column),
              Text(b.policy_id), Text(ToJson(b.exempt_role_ids))})
      .status();
}

arrow::Status StoreTransaction::DeleteUser(const std::string& user_id) {
  ARROW_RETURN_NOT_OK(
      Run(conn_,
          "DELETE FROM " + T("memberships") + " WHERE member_kind = 'user' AND member_id = $1",
          {Text(user_id)})
          .status());
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("user_attributes") + " WHERE user_id = $1", {Text(user_id)})
          .status());
  ARROW_RETURN_NOT_OK(SetPrincipalTenant(PrincipalKind::kUser, user_id, ""));
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("scoped_tokens") + " WHERE user_id = $1", {Text(user_id)})
          .status());
  return Run(conn_, "DELETE FROM " + T("users") + " WHERE user_id = $1", {Text(user_id)}).status();
}

arrow::Status StoreTransaction::InsertRole(const RoleRecord& role) {
  return Run(conn_, "INSERT INTO " + T("roles") + " VALUES ($1, $2, $3, $4)",
             {Text(role.id), Text(role.name), Text(FoldIdentifier(role.name)), Bool(role.builtin)})
      .status();
}

arrow::Status StoreTransaction::DeleteRole(const std::string& role_id) {
  ARROW_RETURN_NOT_OK(Run(conn_,
                          "DELETE FROM " + T("memberships") +
                              " WHERE role_id = $1 OR (member_kind = 'role' AND member_id = $1)",
                          {Text(role_id)})
                          .status());
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("object_grants") + " WHERE role_id = $1", {Text(role_id)})
          .status());
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("system_privileges") + " WHERE role_id = $1", {Text(role_id)})
          .status());
  ARROW_RETURN_NOT_OK(SetPrincipalTenant(PrincipalKind::kRole, role_id, ""));
  ARROW_RETURN_NOT_OK(
      Run(conn_, "DELETE FROM " + T("group_mappings") + " WHERE role_id = $1", {Text(role_id)})
          .status());
  return Run(conn_, "DELETE FROM " + T("roles") + " WHERE role_id = $1", {Text(role_id)}).status();
}

arrow::Status StoreTransaction::InsertMembership(const MembershipRecord& m) {
  return Run(conn_, "INSERT INTO " + T("memberships") + " VALUES ($1, $2, $3, $4, $5)",
             {Text(m.member_kind == PrincipalKind::kRole ? "role" : "user"), Text(m.member_id),
              Text(m.role_id), Bool(m.admin_option), Text(m.granted_by)})
      .status();
}

arrow::Status StoreTransaction::DeleteMembership(PrincipalKind member_kind,
                                                 const std::string& member_id,
                                                 const std::string& role_id) {
  return Run(conn_,
             "DELETE FROM " + T("memberships") +
                 " WHERE member_kind = $1 AND member_id = $2 AND role_id = $3",
             {Text(member_kind == PrincipalKind::kRole ? "role" : "user"), Text(member_id),
              Text(role_id)})
      .status();
}

arrow::Status StoreTransaction::InsertGrant(const ObjectGrantRecord& g) {
  return Run(conn_,
             "INSERT INTO " + T("object_grants") +
                 " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
             {Text(g.id), Text(g.role_id), Text(ScopeName(g.scope)), Text(g.target.catalog),
              Text(ToJson(g.target.schema_path)), Text(g.target.object_name),
              Text(PrivilegeName(g.privilege)), Text(ToJson(g.columns)), Bool(g.grant_option),
              Text(g.granted_by)})
      .status();
}

arrow::Status StoreTransaction::UpdateGrant(const ObjectGrantRecord& g) {
  return Run(conn_,
             "UPDATE " + T("object_grants") +
                 " SET columns = $2, grant_option = $3, granted_by = $4 WHERE grant_id = "
                 "$1",
             {Text(g.id), Text(ToJson(g.columns)), Bool(g.grant_option), Text(g.granted_by)})
      .status();
}

arrow::Status StoreTransaction::DeleteGrant(const std::string& grant_id) {
  return Run(conn_, "DELETE FROM " + T("object_grants") + " WHERE grant_id = $1", {Text(grant_id)})
      .status();
}

arrow::Status StoreTransaction::UpsertOwner(const OwnerRecord& o) {
  ARROW_RETURN_NOT_OK(DeleteOwner(o.object));
  return Run(conn_, "INSERT INTO " + T("owners") + " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
             {Text(ObjectKey(o.object)), Text(o.object.catalog), Text(ToJson(o.object.schema_path)),
              Text(o.object.object_name), Text(ObjectTypeName(o.type)),
              Text(OwnerKindName(o.owner_kind)), Text(o.owner_id), Text(o.definition_hash)})
      .status();
}

arrow::Status StoreTransaction::UpsertSystemPrivilege(const SystemPrivilegeRecord& r) {
  ARROW_RETURN_NOT_OK(DeleteSystemPrivilege(r));
  return Run(conn_, "INSERT INTO " + T("system_privileges") + " VALUES ($1, $2, $3, $4)",
             {Text(r.role_id), Text(SystemPrivilegeName(r.privilege)), Bool(r.admin_option),
              Text(r.granted_by)})
      .status();
}

arrow::Status StoreTransaction::DeleteSystemPrivilege(const SystemPrivilegeRecord& r) {
  return Run(conn_,
             "DELETE FROM " + T("system_privileges") +
                 " WHERE role_id = $1 AND privilege = $2 AND granted_by = $3",
             {Text(r.role_id), Text(SystemPrivilegeName(r.privilege)), Text(r.granted_by)})
      .status();
}

arrow::Result<std::shared_ptr<const Snapshot>> StoreTransaction::Reload() {
  return store_.LoadLocked();
}

arrow::Status StoreTransaction::DeleteOwner(const ObjectPath& object) {
  return Run(conn_, "DELETE FROM " + T("owners") + " WHERE object_key = $1",
             {Text(ObjectKey(object))})
      .status();
}

}  // namespace hardcharger::authz
