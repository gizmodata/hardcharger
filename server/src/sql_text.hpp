// SQL text as DuckDB's tokenizer sees it (v2 C API): statements split at their
// terminators, and the commands Hardcharger handles itself before DuckDB parses
// anything (DuckDB 2.0's grammar rejects `SET hardcharger.x` and `KILL SESSION`).
#pragma once

#include <duckdb_v2.h>

#include <chrono>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

struct Token {
  DUCKDB_V2_TOKEN_TYPE type;
  std::string_view text;  // a view into the tokenized SQL

  bool Is(DUCKDB_V2_TOKEN_TYPE t) const { return type == t; }
  // A keyword or bare identifier equal to `word`, ignoring case.
  bool IsWord(std::string_view word) const;
  // An operator token that starts with `op` (the tokenizer joins punctuation runs).
  bool IsOp(std::string_view op) const;
  // Identifier or keyword text, unquoted and lower-cased; a string literal's value.
  std::string Value() const;
};

// One statement: its text (without the terminator) and its tokens, comments dropped.
struct Piece {
  std::string_view text;
  std::vector<Token> tokens;
};

// Every token of `sql`, comments included. `sql` must outlive the tokens.
std::vector<Token> Tokenize(duckdb_v2_connection_handle connection, std::string_view sql);

// Statements in `sql`, split at top-level terminators; pieces of only comments and
// whitespace are dropped. An unterminated quote runs to the end.
std::vector<Piece> SplitStatements(duckdb_v2_connection_handle connection, std::string_view sql);

// `SET [SESSION|GLOBAL|LOCAL] hardcharger.<name> = <value>`,
// and `RESET ...` of one.
struct SettingCommand {
  enum class Scope { kAutomatic, kSession, kGlobal };
  Scope scope = Scope::kAutomatic;
  bool reset = false;
  std::string name;   // without the prefix, lower case: "query_timeout"
  std::string value;  // the value as text: a string literal's contents, else its tokens
};
std::optional<SettingCommand> ParseSettingCommand(const Piece& piece);

// `KILL SESSION '<id>'`, `"<id>"` or a bare id, any case; returns the id.
std::optional<std::string> ParseKillSession(const Piece& piece);

// Removes one layer of SQL quoting: 'it''s' -> it's, "a""b" -> a"b, E'..' escapes,
// $tag$..$tag$ bodies.
std::string Unquote(std::string_view literal);

// The other way, for SQL the server writes itself (instrumentation, catalog logging).
std::string SqlLiteral(std::string_view text);        // 'it''s'
std::string SqlIdentifier(std::string_view name);     // "a""b"
// A TIMESTAMPTZ literal in UTC with microseconds: '2026-10-17 12:00:00.000001+00'::TIMESTAMPTZ.
std::string SqlTimestamp(std::chrono::system_clock::time_point time);

}  // namespace hardcharger
