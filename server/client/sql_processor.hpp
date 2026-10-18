// Ported from GizmoSQL 1.x src/client/sql_processor.hpp.

#pragma once

#include <string>

namespace hardcharger::client {

class SqlProcessor {
 public:
  // Returns true when a complete statement is ready (terminated by semicolon).
  bool AccumulateLine(const std::string& line);

  std::string GetStatement() const;
  std::string GetRemainder() const;
  void Reset();
  bool IsAccumulating() const;

  // Whether `text` holds nothing but whitespace and comments (-- to the end of a line,
  // /* ... */, an unterminated one included): a script's trailing or standalone comment,
  // which is never sent to the server as a statement.
  static bool IsOnlyComments(const std::string& text);

  // Check if a SQL statement is a DML/DDL command that should use ExecuteUpdate.
  static bool IsUpdateStatement(const std::string& sql);

  // Check if a SQL statement is a DDL command that changes the schema.
  static bool IsDdlStatement(const std::string& sql);

 private:
  std::string buffer_;
  size_t semicolon_pos_ = std::string::npos;
  bool in_single_quote_ = false;
  bool in_double_quote_ = false;
  bool in_block_comment_ = false;
};

}  // namespace hardcharger::client
