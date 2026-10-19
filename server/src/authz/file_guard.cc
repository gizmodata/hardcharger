// The serving instance's guard of the files no statement may reach: a sub-file-system of
// DuckDB's virtual file system that takes the paths it guards, so whatever a statement
// computes (a concatenated path, query('...'), a macro or view, a prepared statement's
// parameter, a glob's matches, file_search_path, a symlink, ~ under another
// home_directory), opening, listing, globbing, stat-ing, moving or removing it ends here.
//
//  - The server's own files (PrivateFiles, guards.hpp), always. An attached file (the
//    database, the instrumentation and log files) is opened only by the instance's own
//    storage, through the database's file opener (no client context), under the spelling
//    it was attached with; a store file by nothing at all.
//  - The sensitive paths (SensitivePaths: credential and system files), when
//    block_sensitive_paths is on, once the server's startup is over: then every local path
//    comes here, so each is checked by the path DuckDB resolved, whoever runs the
//    statement (the store's admin and DuckDB's own storage included). A glob whose fixed
//    part lies in a protected path is refused; one that reaches into a protected path from
//    above (/home/*/.ssh/*) lists its other matches only, as does a directory listing.
//    DuckDB's secret manager reads its persistent secrets past this file system (its
//    LocalDatabaseFileSystem), so they still load.

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

class ServerFileGuard : public dd::FileSystem {
 public:
  ServerFileGuard(dd::VirtualFileSystem& vfs, std::shared_ptr<const PrivateFiles> files,
                  std::shared_ptr<const SensitivePaths> sensitive)
      : vfs_(vfs),
        local_name_(vfs.GetDefaultFileSystem().GetName()),
        files_(std::move(files)),
        sensitive_(std::move(sensitive)) {}

  // (The name the private-file guard always had: a disabled_filesystems list may name it.)
  std::string GetName() const override { return "HardchargerPrivateFiles"; }
  bool IsManuallySet() override { return true; }

  bool CanHandleFile(const std::string& path) override {
    if (path.empty() || (path.find("://") != std::string::npos && !StartsWithFile(path))) return false;
    // With the local file system disabled, nothing local opens at all.
    if (vfs_.SubSystemIsDisabled(local_name_)) return false;
    // Every local path, checked by each operation (which knows the opener, and so ~).
    if (Sensitive()) return true;
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
    if (!Sensitive()) return Local().ListFiles(directory, callback, opener);
    // A listing names no protected path (a home directory's .ssh, .aws, ...).
    const std::string base = Expanded(directory, opener);
    return Local().ListFiles(
        directory,
        [&](dd::OpenFileInfo& info) {
          if (!sensitive_->Check(Local().JoinPath(base, info.path))) callback(info);
        },
        opener);
  }
  bool SupportsListFilesExtended() const override { return true; }

