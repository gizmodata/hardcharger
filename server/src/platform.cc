#include "platform.hpp"

#ifdef _WIN32
#include <windows.h>
#include <wincrypt.h>
#include <psapi.h>

#include <cwctype>
#else
#include <fnmatch.h>
#include <pwd.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/utsname.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <mach/mach.h>
#include <sys/sysctl.h>
#endif
#endif

#include <openssl/ssl.h>
#include <openssl/x509.h>

#include <cmath>
#include <cstdio>
#include <iterator>
#include <random>
#include <stdexcept>
#include <cstdlib>
#include <fstream>
#include <sstream>

namespace hardcharger {

namespace {

// "key<tabs>: value" -> value, for a line whose key is `key`; empty otherwise.
std::string_view CpuinfoValue(std::string_view line, std::string_view key) {
  if (!line.starts_with(key)) return {};
  const auto colon = line.find(':');
  if (colon == std::string_view::npos || line.substr(key.size(), colon - key.size()).find_first_not_of(" \t") !=
                                             std::string_view::npos) {
    return {};
  }
  const auto start = line.find_first_not_of(" \t", colon + 1);
  if (start == std::string_view::npos) return {};
  const auto end = line.find_last_not_of(" \t\r");
  return line.substr(start, end - start + 1);
}

// ARM's MIDR implementer and part numbers, as Linux's /proc/cpuinfo prints them (the
// cores servers and laptops run; the same names as util-linux's lscpu).
std::string ArmCpuName(std::string_view implementer, std::string_view part) {
  struct Part {
    std::string_view implementer, part, name;
  };
  static constexpr Part kParts[] = {
      {"0x41", "0xd03", "ARM Cortex-A53"},     {"0x41", "0xd04", "ARM Cortex-A35"},
      {"0x41", "0xd05", "ARM Cortex-A55"},     {"0x41", "0xd07", "ARM Cortex-A57"},
      {"0x41", "0xd08", "ARM Cortex-A72"},     {"0x41", "0xd09", "ARM Cortex-A73"},
      {"0x41", "0xd0a", "ARM Cortex-A75"},     {"0x41", "0xd0b", "ARM Cortex-A76"},
      {"0x41", "0xd0c", "ARM Neoverse-N1"},    {"0x41", "0xd0d", "ARM Cortex-A77"},
      {"0x41", "0xd40", "ARM Neoverse-V1"},    {"0x41", "0xd41", "ARM Cortex-A78"},
      {"0x41", "0xd44", "ARM Cortex-X1"},      {"0x41", "0xd46", "ARM Cortex-A510"},
      {"0x41", "0xd47", "ARM Cortex-A710"},    {"0x41", "0xd48", "ARM Cortex-X2"},
      {"0x41", "0xd49", "ARM Neoverse-N2"},    {"0x41", "0xd4a", "ARM Neoverse-E1"},
      {"0x41", "0xd4b", "ARM Cortex-A78C"},    {"0x41", "0xd4d", "ARM Cortex-A715"},
      {"0x41", "0xd4e", "ARM Cortex-X3"},      {"0x41", "0xd4f", "ARM Neoverse-V2"},
      {"0x41", "0xd80", "ARM Cortex-A520"},    {"0x41", "0xd81", "ARM Cortex-A720"},
      {"0x41", "0xd82", "ARM Cortex-X4"},      {"0x41", "0xd84", "ARM Neoverse-V3"},
      {"0x41", "0xd87", "ARM Cortex-A725"},    {"0x41", "0xd8e", "ARM Neoverse-N3"},
      {"0xc0", "0xac3", "Ampere AmpereOne"},   {"0xc0", "0xac4", "Ampere AmpereOne-2"},
      {"0x61", "0x022", "Apple M1 Icestorm"},  {"0x61", "0x023", "Apple M1 Firestorm"},
  };
  for (const auto& p : kParts) {
    if (p.implementer == implementer && p.part == part) return std::string(p.name);
  }
  if (implementer.empty() || part.empty()) return {};
  struct Vendor {
    std::string_view implementer, name;
  };
  static constexpr Vendor kVendors[] = {{"0x41", "ARM"},      {"0x48", "HiSilicon"}, {"0x4e", "NVIDIA"},
                                        {"0x51", "Qualcomm"}, {"0x61", "Apple"},     {"0xc0", "Ampere"}};
  std::string_view vendor = "ARM";
  for (const auto& v : kVendors) {
    if (v.implementer == implementer) vendor = v.name;
  }
  return std::string(vendor) + " implementer " + std::string(implementer) + " part " + std::string(part);
}

}  // namespace

std::string CpuModelFromCpuinfo(std::string_view cpuinfo) {
  std::string_view implementer, part;
  std::size_t at = 0;
  while (at < cpuinfo.size()) {
    auto end = cpuinfo.find('\n', at);
    if (end == std::string_view::npos) end = cpuinfo.size();
    const std::string_view line = cpuinfo.substr(at, end - at);
    at = end + 1;
    for (const std::string_view key : {std::string_view("model name"), std::string_view("Model")}) {
      if (const auto value = CpuinfoValue(line, key); !value.empty()) return std::string(value);
    }
    if (implementer.empty()) implementer = CpuinfoValue(line, "CPU implementer");
    if (part.empty()) part = CpuinfoValue(line, "CPU part");
  }
  return ArmCpuName(implementer, part);
}

std::filesystem::path PathFromUtf8(const std::string& text) {
  return std::filesystem::path(std::u8string(reinterpret_cast<const char8_t*>(text.data()), text.size()));
}

std::string Utf8Path(const std::filesystem::path& path) {
  const std::u8string text = path.generic_u8string();
  return std::string(reinterpret_cast<const char*>(text.data()), text.size());
}

std::string MakePrivateTempDirectory(const std::string& prefix) {
#ifdef _WIN32
  // %TEMP% is the user's own (inheritable ACL); a random name no one can take first,
  // created atomically by create_directory (false when it already exists).
  std::random_device random;
  for (int attempt = 0; attempt < 100; attempt++) {
    char suffix[13];
    std::snprintf(suffix, sizeof(suffix), "%06x%06x", random() & 0xffffff, random() & 0xffffff);
    const auto path = std::filesystem::temp_directory_path() / (prefix + suffix);
    std::error_code ec;
    if (std::filesystem::create_directory(path, ec)) return Utf8Path(path);
    if (ec) break;
  }
  throw std::runtime_error("cannot create a private temporary directory");
#else
  std::string base = std::filesystem::temp_directory_path().string() + "/" + prefix + "XXXXXX";
  if (mkdtemp(base.data()) == nullptr) throw std::runtime_error("cannot create a private temporary directory");
  return base;
#endif
}

#ifdef _WIN32

namespace {

std::wstring Wide(const std::string& text) {
  if (text.empty()) return {};
  const int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
  std::wstring out(static_cast<std::size_t>(size), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), size);
  return out;
}

std::string Narrow(const std::wstring& text) {
  if (text.empty()) return {};
  const int size =
      WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
  std::string out(static_cast<std::size_t>(size), '\0');
  WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), size, nullptr, nullptr);
  return out;
}

}  // namespace

