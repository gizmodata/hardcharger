#include "guards.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <set>

#include "duckdb_sql.hpp"
#include "platform.hpp"
#include "warpsql/errors.hpp"

#ifndef _WIN32
#include <sys/stat.h>
#endif

namespace hardcharger {

namespace {

namespace fs = std::filesystem;
using warpsql::Error;
using warpsql::ErrorCode;

constexpr const char* kSystemAccounts = "system account files";
constexpr const char* kProcFs = "process information (/proc)";
constexpr const char* kContainerSecrets = "container and Kubernetes secrets";
constexpr const char* kUserCredentials = "user credential store";
constexpr const char* kDuckDBSecrets = "DuckDB persistent secrets";
constexpr const char* kServerCredentials = "Hardcharger server credentials";
constexpr const char* kOperatorPath = "operator-protected path";
#ifdef _WIN32
constexpr const char* kWindowsCredentials = "Windows credential and key store";
constexpr const char* kDevices = "Windows device namespace";
constexpr const char* kAdminShares = "Windows administrative share";
#endif

std::string Lower(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

bool StartsWithIgnoringCase(std::string_view text, std::string_view prefix) {
  return text.size() >= prefix.size() && Lower(text.substr(0, prefix.size())) == prefix;
}

// ---- Paths ----

// Remote only when it starts with a scheme we know is not the local file system.
bool IsProvenRemote(std::string_view path) {
  static const std::array<std::string_view, 16> kSchemes = {
      "s3://", "s3a://", "s3n://", "gs://", "gcs://", "r2://", "az://", "azure://",
      "abfs://", "abfss://", "http://", "https://", "hf://", "remote://", "hdfs://", "webhdfs://"};
  for (const auto scheme : kSchemes) {
    if (StartsWithIgnoringCase(path, scheme)) return true;
  }
  return false;
}

// The path DuckDB opens for a file: URL (its FileSystem::ExpandPath): file:/x, file:///x
// and file://localhost/x name /x, and on Windows the '/' before a drive goes too
// (file:///C:/x is C:/x). Any other file://host/x is compared as host/x.
std::string StripFileScheme(const std::string& path) {
  if (!StartsWithIgnoringCase(path, "file:/")) return path;
#ifdef _WIN32
  constexpr std::size_t kDriveSlash = 1;
#else
  constexpr std::size_t kDriveSlash = 0;
#endif
  if (path.size() < 7 || path[6] != '/') return path.substr(5 + kDriveSlash);  // file:/x
  if (path.size() >= 8 && path[7] == '/') return path.substr(7 + kDriveSlash);  // file:///x
  if (StartsWithIgnoringCase(path.substr(7), "localhost/")) return path.substr(16 + kDriveSlash);
  return path.substr(7);
}

#ifdef _WIN32
// Windows spells one file many ways: '\' or '/', \\?\C:\x and \\.\C:\x for C:\x,
// \\?\UNC\host\share for \\host\share, and it drops a component's trailing dots and
// spaces and opens x::$DATA (a stream) as x. This brings them to one ('/' separators;
// each component cut at a stream's ':'), before std::filesystem sees the path.
std::string WindowsSpelling(std::string path) {
  std::replace(path.begin(), path.end(), '\\', '/');
  if (StartsWithIgnoringCase(path, "//?/unc/")) {
    path = "//" + path.substr(8);
  } else if ((path.starts_with("//?/") || path.starts_with("//./")) && path.size() >= 6 &&
             std::isalpha(static_cast<unsigned char>(path[4])) && path[5] == ':') {
    path = path.substr(4);
  }
  std::string out;
  std::size_t start = 0;
  // The drive ("C:") and a UNC or device prefix ("//") stay as they are.
  if (path.size() >= 2 && std::isalpha(static_cast<unsigned char>(path[0])) && path[1] == ':') {
    out = path.substr(0, 2);
    start = 2;
  }
  while (start < path.size()) {
    const auto end = std::min(path.find('/', start), path.size());
    std::string part = path.substr(start, end - start);
    if (part != "." && part != "..") {
      if (const auto colon = part.find(':'); colon != std::string::npos) part.resize(colon);
      while (!part.empty() && (part.back() == '.' || part.back() == ' ')) part.pop_back();
    }
    out += part;
    if (end < path.size()) out += '/';
    start = end + 1;
  }
  return out;
}
#endif

// Case-insensitive file systems (macOS, Windows) open /ETC/PASSWD as /etc/passwd, so
// keys are case-folded there.
std::string ComparisonKey(std::string s) {
  while (s.size() > 1 && s.back() == '/') s.pop_back();
#if defined(__APPLE__) || defined(_WIN32)
  s = Lower(s);
#endif
  return s;
}

// Every spelling of `raw` to compare: absolute and lexically normal, and with
// symlinks resolved as far as the path exists (when `resolve`).
std::vector<std::string> Keys(std::string raw, const std::string& home, bool resolve) {
  std::vector<std::string> keys;
  if (raw.empty()) return keys;
#ifdef _WIN32
  raw = WindowsSpelling(std::move(raw));
#endif
  if (raw == "~" || raw.starts_with("~/")) raw = home + raw.substr(1);
  std::error_code ec;
  fs::path path = PathFromUtf8(raw);
  fs::path absolute = path.is_absolute() ? path : fs::absolute(path, ec);
  if (ec) absolute = path;
  absolute = absolute.lexically_normal();
  keys.push_back(ComparisonKey(Utf8Path(absolute)));
  if (resolve) {
    fs::path resolved = fs::weakly_canonical(absolute, ec);
    if (!ec) {
      std::string text = Utf8Path(resolved);
#ifdef _WIN32
      text = WindowsSpelling(std::move(text));  // canonical paths come back as \\?\C:\...
#endif
      auto key = ComparisonKey(std::move(text));
      if (key != keys.front()) keys.push_back(std::move(key));
    }
  }
  return keys;
}

std::vector<std::string> Segments(const std::string& path) {
  std::vector<std::string> out;
  std::size_t start = 0;
  while (start <= path.size()) {
    const auto end = path.find('/', start);
    const std::string part = path.substr(start, end == std::string::npos ? std::string::npos : end - start);
    if (!part.empty()) out.push_back(part);
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return out;
}

bool KeyCovers(const std::string& entry, const std::string& candidate) {
  if (candidate.size() < entry.size() || candidate.compare(0, entry.size(), entry) != 0) return false;
  return candidate.size() == entry.size() || entry == "/" || candidate[entry.size()] == '/';
}

// ---- Tokens ----

// Parenthesis depth change of an operator token ("(", "),", "))" ...).
int DepthChange(const Token& token) {
  if (!token.Is(DUCKDB_V2_TOKEN_TYPE_OPERATOR)) return 0;
  int change = 0;
  for (char c : token.text) {
    if (c == '(') change++;
    if (c == ')') change--;
  }
  return change;
}

Piece Rest(const Piece& piece, std::size_t from) {
  Piece rest;
  if (from >= piece.tokens.size()) return rest;
  rest.tokens.assign(piece.tokens.begin() + static_cast<std::ptrdiff_t>(from), piece.tokens.end());
  const char* begin = rest.tokens.front().text.data();
  const char* end = piece.text.data() + piece.text.size();
  rest.text = std::string_view(begin, static_cast<std::size_t>(end - begin));
  return rest;
}

const std::set<std::string>& FileReadingFunctions() {
  static const std::set<std::string> kFunctions = {
      "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
      "read_json_objects", "read_json_objects_auto", "read_ndjson", "read_ndjson_auto",
      "read_ndjson_objects", "read_text", "read_blob", "glob", "sniff_csv", "parquet_metadata",
      "parquet_schema", "parquet_file_metadata", "parquet_kv_metadata", "parquet_bloom_probe",
      "read_text_auto", "read_single_csv_file", "read_single_json_file", "read_xlsx", "st_read",
      "read_duckdb", "iceberg_scan", "delta_scan"};
  return kFunctions;
}

// Settings that change the whole instance (or reach the host) even without GLOBAL.
// Session-level tuning (search_path, TimeZone, ...) stays open to everyone.
bool IsInstanceSetting(const std::string& name) {
  static const std::set<std::string> kSettings = {
      "memory_limit", "max_memory", "threads", "worker_threads", "external_threads",
      "temp_directory", "max_temp_directory_size", "enable_external_access",
      "allow_unsigned_extensions", "allow_community_extensions", "allow_extensions_metadata_query",
      "autoinstall_known_extensions", "autoload_known_extensions", "custom_extension_repository",
      "autoinstall_extension_repository", "extension_directory", "enable_external_file_cache",
      "secret_directory", "allow_persistent_secrets", "disabled_filesystems", "allowed_directories",
      "allowed_paths", "home_directory", "lock_configuration", "access_mode", "checkpoint_threshold",
      "wal_autocheckpoint", "storage_compatibility_version", "default_block_size"};
  return kSettings.contains(name);
}

// A replacement scan's target: FROM 'file.csv' or FROM "dir/file.parquet".
bool LooksLikeFile(const std::string& name) {
  if (name.find('/') != std::string::npos || name.find('\\') != std::string::npos) return true;
  static const std::array<std::string_view, 14> kExtensions = {
      ".csv", ".tsv", ".parquet", ".json", ".ndjson", ".jsonl", ".txt", ".gz",
      ".zst", ".duckdb", ".db", ".xlsx", ".arrow", ".avro"};
  const std::string lower = Lower(name);
  return std::any_of(kExtensions.begin(), kExtensions.end(),
                     [&](std::string_view ext) { return lower.find(ext) != std::string::npos; });
}

// Indices of string literals and quoted identifiers a query reads as files: FROM
// '<file>' (also FROM-first syntax, joins and comma lists). A FROM counts only where a
// query is (after SELECT at the same depth, or first at its depth), so EXTRACT(x FROM
// y) and TRIM('x' FROM y) are not file reads.
std::vector<std::size_t> ReplacementScans(const Piece& piece) {
  std::vector<std::size_t> out;
  const auto& t = piece.tokens;
  std::vector<bool> select_at_depth = {false};
  std::vector<bool> in_from = {false};
  bool depth_start = true;
  for (std::size_t i = 0; i < t.size(); i++) {
    const int change = DepthChange(t[i]);
    if (change != 0) {
      for (int k = 0; k < change; k++) {
        select_at_depth.push_back(false);
        in_from.push_back(false);
      }
      for (int k = 0; k > change && select_at_depth.size() > 1; k--) {
        select_at_depth.pop_back();
        in_from.pop_back();
      }
      depth_start = change > 0;
      continue;
    }
    const bool first = depth_start;
    depth_start = false;
    if (t[i].IsWord("SELECT")) {
      select_at_depth.back() = true;
      continue;
    }
    if (t[i].IsWord("FROM") || t[i].IsWord("JOIN")) {
      const bool query = select_at_depth.back() || first || t[i].IsWord("JOIN");
      in_from.back() = query;
      if (query && i + 1 < t.size()) {
        const Token& next = t[i + 1];
        if (next.Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) ||
            (next.Is(DUCKDB_V2_TOKEN_TYPE_IDENTIFIER) && next.text.starts_with('"') &&
             LooksLikeFile(next.Value()))) {
          out.push_back(i + 1);
        }
      }
      continue;
    }
    if (in_from.back() && t[i].IsOp(",") && t[i].text == "," && i + 1 < t.size() &&
        t[i + 1].Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL)) {
      out.push_back(i + 1);
      continue;
    }
    if (t[i].IsWord("WHERE") || t[i].IsWord("GROUP") || t[i].IsWord("ORDER") || t[i].IsWord("HAVING") ||
        t[i].IsWord("LIMIT") || t[i].IsWord("UNION") || t[i].IsWord("QUALIFY") || t[i].IsWord("WINDOW")) {
      in_from.back() = false;
    }
  }
  return out;
}

// The gated table functions in `piece`, wherever they appear (subqueries, CTEs, joins,
// IN lists, CTAS, INSERT ... SELECT, UNION, UPDATE ... FROM, DELETE ... USING).
std::optional<std::string> GatedFunction(const Piece& piece) {
  const auto& t = piece.tokens;
  for (std::size_t i = 0; i + 1 < t.size(); i++) {
    if (!(t[i].Is(DUCKDB_V2_TOKEN_TYPE_IDENTIFIER) || t[i].Is(DUCKDB_V2_TOKEN_TYPE_KEYWORD))) continue;
    if (!t[i + 1].IsOp("(")) continue;
    const std::string name = t[i].Value();
    if (name == "duckdb_secrets") return "duckdb_secrets()";
    if (name.starts_with("quack_")) return name + "()";
    if (name == "checkpoint" || name == "force_checkpoint") return "CHECKPOINT";
    if (name == "import_database") return "IMPORT DATABASE";
    if (!FileReadingFunctions().contains(name)) continue;
    // Only a literal remote URL as the first argument is allowed; anything else (a
    // local path, a list, a computed path) is refused.
    const bool literal = t[i + 1].text == "(" && i + 2 < t.size() &&
                         t[i + 2].Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) &&
                         (i + 3 >= t.size() || t[i + 3].IsOp(",") || t[i + 3].IsOp(")"));
    if (literal && IsProvenRemote(t[i + 2].Value())) continue;
    return name + "() (local filesystem)";
  }
  for (std::size_t index : ReplacementScans(piece)) {
    const std::string target = t[index].Value();
    if (!IsProvenRemote(target)) return "reading a local file via FROM '" + target + "'";
  }
  return std::nullopt;
}

// SET/RESET [GLOBAL|SESSION|LOCAL] <name>, PRAGMA <name> = ...: the name and scope.
struct SetTarget {
  std::string name;
  bool global = false;
  bool automatic = true;
  bool reset = false;
  std::size_t value_from = 0;  // first value token, after = or TO
};

std::optional<SetTarget> SetOf(const Piece& piece) {
  const auto& t = piece.tokens;
  if (t.size() < 2) return std::nullopt;
  SetTarget target;
  std::size_t i = 1;
  if (t[0].IsWord("SET") || t[0].IsWord("RESET")) {
    target.reset = t[0].IsWord("RESET");
    if (t[1].IsWord("VARIABLE")) return std::nullopt;  // a user variable
    if (t[1].IsWord("GLOBAL")) {
      target.global = true;
      target.automatic = false;
      i++;
    } else if (t[1].IsWord("SESSION") || t[1].IsWord("LOCAL")) {
      target.automatic = false;
      i++;
    }
  } else if (!t[0].IsWord("PRAGMA")) {
    return std::nullopt;
  }
  if (i >= t.size()) return std::nullopt;
  target.name = t[i].Value();
  i++;
  if (t[0].IsWord("PRAGMA") && !(i < t.size() && t[i].IsOp("="))) return std::nullopt;  // a call
  if (i < t.size() && (t[i].IsOp("=") || t[i].IsWord("TO"))) i++;
  target.value_from = i;
  return target;
}

// Every SET/PRAGMA of `name` in `piece`, wherever it starts (so also inside EXPLAIN or
// PREPARE), with the piece from that SET on.
std::vector<std::pair<SetTarget, Piece>> SetsOf(const Piece& piece, std::string_view name) {
  std::vector<std::pair<SetTarget, Piece>> out;
  for (std::size_t i = 0; i < piece.tokens.size(); i++) {
    if (!(piece.tokens[i].IsWord("SET") || piece.tokens[i].IsWord("RESET") || piece.tokens[i].IsWord("PRAGMA"))) {
      continue;
    }
    Piece rest = Rest(piece, i);
    if (auto set = SetOf(rest); set && set->name == name) out.emplace_back(*set, std::move(rest));
  }
  return out;
}

// True only when the value is provably false; anything else may turn the setting on.
bool ValueIsFalse(const Piece& piece, std::size_t from) {
  std::vector<Token> value(piece.tokens.begin() + static_cast<std::ptrdiff_t>(from), piece.tokens.end());
  while (value.size() >= 2 && value.front().text == "(" && value.back().text == ")") {
    value.erase(value.begin());
    value.pop_back();
  }
  if (value.size() == 3 && value[1].text == "::" &&
      (value[2].IsWord("BOOLEAN") || value[2].IsWord("BOOL") || value[2].IsWord("LOGICAL"))) {
    value.resize(1);
  }
  if (value.size() != 1) return false;
  const Token& v = value.front();
  if (v.Is(DUCKDB_V2_TOKEN_TYPE_NUMBER_LITERAL)) return v.text == "0";
  if (v.Is(DUCKDB_V2_TOKEN_TYPE_KEYWORD) || v.Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL)) {
    const std::string text = Lower(v.Value());
    return text == "false" || text == "f" || text == "0" || text == "no" || text == "n" || text == "off";
  }
  return false;
}

