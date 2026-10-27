// Operating-system differences (POSIX and Windows), kept out of the server's logic.
#pragma once

#include <boost/asio/ip/tcp.hpp>

#include <cmath>
#include <cstdint>
#include <ctime>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

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
// Current interface addresses; no DNS lookup. IPv4-mapped IPv6 is normalized to IPv4.
std::vector<std::string> LocalIpAddresses();
// An SDK client address (IPv4[:port] or [IPv6][:port]) without the port/brackets.
std::string ClientIpAddress(std::string_view address);

struct OsInfo {
  std::string system;   // uname's sysname: "Linux", "Darwin", "Windows"
  std::string node;     // the host name
  std::string release;  // the kernel or Windows version
  std::string machine;  // the CPU architecture
};
OsInfo Os();

// The CPU's model name ("Apple M4 Max", "AMD EPYC 9R14", "ARM Neoverse-V1"); empty when
// the system doesn't say.
std::string CpuModel();
// The model named by Linux's /proc/cpuinfo text: x86's "model name", a board's "Model"
// (Raspberry Pi), else ARM's "CPU implementer" and "CPU part" (most ARM kernels, AWS
// Graviton and Azure Cobalt among them, name no model): "ARM Neoverse-N2", or the raw ids
// ("ARM implementer 0x41 part 0xd99") for a part not in the table. Empty when none is there.
std::string CpuModelFromCpuinfo(std::string_view cpuinfo);

int64_t PhysicalMemoryBytes();
// The process's resident memory; NaN when unknown.
double ResidentMemoryBytes();

// For the process_* metrics (Prometheus's standard meanings); NaN when unknown.
// When the process started, as Unix time in seconds (Linux: /proc/self/stat's start
// ticks plus /proc/stat's btime; macOS: proc_pidinfo; Windows: GetProcessTimes).
double ProcessStartTimeSeconds();
// Open file descriptors (Linux: /proc/self/fd; macOS: proc_pidinfo). NaN on Windows,
// whose handles are not file descriptors.
double ProcessOpenFds();
// The process's operating-system threads.
double ProcessThreads();

// Raises the soft limit on open files (RLIMIT_NOFILE) to the hard limit, as Go programs
// and most servers do at startup. Docker, systemd and Kubernetes start a process at 1024
// (a macOS shell at 256), far below their hard limits, and DuckDB can hold more files open
// than that at once: a DuckLake scan of a table with a thousand partition files failed
// with "Too many open files" (issue #8). nullopt on Windows, which has no such limit on
// the handles DuckDB opens, or when the limit can't be read. kUnlimited stands for
// RLIM_INFINITY.
struct OpenFileLimit {
  static constexpr uint64_t kUnlimited = UINT64_MAX;
  uint64_t before = 0;  // the soft limit the process started with
  uint64_t after = 0;   // the soft limit now: before when it was already the most allowed
  uint64_t hard = 0;
};
std::optional<OpenFileLimit> RaiseOpenFileLimit();

// The container's limits from the cgroup file system mounted at `root`: cgroup v2's
// memory.max and cpu.max, else v1's memory/memory.limit_in_bytes and
// cpu/cpu.cfs_quota_us / cpu.cfs_period_us. NaN where there is no limit ("max", -1, v1's
// huge "unlimited" number), or no cgroup file system (macOS, Windows).
struct CGroupLimits {
  double memory_limit_bytes = NAN;
  double cpu_quota_cores = NAN;  // quota / period: fractional CPUs
};
CGroupLimits ReadCGroupLimits(const std::string& root = "/sys/fs/cgroup");

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
