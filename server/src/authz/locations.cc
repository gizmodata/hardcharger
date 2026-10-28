#include "authz/locations.hpp"

#include <algorithm>
#include <cctype>
#include <filesystem>

#include "guards.hpp"

namespace hardcharger::authz {

namespace {

std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
  return s;
}

bool StartsWithIgnoringCase(const std::string& text, const std::string& prefix) {
  return text.size() >= prefix.size() && Lower(text.substr(0, prefix.size())) == prefix;
}

bool ContainsIgnoringCase(const std::string& text, const std::string& needle) {
  return Lower(text).find(needle) != std::string::npos;
}

struct Scheme {
  StorageKind kind;
  std::string family;  // the key's scheme: aliases folded
  bool globs;          // DuckDB globs paths of this scheme
};

std::optional<Scheme> SchemeOf(const std::string& scheme) {
  if (scheme == "s3" || scheme == "s3a" || scheme == "s3n") return Scheme{StorageKind::kObjectStorage, "s3", true};
  if (scheme == "gs" || scheme == "gcs") return Scheme{StorageKind::kObjectStorage, "gs", true};
  if (scheme == "r2") return Scheme{StorageKind::kObjectStorage, "r2", true};
  if (scheme == "az" || scheme == "azure") return Scheme{StorageKind::kObjectStorage, "az", true};
  if (scheme == "abfs" || scheme == "abfss") return Scheme{StorageKind::kObjectStorage, "abfss", true};
  if (scheme == "http" || scheme == "https") return Scheme{StorageKind::kHttp, scheme, false};
  if (scheme == "hf") return Scheme{StorageKind::kHttp, "hf", true};
  return std::nullopt;
}

arrow::Status Refuse(const std::string& path, const std::string& why) {
  return arrow::Status::Invalid("'", path, "' ", why);
}

// The fixed part of a glob: everything up to the '/' before its first wildcard (empty when
// there is no wildcard).
std::optional<std::size_t> FirstWildcard(const std::string& path) {
  const auto at = path.find_first_of("*?[");
  if (at == std::string::npos) return std::nullopt;
  return at;
}

// An Azure path without its storage account: az://<container>/<path>.
std::optional<std::string> AzureContainerKey(const std::string& family, const std::string& authority,
                                             const std::string& path) {
  if (family == "abfss") {
    const auto at = authority.find('@');
    if (at != std::string::npos) return "az://" + authority.substr(0, at) + path;
    return "az://" + authority + path;
  }
  if (family != "az") return std::nullopt;
  for (const char* host : {".blob.core.windows.net", ".dfs.core.windows.net"}) {
    const std::string suffix(host);
    if (authority.size() > suffix.size() &&
        authority.compare(authority.size() - suffix.size(), suffix.size(), suffix) == 0) {
      // az://<account>.blob.core.windows.net/<container>/<path>
      return "az:/" + path;
    }
  }
  return std::nullopt;
}

std::string TrimTrailingSlashes(std::string key, std::size_t keep) {
  while (key.size() > keep && key.back() == '/') key.pop_back();
  return key;
}

arrow::Result<StoragePath> ParseRemote(const std::string& text, bool allow_pattern) {
  const auto colon = text.find("://");
  const std::string scheme_name = Lower(text.substr(0, colon));
  const auto scheme = SchemeOf(scheme_name);
  if (!scheme) {
    return Refuse(text, "uses the scheme " + scheme_name +
                            "://, which access control can't place in the server's file system, object "
                            "storage or the web");
  }
  std::string rest = text.substr(colon + 3);
  const bool web = scheme->kind == StorageKind::kHttp && !scheme->globs;
  if (web) {
    // A query and a fragment are the server's business; they don't change which host and
    // path are asked.
    rest = rest.substr(0, rest.find_first_of("?#"));
  } else if (rest.find_first_of("?#") != std::string::npos) {
    return Refuse(text, "has a '?' or '#', which DuckDB reads as settings of its own (or, for '?', a "
                        "one-character wildcard); name the object without them");
  }
  const auto slash = rest.find('/');
  std::string authority = Lower(rest.substr(0, slash));
  std::string path = slash == std::string::npos ? std::string("/") : rest.substr(slash);
  if (authority.empty()) return Refuse(text, "names no bucket, container or host");
  if (authority.find('@') != std::string::npos && scheme->family != "abfss") {
    return Refuse(text, "has user information in its host");
  }
  if (scheme->family == "https" && authority.ends_with(":443")) authority.resize(authority.size() - 4);
  if (scheme->family == "http" && authority.ends_with(":80")) authority.resize(authority.size() - 3);
  if (path.find('\\') != std::string::npos || authority.find('\\') != std::string::npos) {
    return Refuse(text, "has a backslash");
  }
  if (scheme->kind == StorageKind::kObjectStorage || scheme->family == "hf") {
    if (path.find('%') != std::string::npos) return Refuse(text, "has a percent-escape");
  } else {
    for (const char* escape : {"%2e", "%2f", "%5c"}) {
      if (ContainsIgnoringCase(path, escape)) return Refuse(text, "has an escaped '.', '/' or '\\'");
    }
  }
  // Segments: none empty but a trailing one, none '.' or '..'.
  for (std::size_t start = 1; start <= path.size();) {
    const auto end = std::min(path.find('/', start), path.size());
    const std::string segment = path.substr(start, end - start);
    if (segment == "." || segment == "..") return Refuse(text, "has a '.' or '..' segment");
    if (segment.empty() && end < path.size()) return Refuse(text, "has an empty path segment ('//')");
    start = end + 1;
  }
  StoragePath parsed;
  parsed.kind = scheme->kind;
  parsed.text = text;
  if (allow_pattern && scheme->globs) {
    if (const auto wildcard = FirstWildcard(path)) {
      parsed.pattern = true;
      path = path.substr(0, path.rfind('/', *wildcard) + 1);
    }
  }
  const std::string root = scheme->family + "://" + authority;
  parsed.keys.push_back(TrimTrailingSlashes(root + path, root.size()));
  if (auto container = AzureContainerKey(scheme->family, authority, path)) {
    parsed.other_keys.push_back(TrimTrailingSlashes(*container, 5));
  }
  return parsed;
}

}  // namespace

