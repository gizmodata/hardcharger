// The serving instance's guard of the server's own files (PrivateFiles, guards.hpp): a
// sub-file-system of DuckDB's virtual file system that takes every path naming one of
// them, so whatever a statement computes (a concatenated path, a glob's matches,
// file_search_path, a symlink, ~ under another home_directory), opening, listing,
// moving or removing it ends here.
//
// An attached file (the database, the instrumentation and log files) is opened only by
// the instance's own storage, through the database's file opener (no client context),
// under the spelling it was attached with; a store file by nothing at all.

#include <duckdb.hpp>
#include <duckdb/common/exception.hpp>
#include <duckdb/common/file_opener.hpp>
#include <duckdb/common/file_system.hpp>
#include <duckdb/common/memory_mapped_file.hpp>
#include <duckdb/common/multi_file/multi_file_list.hpp>
#include <duckdb/common/string_util.hpp>
#include <duckdb/common/virtual_file_system.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database.hpp>
#include <duckdb/main/database_path_and_type.hpp>
#include <duckdb/main/extension_helper.hpp>
#include <duckdb/planner/operator/logical_attach.hpp>
#include <duckdb/planner/operator/logical_get.hpp>
#include <duckdb/planner/planner_extension.hpp>

#include "authz/authz.hpp"
#include "guards.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;

class PrivateFileGuard : public dd::FileSystem {
 public:
  PrivateFileGuard(dd::VirtualFileSystem& vfs, std::shared_ptr<const PrivateFiles> files)
      : vfs_(vfs), files_(std::move(files)) {}

  std::string GetName() const override { return "HardchargerPrivateFiles"; }
  bool IsManuallySet() override { return true; }

  bool CanHandleFile(const std::string& path) override {
    if (path.empty() || (path.find("://") != std::string::npos && !StartsWithFile(path))) return false;
    // ~ is the instance's home_directory, which only the opener knows: every such path
    // comes here, and Check expands it.
    if (path[0] == '~') return true;
    if (!files_->MayName(path)) return false;  // the usual case, at the cost of one lstat
    return files_->Find(path).has_value() || files_->IsCopyTwin(path);
  }

  // ---- Paths ----

  dd::unique_ptr<dd::FileHandle> OpenFileExtended(const dd::OpenFileInfo& file, dd::FileOpenFlags flags,
                                                  dd::optional_ptr<dd::FileOpener> opener) override {
    Check(file.path, opener);
    return Local().OpenFile(file, flags, opener);
  }
  bool SupportsOpenFileExtended() const override { return true; }

  dd::unique_ptr<dd::MemoryMappedFile> MemoryMapFile(const dd::OpenFileInfo& file, dd::FileOpenFlags flags,
                                                     const dd::MMapOptions& options,
                                                     dd::optional_ptr<dd::FileOpener> opener) override {
    Check(file.path, opener);
    return Local().MemoryMapFile(file, flags, options, opener);
  }

  dd::optional<dd::FileMetadata> GetStatsIfExists(const dd::OpenFileInfo& file,
                                                  dd::optional_ptr<dd::FileOpener> opener) override {
    Check(file.path, opener);
    return Local().GetStatsIfExists(file, opener);
  }

  bool DirectoryExists(const std::string& directory, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    return Local().DirectoryExists(directory, opener);
  }
  void CreateDirectory(const std::string& directory, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    Local().CreateDirectory(directory, opener);
  }
  bool CreateDirectoryExtended(const std::string& directory, const dd::CreateDirectoryOptions& options,
                               dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    return Local().CreateDirectoryExtended(directory, options, opener);
  }
  void CreateDirectoriesRecursive(const std::string& path, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(path, opener);
    Local().CreateDirectoriesRecursive(path, opener);
  }
  void RemoveDirectory(const std::string& directory, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    Local().RemoveDirectory(directory, opener);
  }
  bool RemoveDirectoryExtended(const std::string& directory, const dd::RemoveDirectoryOptions& options,
                               dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    return Local().RemoveDirectoryExtended(directory, options, opener);
  }

  bool ListFilesExtended(const std::string& directory, const std::function<void(dd::OpenFileInfo& info)>& callback,
                         dd::optional_ptr<dd::FileOpener> opener) override {
    Check(directory, opener);
    return Local().ListFiles(directory, callback, opener);
  }
  bool SupportsListFilesExtended() const override { return true; }

