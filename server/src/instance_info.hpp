// What this instance is and runs on, collected once at startup: the instrumentation
// instances row and hardcharger_server_info() both read it, so the two can't drift.
#pragma once

#include <chrono>
#include <cstdint>
#include <string>

namespace hardcharger {

struct Config;

struct InstanceInfo {
  std::string instance_id;
  std::string cluster_id;  // empty: none
  std::string hardcharger_version;
  std::string duckdb_version;
  std::string arrow_version;
  std::string hostname;
  std::string hostname_arg;   // the host option (the listen address)
  std::string database_path;  // the database option, as given
  int port = 0;               // the port option (0: any free port)
  bool tls_enabled = false;
  std::string tls_cert_path;
  std::string tls_key_path;
  bool mtls_required = false;  // client certificates are verified (mtls_ca_cert)
  std::string mtls_ca_cert_path;
  bool readonly = false;
  std::string os_platform;  // uname's sysname: Linux, Darwin, Windows
  std::string os_name;      // uname's nodename (as the instances table has always had it)
  std::string os_version;   // the kernel or Windows version
  std::string cpu_arch;
  std::string cpu_model;  // empty when the system doesn't say
  int cpu_count = 0;
  int64_t memory_total_bytes = 0;
  std::chrono::system_clock::time_point start_time;
  std::string instance_tag;  // JSON object text, or empty
  // SHA-256 of the server certificate's DER (the first in tls_cert), as uppercase hex
  // pairs joined by ':' (openssl x509 -fingerprint -sha256); empty without TLS.
  std::string server_certificate_sha256;
};

// Reads the host, the configuration and the server certificate.
InstanceInfo CollectInstanceInfo(const Config& config, const std::string& instance_id,
                                 const std::string& duckdb_version,
                                 std::chrono::system_clock::time_point start_time);

// The SHA-256 fingerprint of the first certificate in a PEM file (InstanceInfo's form);
// empty when it can't be read.
std::string CertificateFingerprintSha256(const std::string& pem_file);

}  // namespace hardcharger
