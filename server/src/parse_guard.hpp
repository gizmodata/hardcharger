// The one place the limits on SQL text's shape live: DuckDB's parser_override hook, which
// DuckDB calls with every text a connection asks its parser to parse (a statement, a request
// of several, a prepare, the init SQL, the SQL query() or json_serialize_sql() is given,
// computed or not), before its own parser runs.
//
// The hook reads the text with the tokenizer DuckDB's parser itself runs and refuses, before
// that parser sees it:
// - parentheses opened and never closed: DuckDB 2.0's parser takes time quadratic in their
//   number to say the text cannot parse (8,000 of them: about 20 s; 500,000: hours), and
//   neither a statement timeout nor a cancel interrupts a parse;
// - brackets ( ( [ { together ) nested deeper than the server's limit (max_sql_nesting): the
//   parser recurses once per level before any limit of its own is checked;
// - more than four times that many tables in one FROM clause: the parser recurses once per
//   table as well;
// - more tables in one FROM clause than max_joined_tables, or more than eight times that in
//   all the statement's FROM clauses together: DuckDB's join-order search takes time that
//   grows steeply with the tables one clause joins (on a busy laptop: a chain of 128 tables
//   0.7 s, 256 16 s; a join graph with cycles, 96 tables 1.9 s, 128 7 s; 600 over 2 minutes),
//   and no timeout or cancel interrupts it (hc-v214-notes/masking-parser/join-cap);
// - a dotted name (a.b.c...) of more parts than the limit: DuckDB's binder takes time
//   quadratic in their number (20,000 parts: 45 s), uninterrupted.
// Anything else goes on to DuckDB's own parser unchanged. The hook needs DuckDB's setting
// allow_parser_override_extension at 'fallback' (DuckDB's default leaves the hook uncalled);
// a planner extension refuses every statement that would change that setting, or raise
// max_expression_depth past the limit, once startup is over.
#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <string_view>

#include <duckdb_v2.h>

namespace hardcharger {

// What the guard reads in a text.
struct SqlShape {
  std::size_t unclosed = 0;    // parentheses never closed, in the statement that has the most
  std::size_t position = 0;    // of that statement's first one, in bytes from the start of the text
  std::size_t nesting = 0;     // the deepest the text's brackets nest
  std::size_t tables = 0;      // the most tables one FROM clause names (joined, or listed with commas)
  // The most tables one statement's FROM clauses join in all: of every clause in it that names
  // two or more (a derived table's and a CTE's included), all of them, added up.
  std::size_t joined = 0;
  std::size_t name_parts = 0;  // the most parts one dotted name has (a.b.c: 3)
  // The text ends in a string literal, a quoted name or a /* comment that never ends: DuckDB's
  // tokenizer refuses it at once (an unterminated dollar-quoted string it does not: that one
  // goes on to the parser), so it is left to DuckDB, whose error says so.
  bool refused_by_tokenizer = false;
};

// `sql` as DuckDB's parser is handed it (Parser::NormalizeSQLString applied). One pass of
// DuckDB's tokenizer, nothing kept but counts.
SqlShape MeasureSql(const std::string& sql);

// The error that refuses a text of that shape on a server whose limit is `nesting_limit`
// (0: no limit on nesting, tables or names) and whose limit on the tables a statement joins
// is `joined_limit` (0: none), without DuckDB's "Parser Error: " prefix; "" when the text may
// go on to DuckDB's parser.
std::string ShapeRefusal(const SqlShape& shape, std::size_t nesting_limit, std::size_t joined_limit = 0);

// DuckDB's autocomplete extension: sql_auto_complete(), sql_tokenize(), check_peg_parser() and
// duckdb_format_sql() run DuckDB's PEG matcher (or formatter) on their argument directly, not
// through the parser and so not through the hook. sql_auto_complete's input is unfinished SQL
// by design, and the matcher's time grows exponentially with the brackets it leaves open
// (`1 + (` six times: 1.7 s; eight: over 15 s; sql_tokenize: 2,000 unclosed parentheses ran
// past 120 s), with no statement timeout or cancel able to stop it: no cap on the input keeps
// every shape fast. So they are off unless the server's enable_sql_auto_complete option is
// on, and then for administrators only.
struct AutoCompletePolicy {
  bool enabled = false;
  // Whether the session running the statement being bound is an administrator.
  std::function<bool()> is_administrator;
};
inline constexpr char kAutoCompleteExtension[] = "autocomplete";

class ParseGuard {
 public:
  // Registers the hook, the setting guard and the autocomplete guard on `instance`. The
  // setting must already be 'fallback' (an instance option, DuckDBBackend's): from here on
  // every parse is checked.
  static std::shared_ptr<ParseGuard> Install(duckdb_v2_instance_handle instance, std::size_t nesting_limit,
                                             std::size_t joined_limit,
                                             AutoCompletePolicy autocomplete);

  // The end of startup, before any session: the autocomplete extension's functions, if it is
  // loaded (statically linked, by the init SQL, or by the server for enable_sql_auto_complete),
  // are wrapped in the policy's check, on `connection`'s catalog; from now on no load of that
  // extension is allowed; and any statement that changes allow_parser_override_extension or
  // raises max_expression_depth past the limit is refused, for every session, the
  // administrator's included. Called after the init SQL (the operator's, which may change
  // either setting) once the server has set both back globally (Backend: SET GLOBAL ...).
  void Lock(duckdb_v2_connection_handle connection);

  ~ParseGuard();
  ParseGuard(const ParseGuard&) = delete;
  ParseGuard& operator=(const ParseGuard&) = delete;

  std::size_t nesting_limit() const { return nesting_limit_; }
  // Whether the autocomplete extension was loaded when Lock() ran (and is guarded).
  bool autocomplete_loaded() const { return autocomplete_loaded_; }
  bool locked() const;

  struct State;

 private:
  explicit ParseGuard(std::shared_ptr<State> state, std::size_t nesting_limit)
      : state_(std::move(state)), nesting_limit_(nesting_limit) {}
  std::shared_ptr<State> state_;
  std::size_t nesting_limit_;
  bool autocomplete_loaded_ = false;
};

// The DuckDB setting the hook needs, and the value it needs.
inline constexpr char kParserOverrideSetting[] = "allow_parser_override_extension";
inline constexpr char kParserOverrideValue[] = "fallback";

}  // namespace hardcharger