// The statement inside EXPLAIN [ANALYZE] [(options)] or PREPARE name [(types)] AS.
std::optional<Piece> Wrapped(const Piece& piece, DUCKDB_V2_STATEMENT_TYPE type) {
  const auto& t = piece.tokens;
  std::size_t i = 0;
  if (type == DUCKDB_V2_STATEMENT_TYPE_EXPLAIN && !t.empty() && t[0].IsWord("EXPLAIN")) {
    i = 1;
    if (i < t.size() && (t[i].IsWord("ANALYZE") || t[i].IsWord("ANALYSE"))) i++;
    if (i < t.size() && t[i].IsOp("(")) {
      int depth = 0;
      do {
        depth += DepthChange(t[i]);
        i++;
      } while (i < t.size() && depth > 0);
    }
    return Rest(piece, i);
  }
  if (type == DUCKDB_V2_STATEMENT_TYPE_PREPARE && !t.empty() && t[0].IsWord("PREPARE")) {
    int depth = 0;
    for (i = 1; i < t.size(); i++) {
      depth += DepthChange(t[i]);
      if (depth == 0 && t[i].IsWord("AS")) return Rest(piece, i + 1);
    }
  }
  return std::nullopt;
}

// COPY ... TO|FROM '<path>' anywhere in the statement: DuckDB 2.0 also takes COPY inside
// a CTE (WITH c AS (COPY (...) TO 'x.csv') SELECT 1). The direction is the first TO or
// FROM at the COPY's own depth; only a literal remote URL there is allowed.
std::optional<std::string> LocalCopy(const Piece& piece) {
  const auto& t = piece.tokens;
  for (std::size_t start = 0; start < t.size(); start++) {
    if (!t[start].IsWord("COPY")) continue;
    if (start + 2 < t.size() && t[start + 1].IsWord("FROM") && t[start + 2].IsWord("DATABASE")) continue;
    int depth = 0;
    for (std::size_t i = start + 1; i < t.size(); i++) {
      depth += DepthChange(t[i]);
      if (depth < 0) break;  // the COPY's enclosing parenthesis closed
      if (depth != 0 || !(t[i].IsWord("TO") || t[i].IsWord("FROM"))) continue;
      if (i + 1 < t.size() && t[i + 1].Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) && IsProvenRemote(t[i + 1].Value())) {
        break;
      }
      return std::string(t[i].IsWord("TO") ? "COPY TO" : "COPY FROM") + " (local filesystem)";
    }
  }
  return std::nullopt;
}

