#include "sql_lex.hpp"

#include <duckdb/common/exception.hpp>
#include <duckdb/common/string_util.hpp>
#include <duckdb/parser/parser.hpp>
#include <duckdb/parser/peg/keyword_helper/duckdb_keyword_helper.hpp>
#include <duckdb/parser/peg/tokenizer/parser_tokenizer.hpp>
#include <duckdb/parser/peg/tokenizer/tokenizer.hpp>

#include <algorithm>
#include <limits>

namespace hardcharger {

namespace dd = duckdb;

namespace {

SqlTokenKind KindOf(dd::TokenType type, std::string_view text) {
  switch (type) {
    case dd::TokenType::STRING_LITERAL:
      return SqlTokenKind::kString;
    case dd::TokenType::NUMBER_LITERAL:
      return SqlTokenKind::kNumber;
    case dd::TokenType::OPERATOR:
      return SqlTokenKind::kOperator;
    case dd::TokenType::COMMENT:
      return SqlTokenKind::kComment;
    case dd::TokenType::TERMINATOR:
      return SqlTokenKind::kTerminator;
    default:  // a keyword or an identifier, quoted or not
      return !text.empty() && (text.front() == '"' || text.front() == '`') ? SqlTokenKind::kQuotedName
                                                                           : SqlTokenKind::kWord;
  }
}

}  // namespace

// Receives DuckDB's tokens as its tokenizer makes them. The tokenizer hands some to
// PushToken and appends the others (single-character operators, `::`, a parameter's `$`)
// to the vector it was given; both arrive here in the order of the text, and the vector is
// emptied as it is read, so a statement's tokens are never all held as DuckDB's (about
// 100 bytes each) at once.
class SqlTokenCollector final : public dd::TokenizerBehavior {
 public:
  SqlTokenCollector(const dd::string& sql, dd::vector<dd::MatcherToken>& pending, SqlTokens& out)
      : dd::TokenizerBehavior(sql, pending), out_(out) {}

  void PushToken(dd::idx_t start, dd::idx_t end, dd::TokenType type, bool unterminated) override {
    Drain();
    // (The tokenizer's start for a comment can take in the `$` before it, a token already.)
    start = std::max<dd::idx_t>(start, end_);
    if (start >= end) return;
    Add(start, end, KindOf(type, std::string_view(sql).substr(start, end - start)));
    if (unterminated) {
      Unreadable(type == dd::TokenType::COMMENT ? "an unterminated /* comment" : "an unterminated dollar-quoted string");
    }
  }

  void OnStatementEnd(dd::idx_t pos) override {
    Drain();
    Add(pos, pos + 1, SqlTokenKind::kTerminator);
  }

  void OnLastToken(const dd::Tokenizer&, dd::TokenizeState state, dd::string last_word, dd::idx_t last_pos) override {
    Drain();
    if (last_word.empty()) return;
    const std::string_view text = std::string_view(sql).substr(last_pos);
    Add(last_pos, sql.size(), KindOf(dd::Tokenizer::TokenizeStateToType(state), text));
    if (state == dd::TokenizeState::STRING_LITERAL) Unreadable("an unterminated string literal");
    if (state == dd::TokenizeState::QUOTED_IDENTIFIER) Unreadable("an unterminated quoted identifier");
  }

  // The tokens the tokenizer appended itself since the last call.
  void Drain() {
    for (const dd::MatcherToken& token : tokens) {
      if (token.type == dd::TokenType::END_OF_INPUT || token.type == dd::TokenType::END_OF_INPUT_AUTOCOMPLETE) continue;
      // The tokenizer's offset for one of these can be a byte early (after a `$` that opens
      // nothing): the token is where its text is, at or after the end of the one before.
      std::size_t start = std::max<std::size_t>(token.offset, end_);
      while (start < sql.size() && sql.compare(start, token.length, token.text) != 0 &&
             dd::StringUtil::CharacterIsSpace(sql[start])) {
        start++;
      }
      if (start + token.length > sql.size() || sql.compare(start, token.length, token.text) != 0) {
        Unreadable("tokens DuckDB places outside the text");
        continue;
      }
      Add(start, start + token.length, KindOf(token.type, token.text));
      if (token.unterminated) Unreadable("an unterminated token");
    }
    tokens.clear();
  }

  // After the last token: the rest of the text is white space.
  void Finish() {
    Drain();
    if (end_ < sql.size()) AddRaw(end_, SqlTokenKind::kSpace);
  }

 private:
  void Add(std::size_t start, std::size_t end, SqlTokenKind kind) {
    if (start < end_ || end > sql.size() || start >= end) {
      Unreadable("tokens DuckDB places out of order");
      return;
    }
    if (start > end_) {
      // Between two tokens DuckDB skips white space only. Anything else it dropped is shown
      // to no one as a token of its own kind: it is kept as an operator, which nothing masks
      // around, and the differential test fails on it.
      bool space = true;
      for (std::size_t i = end_; i < start; i++) space = space && dd::StringUtil::CharacterIsSpace(sql[i]);
      AddRaw(end_, space ? SqlTokenKind::kSpace : SqlTokenKind::kOperator);
    }
    AddRaw(start, kind);
    end_ = end;
  }

  void AddRaw(std::size_t start, SqlTokenKind kind) {
    out_.starts_.push_back(static_cast<std::uint32_t>(start));
    out_.kinds_.push_back(kind);
  }

  void Unreadable(const char* why) {
    if (!out_.unreadable_) out_.unreadable_ = why;
  }

  SqlTokens& out_;
  std::size_t end_ = 0;  // of the last token added
};

SqlTokens LexSql(std::string_view sql) {
  SqlTokens out;
  if (sql.size() >= std::numeric_limits<std::uint32_t>::max()) {
    out.unreadable_ = "longer than 4 GiB";
    return out;
  }
  try {
    // What Parser::ParseQuery does first: refuse invalid UTF-8, then turn the Unicode
    // spaces it reads as white space into plain ones.
    out.text_ = dd::Parser::NormalizeSQLString(dd::string(sql));
  } catch (const std::exception&) {
    out.unreadable_ = "invalid UTF-8";
    return out;
  }
  try {
    dd::vector<dd::MatcherToken> pending;
    SqlTokenCollector collector(out.text_, pending, out);
    static const dd::Tokenizer tokenizer(dd::DuckDBKeywordHelper::Instance());
    tokenizer.TokenizeInput(collector);
    collector.Finish();
  } catch (const std::exception&) {
    if (!out.unreadable_) out.unreadable_ = "text DuckDB's tokenizer refuses";
  }
  return out;
}

ParserTokensForTesting DuckDBParserTokensForTesting(std::string_view sql) {
  ParserTokensForTesting out;
  try {
    out.normalized = dd::Parser::NormalizeSQLString(dd::string(sql));
    dd::vector<dd::MatcherToken> tokens;
    dd::ParserTokenizerBehavior behavior(out.normalized, tokens);
    dd::Tokenizer(dd::DuckDBKeywordHelper::Instance()).TokenizeInput(behavior);
    for (const auto& token : tokens) {
      if (token.type == dd::TokenType::END_OF_INPUT || token.type == dd::TokenType::END_OF_INPUT_AUTOCOMPLETE) continue;
      out.tokens.push_back({KindOf(token.type, token.text), token.offset, token.text, token.unterminated});
    }
  } catch (const std::exception& error) {
    out.error = error.what();
  }
  return out;
}

}  // namespace hardcharger
