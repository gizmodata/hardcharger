#include "authz/storage_access.hpp"

#include <duckdb.hpp>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/common/file_opener.hpp>
#include <duckdb/common/file_system.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/client_data.hpp>
#include <duckdb/main/database_manager.hpp>

#include <filesystem>
#include <map>
#include <mutex>

#include "authz/duckdb_internal.hpp"
#include "guards.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;

// Where a DuckLake catalog keeps its data changes only when an administrator moves it
// (set_option): looked up again after this long. The plan check and the gate before it ask
// for every statement that reaches storage, and a catalog's metadata may be a network hop
// away (PostgreSQL).
constexpr auto kLakePathsFresh = std::chrono::seconds(2);

struct CachedPaths {
  std::chrono::steady_clock::time_point at;
  std::vector<std::string> paths;
};

std::mutex& CacheMutex() {
  static std::mutex mutex;
  return mutex;
}

std::map<std::pair<std::string, const void*>, CachedPaths>& Cache() {
  static std::map<std::pair<std::string, const void*>, CachedPaths> cache;
  return cache;
}

std::string Joined(const std::vector<std::string>& names) {
  std::string out;
  for (std::size_t i = 0; i < names.size(); ++i) {
    if (i) out += i + 1 == names.size() ? " and " : ", ";
    out += names[i];
  }
  return out;
}

// The data paths DuckLake catalog `name` writes to: its data_path, and any a schema or table
// was given of its own.
arrow::Result<std::vector<std::string>> DataPaths(dd::DatabaseInstance& db, const std::string& name) {
  std::string literal = "'";
  for (const char c : name) literal += c == '\'' ? std::string("''") : std::string(1, c);
  literal += "'";
  dd::Connection connection(db);
  auto result = connection.Query("SELECT DISTINCT value FROM ducklake_options(" + literal +
                                 ") WHERE option_name = 'data_path' AND value IS NOT NULL AND value <> ''");
  if (result->HasError()) {
    return arrow::Status::IOError("Hardcharger can't tell where DuckLake catalog ", name,
                                  " keeps its data, so it refuses every path until it can: ", result->GetError());
  }
  std::vector<std::string> paths;
  for (dd::idx_t row = 0; row < ddb::ResultRowCount(*result); ++row) {
    paths.push_back(ddb::ResultValue(*result, 0, row).ToString());
  }
  return paths;
}

}  // namespace

StorageAccess::StorageAccess(dd::ClientContext& context) : context_(context) {
  local_.home = dd::FileSystem::GetHomeDirectory(dd::optional_ptr<dd::FileOpener>(dd::ClientData::Get(context).file_opener.get()));
  dd::Value search_path;
  if (context.TryGetCurrentSetting(dd::Identifier("file_search_path"), search_path) && !search_path.IsNull()) {
    const std::string text = search_path.ToString();
    std::size_t start = 0;
    while (start <= text.size()) {
      const auto end = std::min(text.find(',', start), text.size());
      if (end > start) search_path_.push_back(text.substr(start, end - start));
      start = end + 1;
    }
  }
}

arrow::Result<std::vector<StorageAccess::LakeArea>> StorageAccess::LakeAreas() {
  std::vector<LakeArea> areas;
  auto& db = dd::DatabaseInstance::GetDatabase(context_);
  for (const auto& attached : dd::DatabaseManager::Get(db).GetDatabases()) {
    if (!attached || attached->IsSystem() || attached->IsTemporary()) continue;
    if (attached->GetCatalog().GetCatalogType() != "ducklake") continue;
    const std::string name = ddb::Str(attached->GetName());
    const auto key = std::make_pair(name, static_cast<const void*>(attached.get()));
    std::vector<std::string> paths;
    bool cached = false;
    {
      std::lock_guard lock(CacheMutex());
      auto it = Cache().find(key);
      if (it != Cache().end() && std::chrono::steady_clock::now() - it->second.at < kLakePathsFresh) {
        paths = it->second.paths;
        cached = true;
      }
    }
    if (!cached) {
      ARROW_ASSIGN_OR_RAISE(paths, DataPaths(db, name));
      std::lock_guard lock(CacheMutex());
      Cache()[key] = {std::chrono::steady_clock::now(), paths};
    }
    for (const auto& path : paths) {
      auto area = ParseStoragePath(path, local_, /*allow_pattern=*/false);
      if (!area.ok()) {
        return arrow::Status::IOError("Hardcharger can't compare the data path of DuckLake catalog ", name, " (",
                                      area.status().message(), "), so it refuses every path until it can");
      }
      areas.push_back({name, std::move(*area)});
    }
  }
  return areas;
}

