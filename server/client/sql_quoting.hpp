// Ported from GizmoSQL 1.x src/client/sql_quoting.hpp.
#pragma once

#include <string>
#include <string_view>

namespace hardcharger::client {

// Identifiers cannot be bound. Quote each component of a qualified name separately.
inline std::string QuoteSqlIdentifier(std::string_view value) {
  std::string result = "\"";
  for (char ch : value) {
    result += ch;
    if (ch == '"') result += '"';
  }
  return result + '"';
}

// Prefer bind parameters. Use this only for SQL output or statements such as
// Hardcharger SET whose server-side parser requires a literal expression.
inline std::string QuoteSqlLiteral(std::string_view value) {
  std::string result = "'";
  for (char ch : value) {
    result += ch;
    if (ch == '\'') result += '\'';
  }
  return result + '\'';
}

}  // namespace hardcharger::client
