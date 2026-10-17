// The server's own outbound HTTP(S) calls: OpenID Connect discovery, JWKS, and the
// OAuth code exchange. HTTPS servers are verified against the system's CA bundle.
#pragma once

#include <chrono>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hardcharger {

// The system's CA bundle: $SSL_CERT_FILE when it names a readable file, else the first
// well-known bundle that exists (Debian/Ubuntu/Alpine, RHEL/Amazon Linux, OpenSUSE,
// macOS and Homebrew). Portable builds can't rely on OpenSSL's compiled-in directory,
// which is a build-time path; nullopt when none is found.
std::optional<std::string> FindSystemCaCertFile();

struct HttpResponse {
  int status = 0;
  std::string body;
  std::string location;  // a redirect's Location
};

// GET `url`, or POST `form` (application/x-www-form-urlencoded) to it; follows up to five
// redirects. Throws std::runtime_error when the server can't be reached, its certificate
// doesn't verify, or the call takes longer than `timeout`.
HttpResponse HttpGet(const std::string& url, std::chrono::seconds timeout = std::chrono::seconds(10));
HttpResponse HttpPostForm(const std::string& url, const std::vector<std::pair<std::string, std::string>>& form,
                          std::chrono::seconds timeout = std::chrono::seconds(10));

// Percent-encodes `text` for a query string or form value (RFC 3986 unreserved kept).
std::string UrlEncode(std::string_view text);

}  // namespace hardcharger
