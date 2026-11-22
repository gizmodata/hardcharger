#pragma once

#include <charconv>
#include <compare>
#include <format>
#include <initializer_list>
#include <string>
#include <string_view>

namespace hardcharger {

// Format versions are independent of the server release and compare as integer pairs.
struct StoreFormat {
  unsigned major;
  unsigned minor;

  auto operator<=>(const StoreFormat&) const = default;
  std::string Text() const { return std::format("{}.{}", major, minor); }

  // Accepts a store of this server's major version with any minor up to its own, and any
  // format listed in `migrated` (older formats the server upgrades in place). Refuses a
  // newer format, an unknown older one, and text that is not a version.
  std::string Check(std::string_view store, std::string_view text,
                    std::initializer_list<StoreFormat> migrated = {}) const {
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
    if (found.major == major) return {};
    for (const auto& format : migrated) {
      if (found == format) return {};
    }
    return std::format("{} has unsupported schema version '{}'", store, text);
  }
};

}  // namespace hardcharger