[[noreturn]] void Deny(const std::string& message) {
  throw Error(ErrorCode::kPermissionDenied, message, "42501");
}

std::string GatedMessage(const std::string& category) {
  return "Permission denied: Hardcharger blocked " + category +
         ", which requires the 'admin' role. Hardcharger confines file-system and instance-level "
         "commands to admins, so users sharing this server cannot read, write or reconfigure the "
         "host. (Server operators: roles come from the sign-in token's 'role' claim.)";
}

}  // namespace

// ---- SensitivePaths ----

SensitivePaths::SensitivePaths(bool enabled, const std::vector<std::string>& operator_paths,
                               const std::vector<std::string>& server_files, std::string home)
    : enabled_(enabled), home_(home.empty() ? HomeDirectory() : std::move(home)) {
#ifdef _WIN32
  AddWindowsDefaults();
#else
  for (const char* path : {"/etc/passwd", "/etc/shadow", "/etc/gshadow", "/etc/master.passwd", "/etc/sudoers",
                           "/etc/sudoers.d"}) {
    Add(path, kSystemAccounts);
  }
  Add("/proc", kProcFs);
  // DuckDB sizes its default memory limit from the container's cgroup; that file holds
  // only cgroup membership and stays readable.
  allowed_keys_ = Keys("/proc/self/cgroup", home_, true);
  Add("/dev/fd", kProcFs);
  Add("/var/run/secrets", kContainerSecrets);
  Add("/run/secrets", kContainerSecrets);
#endif
  if (!home_.empty()) {
    for (const char* rel : {".ssh", ".aws", ".azure", ".config/gcloud", ".kube", ".docker/config.json", ".netrc",
                            ".pgpass", ".git-credentials", ".gnupg"}) {
      Add(home_ + "/" + rel, kUserCredentials);
    }
    Add(home_ + "/.duckdb/stored_secrets", kDuckDBSecrets);
  }
  for (const auto& path : server_files) {
    if (!path.empty()) Add(path, kServerCredentials);
  }
  for (const auto& path : operator_paths) {
    if (!path.empty()) Add(path, kOperatorPath);
  }
}

