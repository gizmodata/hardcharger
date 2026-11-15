// SQL text as DuckDB reads it, token by token, with nothing left out: DuckDB's own
// tokenizer (duckdb::Tokenizer, the class its parser runs) over the text its parser
// tokenizes (duckdb::Parser::NormalizeSQLString: UTF-8 checked, Unicode spaces made plain
// ones), with the comments and the white space between tokens kept as tokens too. The log
// masking (redact.hpp) and the statement intake's bracket check read statements through
// this, so neither can see a token boundary where DuckDB sees none.
//
// No connection is needed: every DuckDB grammar, extended or not, is tokenized by this one
// class; a grammar extension changes only which words are keywords, which nothing here
// reports.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

enum class SqlTokenKind : std::uint8_t {
  kSpace,       // white space between tokens
  kComment,     // -- to the end of the line, /* nested */
  kString,      // '..', E'..', x'..', b'..', n'..', $tag$..$tag$
  kQuotedName,  // "..", `..`
  kWord,        // a keyword or an unquoted name (a$b is one word)
  kNumber,      // 1, 1.5e3, and a lone '.' (a.b is three tokens)
  kOperator,    // ( ) [ ] { } , : ? $ # each on its own; ::, :=; a run of + - * / % ^ < > = ~ ! @ & |
  kTerminator,  // ;
};

struct SqlToken {
  SqlTokenKind kind;
  std::string_view text;  // a view into SqlTokens::text
};

// The tokens of one text, covering every byte of it in order.
class SqlTokens {
 public:
  // The text the tokens are of: the SQL as DuckDB's parser tokenizes it (a Unicode space
  // DuckDB reads as white space is a plain space here).
  const std::string& text() const { return text_; }
  std::size_t size() const { return kinds_.size(); }
  SqlToken operator[](std::size_t i) const {
    const std::size_t start = starts_[i];
    const std::size_t end = i + 1 < starts_.size() ? starts_[i + 1] : text_.size();
    return {kinds_[i], std::string_view(text_).substr(start, end - start)};
  }
  SqlTokenKind kind(std::size_t i) const { return kinds_[i]; }

  // Why DuckDB cannot tokenize the text, when it cannot: "an unterminated string literal",
  // "an unterminated quoted identifier", "an unterminated /* comment", "an unterminated
  // dollar-quoted string", "invalid UTF-8", "longer than 4 GiB". Such text never parses,
  // and where its tokens end is anyone's guess: readers must not show any of it.
  const std::optional<std::string>& unreadable() const { return unreadable_; }

 private:
  friend SqlTokens LexSql(std::string_view sql);
  friend class SqlTokenCollector;
  std::string text_;
  std::vector<std::uint32_t> starts_;
  std::vector<SqlTokenKind> kinds_;
  std::optional<std::string> unreadable_;
};

SqlTokens LexSql(std::string_view sql);

// The parentheses a statement opens and never closes. Such a statement cannot parse, and
// DuckDB 2.0's parser takes time quadratic in their number to say so (8,000 of them: 20
// seconds; 500,000: hours), which neither the statement timeout nor a cancel interrupts: the
// statement intake refuses it before the parser sees it. Counted on DuckDB's tokens, so a
// parenthesis in a string, a quoted name or a comment is none.
//
// And how deep its brackets nest ( ( [ { together ). DuckDB's parser recurses once per level
// before any of its own limits is checked: a few thousand nested subqueries overflow the
// stack of the thread that parses them, and the server stops. The intake refuses a statement
// nested deeper than the server allows (max_sql_nesting), which DuckDB would refuse anyway.
struct SqlUnclosed {
  std::size_t count = 0;      // in the statement that has the most
  std::size_t position = 0;   // of that statement's first one, in bytes from the start of the text
  bool in_query_text = false; // in SQL given to query() as a literal, which DuckDB parses when it binds
  std::size_t nesting = 0;    // the deepest the text's brackets nest, a query() literal's included
  // The most tables one FROM clause names (joined, or listed with commas). The parser
  // recurses once per table as well, and DuckDB binds no more of them than its nesting limit.
  std::size_t tables = 0;
};
SqlUnclosed UnclosedParentheses(const SqlTokens& tokens);
// The error that refuses such a statement: parentheses never closed, brackets nested deeper
// than `nesting_limit`, or more than four times that many tables in one FROM clause (0: no
// limit on either). "" when the statement may go on to the parser.
std::string UnclosedParenthesesError(const SqlUnclosed& unclosed, std::size_t nesting_limit = 0);

// For tests: the tokens DuckDB's parser itself builds for `sql` (Parser::ParseQuery's own
// steps: NormalizeSQLString, then the tokenizer with the parser's behaviour, which throws
// where the parser refuses), as (kind, offset, text) with comments and white space left out
// as the parser leaves them out; or the parser's error when it refuses the text at this stage.
struct ParserTokensForTesting {
  struct Token {
    SqlTokenKind kind;
    std::size_t offset;
    std::string text;
    bool unterminated;
  };
  std::string normalized;
  std::vector<Token> tokens;
  std::optional<std::string> error;
};
ParserTokensForTesting DuckDBParserTokensForTesting(std::string_view sql);

}  // namespace hardcharger
