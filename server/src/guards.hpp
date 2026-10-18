// Checks on a statement before DuckDB runs it, from its type and its tokens (DuckDB's
// v2 C API gives no access to the parse tree):
//
//  - the admin command guard: users without the admin role may not run commands that
//    reach the host or reconfigure the instance (ATTACH, INSTALL/LOAD, SET GLOBAL,
//    COPY to or from local files, read_csv('/local/file'), CREATE SECRET, ...). Where
//    a path decides, only a literal remote URL (s3://, https://, ...) is allowed; a
//    computed path is refused (fail closed);
//  - allow_unredacted_secrets can't be turned on, by anyone;
//  - the sensitive path guard: no statement, an admin's included, may name a
//    credential or system file (~/.ssh, /etc/shadow, the server's own TLS key, ...),
//    however it is spelled (.., symlinks, file://, ~, relative to the working
//    directory).
//
// What the tokens can't show: a path computed at run time ('/etc/' || 'passwd') by an
// admin, and the entries glob() lists. Closing those needs a hook in DuckDB's file
// system, which the v2 C API does not offer.
//
// The server's own files (PrivateFiles below) do get that hook: access control, the one
// part of the server on DuckDB's C++ API, puts a guard into the serving instance's file
// system (authz::InstallPrivateFileGuard), so no statement opens them however it
// computes the path.
#pragma once

#include <duckdb_v2.h>

#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "sql_text.hpp"

namespace hardcharger {

class SensitivePaths {
 public:
  // `home`: the server user's home directory (empty: $HOME).
  SensitivePaths(bool enabled, const std::vector<std::string>& operator_paths,
                 const std::vector<std::string>& server_files, std::string home = {});

  bool enabled() const { return enabled_; }
  // The category of the protected path `path` falls under, if any.
  std::optional<std::string> Check(const std::string& path) const;
  // Adds DuckDB's secret directory (it can change at startup through init SQL).
  void AddSecretDirectory(const std::string& path);

 private:
  struct Entry {
    std::string key;
    std::string category;
  };
  void Add(const std::string& path, const std::string& category);
#ifdef _WIN32
  void AddWindowsDefaults();
#endif

  bool enabled_;
  std::string home_;
  mutable std::mutex mutex_;
  std::vector<Entry> entries_;
  std::vector<std::string> allowed_keys_;
};

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
  // The entry `path` is (or is a file beside), if any. `thorough` (the file system's
  // guard) also resolves symlinks in any path and compares file identity (a hard link);
  // a statement's literals (thorough = false) resolve only what looks like a path, so a
  // statement full of string literals costs no system calls for them.
  std::optional<Entry> Find(const std::string& path, bool thorough = true) const;
  // Whether some entry is not attached (a store file): the only ones a statement's
  // literals are checked against.
  bool HasUnattached() const;
  // A cheap first look, for the file system's every lookup: false only when `path`
  // can't be one of the files (no component has one of their names, and it is no
  // symlink, no second hard link of a file and nothing under /dev or /proc). One lstat
  // at most.
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
  // The server's own files (always checked, whatever block_sensitive_paths says).
  const PrivateFiles* private_files = nullptr;
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