#ifdef _WIN32
// Windows' own: the account database and the credential and key stores (SAM and the
// registry hives, DPAPI master keys, Credential Manager, the vaults, private keys), the
// installer answer files that can hold an administrator's password, the device and pipe
// namespace (\\.\, \\?\GLOBALROOT, the /proc of Windows), and Kubernetes' secret mounts,
// which Windows pods get at C:\var\run\secrets.
void SensitivePaths::AddWindowsDefaults() {
  const std::string windows = EnvUtf8("SystemRoot").value_or("C:\\Windows");
  for (const char* rel : {"System32/config", "repair", "NTDS", "Panther", "System32/Sysprep"}) {
    Add(windows + "/" + rel, kSystemAccounts);
  }
  for (const char* rel : {"System32/Microsoft/Protect", "ServiceProfiles"}) {
    Add(windows + "/" + rel, kWindowsCredentials);
  }
  const std::string program_data = EnvUtf8("ProgramData").value_or("C:\\ProgramData");
  Add(program_data + "/Microsoft/Crypto", kWindowsCredentials);
  Add(program_data + "/Microsoft/Vault", kWindowsCredentials);
  Add("//./", kDevices);
  Add("//?/", kDevices);
  const std::string drive = EnvUtf8("SystemDrive").value_or("C:");
  Add(drive + "/var/run/secrets", kContainerSecrets);
  Add(drive + "/run/secrets", kContainerSecrets);
  Add("/var/run/secrets", kContainerSecrets);  // on the working directory's drive
  if (home_.empty()) return;
  std::vector<std::string> roaming = {home_ + "/AppData/Roaming"};
  std::vector<std::string> local = {home_ + "/AppData/Local"};
  if (auto dir = EnvUtf8("APPDATA")) roaming.push_back(*dir);
  if (auto dir = EnvUtf8("LOCALAPPDATA")) local.push_back(*dir);
  for (const auto& dir : roaming) {
    for (const char* rel : {"Microsoft/Credentials", "Microsoft/Protect", "Microsoft/Crypto",
                            "Microsoft/SystemCertificates", "Microsoft/Vault"}) {
      Add(dir + "/" + rel, kWindowsCredentials);
    }
    for (const char* rel : {"gcloud", "postgresql", "gnupg"}) {
      Add(dir + "/" + rel, kUserCredentials);
    }
  }
  for (const auto& dir : local) {
    for (const char* rel : {"Microsoft/Credentials", "Microsoft/Vault"}) {
      Add(dir + "/" + rel, kWindowsCredentials);
    }
  }
  Add(home_ + "/_netrc", kUserCredentials);
}
#endif

