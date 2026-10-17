#include "patterns.hpp"

#include <cctype>

namespace hardcharger {

namespace {

// Greedy matching with one backtrack point (the last `*`), linear in practice and
// never exponential, unlike a recursive matcher on patterns such as "a*a*a*a*b".
template <typename Equal>
bool Match(std::string_view pattern, std::string_view text, Equal equal) {
  std::size_t p = 0;
  std::size_t t = 0;
  std::size_t star = std::string_view::npos;
  std::size_t resume = 0;
  while (t < text.size()) {
    if (p < pattern.size() && pattern[p] == '*') {
      star = p++;
      resume = t;
    } else if (p < pattern.size() && (pattern[p] == '?' || equal(pattern[p], text[t]))) {
      p++;
      t++;
    } else if (star != std::string_view::npos) {
      p = star + 1;
      t = ++resume;
    } else {
      return false;
    }
  }
  while (p < pattern.size() && pattern[p] == '*') p++;
  return p == pattern.size();
}

}  // namespace

bool MatchesPattern(std::string_view pattern, std::string_view text) {
  return Match(pattern, text, [](char a, char b) { return a == b; });
}

bool MatchesPatternIgnoringCase(std::string_view pattern, std::string_view text) {
  return Match(pattern, text, [](char a, char b) {
    return std::tolower(static_cast<unsigned char>(a)) == std::tolower(static_cast<unsigned char>(b));
  });
}

bool MatchesAnyIgnoringCase(const std::vector<std::string>& patterns, std::string_view text) {
  for (const auto& pattern : patterns) {
    if (MatchesPatternIgnoringCase(pattern, text)) return true;
  }
  return false;
}

}  // namespace hardcharger