std::optional<std::string> StorageAccess::Refusal(const Snapshot& snapshot, const std::vector<Principal>& principals,
                                                  const StorageRequest& request, const std::string& user_name) {
  std::vector<const Principal*> restricted;
  for (const auto& principal : principals) {
    if (!principal.superuser) restricted.push_back(&principal);
  }
  if (restricted.empty()) return std::nullopt;
  const std::string blocked = "Hardcharger blocked " + request.what;
  const auto lacks = [&](const Principal& principal, SystemPrivilege privilege, const std::string& path) -> std::string {
    const std::string what = request.what + (path.empty() ? "" : " of '" + path + "'");
    if (principal.rules_administrator) return "Hardcharger blocked " + RulesAdministratorRefusal(what, privilege);
    const std::string needs = "Hardcharger blocked " + what + ", which needs the " + SystemPrivilegeName(privilege) +
                              " system privilege" +
                              (path.empty() ? " everywhere" : ", everywhere or on a location that covers it");
    if (&principal == restricted.front() && principal.user_name == user_name) {
      return needs + ". User " + user_name + " does not hold it (see SHOW GRANTS)";
    }
    return needs + ": a view the statement reads runs with its owner's rights, and its owner lacks a privilege (" +
           principal.user_name + " does not hold it)";
  };
  if (!request.paths) {
    // Not known until DuckDB binds it, and binding already reads: only a principal that may
    // reach every kind of storage may name a path DuckDB has to compute.
    for (const auto kind : {StorageKind::kFilesystem, StorageKind::kObjectStorage, StorageKind::kHttp}) {
      const auto privilege = StoragePrivilege(kind, request.mode);
      for (const auto* principal : restricted) {
        if (snapshot.HasSystemPrivilege(*principal, privilege)) continue;
        if (principal->rules_administrator) return lacks(*principal, privilege, {});
        return blocked + ": its path is an expression or a parameter, which Hardcharger can't check before DuckDB "
                         "reads it. Write the path as a string literal (a computed path needs " +
               Joined({SystemPrivilegeName(StoragePrivilege(StorageKind::kFilesystem, request.mode)),
                       SystemPrivilegeName(StoragePrivilege(StorageKind::kObjectStorage, request.mode)),
                       SystemPrivilegeName(StoragePrivilege(StorageKind::kHttp, request.mode))}) +
               " everywhere)";
      }
    }
    return std::nullopt;
  }
  for (const auto& path : *request.paths) {
    // A relative local path is looked for in each file_search_path directory too.
    std::vector<std::string> candidates{path};
    if (!search_path_.empty() && IsLocalFilePath(path) && !path.starts_with("~") &&
        !(path.size() >= 5 && (path.compare(0, 5, "file:") == 0 || path.compare(0, 5, "FILE:") == 0)) &&
        !std::filesystem::path(path).is_absolute()) {
      for (const auto& directory : search_path_) candidates.push_back(directory + "/" + path);
    }
    for (const auto& candidate : candidates) {
      auto parsed = ParseStoragePath(candidate, local_, /*allow_pattern=*/request.mode == StorageMode::kRead);
      if (!parsed.ok()) return blocked + ": " + parsed.status().message();
      if (!lakes_) lakes_ = LakeAreas();
      if (!lakes_->ok()) return blocked + " of '" + path + "': " + lakes_->status().message();
      for (const auto& lake : **lakes_) {
        if (StorageOverlaps(*parsed, request.directory, lake.area)) {
          return blocked + " of '" + path + "': it reaches into the storage of DuckLake catalog " + lake.catalog +
                 " ('" + lake.area.text + "'), which only DuckLake itself reads and writes, for statements on its "
                 "tables";
        }
      }
      const auto privilege = StoragePrivilege(parsed->kind, request.mode);
      for (const auto* principal : restricted) {
        if (!snapshot.HasStorageAccess(*principal, privilege, *parsed, local_)) return lacks(*principal, privilege, path);
      }
    }
  }
  return std::nullopt;
}

std::vector<std::string> KnownLakeDataPaths() {
  std::vector<std::string> paths;
  std::lock_guard lock(CacheMutex());
  for (const auto& [key, cached] : Cache()) paths.insert(paths.end(), cached.paths.begin(), cached.paths.end());
  return paths;
}

}  // namespace hardcharger::authz