void SensitivePaths::Add(const std::string& path, const std::string& category) {
  for (auto& key : Keys(StripFileScheme(path), home_, true)) {
    std::lock_guard lock(mutex_);
    const bool present =
        std::any_of(entries_.begin(), entries_.end(), [&](const Entry& e) { return e.key == key; });
    if (!present) entries_.push_back({std::move(key), category});
  }
}

void SensitivePaths::AddSecretDirectory(const std::string& path) {
  if (!path.empty()) Add(path, kDuckDBSecrets);
}

std::optional<std::string> SensitivePaths::Check(const std::string& path) const {
  if (!enabled_ || path.empty() || path.size() > 4096 || IsProvenRemote(path) ||
      (path.find("://") != std::string::npos && !StartsWithIgnoringCase(path, "file://"))) {
    return std::nullopt;
  }
#ifdef _WIN32
  const std::string local = WindowsSpelling(StripFileScheme(path));
  // \\host\C$, \\host\ADMIN$, ...: a drive or system folder of some host, this one
  // included, over SMB, past every local spelling above.
  // \\?\GLOBALROOT\..., \\.\pipe\..., \\?\Volume{...}\...: devices, pipes and raw volumes
  // (\\?\C:\ and \\?\UNC\ were respelled above). Before the pattern check: '?' here is
  // no wildcard.
  if (local.starts_with("//?/") || local.starts_with("//./")) return kDevices;
  if (local.starts_with("//")) {
    const auto share = Segments(local.substr(2));
    if (share.size() >= 2 && share[1].ends_with('$')) return kAdminShares;
  }
#else
  const std::string local = StripFileScheme(path);
#endif
  // A pattern (glob('/home/*/.ssh/*'), read_csv('dir/*.csv')) is refused when it can
  // match a protected path or something inside one: the entries a glob lists can't be
  // filtered without a hook in DuckDB's file system, so they are never listed instead.
  if (const auto wildcard = local.find_first_of("*?[{"); wildcard != std::string::npos) {
    const auto slash = local.find_last_of('/', wildcard);
    const std::string prefix = slash == std::string::npos ? "." : local.substr(0, slash == 0 ? 1 : slash);
    const std::vector<std::string> pattern = Segments(ComparisonKey(local.substr(slash == std::string::npos ? 0 : slash + 1)));
    const bool recursive = local.find("**") != std::string::npos || local.find('{') != std::string::npos;
    std::lock_guard lock(mutex_);
    for (const auto& key : Keys(prefix, home_, true)) {
      for (const auto& entry : entries_) {
        if (KeyCovers(entry.key, key)) return entry.category;  // the pattern starts inside it
        if (!KeyCovers(key, entry.key)) continue;
        if (recursive) return entry.category;
        const auto inside = Segments(entry.key.substr(key.size() == 1 ? 1 : key.size() + 1));
        if (inside.size() > pattern.size()) continue;  // it can only list a parent directory
        bool matches = true;
        for (std::size_t i = 0; i < inside.size() && matches; i++) {
          matches = GlobSegmentMatches(pattern[i], inside[i]);
        }
        if (matches) return entry.category;
      }
    }
    return std::nullopt;
  }
  // Resolving symlinks costs a stat per component, so only for text that can name a
  // directory; a bare word is still compared as a path relative to the working directory.
  const bool resolve = local.find('/') != std::string::npos || local.starts_with('~') || local.starts_with('.');
  const auto candidates = Keys(local, home_, resolve);
  std::lock_guard lock(mutex_);
  for (const auto& candidate : candidates) {
    if (std::find(allowed_keys_.begin(), allowed_keys_.end(), candidate) != allowed_keys_.end()) {
      return std::nullopt;
    }
  }
  for (const auto& candidate : candidates) {
    for (const auto& entry : entries_) {
      if (KeyCovers(entry.key, candidate)) return entry.category;
    }
  }
  return std::nullopt;
}

