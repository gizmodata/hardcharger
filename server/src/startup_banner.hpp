// What the server prints about itself at startup: the addresses a person can open (not the
// 0.0.0.0 it binds), and the banner of a text log.
#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

// The banner's wordmark ("slant", pure ASCII), six lines of at most 64 columns, each ending
// in a newline. server/tests/fixtures/banner-art.txt holds the same bytes.
extern const std::string_view kBannerArt;

// Where the documentation is.
inline constexpr std::string_view kDocsUrl = "https://docs.hardcharger.gizmodata.com";

// A bind address that means "every interface": 0.0.0.0, ::, [::] or empty.
bool IsWildcardHost(std::string_view host);

// The DNS names of a PEM certificate's subjectAltName, in order; none when the file can't be
// read or has none.
std::vector<std::string> CertificateDnsNames(const std::string& pem_file);

// scheme://host:port for the addresses a person can use, most useful first. A specific bind
// address is itself (an IPv6 one in brackets). A wildcard: over plain HTTP, localhost and then
// `hostname`; over TLS, the certificate's first DNS name that isn't a wildcard (else
// `hostname`, else localhost), then localhost too when the certificate names it.
std::vector<std::string> UsableBaseUrls(std::string_view host, int port, bool tls,
                                        const std::vector<std::string>& certificate_dns_names,
                                        std::string_view hostname);

struct BannerInfo {
  std::string version;           // Hardcharger's
  std::string warp_sql_version;  // the specification's
  std::string duckdb_version;
  std::string label;             // "UI" or "Warp SQL"
  std::vector<std::string> urls;  // what the label names, most useful first
};

// The banner: the wordmark, then a line of versions and the URLs with the docs' URL, every
// line ASCII, at most 80 columns and ending in a newline. A URL too long for a line of its own
// is printed whole all the same.
std::string Banner(const BannerInfo& info);

}  // namespace hardcharger
