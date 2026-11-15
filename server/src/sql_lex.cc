#include "sql_lex.hpp"

#include <duckdb/common/exception.hpp>
#include <duckdb/common/string_util.hpp>
#include <duckdb/parser/parser.hpp>
#include <duckdb/parser/peg/keyword_helper/duckdb_keyword_helper.hpp>
#include <duckdb/parser/peg/tokenizer/parser_tokenizer.hpp>
#include <duckdb/parser/peg/tokenizer/tokenizer.hpp>

#include <algorithm>
#include <limits>
#include <string_view>
#include <vector>

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

namespace {

// What the literal `token` holds, for the SQL DuckDB will parse out of it: '..' with its
// doubled quotes, $tag$..$tag$. (An E'..' literal's escapes cannot make a parenthesis out of
// something else, nor the other way round.)
std::string QueryText(std::string_view token) {
  if (token.starts_with('$')) {
    const std::size_t tag = token.find('$', 1);
    if (tag == std::string_view::npos || token.size() < 2 * (tag + 1)) return std::string(token);
    return std::string(token.substr(tag + 1, token.size() - 2 * (tag + 1)));
  }
  const std::size_t open = token.find('\'');
  if (open == std::string_view::npos || token.size() < open + 2) return std::string(token);
  std::string out;
  const std::string_view inner = token.substr(open + 1, token.size() - open - 2);
  for (std::size_t i = 0; i < inner.size(); i++) {
    out += inner[i];
    if (inner[i] == '\'' && i + 1 < inner.size() && inner[i + 1] == '\'') i++;
  }
  return out;
}

bool IsWord(const SqlTokens& tokens, std::size_t i, std::string_view lower) {
  if (tokens.kind(i) != SqlTokenKind::kWord) return false;
  const std::string_view text = tokens[i].text;
  return text.size() == lower.size() && std::equal(text.begin(), text.end(), lower.begin(), [](char a, char b) {
           return dd::StringUtil::CharacterToLower(a) == b;
         });
}

SqlUnclosed Unclosed(const SqlTokens& tokens, int depth) {
  SqlUnclosed worst;
  if (tokens.unreadable()) return worst;  // (DuckDB's tokenizer refuses it at once)
  std::size_t open = 0;                   // parentheses open in this statement
  std::size_t first = 0;                  // where the outermost of them is
  std::size_t brackets = 0;               // ( [ { open in this statement
  std::size_t nesting = 0;                // the deepest they got anywhere
  std::size_t offset = 0;
  // For each bracket open (and the statement itself): the tables its FROM clause has named
  // so far, or 0 outside a FROM clause.
  std::vector<std::size_t> from(1, 0);
  std::size_t tables = 0;
  auto counted = [&](std::size_t n) { tables = std::max(tables, n); };
  auto statement_ends = [&] {
    if (open > worst.count) {
      worst.count = open;
      worst.position = first;
      worst.in_query_text = false;
    }
    open = brackets = 0;
    from.assign(1, 0);
  };
  static constexpr std::string_view kEndsFrom[] = {"where",  "group",     "order",  "having",  "limit",     "offset", "union",
                                                   "except", "intersect", "window", "qualify", "returning", "select", "set",
                                                   "values", "sample"};
  std::size_t previous_word = tokens.size();  // the last word token read
  for (std::size_t i = 0; i < tokens.size(); i++) {
    const SqlToken token = tokens[i];
    if (token.kind == SqlTokenKind::kTerminator) {
      statement_ends();
    } else if (token.kind == SqlTokenKind::kWord) {
      // FROM a, b JOIN c ...: one more table for each comma and each JOIN, until the clause ends.
      // (x IS DISTINCT FROM y is no clause.)
      if (IsWord(tokens, i, "from")) {
        if (!(previous_word < tokens.size() && IsWord(tokens, previous_word, "distinct"))) counted(from.back() = 1);
      } else if (IsWord(tokens, i, "join")) {
        if (from.back() > 0) counted(++from.back());
      } else if (from.back() > 0) {
        for (const std::string_view word : kEndsFrom) {
          if (IsWord(tokens, i, word)) from.back() = 0;
        }
      }
      previous_word = i;
    } else if (token.kind == SqlTokenKind::kOperator && token.text == ",") {
      if (from.back() > 0) counted(++from.back());
    } else if (token.kind == SqlTokenKind::kOperator && (token.text == "[" || token.text == "{")) {
      nesting = std::max(nesting, ++brackets);
      from.push_back(0);
    } else if (token.kind == SqlTokenKind::kOperator && (token.text == "]" || token.text == "}")) {
      if (brackets > 0) brackets--;
      if (from.size() > 1) from.pop_back();
    } else if (token.kind == SqlTokenKind::kOperator && token.text == "(") {
      if (open++ == 0) first = offset;
      nesting = std::max(nesting, ++brackets);
      from.push_back(0);
      // query('<sql>'): DuckDB parses the literal when it binds the call.
      std::size_t name = i;
      while (name > 0 && (tokens.kind(name - 1) == SqlTokenKind::kSpace || tokens.kind(name - 1) == SqlTokenKind::kComment)) name--;
      std::size_t argument = i + 1;
      while (argument < tokens.size() &&
             (tokens.kind(argument) == SqlTokenKind::kSpace || tokens.kind(argument) == SqlTokenKind::kComment)) {
        argument++;
      }
      if (depth < 4 && name > 0 && IsWord(tokens, name - 1, "query") && argument < tokens.size() &&
          tokens.kind(argument) == SqlTokenKind::kString) {
        const SqlUnclosed inner = Unclosed(LexSql(QueryText(tokens[argument].text)), depth + 1);
        if (inner.count > worst.count) {
          worst.count = inner.count;
          worst.position = offset;
          worst.in_query_text = true;
        }
        nesting = std::max(nesting, brackets + inner.nesting);
        counted(inner.tables);
      }
    } else if (token.kind == SqlTokenKind::kOperator && token.text == ")") {
      if (open > 0) open--;  // (one too many is DuckDB's to refuse: it does so at once)
      if (brackets > 0) brackets--;
      if (from.size() > 1) from.pop_back();
    }
    offset += token.text.size();
  }
  statement_ends();
  worst.nesting = nesting;
  worst.tables = tables;
  return worst;
}

}  // namespace

SqlUnclosed UnclosedParentheses(const SqlTokens& tokens) { return Unclosed(tokens, 0); }

std::string UnclosedParenthesesError(const SqlUnclosed& unclosed, std::size_t nesting_limit) {
  if (unclosed.count == 0) {
    if (nesting_limit == 0) return {};
    if (unclosed.nesting > nesting_limit) {
      return "Parser Error: the statement's brackets nest " + std::to_string(unclosed.nesting) +
             " deep, and this server takes " + std::to_string(nesting_limit) + " (its max_sql_nesting option)";
    }
    if (unclosed.tables > 4 * nesting_limit) {
      return "Parser Error: a FROM clause of the statement names " + std::to_string(unclosed.tables) +
             " tables, and this server takes " + std::to_string(4 * nesting_limit) +
             " in one (four times its max_sql_nesting option)";
    }
    return {};
  }
  return "Parser Error: syntax error: " + std::to_string(unclosed.count) +
         (unclosed.count == 1 ? " opening parenthesis is" : " opening parentheses are") + " never closed (" +
         (unclosed.in_query_text ? "in the SQL given to query() at position " : "the first \"(\" at position ") +
         std::to_string(unclosed.position + 1) + ")";
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
