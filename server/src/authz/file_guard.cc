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
#include <duckdb/common/virtual_file_system.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database.hpp>

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

}  // namespace

void InstallPrivateFileGuard(duckdb_v2_instance_handle instance, std::shared_ptr<const PrivateFiles> files) {
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  auto& vfs = dynamic_cast<dd::VirtualFileSystem&>(*dd::DBConfig::GetConfig(database).file_system);
  vfs.RegisterSubSystem(dd::make_uniq<PrivateFileGuard>(vfs, std::move(files)));
}

}  // namespace hardcharger::authz
