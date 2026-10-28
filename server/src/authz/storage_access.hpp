// The storage check every statement that reads or writes files goes through, before DuckDB
// binds it (gate.hpp, with the paths written in it) and again in the plan check (with the
// paths DuckDB bound): the principal needs the storage privilege for each path's kind of
// storage and mode, everywhere or on a location that covers it (locations.hpp). And no
// principal but the administrator reads or writes, lists or globs anything in an attached
// DuckLake catalog's storage, whatever it was granted: DuckLake itself reads and writes there,
// for statements on its tables, and only DuckLake may (a file written there could be
// registered as a table's data; a listing shows another catalog's files).
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "authz/locations.hpp"
#include "authz/model.hpp"

namespace duckdb {
class ClientContext;
}

namespace hardcharger::authz {

// What a statement does to storage.
struct StorageRequest {
  std::string what;  // "read_csv()", "COPY ... TO", ...: for messages
  StorageMode mode = StorageMode::kRead;
  // The paths, as written or bound; nullopt when the gate can't read them before binding
  // (a computed path, a prepared statement's parameter).
  std::optional<std::vector<std::string>> paths;
  // It writes a directory of files (COPY ... TO with PARTITION_BY, EXPORT DATABASE).
  bool directory = false;
};

// One session's storage checks (on one statement): reads the session's home directory and
// file_search_path, and, once per check at most, where the attached DuckLake catalogs keep
// their data.
class StorageAccess {
 public:
  explicit StorageAccess(duckdb::ClientContext& context);

  // Why `request` is refused, or nullopt. Every principal of `principals` (the statement's
  // user, and the owners of the views it reads through) must be allowed it; refusals name
  // `user_name`.
  std::optional<std::string> Refusal(const Snapshot& snapshot, const std::vector<Principal>& principals,
                                     const StorageRequest& request, const std::string& user_name);

 private:
  struct LakeArea {
    std::string catalog;
    StoragePath area;
  };
  // Where each attached DuckLake catalog keeps its data, or why that isn't known.
  arrow::Result<std::vector<LakeArea>> LakeAreas();

  duckdb::ClientContext& context_;
  LocalPathContext local_;
  std::vector<std::string> search_path_;
  std::optional<arrow::Result<std::vector<LakeArea>>> lakes_;
};

// The data paths of the DuckLake catalogs the storage checks have looked up (however long
// ago): for the file system's guard, which must not run queries of its own.
std::vector<std::string> KnownLakeDataPaths();

}  // namespace hardcharger::authz
