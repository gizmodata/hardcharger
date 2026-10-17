// Ported from GizmoSQL 1.x src/client/pager.hpp.

#pragma once

#include <string>
#include <vector>

namespace hardcharger::client {

/// Built-in pager for displaying large query results.
/// Splits rendered output into pages and handles keyboard navigation.
class Pager {
 public:
  /// Display content in pager mode. Returns when the user exits (q/Escape).
  /// Only operates on a TTY; if stdout is not a terminal, writes directly.
  static void Display(const std::string& content);

  /// Get the terminal height in rows.
  static int GetTerminalHeight();
};

}  // namespace hardcharger::client
