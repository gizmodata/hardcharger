// Operating-system differences (POSIX and Windows), kept out of the server's logic.
#pragma once

#include <boost/asio/ip/tcp.hpp>

#include <cstdint>
#include <ctime>
#include <filesystem>
#include <optional>
#include <string>

struct ssl_ctx_st;

namespace hardcharger {

// Paths as UTF-8 text and back. std::filesystem reads narrow strings in the ANSI code
// page on Windows, and DuckDB's (and the config's) paths are UTF-8.
std::filesystem::path PathFromUtf8(const std::string& text);
std::string Utf8Path(const std::filesystem::path& path);  // generic form ('/' separators)

// An environment variable as UTF-8; nullopt when unset.
std::optional<std::string> EnvUtf8(const char* name);
// For tests: sets (or, with nullopt, removes) an environment variable.
void SetEnv(const char* name, const std::optional<std::string>& value);

// The user's home directory ('~'): HOME on POSIX (else the password database),
// USERPROFILE on Windows. Empty when unknown. DuckDB itself reads only HOME
// (USERPROFILE); the server hands it this one when HOME is unset (DuckDBDirectories).
std::string HomeDirectory();
// Whether HOME (USERPROFILE on Windows) is set and not empty.
bool HomeFromEnvironment();
// Whether `path` is an existing directory this process may create files in.
bool IsWritableDirectory(const std::string& path);

// A new directory only this user can use, named `prefix` plus a random suffix, in the
// system's temporary directory (mkdtemp). Throws when it can't be made.
std::string MakePrivateTempDirectory(const std::string& prefix);

std::tm UtcTime(std::time_t seconds);
int64_t ProcessId();
std::string HostName();

struct OsInfo {
  std::string system;   // uname's sysname: "Linux", "Darwin", "Windows"
  std::string node;     // the host name
  std::string release;  // the kernel or Windows version
  std::string machine;  // the CPU architecture
};
OsInfo Os();

// The CPU's model name ("Apple M4 Max", "AMD EPYC 9R14"); empty when the system doesn't say.
std::string CpuModel();

int64_t PhysicalMemoryBytes();
// The process's resident memory; NaN when unknown.
double ResidentMemoryBytes();

// Whether a listener sets SO_REUSEADDR (POSIX: rebind past TIME_WAIT). Never on Windows,
// where it lets a second server bind a port the first still listens on.
inline constexpr bool kListenerReusesAddress =
#ifdef _WIN32
    false;
#else
    true;
#endif

// Read and write timeouts on a blocking socket.
void SetSocketTimeouts(boost::asio::ip::tcp::socket& socket, int seconds);

// Adds the operating system's trusted root certificates to a TLS client context when
// they don't come from a file (Windows: the certificate store). Returns whether any were.
bool AddSystemTrustStore(ssl_ctx_st* context);

// One path segment matches a glob segment (*, ?, [...]): fnmatch on POSIX; on Windows
// case-insensitively, with '\' an ordinary character (it separates paths there).
bool GlobSegmentMatches(const std::string& pattern, const std::string& segment);

}  // namespace hardcharger
