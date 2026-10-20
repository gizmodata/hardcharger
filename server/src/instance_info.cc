#include "instance_info.hpp"

#include <arrow/config.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/x509.h>

#include <cmath>
#include <cstdio>
#include <thread>

#include "config.hpp"
#include "platform.hpp"
#include "version.hpp"

namespace hardcharger {

std::string CertificateFingerprintSha256(const std::string& pem_file) {
  if (pem_file.empty()) return {};
  BIO* bio = BIO_new_file(pem_file.c_str(), "r");
  if (bio == nullptr) return {};
  X509* certificate = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  if (certificate == nullptr) return {};
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int length = 0;
  const bool ok = X509_digest(certificate, EVP_sha256(), digest, &length) == 1;
  X509_free(certificate);
  if (!ok) return {};
  std::string out;
  for (unsigned int i = 0; i < length; i++) {
    char pair[4];
    std::snprintf(pair, sizeof(pair), "%02X", digest[i]);
    if (i > 0) out += ':';
    out += pair;
  }
  return out;
}

double CertificateNotAfterSeconds(const std::string& pem_file) {
  if (pem_file.empty()) return NAN;
  BIO* bio = BIO_new_file(pem_file.c_str(), "r");
  if (bio == nullptr) return NAN;
  X509* certificate = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  if (certificate == nullptr) return NAN;
  // The distance from the Unix epoch, which needs no time-zone or timegm() call.
  ASN1_TIME* epoch = ASN1_TIME_set(nullptr, 0);
  int days = 0, seconds = 0;
  const bool ok = epoch != nullptr && ASN1_TIME_diff(&days, &seconds, epoch, X509_get0_notAfter(certificate)) == 1;
  ASN1_TIME_free(epoch);
  X509_free(certificate);
  return ok ? static_cast<double>(days) * 86400.0 + static_cast<double>(seconds) : NAN;
}

InstanceInfo CollectInstanceInfo(const Config& config, const std::string& instance_id,
                                 const std::string& duckdb_version, std::chrono::system_clock::time_point start_time) {
  const OsInfo host = Os();
  InstanceInfo info;
  info.instance_id = instance_id;
  info.cluster_id = config.cluster_id;
  info.hardcharger_version = kVersion;
  info.duckdb_version = duckdb_version;
  info.arrow_version = arrow::GetBuildInfo().version_string;
  info.hostname = HostName();
  info.hostname_arg = config.host;
  info.database_path = config.database;
  info.port = static_cast<int>(config.port);
  info.tls_enabled = !config.tls_cert.empty();
  info.tls_cert_path = config.tls_cert;
  info.tls_key_path = config.tls_key;
  info.mtls_required = !config.mtls_ca_cert.empty();
  info.mtls_ca_cert_path = config.mtls_ca_cert;
  info.readonly = config.read_only;
  info.os_platform = host.system;
  info.os_name = host.node;
  info.os_version = host.release;
  info.cpu_arch = host.machine;
  info.cpu_model = CpuModel();
  info.cpu_count = static_cast<int>(std::thread::hardware_concurrency());
  info.memory_total_bytes = PhysicalMemoryBytes();
  info.start_time = start_time;
  info.instance_tag = config.instance_tag;
  info.server_certificate_sha256 = CertificateFingerprintSha256(config.tls_cert);
  return info;
}

}  // namespace hardcharger
