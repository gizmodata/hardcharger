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