// ---- PrivateFiles ----

namespace {

// The files DuckDB keeps beside a database file (storage_manager.cpp, config.cpp).
constexpr std::array<const char*, 5> kBeside = {"", ".wal", ".wal.checkpoint", ".wal.recovery", ".tmp"};

bool IsLocalPath(const std::string& path) {
  return !path.empty() && !IsProvenRemote(path) &&
         (path.find("://") == std::string::npos || StartsWithIgnoringCase(path, "file://"));
}

std::string LocalSpelling(const std::string& path, const std::string& home) {
#ifdef _WIN32
  std::string local = WindowsSpelling(StripFileScheme(path));
#else
  std::string local = StripFileScheme(path);
#endif
  if (local == "~" || local.starts_with("~/")) local = home + local.substr(1);
  return local;
}

}  // namespace

PrivateFiles::PrivateFiles(std::string home) : home_(home.empty() ? HomeDirectory() : std::move(home)) {}

void PrivateFiles::Add(const std::string& path, const std::string& category, bool attached) {
  if (path.empty()) return;
  Keyed keyed{{path, category, attached}, {}, {}};
  const fs::path file = PathFromUtf8(LocalSpelling(path, home_));
  for (const char* suffix : kBeside) {
    for (auto& key : Keys(LocalSpelling(path, home_) + suffix, home_, true)) keyed.keys.push_back(std::move(key));
    const fs::path twin = file.parent_path() / PathFromUtf8("tmp_" + Utf8Path(file.filename()) + suffix);
    for (auto& key : Keys(Utf8Path(twin), home_, true)) keyed.twins.push_back(std::move(key));
    keyed.names.push_back(ComparisonKey(Utf8Path(file.filename()) + suffix));
    keyed.names.push_back(ComparisonKey("tmp_" + Utf8Path(file.filename()) + suffix));
  }
  std::lock_guard lock(mutex_);
  entries_.push_back(std::move(keyed));
}

bool PrivateFiles::empty() const {
  std::lock_guard lock(mutex_);
  return entries_.empty();
}

// Every key `path` is compared by: its spellings (absolute, lexical, symlinks resolved as
// far as the path exists), and those of the files a dangling symlink at its end points to
// (a WAL that doesn't exist yet).
std::vector<std::string> PrivateFiles::CandidateKeys(const std::string& path, bool resolve) const {
  if (!IsLocalPath(path) || path.size() > 4096) return {};
  const std::string local = LocalSpelling(path, home_);
  std::vector<std::string> keys = Keys(local, home_, resolve);
  if (!resolve) return keys;
  std::error_code ec;
  fs::path at = PathFromUtf8(local);
  for (int hop = 0; hop < 8; hop++) {
    if (!fs::is_symlink(fs::symlink_status(at, ec)) || fs::exists(at, ec)) break;
    const fs::path target = fs::read_symlink(at, ec);
    if (ec) break;
    at = target.is_absolute() ? target : at.parent_path() / target;
    for (auto& key : Keys(Utf8Path(at), home_, true)) keys.push_back(std::move(key));
  }
  return keys;
}

