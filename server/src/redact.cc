#include "redact.hpp"

#include <regex>

namespace hardcharger {

std::string RedactCredentials(std::string text) {
  // The value of a credential key: quoted, or up to whitespace, a quote or the end of a
  // clause. (A URL's '&' does not end it: in libpq's keyword form '&' may be part of a
  // password, so the rest of a query string goes too rather than part of a password stay.)
  static const std::regex kPair(
      R"(((?:password|passwd|pwd|secret|token|access_key|accountkey|sig|x-amz-[a-z-]+)\s*=\s*)('[^']*'|"[^"]*"|[^\s"';)]+))",
      std::regex::icase | std::regex::optimize);
  static const std::regex kQuoted(
      R"(('(?:password|passwd|pwd|secret|token|access_key|accountkey|sig)'\s*:\s*)('[^']*'))",
      std::regex::icase | std::regex::optimize);
  // user:password@ in an authority, the password up to the last '@' before the host.
  static const std::regex kAuthority(R"((://[^:/@\s"']*:)[^\s"']*@)", std::regex::optimize);
  text = std::regex_replace(text, kPair, "$1***");
  text = std::regex_replace(text, kQuoted, "$1'***'");
  text = std::regex_replace(text, kAuthority, "$1***@");
  return text;
}

std::string RedactCredentials(std::string text, const std::vector<std::string>& secrets) {
  for (const auto& secret : secrets) {
    if (secret.empty()) continue;
    for (std::size_t at; (at = text.find(secret)) != std::string::npos;) {
      text.replace(at, secret.size(), "<connection string>");
    }
  }
  return RedactCredentials(std::move(text));
}

}  // namespace hardcharger