  dd::unique_ptr<dd::MultiFileList> GlobFilesExtended(const std::string& path, const dd::FileGlobInput& input,
                                                      dd::optional_ptr<dd::FileOpener> opener) override {
    // A pattern is checked as the path it spells ('*' an ordinary character): one whose
    // fixed part lies in a protected path (~/.ssh/*) is refused.
    Check(path, opener);
    auto matches = Local().Glob(path, input, opener);
    if (!Sensitive() || !matches) return matches;
    // One that reaches into a protected path from above (/home/*/.ssh/*, **/id_rsa, or
    // a relative pattern under file_search_path) lists its other matches only. (Reading a
    // match is checked again when it is opened.)
    dd::vector<dd::OpenFileInfo> allowed;
    for (auto& file : matches->GetAllFiles()) {
      if (!sensitive_->Check(file.path)) allowed.push_back(std::move(file));
    }
    return dd::make_uniq<dd::SimpleMultiFileList>(std::move(allowed));
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

  bool Sensitive() const { return sensitive_ && sensitive_->enforcing(); }

  // ~ is the instance's home_directory (DuckDB's rule, as its local file system applies it).
  static std::string Expanded(const std::string& path, dd::optional_ptr<dd::FileOpener> opener) {
    return !path.empty() && path[0] == '~' ? dd::FileSystem::ExpandPath(path, opener) : path;
  }

  void Check(const std::string& path, dd::optional_ptr<dd::FileOpener> opener) const {
    const std::string expanded = Expanded(path, opener);
    if (files_->MayName(expanded)) {
      if (const auto entry = files_->Find(expanded)) {
        // The instance's own storage: the database's opener (no client), under the
        // spelling the file was attached with. A client's statement, or another spelling
        // of the same file (which DuckDB would open as a second database), is refused.
        const bool own_storage =
            entry->attached && !dd::FileOpener::TryGetClientContext(opener) && path.rfind(entry->path, 0) == 0;
        if (!own_storage) throw dd::PermissionException(PrivateFileMessage(path, entry->category));
      }
    }
    if (Sensitive()) {
      if (const auto category = sensitive_->Check(expanded)) {
        throw dd::PermissionException(SensitivePathMessage(path, *category));
      }
    }
  }

  dd::VirtualFileSystem& vfs_;
  const std::string local_name_;
  std::shared_ptr<const PrivateFiles> files_;
  std::shared_ptr<const SensitivePaths> sensitive_;
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
// 8.3), or, with block_sensitive_paths on, a protected path (SensitivePaths, by the same
// spellings). DuckDB-storage attaches are left alone: the file-system guard already
// covers them, and the server's own startup attaches its files that way.

// The list of table functions that open a local database file outside DuckDB's file
// system. postgres_scanner and ducklake take a connection string or go through the file
// system, and httpfs is remote, so only sqlite_scanner's are here.
bool OpensFileOutsideFileSystem(const std::string& function_name) {
  const std::string name = dd::StringUtil::Lower(function_name);
  return name == "sqlite_scan" || name == "sqlite_query" || name == "sqlite_attach";
}

class AttachGuardInfo : public dd::PlannerExtensionInfo {
 public:
  AttachGuardInfo(std::shared_ptr<const PrivateFiles> files, std::shared_ptr<const SensitivePaths> sensitive)
      : files(std::move(files)), sensitive(std::move(sensitive)) {}
  std::shared_ptr<const PrivateFiles> files;
  std::shared_ptr<const SensitivePaths> sensitive;
};

// Refuses `path` when it is one of the server's own files (or a sibling), or, with the
// sensitive path guard on, a protected path. `path` is the bare filesystem path, any
// storage-type prefix already stripped.
void RefuseIfGuarded(const AttachGuardInfo& info, const std::string& path) {
  if (path.empty()) return;
  if (auto entry = info.files->Find(path)) {
    throw dd::PermissionException(PrivateFileMessage(path, entry->category));
  }
  if (info.sensitive && info.sensitive->enforcing()) {
    if (auto category = info.sensitive->Check(path)) throw dd::PermissionException(SensitivePathMessage(path, *category));
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

void CheckPlan(const AttachGuardInfo& guard, dd::LogicalOperator& op) {
  if (op.type == dd::LogicalOperatorType::LOGICAL_ATTACH) {
    auto& info = *op.Cast<dd::LogicalAttach>().info;
    std::string path;
    if (!AttachStorageType(info, path).empty()) RefuseIfGuarded(guard, path);
  } else if (op.type == dd::LogicalOperatorType::LOGICAL_GET) {
    auto& get = op.Cast<dd::LogicalGet>();
    if (OpensFileOutsideFileSystem(get.function.GetName().GetIdentifierName())) {
      // The database file is the first positional argument (sqlite_attach('<path>'),
      // sqlite_scan('<path>', '<table>'), sqlite_query('<path>', '<sql>')).
      for (const auto& parameter : get.parameters) {
        if (parameter.type().id() == dd::LogicalTypeId::VARCHAR && !parameter.IsNull()) {
          RefuseIfGuarded(guard, parameter.ToString());
          break;
        }
      }
    }
  }
  for (auto& child : op.children) {
    if (child) CheckPlan(guard, *child);
  }
}

void AttachGuardPostBind(dd::PlannerExtensionInput& input, dd::BoundStatement& statement) {
  auto* info = dynamic_cast<AttachGuardInfo*>(input.info.get());
  if (!info || !info->files || !statement.plan) return;
  CheckPlan(*info, *statement.plan);
}

}  // namespace

void InstallFileGuard(duckdb_v2_instance_handle instance, std::shared_ptr<const PrivateFiles> files,
                      std::shared_ptr<const SensitivePaths> sensitive) {
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  auto& config = dd::DBConfig::GetConfig(database);
  auto& vfs = dynamic_cast<dd::VirtualFileSystem&>(*config.file_system);
  vfs.RegisterSubSystem(dd::make_uniq<ServerFileGuard>(vfs, files, sensitive));
  // And the extensions that open files outside that file system (sqlite_scanner): refused
  // at plan time by the resolved path. Runs for every connection, the server's own
  // included, which attach their files as DuckDB storage and so are never caught here.
  dd::PlannerExtension extension;
  extension.post_bind_function = AttachGuardPostBind;
  extension.planner_info = dd::make_shared_ptr<AttachGuardInfo>(std::move(files), std::move(sensitive));
  dd::PlannerExtension::Register(config, std::move(extension));
}

}  // namespace hardcharger::authz
