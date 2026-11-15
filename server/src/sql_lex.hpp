// SQL text as DuckDB reads it, token by token, with nothing left out: DuckDB's own
// tokenizer (duckdb::Tokenizer, the class its parser runs) over the text its parser
// tokenizes (duckdb::Parser::NormalizeSQLString: UTF-8 checked, Unicode spaces made plain
// ones), with the comments and the white space between tokens kept as tokens too. The log
// masking (redact.hpp) reads statements through this, so it cannot see a token boundary
// where DuckDB sees none. (The limits on a statement's shape are DuckDB's parser hook's,
// parse_guard.hpp, which reads every text DuckDB parses.)
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
