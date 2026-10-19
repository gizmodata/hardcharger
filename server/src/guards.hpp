// Checks on a statement before DuckDB runs it, from its type and its tokens (DuckDB's
// v2 C API gives no access to the parse tree):
//
//  - the admin command guard: users without the admin role may not run commands that
//    reach the host or reconfigure the instance (ATTACH, INSTALL/LOAD, SET GLOBAL,
//    COPY to or from local files, read_csv('/local/file'), CREATE SECRET, ...). Where
//    a path decides, only a literal remote URL (s3://, https://, ...) is allowed; a
//    computed path is refused (fail closed);
//  - allow_unredacted_secrets can't be turned on, by anyone;
//  - secret_directory can't be moved after startup while the sensitive path guard is on.
//
// The files no statement may reach are guarded where DuckDB opens them, not in the SQL
// text: access control, the one part of the server on DuckDB's C++ API, puts a guard into
// the serving instance's file system (authz::InstallFileGuard, file_guard.cc). It sees
// every open, glob, listing and stat DuckDB performs, by the path DuckDB resolved,
// however the statement computed it (a concatenation, query('...'), a macro, a view, a
// prepared statement's parameter, file_search_path, ~ under another home_directory):
//
//  - the sensitive path guard (SensitivePaths): credential and system files on the
//    server host (~/.ssh, ~/.aws, /etc/shadow, /proc, the server's own TLS key, ...),
//    for every principal, admins included, when block_sensitive_paths is on;
//  - the server's own files (PrivateFiles), always.
//
// So no string literal is ever refused for looking like a path: only what DuckDB is
// about to open, list or stat decides.
#pragma once

#include <duckdb_v2.h>

#include <atomic>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "sql_text.hpp"

namespace hardcharger {

// The credential and system files no statement may open, list or stat: the built-in
// categories, DuckDB's secret directory, the server's own credential files and the
// operator's `sensitive_paths`. A path is compared by every spelling of it (absolute and
// lexically normal, symlinks resolved, case-folded where the file system is, Windows'
// spellings and 8.3 names) and, for a file with a second hard link, by identity with the
// protected files.
class SensitivePaths {
 public:
  // `home`: the server user's home directory (empty: $HOME).
  SensitivePaths(bool enabled, const std::vector<std::string>& operator_paths,
                 const std::vector<std::string>& server_files, std::string home = {});

  bool enabled() const { return enabled_; }
  // Whether the file system's guard enforces it now: enabled, and the server's startup
  // (its own attaches and the operator's init SQL) is over.
  bool enforcing() const { return enabled_ && active_.load(std::memory_order_acquire); }
  void Activate() { active_.store(true, std::memory_order_release); }

  // The category of the protected path `path` falls under, if any: `path` names one, or
  // something inside one. `path` is a concrete path: '*' in it is an ordinary character, so a
  // glob pattern is refused here only when its fixed part (~/.ssh/*) is protected; the file
  // system drops a glob's protected matches (/home/*/.ssh/*) one by one.
  std::optional<std::string> Check(const std::string& path) const;
  // Adds DuckDB's secret directory (it can change at startup through init SQL).
  void AddSecretDirectory(const std::string& path);

 private:
  struct Entry {
    std::string key;
    std::string category;
    std::string path;  // as added, for comparing file identity
  };
  void Add(const std::string& path, const std::string& category);
#ifdef _WIN32
  void AddWindowsDefaults();
#endif

  bool enabled_;
  std::atomic<bool> active_{false};
  std::string home_;
  mutable std::mutex mutex_;
  std::vector<Entry> entries_;
  std::vector<std::string> allowed_keys_;
};

// The refusal of `path` (a protected path in `category`).
std::string SensitivePathMessage(const std::string& path, const std::string& category);

// Files of the server's own that no statement may open, an admin's included, however it
// spells or computes the path (.., symlinks, case, file://, ~, relative, glob):
//
//  - a DuckDB access-control store: users, password hashes and grants. It is open in
//    the store's own DuckDB instance, and DuckDB's file lock doesn't stop this process
//    from opening it a second time, so nothing in the serving instance may open it;
//  - the files the serving instance keeps attached (the database, the instrumentation
//    and log files): read through their catalogs only, which access control governs.
//    Only the instance's own storage, under the spelling it attached, opens them.
//
// Each covers the files DuckDB keeps beside it: <file>.wal, .wal.checkpoint,
// .wal.recovery and the .tmp directory, and the tmp_<name> COPY ... TO writes before it
// renames it onto the target. Matched by spelling (every key SensitivePaths compares)
// and by file identity (a hard link or an open descriptor's /dev/fd name).
class PrivateFiles {
 public:
  struct Entry {
    std::string path;      // as given (for an attached file, the spelling DuckDB opens)
    std::string category;  // e.g. "the access-control store"
    bool attached = false; // the serving instance's own storage opens it
  };

  // `home`: the server user's home directory, for '~' (empty: $HOME).
  explicit PrivateFiles(std::string home = {});

  void Add(const std::string& path, const std::string& category, bool attached);
  // The entry `path` is (or is a file beside), if any: by every spelling (symlinks
  // resolved) and by file identity (a hard link).
  std::optional<Entry> Find(const std::string& path) const;
  // A cheap first look, for the file system's every lookup: false only when `path`
  // can't be one of the files (no component has one of their names, and it is no
  // symlink, no second hard link of a file, nothing under /dev or /proc and, on Windows,
  // no 8.3 short name). One lstat at most.
  bool MayName(const std::string& path) const;
  // Whether `path` is the tmp_<name> twin of one of the files: DuckDB renames it onto the
  // file when a COPY ... TO finishes.
  bool IsCopyTwin(const std::string& path) const;
  bool empty() const;

 private:
  struct Keyed {
    Entry entry;
    std::vector<std::string> keys;  // of the file and each file beside it
    std::vector<std::string> twins; // the tmp_ twins' keys
    std::vector<std::string> names; // the file names of all of them (case-folded where the file system is)
  };
  std::vector<std::string> CandidateKeys(const std::string& path, bool resolve) const;

  std::string home_;
  mutable std::mutex mutex_;
  std::vector<Keyed> entries_;
};

// The refusal of `path`, one of the PrivateFiles (`category`).
std::string PrivateFileMessage(const std::string& path, const std::string& category);

struct GuardContext {
  duckdb_v2_connection_handle connection;
  bool admin;
  bool block_unredacted_secrets;
  const SensitivePaths* sensitive_paths;
};

// The category of command a non-admin may not run, if `piece` is one (e.g. "ATTACH",
// "COPY TO (local filesystem)", "read_csv() (local filesystem)").
std::optional<std::string> ClassifyGatedCommand(duckdb_v2_connection_handle connection, const Piece& piece,
                                                DUCKDB_V2_STATEMENT_TYPE type);

// Throws warpsql::Error(kPermissionDenied) when `piece` may not run.
void CheckStatement(const GuardContext& context, const Piece& piece, DUCKDB_V2_STATEMENT_TYPE type);

// The type of the statement that actually runs: EXPLAIN ANALYZE runs the statement it
// wraps, so it counts as that statement (plain EXPLAIN runs nothing).
DUCKDB_V2_STATEMENT_TYPE ExecutedType(duckdb_v2_connection_handle connection, const Piece& piece,
                                      DUCKDB_V2_STATEMENT_TYPE type);

// The statement's type as DuckDB parses it; throws DuckDB's parse error.
DUCKDB_V2_STATEMENT_TYPE StatementType(duckdb_v2_connection_handle connection, std::string_view sql);

}  // namespace hardcharger