SystemPrivilege StoragePrivilege(StorageKind kind, StorageMode mode) {
  const bool read = mode == StorageMode::kRead;
  switch (kind) {
    case StorageKind::kFilesystem:
      return read ? SystemPrivilege::kReadFilesystem : SystemPrivilege::kWriteFilesystem;
    case StorageKind::kObjectStorage:
      return read ? SystemPrivilege::kReadObjectStorage : SystemPrivilege::kWriteObjectStorage;
    case StorageKind::kHttp:
      return read ? SystemPrivilege::kReadHttp : SystemPrivilege::kWriteHttp;
  }
  return SystemPrivilege::kReadFilesystem;
}

std::optional<std::pair<StorageKind, StorageMode>> StorageOf(SystemPrivilege privilege) {
  switch (privilege) {
    case SystemPrivilege::kReadFilesystem:
      return std::make_pair(StorageKind::kFilesystem, StorageMode::kRead);
    case SystemPrivilege::kWriteFilesystem:
      return std::make_pair(StorageKind::kFilesystem, StorageMode::kWrite);
    case SystemPrivilege::kReadObjectStorage:
      return std::make_pair(StorageKind::kObjectStorage, StorageMode::kRead);
    case SystemPrivilege::kWriteObjectStorage:
      return std::make_pair(StorageKind::kObjectStorage, StorageMode::kWrite);
    case SystemPrivilege::kReadHttp:
      return std::make_pair(StorageKind::kHttp, StorageMode::kRead);
    case SystemPrivilege::kWriteHttp:
      return std::make_pair(StorageKind::kHttp, StorageMode::kWrite);
    default:
      return std::nullopt;
  }
}

bool IsOriginalSystemPrivilege(SystemPrivilege privilege) {
  const auto storage = StorageOf(privilege);
  return !storage || storage->first == StorageKind::kFilesystem;
}

std::string StorageKindName(StorageKind kind) {
  switch (kind) {
    case StorageKind::kFilesystem:
      return "the server's file system";
    case StorageKind::kObjectStorage:
      return "object storage";
    case StorageKind::kHttp:
      return "the web";
  }
  return "storage";
}