  dd::unique_ptr<dd::MultiFileList> GlobFilesExtended(const std::string& path, const dd::FileGlobInput& input,
                                                      dd::optional_ptr<dd::FileOpener> opener) override {
    Check(path, opener);
    return Local().Glob(path, input, opener);
  }
  bool SupportsGlobExtended() const override { return true; }

  // The virtual file system picks the file system by the source: COPY ... TO renames its
  // tmp_ twin onto the target, which is checked here too.
  void MoveFile(const std::string& source, const std::string& target,
                dd::optional_ptr<dd::FileOpener> opener) override {
    Check(source, opener);
    Check(target, opener);
    Local().MoveFile(source, target, opener);
  }
  bool FileExists(const std::string& filename, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(filename, opener);
    return Local().FileExists(filename, opener);
  }
  bool IsPipe(const std::string& filename, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(filename, opener);
    return Local().IsPipe(filename, opener);
  }
  void RemoveFile(const std::string& filename, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(filename, opener);
    Local().RemoveFile(filename, opener);
  }
  bool TryRemoveFile(const std::string& filename, dd::optional_ptr<dd::FileOpener> opener) override {
    Check(filename, opener);
    return Local().TryRemoveFile(filename, opener);
  }
  void RemoveFiles(const dd::vector<std::string>& filenames, dd::optional_ptr<dd::FileOpener> opener) override {
    for (const auto& filename : filenames) Check(filename, opener);
    Local().RemoveFiles(filenames, opener);
  }

  // ---- No file access ----

  std::string PathSeparator(const std::string& path) override { return Local().PathSeparator(path); }
  bool IsPathAbsolute(const std::string& path) override { return Local().IsPathAbsolute(path); }
  std::string ExpandPath(const std::string& path) override { return Local().ExpandPath(path); }
  std::string GetHomeDirectory() override { return Local().GetHomeDirectory(); }
  std::string CanonicalizePath(const std::string& path, dd::optional_ptr<dd::FileOpener> opener) override {
    return Local().CanonicalizePath(path, opener);
  }

 private:
  static bool StartsWithFile(const std::string& path) {
    return path.size() >= 5 && (path[0] == 'f' || path[0] == 'F') && dd::StringUtil::CIEquals(path.substr(0, 5), "file:");
  }

  // The local file system, as DuckDB itself would pick it (refused when an operator
  // disabled it).
  dd::FileSystem& Local() { return vfs_.GetDefaultFileSystem(); }

  void Check(const std::string& path, dd::optional_ptr<dd::FileOpener> opener) const {
    const std::string expanded = !path.empty() && path[0] == '~' ? dd::FileSystem::ExpandPath(path, opener) : path;
    const auto entry = files_->Find(expanded);
    if (!entry) return;
    // The instance's own storage: the database's opener (no client), under the spelling
    // the file was attached with. A client's statement, or another spelling of the same
    // file (which DuckDB would open as a second database), is refused.
    if (entry->attached && !dd::FileOpener::TryGetClientContext(opener) && path.rfind(entry->path, 0) == 0) return;
    throw dd::PermissionException(PrivateFileMessage(path, entry->category));
  }

  dd::VirtualFileSystem& vfs_;
  std::shared_ptr<const PrivateFiles> files_;
};

// ---- The attach guard ----
//
// The file-system guard above closes every path DuckDB's own file system opens. An
// extension that does its own file I/O never reaches it: sqlite_scanner opens the file
// with SQLite's C API (ATTACH '<path>' (TYPE sqlite), the sqlite: path prefix, and the
// sqlite_scan / sqlite_query / sqlite_attach table functions), so a statement could create
// or read one of the server's own files through it (a SQLite file written where the
// database's WAL goes made a committed INSERT vanish after a crash and restart). So, at
// plan time (after binding, when a computed path -- a concatenation, a variable, a
// subquery -- has been resolved to a string), any attach or table function whose storage
// is not DuckDB's own is refused when its path names one of the server's files or a
// sibling, by every spelling PrivateFiles knows (relative, .., symlink, hard link, case,
// 8.3). DuckDB-storage attaches are left alone: the file-system guard already covers them,
// and the server's own startup attaches its files that way.