std::optional<std::string> EnvUtf8(const char* name) {
  const std::wstring wide = Wide(name);
  const DWORD size = GetEnvironmentVariableW(wide.c_str(), nullptr, 0);
  if (size == 0) return std::nullopt;
  std::wstring value(size, L'\0');
  const DWORD written = GetEnvironmentVariableW(wide.c_str(), value.data(), size);
  value.resize(written);
  return Narrow(value);
}

void SetEnv(const char* name, const std::optional<std::string>& value) {
  // Both the C runtime's copy (getenv) and the process's (GetEnvironmentVariable).
  _putenv_s(name, value ? value->c_str() : "");
  SetEnvironmentVariableW(Wide(name).c_str(), value ? Wide(*value).c_str() : nullptr);
}

std::string HomeDirectory() {
  if (auto profile = EnvUtf8("USERPROFILE"); profile && !profile->empty()) return *profile;
  auto drive = EnvUtf8("HOMEDRIVE");
  auto path = EnvUtf8("HOMEPATH");
  if (drive && path) return *drive + *path;
  return {};
}

bool HomeFromEnvironment() {
  auto profile = EnvUtf8("USERPROFILE");
  return profile && !profile->empty();
}

bool IsWritableDirectory(const std::string& path) {
  std::error_code ec;
  if (path.empty() || !std::filesystem::is_directory(PathFromUtf8(path), ec)) return false;
  return _waccess(PathFromUtf8(path).c_str(), 2) == 0;
}

std::tm UtcTime(std::time_t seconds) {
  std::tm utc{};
  gmtime_s(&utc, &seconds);
  return utc;
}

