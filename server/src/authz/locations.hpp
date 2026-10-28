// Where a statement reads or writes files, and which storage privileges reach it.
//
// Three kinds of storage, each with a READ and a WRITE privilege: the server's local file
// system (READ / WRITE FILESYSTEM), object storage (s3://, gs://, r2://, az://, abfss://:
// READ / WRITE OBJECT STORAGE) and the web (http://, https://, hf://: READ / WRITE HTTP).
// A privilege is granted everywhere (GRANT READ HTTP TO r) or on locations (GRANT READ
// OBJECT STORAGE ON LOCATION 's3://bucket/exports/' TO r); a location covers itself and
// everything below it.
//
// Paths are compared by keys. A local path's keys are every spelling the server's file
// guards compare (guards.hpp: absolute, lexically normal, symlinks resolved, case-folded
// where the file system is). A remote path's key is its scheme (aliases folded: s3a and s3n
// are s3, gcs is gs, azure is az, abfs is abfss), its bucket, container or host in lower
// case, and its path. A remote path that could name one object two ways, or that makes
// DuckDB read with settings of its own, is refused rather than compared: '.' or '..'
// segments, '//' inside the path, a backslash, user information in the host,
// percent-escapes (in object storage; on the web only escaped '.', '/' and '\'), and in
// object storage '?' (DuckDB reads query parameters as S3 settings, the endpoint and
// credentials among them) and '#'. An unknown scheme is refused too.
#pragma once

#include <arrow/result.h>

#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "authz/types.hpp"

namespace hardcharger::authz {

enum class StorageKind { kFilesystem, kObjectStorage, kHttp };
enum class StorageMode { kRead, kWrite };

// READ FILESYSTEM, WRITE OBJECT STORAGE, ...
SystemPrivilege StoragePrivilege(StorageKind kind, StorageMode mode);
// The kind and mode of a storage privilege; nullopt for the other system privileges.
std::optional<std::pair<StorageKind, StorageMode>> StorageOf(SystemPrivilege privilege);
// Privileges that, granted everywhere, live in the store's original system_privileges
// table: every system privilege but the four object storage and HTTP ones.
bool IsOriginalSystemPrivilege(SystemPrivilege privilege);
// "the server's file system", "object storage", "the web"
std::string StorageKindName(StorageKind kind);

// A path a statement names, made comparable.
struct StoragePath {
  StorageKind kind = StorageKind::kFilesystem;
  std::string text;               // as the statement named it
  std::vector<std::string> keys;  // every spelling; of a glob's fixed directory
  // Further keys that may name the same objects (an Azure path without its storage
  // account), compared only to keep a path out of DuckLake's storage.
  std::vector<std::string> other_keys;
  bool pattern = false;  // a glob: it names what lies below its keys
};

// How the session resolves local paths: '~' is `home`.
struct LocalPathContext {
  std::string home;
};

// `path` as DuckDB will open it, or why it can't be compared. With `allow_pattern`, a glob
// ('*', '?', '[') names the files below its fixed part (a local or object storage path, or
// hf://; DuckDB never globs an http(s) URL).
arrow::Result<StoragePath> ParseStoragePath(const std::string& path, const LocalPathContext& context,
                                            bool allow_pattern);

// A GRANT's location: its kind and the key it is stored as. A local location is an absolute
// path; no location is a glob, and a web location has no query.
arrow::Result<std::pair<StorageKind, std::string>> ParseGrantLocation(const std::string& text);

// Does a grant on the location stored as `location_key` (of kind `kind`) cover `path`? Each
// key of `path` must lie within the location by one of the location's own spellings (a local
// location's symlinks resolved too), so that no symlink leads out of it.
bool LocationCovers(StorageKind kind, const std::string& location_key, const StoragePath& path,
                    const LocalPathContext& context);

// Is the location stored as `inner` within the one stored as `outer` (both of one kind)? An
// empty `outer` is everywhere; an empty `inner` (everywhere) is within that only.
bool LocationWithin(const std::string& outer, const std::string& inner);

// Do `path` and the storage `area` (a directory) overlap: is `path` inside it or, when `path`
// is a glob or `path_is_directory`, is `area` inside what `path` names?
bool StorageOverlaps(const StoragePath& path, bool path_is_directory, const StoragePath& area);

}  // namespace hardcharger::authz
