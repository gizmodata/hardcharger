#pragma once

#include <charconv>
#include <compare>
#include <format>
#include <string>
#include <string_view>

namespace hardcharger {

// Format versions are independent of the server release and compare as integer pairs.
struct StoreFormat {
  unsigned major;
  unsigned minor;

  auto operator<=>(const StoreFormat&) const = default;
  std::string Text() const { return std::format("{}.{}", major, minor); }

  std::string Check(std::string_view store, std::string_view text) const {
    const auto dot = text.find('.');
    auto component = [](std::string_view digits, unsigned& number) {
      if (digits.empty()) return false;
      const auto end = digits.data() + digits.size();
      const auto result = std::from_chars(digits.data(), end, number);
      return result.ec == std::errc{} && result.ptr == end;
    };
    StoreFormat found{0, 0};
    if (!component(text.substr(0, dot), found.major) ||
        (dot != text.npos && !component(text.substr(dot + 1), found.minor))) {
      return std::format("{} has an invalid schema version '{}'", store, text);
    }
    if (found > *this) {
      return std::format("{} is version {}, newer than this server's ({})", store, text, Text());
    }
    if (found.major != major) return std::format("{} has unsupported schema version '{}'", store, text);
    return {};
  }
};

}  // namespace hardcharger
