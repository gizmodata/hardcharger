// Wildcard patterns for catalog names and authorized e-mail
// addresses: only `*` (any run, also empty) and `?` (exactly one character) are
// special; everything else, `.` and `-` included, matches itself.
#pragma once

#include <string_view>
#include <vector>
#include <string>

namespace hardcharger {

// Case-sensitive (catalog names).
bool MatchesPattern(std::string_view pattern, std::string_view text);

// Case-insensitive (e-mail addresses).
bool MatchesPatternIgnoringCase(std::string_view pattern, std::string_view text);

// True if `text` matches any of `patterns`, ignoring case.
bool MatchesAnyIgnoringCase(const std::vector<std::string>& patterns, std::string_view text);

}  // namespace hardcharger