// The list of table functions that open a local database file outside DuckDB's file
// system. postgres_scanner and ducklake take a connection string or go through the file
// system, and httpfs is remote, so only sqlite_scanner's are here.
bool OpensFileOutsideFileSystem(const std::string& function_name) {
  const std::string name = dd::StringUtil::Lower(function_name);
  return name == "sqlite_scan" || name == "sqlite_query" || name == "sqlite_attach";
}

class AttachGuardInfo : public dd::PlannerExtensionInfo {
 public:
  explicit AttachGuardInfo(std::shared_ptr<const PrivateFiles> files) : files(std::move(files)) {}
  std::shared_ptr<const PrivateFiles> files;
};

// Refuses `path` when it is one of the server's own files (or a sibling). `path` is the
// bare filesystem path, any storage-type prefix already stripped.
void RefuseIfPrivate(const PrivateFiles& files, const std::string& path) {
  if (path.empty()) return;
  if (auto entry = files.Find(path, /*thorough=*/true)) {
    throw dd::PermissionException(PrivateFileMessage(path, entry->category));
  }
}

// An attach's effective storage type, lower-cased and alias-resolved (sqlite3 -> sqlite),
// and the path with any type prefix stripped. db_type is empty for a DuckDB-storage attach
// (an explicit TYPE duckdb, or none, when DuckDB reads the file's magic bytes -- a path
// that is a DuckDB file, which the file-system guard already covers).
std::string AttachStorageType(dd::AttachInfo& info, std::string& path) {
  path = info.path;
  std::string db_type;
  for (const auto& [key, value] : info.options) {
    if (dd::StringUtil::CIEquals(key, "type") && !value.IsNull()) {
      db_type = dd::StringUtil::Lower(value.ToString());
    }
  }
  if (db_type.empty()) {
    // sqlite:/path, postgres:dbname, ... : the prefix names the storage and is stripped.
    dd::DBPathAndType::ExtractExtensionPrefix(path, db_type);
  }
  if (db_type.empty()) return "";
  db_type = dd::StringUtil::Lower(dd::ExtensionHelper::ApplyExtensionAlias(db_type));
  return db_type == "duckdb" ? "" : db_type;
}

void CheckPlan(const PrivateFiles& files, dd::LogicalOperator& op) {
  if (op.type == dd::LogicalOperatorType::LOGICAL_ATTACH) {
    auto& info = *op.Cast<dd::LogicalAttach>().info;
    std::string path;
    if (!AttachStorageType(info, path).empty()) RefuseIfPrivate(files, path);
  } else if (op.type == dd::LogicalOperatorType::LOGICAL_GET) {
    auto& get = op.Cast<dd::LogicalGet>();
    if (OpensFileOutsideFileSystem(get.function.GetName().GetIdentifierName())) {
      // The database file is the first positional argument (sqlite_attach('<path>'),
      // sqlite_scan('<path>', '<table>'), sqlite_query('<path>', '<sql>')).
      for (const auto& parameter : get.parameters) {
        if (parameter.type().id() == dd::LogicalTypeId::VARCHAR && !parameter.IsNull()) {
          RefuseIfPrivate(files, parameter.ToString());
          break;
        }
      }
    }
  }
  for (auto& child : op.children) {
    if (child) CheckPlan(files, *child);
  }
}

void AttachGuardPostBind(dd::PlannerExtensionInput& input, dd::BoundStatement& statement) {
  auto* info = dynamic_cast<AttachGuardInfo*>(input.info.get());
  if (!info || !info->files || !statement.plan) return;
  CheckPlan(*info->files, *statement.plan);
}

}  // namespace

void InstallPrivateFileGuard(duckdb_v2_instance_handle instance, std::shared_ptr<const PrivateFiles> files) {
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  auto& config = dd::DBConfig::GetConfig(database);
  auto& vfs = dynamic_cast<dd::VirtualFileSystem&>(*config.file_system);
  vfs.RegisterSubSystem(dd::make_uniq<PrivateFileGuard>(vfs, files));
  // And the extensions that open files outside that file system (sqlite_scanner): refused
  // at plan time by the resolved path. Runs for every connection, the server's own
  // included, which attach their files as DuckDB storage and so are never caught here.
  dd::PlannerExtension extension;
  extension.post_bind_function = AttachGuardPostBind;
  extension.planner_info = dd::make_shared_ptr<AttachGuardInfo>(std::move(files));
  dd::PlannerExtension::Register(config, std::move(extension));
}

}  // namespace hardcharger::authz