bool PrivateFiles::MayName(const std::string& path) const {
  if (!IsLocalPath(path)) return false;
  const std::string local = LocalSpelling(path, home_);
  {
    std::lock_guard lock(mutex_);
    if (entries_.empty()) return false;
    for (const auto& part : Segments(ComparisonKey(local))) {
      for (const auto& keyed : entries_) {
        if (std::find(keyed.names.begin(), keyed.names.end(), part) != keyed.names.end()) return true;
      }
    }
  }
#ifdef _WIN32
  // An 8.3 short name (RBAC~1.DUC) shows none of the names.
  if (local.find('~') != std::string::npos) return true;
  std::error_code ec;
  const fs::path file = PathFromUtf8(local);
  const auto status = fs::symlink_status(file, ec);
  if (ec || !fs::exists(status)) return false;  // not there: by another name, it is no private file
  if (fs::is_symlink(status)) return true;
  return fs::is_regular_file(status) && fs::hard_link_count(file, ec) > 1 && !ec;
#else
  if (local.starts_with("/dev/") || local.starts_with("/proc/")) return true;
  struct stat st {};
  if (::lstat(local.c_str(), &st) != 0) return false;  // not there: by another name, it is no private file
  return S_ISLNK(st.st_mode) || (S_ISREG(st.st_mode) && st.st_nlink > 1);
#endif
}

bool PrivateFiles::HasUnattached() const {
  std::lock_guard lock(mutex_);
  return std::any_of(entries_.begin(), entries_.end(), [](const Keyed& k) { return !k.entry.attached; });
}

std::optional<PrivateFiles::Entry> PrivateFiles::Find(const std::string& path, bool thorough) const {
  const bool path_like = path.find('/') != std::string::npos || path.find('\\') != std::string::npos ||
                         path.starts_with('~') || path.starts_with('.');
  const auto candidates = CandidateKeys(path, thorough || path_like);
  if (candidates.empty()) return std::nullopt;
  std::lock_guard lock(mutex_);
  for (const auto& keyed : entries_) {
    for (const auto& candidate : candidates) {
      for (const auto& key : keyed.keys) {
        if (KeyCovers(key, candidate)) return keyed.entry;
      }
    }
  }
  if (!thorough) return std::nullopt;
  // The same file under a name no spelling shows: a hard link, /dev/fd/N.
  std::error_code ec;
  const fs::path file = PathFromUtf8(LocalSpelling(path, home_));
  if (!fs::exists(file, ec) || fs::is_directory(file, ec)) return std::nullopt;
  for (const auto& keyed : entries_) {
    for (const char* suffix : {"", ".wal"}) {
      const fs::path mine = PathFromUtf8(LocalSpelling(keyed.entry.path, home_) + suffix);
      if (fs::equivalent(file, mine, ec) && !ec) return keyed.entry;
    }
  }
  return std::nullopt;
}

bool PrivateFiles::IsCopyTwin(const std::string& path) const {
  const auto candidates = CandidateKeys(path, true);
  std::lock_guard lock(mutex_);
  for (const auto& keyed : entries_) {
    for (const auto& candidate : candidates) {
      if (std::find(keyed.twins.begin(), keyed.twins.end(), candidate) != keyed.twins.end()) return true;
    }
  }
  return false;
}

std::string PrivateFileMessage(const std::string& path, const std::string& category) {
  return "Permission denied: Hardcharger blocked access to '" + path + "' (" + category +
         "). It is one of the server's own files, which no SQL may open, read or write, whatever the role.";
}

// ---- Statements ----

DUCKDB_V2_STATEMENT_TYPE StatementType(duckdb_v2_connection_handle connection, std::string_view sql) {
  auto statements = warpduckdb::Parse(connection, sql);
  if (statements.empty()) return DUCKDB_V2_STATEMENT_TYPE_INVALID;
  DUCKDB_V2_STATEMENT_TYPE type = DUCKDB_V2_STATEMENT_TYPE_INVALID;
  duckdb_v2_sql_statement_get_type(statements.front().get(), &type, nullptr);
  return type;
}

DUCKDB_V2_STATEMENT_TYPE ExecutedType(duckdb_v2_connection_handle connection, const Piece& piece,
                                      DUCKDB_V2_STATEMENT_TYPE type) {
  const auto& t = piece.tokens;
  if (type != DUCKDB_V2_STATEMENT_TYPE_EXPLAIN || t.size() < 2 || !(t[1].IsWord("ANALYZE") || t[1].IsWord("ANALYSE"))) {
    return type;
  }
  auto inner = Wrapped(piece, type);
  if (!inner || inner->tokens.empty()) return type;
  return ExecutedType(connection, *inner, StatementType(connection, inner->text));
}