int64_t ProcessId() { return static_cast<int64_t>(GetCurrentProcessId()); }

std::string HostName() {
  wchar_t name[256] = {};
  DWORD size = static_cast<DWORD>(std::size(name));
  if (!GetComputerNameExW(ComputerNameDnsHostname, name, &size)) return {};
  return Narrow(std::wstring(name, size));
}

OsInfo Os() {
  OsInfo info;
  info.system = "Windows";
  info.node = HostName();
  // GetVersionEx reports what the program's manifest claims; RtlGetVersion the truth.
  using RtlGetVersion = LONG(WINAPI*)(OSVERSIONINFOW*);
  if (HMODULE ntdll = GetModuleHandleW(L"ntdll.dll")) {
    if (auto get = reinterpret_cast<RtlGetVersion>(GetProcAddress(ntdll, "RtlGetVersion"))) {
      OSVERSIONINFOW version{};
      version.dwOSVersionInfoSize = sizeof(version);
      if (get(&version) == 0) {
        info.release = std::to_string(version.dwMajorVersion) + "." + std::to_string(version.dwMinorVersion) + "." +
                       std::to_string(version.dwBuildNumber);
      }
    }
  }
  SYSTEM_INFO system{};
  GetNativeSystemInfo(&system);
  switch (system.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64: info.machine = "AMD64"; break;
    case PROCESSOR_ARCHITECTURE_ARM64: info.machine = "ARM64"; break;
    case PROCESSOR_ARCHITECTURE_INTEL: info.machine = "x86"; break;
    default: info.machine = "unknown"; break;
  }
  return info;
}

std::string CpuModel() {
  char name[256] = {};
  DWORD size = sizeof(name);
  if (RegGetValueA(HKEY_LOCAL_MACHINE, "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
                   "ProcessorNameString", RRF_RT_REG_SZ, nullptr, name, &size) != ERROR_SUCCESS) {
    return {};
  }
  std::string model(name);
  const auto start = model.find_first_not_of(' ');
  const auto end = model.find_last_not_of(' ');
  return start == std::string::npos ? std::string() : model.substr(start, end - start + 1);
}

int64_t PhysicalMemoryBytes() {
  MEMORYSTATUSEX status{};
  status.dwLength = sizeof(status);
  return GlobalMemoryStatusEx(&status) ? static_cast<int64_t>(status.ullTotalPhys) : 0;
}

double ResidentMemoryBytes() {
  PROCESS_MEMORY_COUNTERS counters{};
  if (K32GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters))) {
    return static_cast<double>(counters.WorkingSetSize);
  }
  return NAN;
}

void SetSocketTimeouts(boost::asio::ip::tcp::socket& socket, int seconds) {
  const DWORD millis = static_cast<DWORD>(seconds) * 1000;
  setsockopt(socket.native_handle(), SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&millis), sizeof(millis));
  setsockopt(socket.native_handle(), SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&millis), sizeof(millis));
}

bool AddSystemTrustStore(ssl_ctx_st* context) {
  HCERTSTORE store = CertOpenSystemStoreW(0, L"ROOT");
  if (store == nullptr) return false;
  X509_STORE* trusted = SSL_CTX_get_cert_store(context);
  int added = 0;
  for (PCCERT_CONTEXT cert = CertEnumCertificatesInStore(store, nullptr); cert != nullptr;
       cert = CertEnumCertificatesInStore(store, cert)) {
    const unsigned char* der = cert->pbCertEncoded;
    X509* x509 = d2i_X509(nullptr, &der, static_cast<long>(cert->cbCertEncoded));
    if (x509 == nullptr) continue;
    if (X509_STORE_add_cert(trusted, x509) == 1) added++;
    X509_free(x509);
  }
  CertCloseStore(store, 0);
  return added > 0;
}

namespace {

wchar_t Fold(wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); }