arrow::Result<StoragePath> ParseStoragePath(const std::string& path, const LocalPathContext& context,
                                            bool allow_pattern) {
  if (path.empty()) return arrow::Status::Invalid("an empty path");
  if (path.find('\0') != std::string::npos) return Refuse(path, "has a NUL character");
  if (!IsLocalFilePath(path)) return ParseRemote(path, allow_pattern);
  StoragePath parsed;
  parsed.kind = StorageKind::kFilesystem;
  parsed.text = path;
  std::string fixed = path;
  if (allow_pattern) {
    if (const auto wildcard = FirstWildcard(path)) {
      parsed.pattern = true;
      const auto separator = path.find_last_of("/\\", *wildcard);
      fixed = separator == std::string::npos ? std::string(".") : path.substr(0, separator + 1);
    }
  }
  parsed.keys = LocalPathKeys(fixed, context.home, /*resolve=*/true);
  if (parsed.keys.empty()) return Refuse(path, "is no path access control can compare");
  return parsed;
}

arrow::Result<std::pair<StorageKind, std::string>> ParseGrantLocation(const std::string& text) {
  if (text.empty()) return arrow::Status::Invalid("a location can't be empty");
  if (IsLocalFilePath(text)) {
    if (FirstWildcard(text)) return Refuse(text, "is a pattern; a location is a directory or a file");
    const bool url = StartsWithIgnoringCase(text, "file:");
    if (!url && (text.starts_with("~") || !std::filesystem::path(text).is_absolute())) {
      return Refuse(text, "is not an absolute path; a location on the server's file system must be one");
    }
    auto keys = LocalPathKeys(text, "", /*resolve=*/false);
    if (keys.empty()) return Refuse(text, "is no path access control can compare");
    return std::make_pair(StorageKind::kFilesystem, keys.front());
  }
  const auto colon = text.find("://");
  if (const auto scheme = SchemeOf(Lower(text.substr(0, colon)));
      scheme && scheme->kind == StorageKind::kHttp && text.find_first_of("?#") != std::string::npos) {
    return Refuse(text, "has a query or fragment; a location on the web is a host and a path");
  }
  ARROW_ASSIGN_OR_RAISE(auto parsed, ParseRemote(text, /*allow_pattern=*/false));
  if (FirstWildcard(parsed.keys.front().substr(parsed.keys.front().find("://") + 3))) {
    return Refuse(text, "is a pattern; a location is a prefix");
  }
  return std::make_pair(parsed.kind, parsed.keys.front());
}

bool LocationCovers(StorageKind kind, const std::string& location_key, const StoragePath& path,
                    const LocalPathContext& context) {
  if (kind != path.kind || path.keys.empty()) return false;
  std::vector<std::string> location_keys{location_key};
  if (kind == StorageKind::kFilesystem) location_keys = LocalPathKeys(location_key, context.home, /*resolve=*/true);
  return std::all_of(path.keys.begin(), path.keys.end(), [&](const std::string& key) {
    return std::any_of(location_keys.begin(), location_keys.end(),
                       [&](const std::string& location) { return PathKeyCovers(location, key); });
  });
}

bool LocationWithin(const std::string& outer, const std::string& inner) {
  // Both are stored keys, compared as they are.
  return outer.empty() || (!inner.empty() && PathKeyCovers(outer, inner));
}

bool StorageOverlaps(const StoragePath& path, bool path_is_directory, const StoragePath& area) {
  if (path.kind != area.kind) return false;
  auto all = [](const StoragePath& p) {
    std::vector<std::string> keys = p.keys;
    keys.insert(keys.end(), p.other_keys.begin(), p.other_keys.end());
    return keys;
  };
  const auto path_keys = all(path);
  const auto area_keys = all(area);
  const bool below_too = path.pattern || path_is_directory;
  for (const auto& p : path_keys) {
    for (const auto& a : area_keys) {
      if (PathKeyCovers(a, p)) return true;
      if (below_too && PathKeyCovers(p, a)) return true;
    }
  }
  return false;
}

}  // namespace hardcharger::authz