std::optional<std::string> ClassifyGatedCommand(duckdb_v2_connection_handle connection, const Piece& piece,
                                                DUCKDB_V2_STATEMENT_TYPE type) {
  const auto& t = piece.tokens;
  if (t.empty()) return std::nullopt;
  if (auto inner = Wrapped(piece, type); inner && !inner->tokens.empty()) {
    DUCKDB_V2_STATEMENT_TYPE inner_type = DUCKDB_V2_STATEMENT_TYPE_INVALID;
    try {
      inner_type = StatementType(connection, inner->text);
    } catch (const std::exception&) {
      return std::nullopt;  // DuckDB reports the parse error itself
    }
    if (auto category = ClassifyGatedCommand(connection, *inner, inner_type)) return category;
  }
  switch (type) {
    case DUCKDB_V2_STATEMENT_TYPE_ATTACH: return "ATTACH";
    case DUCKDB_V2_STATEMENT_TYPE_DETACH: return "DETACH";
    // CONNECT attaches its target and forwards the session's SQL to it, past every check
    // here; EXTERNAL RESOURCE provisions compute; PASSTHROUGH is raw SQL for a CONNECTed
    // target.
    case DUCKDB_V2_STATEMENT_TYPE_CONNECT: return "CONNECT";
    case DUCKDB_V2_STATEMENT_TYPE_DISCONNECT: return "DISCONNECT";
    case DUCKDB_V2_STATEMENT_TYPE_EXTERNAL_RESOURCE: return "EXTERNAL RESOURCE";
    case DUCKDB_V2_STATEMENT_TYPE_PASSTHROUGH: return "passthrough SQL";
    case DUCKDB_V2_STATEMENT_TYPE_UPDATE_EXTENSIONS: return "UPDATE EXTENSIONS";
    // The whole database, whatever the grants: always an admin's call.
    case DUCKDB_V2_STATEMENT_TYPE_EXPORT: return "EXPORT DATABASE";
    case DUCKDB_V2_STATEMENT_TYPE_LOAD:
      return t[0].IsWord("LOAD") ? "LOAD extension" : "INSTALL extension";
    default:
      break;
  }
  if (auto local = LocalCopy(piece)) return local;
  if (t[0].IsWord("IMPORT")) return "IMPORT DATABASE";
  if (t[0].IsWord("CHECKPOINT") || (t[0].IsWord("FORCE") && t.size() > 1 && t[1].IsWord("CHECKPOINT"))) {
    return "CHECKPOINT";
  }
  if (t[0].IsWord("CREATE") || t[0].IsWord("DROP")) {
    for (std::size_t i = 1; i < t.size() && i < 6; i++) {
      if (t[i].IsWord("SECRET")) return t[0].IsWord("CREATE") ? "CREATE SECRET" : "DROP SECRET";
      if (!(t[i].IsWord("OR") || t[i].IsWord("REPLACE") || t[i].IsWord("PERSISTENT") || t[i].IsWord("TEMPORARY") ||
            t[i].IsWord("TEMP"))) {
        break;
      }
    }
  }
  if (auto set = SetOf(piece)) {
    if (set->global || (set->automatic && IsInstanceSetting(set->name))) {
      return std::string(set->reset ? "RESET GLOBAL " : "SET GLOBAL ") + set->name;
    }
  }
  return GatedFunction(piece);
}

void CheckStatement(const GuardContext& context, const Piece& piece, DUCKDB_V2_STATEMENT_TYPE type) {
  const auto& t = piece.tokens;
  // allow_unredacted_secrets prints secrets in clear: refused for everyone, also inside
  // EXPLAIN or PREPARE, unless the value is provably false.
  if (context.block_unredacted_secrets) {
    for (const auto& [set, rest] : SetsOf(piece, "allow_unredacted_secrets")) {
      if (!set.reset && !ValueIsFalse(rest, set.value_from)) {
        Deny("Permission denied: SET allow_unredacted_secrets is disabled on this server, for every user: it "
             "would show secrets' credentials in clear (block_unredacted_secrets).");
      }
    }
  }
  // The server's own private files, for everyone. (The files it keeps attached are
  // read through their catalogs; their guard is in the file system alone, as a
  // statement may well name the database's path, say in duckdb_databases().)
  if (context.private_files != nullptr && context.private_files->HasUnattached()) {
    for (const Token& token : t) {
      const bool path_like = token.Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) ||
                             (token.Is(DUCKDB_V2_TOKEN_TYPE_IDENTIFIER) && token.text.starts_with('"'));
      if (!path_like) continue;
      const std::string path = Unquote(token.text);
      if (auto entry = context.private_files->Find(path, /*thorough=*/false); entry && !entry->attached) {
        Deny(PrivateFileMessage(path, entry->category));
      }
    }
  }
  // For non-admins the admin guard speaks first: its answer says what the role allows.
  if (!context.admin) {
    if (auto category = ClassifyGatedCommand(context.connection, piece, type)) {
      Deny(GatedMessage(*category));
    }
  }
  if (context.sensitive_paths != nullptr && context.sensitive_paths->enabled()) {
    if (!SetsOf(piece, "secret_directory").empty()) {
      Deny("Permission denied: Hardcharger blocked changing secret_directory after startup; DuckDB's "
           "persistent secrets stay where the server put them.");
    }
    for (const Token& token : t) {
      const bool path_like = token.Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) ||
                             (token.Is(DUCKDB_V2_TOKEN_TYPE_IDENTIFIER) && token.text.starts_with('"'));
      if (!path_like) continue;
      const std::string path = Unquote(token.text);
      if (auto category = context.sensitive_paths->Check(path)) {
        Deny("Permission denied: Hardcharger blocked access to '" + path + "' (" + *category +
             "). Hardcharger keeps credential and system files on the server host out of reach of SQL, "
             "whatever the role.");
      }
    }
  }
}

}  // namespace hardcharger
