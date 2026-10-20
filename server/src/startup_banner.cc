#include "startup_banner.hpp"

#include <openssl/bio.h>
#include <openssl/pem.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

#include <algorithm>
#include <cctype>

namespace hardcharger {

// pyfiglet's "slant" font. A raw string: its backslashes and backticks are the art's.
const std::string_view kBannerArt =
    R"ART(    __  __               __     __
   / / / /___ __________/ /____/ /_  ____ __________ ____  _____
  / /_/ / __ `/ ___/ __  / ___/ __ \/ __ `/ ___/ __ `/ _ \/ ___/
 / __  / /_/ / /  / /_/ / /__/ / / / /_/ / /  / /_/ /  __/ /
/_/ /_/\__,_/_/   \__,_/\___/_/ /_/\__,_/_/   \__, /\___/_/
                                             /____/
)ART";

namespace {

constexpr std::size_t kWidth = 80;

std::string Lower(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

std::string HostForUrl(std::string_view host) {
  std::string h(host);
  if (h.find(':') != std::string::npos && !h.starts_with('[')) h = "[" + h + "]";
  return h;
}

}  // namespace

bool IsWildcardHost(std::string_view host) {
  return host.empty() || host == "0.0.0.0" || host == "::" || host == "[::]";
}

std::vector<std::string> CertificateDnsNames(const std::string& pem_file) {
  std::vector<std::string> names;
  BIO* bio = BIO_new_file(pem_file.c_str(), "r");
  if (bio == nullptr) return names;
  X509* certificate = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  if (certificate == nullptr) return names;
  auto* alt = static_cast<GENERAL_NAMES*>(X509_get_ext_d2i(certificate, NID_subject_alt_name, nullptr, nullptr));
  if (alt != nullptr) {
    for (int i = 0; i < sk_GENERAL_NAME_num(alt); ++i) {
      const GENERAL_NAME* name = sk_GENERAL_NAME_value(alt, i);
      if (name->type != GEN_DNS) continue;
      const ASN1_STRING* dns = name->d.dNSName;
      names.emplace_back(reinterpret_cast<const char*>(ASN1_STRING_get0_data(dns)),
                         static_cast<std::size_t>(ASN1_STRING_length(dns)));
    }
    GENERAL_NAMES_free(alt);
  }
  X509_free(certificate);
  return names;
}

std::vector<std::string> UsableBaseUrls(std::string_view host, int port, bool tls,
                                        const std::vector<std::string>& certificate_dns_names,
                                        std::string_view hostname) {
  const std::string scheme = tls ? "https://" : "http://";
  const std::string suffix = ":" + std::to_string(port);
  if (!IsWildcardHost(host)) return {scheme + HostForUrl(host) + suffix};
  std::vector<std::string> hosts;
  auto add = [&](std::string_view name) {
    if (name.empty()) return;
    for (const auto& known : hosts) {
      if (Lower(known) == Lower(name)) return;
    }
    hosts.emplace_back(name);
  };
  if (!tls) {
    add("localhost");
    add(hostname);
  } else {
    // The name the certificate is valid for, so a browser shows no warning.
    std::string first;
    bool names_localhost = false;
    for (const auto& name : certificate_dns_names) {
      if (name.starts_with("*")) continue;
      if (first.empty()) first = name;
      if (Lower(name) == "localhost") names_localhost = true;
    }
    add(!first.empty() ? std::string_view(first) : (!hostname.empty() ? hostname : std::string_view("localhost")));
    if (names_localhost) add("localhost");
  }
  std::vector<std::string> urls;
  for (const auto& name : hosts) urls.push_back(scheme + HostForUrl(name) + suffix);
  return urls;
}

std::string Banner(const BannerInfo& info) {
  std::string out(kBannerArt);
  auto line = [&](const std::string& text) { out += text + "\n"; };

  const std::string versions = "  Hardcharger " + info.version + " | Warp SQL " + info.warp_sql_version +
                               " | DuckDB " + info.duckdb_version + " | by GizmoData";
  if (versions.size() <= kWidth) {
    line(versions);
  } else {
    line("  Hardcharger " + info.version + " | Warp SQL " + info.warp_sql_version);
    line("  DuckDB " + info.duckdb_version + " | by GizmoData");
  }

  // "  UI   <url>      Docs  <docs>" when it fits, the docs on a line of their own when not.
  const std::string prefix = "  " + info.label + std::string(info.label.size() < 3 ? 5 - info.label.size() : 2, ' ');
  const std::string docs = std::string(kDocsUrl);
  const std::string docs_short = docs.substr(docs.find("://") + 3);
  bool docs_shown = false;
  for (std::size_t i = 0; i < info.urls.size(); ++i) {
    std::string text = (i == 0 ? prefix : std::string(prefix.size(), ' ')) + info.urls[i];
    if (i == 0) {
      for (const std::string& d : {docs, docs_short}) {
        if (text.size() + 6 + 6 + d.size() <= kWidth) {
          text += "      Docs  " + d;
          docs_shown = true;
          break;
        }
      }
    }
    line(text);
  }
  if (!docs_shown) line("  Docs" + std::string(prefix.size() > 6 ? prefix.size() - 6 : 2, ' ') + docs);
  return out;
}

}  // namespace hardcharger