// fnmatch(3) without flags, over UTF-16 and ignoring case: '*', '?' and bracket
// expressions ([abc], [a-z], [!x] or [^x]); '\' is a literal.
bool Match(const wchar_t* p, const wchar_t* s) {
  for (; *p != L'\0'; p++, s++) {
    if (*p == L'*') {
      while (*p == L'*') p++;
      if (*p == L'\0') return true;
      for (; *s != L'\0'; s++) {
        if (Match(p, s)) return true;
      }
      return false;
    }
    if (*s == L'\0') return false;
    if (*p == L'?') continue;
    if (*p == L'[') {
      const wchar_t* q = p + 1;
      const bool negate = *q == L'!' || *q == L'^';
      if (negate) q++;
      bool matched = false;
      bool first = true;
      for (; *q != L'\0' && (first || *q != L']'); q++, first = false) {
        if (q[1] == L'-' && q[2] != L'\0' && q[2] != L']') {
          if (Fold(*s) >= Fold(q[0]) && Fold(*s) <= Fold(q[2])) matched = true;
          q += 2;
        } else if (Fold(*q) == Fold(*s)) {
          matched = true;
        }
      }
      if (*q == L']') {
        if (matched == negate) return false;
        p = q;
        continue;
      }
      // No closing ']': the '[' is an ordinary character.
    }
    if (Fold(*p) != Fold(*s)) return false;
  }
  return *s == L'\0';
}

}  // namespace

bool GlobSegmentMatches(const std::string& pattern, const std::string& segment) {
  return Match(Wide(pattern).c_str(), Wide(segment).c_str());
}

#else  // POSIX

std::optional<std::string> EnvUtf8(const char* name) {
  const char* value = std::getenv(name);
  if (value == nullptr) return std::nullopt;
  return std::string(value);
}

void SetEnv(const char* name, const std::optional<std::string>& value) {
  if (value) {
    setenv(name, value->c_str(), 1);
  } else {
    unsetenv(name);
  }
}

std::string HomeDirectory() {
  if (const char* home = std::getenv("HOME"); home && *home) return home;
  if (const passwd* pw = getpwuid(getuid()); pw && pw->pw_dir) return pw->pw_dir;
  return {};
}

bool HomeFromEnvironment() {
  const char* home = std::getenv("HOME");
  return home != nullptr && *home != '\0';
}

bool IsWritableDirectory(const std::string& path) {
  std::error_code ec;
  return !path.empty() && std::filesystem::is_directory(path, ec) && access(path.c_str(), W_OK) == 0;
}

std::tm UtcTime(std::time_t seconds) {
  std::tm utc{};
  gmtime_r(&seconds, &utc);
  return utc;
}

int64_t ProcessId() { return static_cast<int64_t>(getpid()); }

std::string HostName() {
  char name[256] = {};
  if (gethostname(name, sizeof(name) - 1) != 0) return {};
  return name;
}

OsInfo Os() {
  utsname host{};
  uname(&host);
  return {host.sysname, host.nodename, host.release, host.machine};
}

std::string CpuModel() {
#if defined(__APPLE__)
  char name[256] = {};
  std::size_t size = sizeof(name) - 1;
  if (sysctlbyname("machdep.cpu.brand_string", name, &size, nullptr, 0) == 0) return std::string(name);
  return {};
#else
  std::ifstream file("/proc/cpuinfo");
  std::stringstream text;
  text << file.rdbuf();
  return CpuModelFromCpuinfo(text.str());
#endif
}

int64_t PhysicalMemoryBytes() {
#if defined(__APPLE__)
  int64_t bytes = 0;
  std::size_t size = sizeof(bytes);
  if (sysctlbyname("hw.memsize", &bytes, &size, nullptr, 0) == 0) return bytes;
  return 0;
#else
  return static_cast<int64_t>(sysconf(_SC_PHYS_PAGES)) * static_cast<int64_t>(sysconf(_SC_PAGESIZE));
#endif
}

double ResidentMemoryBytes() {
#if defined(__APPLE__)
  mach_task_basic_info info{};
  mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
  if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO, reinterpret_cast<task_info_t>(&info), &count) == KERN_SUCCESS) {
    return static_cast<double>(info.resident_size);
  }
  return NAN;
#else
  std::ifstream statm("/proc/self/statm");
  long pages = 0, resident = 0;
  if (statm >> pages >> resident) return static_cast<double>(resident) * static_cast<double>(sysconf(_SC_PAGESIZE));
  return NAN;
#endif
}

void SetSocketTimeouts(boost::asio::ip::tcp::socket& socket, int seconds) {
  const timeval timeout{seconds, 0};
  setsockopt(socket.native_handle(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(socket.native_handle(), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
}

bool AddSystemTrustStore(ssl_ctx_st* context) { return SSL_CTX_set_default_verify_paths(context) == 1; }

bool GlobSegmentMatches(const std::string& pattern, const std::string& segment) {
  return fnmatch(pattern.c_str(), segment.c_str(), 0) == 0;
}

#endif

}  // namespace hardcharger
