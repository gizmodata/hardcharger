// Ported from GizmoSQL 1.x src/client/syntax_highlighter.hpp.

#pragma once

#include <string>
#include <unordered_set>

#include <replxx.hxx>

namespace hardcharger::client {

class SyntaxHighlighter {
 public:
  SyntaxHighlighter();

  /// Highlight callback for replxx. Assigns colors to each character position.
  void Highlight(const std::string& input, replxx::Replxx::colors_t& colors);

  /// Enable or disable highlighting. When disabled, all characters get DEFAULT.
  void SetEnabled(bool enabled) { enabled_ = enabled; }
  bool IsEnabled() const { return enabled_; }

 private:
  bool enabled_ = true;
  std::unordered_set<std::string> keywords_;
  std::unordered_set<std::string> functions_;
};

}  // namespace hardcharger::client
