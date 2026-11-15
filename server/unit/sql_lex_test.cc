// SQL as DuckDB's tokenizer reads it (LexSql, sql_lex.hpp), checked against DuckDB itself.
//
// The log masking reads statements through LexSql. Wherever its
// tokens differed from DuckDB's, text DuckDB runs as SQL would be read as a literal or as
// part of a word (the 2.13.0 masker's own lexer did: `$` inside a name, U+2060). LexSql
// runs DuckDB's tokenizer class, so the two agree by construction; the differential test
// below holds that in place against the two other ways DuckDB tokenizes the same text: the
// parser's own steps (Parser::NormalizeSQLString, then the tokenizer with the parser's
// behaviour, which throws where the parser refuses) and the C API on a live connection
// (duckdb_v2_tokenize_sql, the connection's grammar). Seeded, so a failure names its input.
#include <gtest/gtest.h>

#include <cstdint>
#include <random>
#include <string>
#include <vector>

#include "duckdb_v2.hpp"
#include "sql_lex.hpp"

using hardcharger::LexSql;
using hardcharger::SqlTokenKind;
using hardcharger::SqlTokens;

namespace {

std::string Escaped(std::string_view text) {
  std::string out;
  for (const unsigned char c : text) {
    if (c == '\\') out += "\\\\";
    else if (c >= 0x20 && c < 0x7f) out += static_cast<char>(c);
    else {
      char hex[8];
      std::snprintf(hex, sizeof hex, "\\x%02x", c);
      out += hex;
    }
  }
  return out;
}

const char* Name(SqlTokenKind kind) {
  switch (kind) {
    case SqlTokenKind::kSpace: return "space";
    case SqlTokenKind::kComment: return "comment";
    case SqlTokenKind::kString: return "string";
    case SqlTokenKind::kQuotedName: return "quoted-name";
    case SqlTokenKind::kWord: return "word";
    case SqlTokenKind::kNumber: return "number";
    case SqlTokenKind::kOperator: return "operator";
    case SqlTokenKind::kTerminator: return "terminator";
  }
  return "?";
}

struct Plain {
  SqlTokenKind kind;
  std::size_t offset;
  std::string text;
  bool operator==(const Plain&) const = default;
};

std::string Show(const std::vector<Plain>& tokens) {
  std::string out;
  for (const auto& token : tokens) out += std::string(Name(token.kind)) + "@" + std::to_string(token.offset) + "[" + Escaped(token.text) + "] ";
  return out;
}

class SqlLex : public ::testing::Test {
 protected:
  static void SetUpTestSuite() {
    chargewire_duckdb::Call([](auto* e) { return duckdb_v2_environment_create(environment_.out(), e); });
    chargewire_duckdb::Call([](auto* e) {
      return duckdb_v2_instance_create_with_options(environment_.get(), nullptr, nullptr, 0, instance_.out(), e);
    });
    chargewire_duckdb::Call([](auto* e) { return duckdb_v2_connection_create(instance_.get(), connection_.out(), e); });
  }
  static void TearDownTestSuite() {
    connection_.reset();
    instance_.reset();
    environment_.reset();
  }

  // The C API's tokens of `text` on a live connection, comments included.
  static std::vector<Plain> ConnectionTokens(const std::string& text, bool* ends_unterminated) {
    using Iterator = chargewire_duckdb::Owned<duckdb_v2_token_iterator_handle, duckdb_v2_token_iterator_destroy>;
    const duckdb_v2_str sql{text.data(), text.size()};
    Iterator iterator;
    chargewire_duckdb::Call([&](auto* e) { return duckdb_v2_tokenize_sql(connection_.get(), &sql, iterator.out(), e); });
    chargewire_duckdb::Call([&](auto* e) { return duckdb_v2_token_iterator_ends_unterminated(iterator.get(), ends_unterminated, e); });
    std::vector<Plain> tokens;
    for (;;) {
      DUCKDB_V2_TOKEN_TYPE type = DUCKDB_V2_TOKEN_TYPE_INVALID;
      idx_t start = 0, length = 0;
      chargewire_duckdb::Call([&](auto* e) { return duckdb_v2_token_iterator_next(iterator.get(), &type, &start, &length, e); });
      if (type == DUCKDB_V2_TOKEN_TYPE_END_OF_INPUT) return tokens;
      const std::string lexeme = text.substr(start, length);
      SqlTokenKind kind = SqlTokenKind::kWord;
      if (type == DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) kind = SqlTokenKind::kString;
      else if (type == DUCKDB_V2_TOKEN_TYPE_NUMBER_LITERAL) kind = SqlTokenKind::kNumber;
      else if (type == DUCKDB_V2_TOKEN_TYPE_OPERATOR) kind = SqlTokenKind::kOperator;
      else if (type == DUCKDB_V2_TOKEN_TYPE_COMMENT) kind = SqlTokenKind::kComment;
      else if (type == DUCKDB_V2_TOKEN_TYPE_TERMINATOR) kind = SqlTokenKind::kTerminator;
      else if (!lexeme.empty() && (lexeme.front() == '"' || lexeme.front() == '`')) kind = SqlTokenKind::kQuotedName;
      tokens.push_back({kind, start, lexeme});
    }
  }

  // Every difference between LexSql and DuckDB for `sql`, as text ("" for none).
  static std::string Difference(const std::string& sql) {
    const SqlTokens ours = LexSql(sql);
    const auto parser = hardcharger::DuckDBParserTokensForTesting(sql);
    if (!parser.error && !parser.tokens.empty() && parser.normalized.empty()) return "no normalized text";
    // 1. Invalid UTF-8: both refuse, and nothing else is compared.
    if (parser.error && parser.error->find("Invalid UTF-8") != std::string::npos) {
      return ours.unreadable() == std::optional<std::string>("invalid UTF-8") ? "" : "DuckDB refuses the UTF-8, LexSql does not";
    }
    if (ours.unreadable() == std::optional<std::string>("invalid UTF-8")) return "LexSql refuses UTF-8 DuckDB takes";
    const std::string& text = ours.text();
    // 2. The tokens cover the text, in order, and white space is white space.
    std::string joined;
    std::vector<Plain> all, solid;
    for (std::size_t i = 0; i < ours.size(); i++) {
      const auto token = ours[i];
      if (token.text.empty()) return "an empty token";
      const Plain plain{token.kind, joined.size(), std::string(token.text)};
      joined += token.text;
      if (token.kind == SqlTokenKind::kSpace) {
        if (token.text.find_first_not_of(" \t\n\v\f\r") != std::string_view::npos) return "a space token that is not white space";
        continue;
      }
      all.push_back(plain);
      if (token.kind != SqlTokenKind::kComment) solid.push_back(plain);
    }
    if (joined != text) return "the tokens do not cover the text";
    // 3. The C API on a live connection (the connection's grammar; comments are tokens): the same tokens.
    bool ends_unterminated = false;
    const std::vector<Plain> connection = ConnectionTokens(text, &ends_unterminated);
    // (DuckDB reports the token after a `$` that opens nothing one byte early, over the `$`:
    // such a token is the same when it has the same length, or the same end, right after
    // the token before it.)
    bool same = connection.size() == all.size();
    std::size_t before = 0;  // where the token before ended
    for (std::size_t i = 0; same && i < all.size(); i++) {
      const Plain& theirs = connection[i];
      const Plain& mine = all[i];
      const bool early = theirs.offset < before && mine.offset == before &&
                         (theirs.text.size() == mine.text.size() || theirs.offset + theirs.text.size() == mine.offset + mine.text.size());
      same = theirs.kind == mine.kind && (theirs == mine || early);
      before = mine.offset + mine.text.size();
    }
    if (!same) return "connection tokens: " + Show(connection) + "\n   LexSql: " + Show(all);
    if (ours.unreadable() && !ends_unterminated) return "LexSql says unreadable (" + *ours.unreadable() + "), the C API says terminated";
    // 4. The parser's own tokenization: it throws exactly where LexSql says unreadable (an
    // unterminated dollar-quoted string it carries as a token marked so, and fails on later).
    const bool zero_length = parser.error && parser.error->find("zero-length delimited identifier") != std::string::npos;
    if (zero_length) return "";  // "" stops the parser there: it says nothing of the rest
    bool parser_unterminated = parser.error.has_value();
    for (const auto& token : parser.tokens) parser_unterminated = parser_unterminated || token.unterminated;
    if (parser_unterminated != ours.unreadable().has_value()) {
      return std::string("the parser ") + (parser_unterminated ? "refuses" : "takes") + " the text (" + parser.error.value_or("no error") +
             "), LexSql says " + ours.unreadable().value_or("readable");
    }
    if (parser.error) return "";
    if (parser.normalized != text) return "the parser's text differs";
    // (The parser's token offsets are compared by text: one of DuckDB's can be a byte early.)
    if (parser.tokens.size() != solid.size()) {
      std::vector<Plain> theirs;
      for (const auto& token : parser.tokens) theirs.push_back({token.kind, token.offset, token.text});
      return "parser tokens: " + Show(theirs) + "\n   LexSql: " + Show(solid);
    }
    for (std::size_t i = 0; i < solid.size(); i++) {
      if (parser.tokens[i].kind != solid[i].kind || parser.tokens[i].text != solid[i].text) {
        return "token " + std::to_string(i) + ": the parser has " + Name(parser.tokens[i].kind) + " [" + Escaped(parser.tokens[i].text) +
               "], LexSql " + Name(solid[i].kind) + " [" + Escaped(solid[i].text) + "]";
      }
    }
    return "";
  }

  static chargewire_duckdb::Environment environment_;
  static chargewire_duckdb::Instance instance_;
  static chargewire_duckdb::Connection connection_;
};

chargewire_duckdb::Environment SqlLex::environment_;
chargewire_duckdb::Instance SqlLex::instance_;
chargewire_duckdb::Connection SqlLex::connection_;

// Pieces statement text is made of where lexers disagree: every quote and comment opener,
// dollar tags, literal prefixes, numbers with exponents, operators, the Unicode spaces
// DuckDB takes for white space and their neighbours it does not, and bytes that are not UTF-8.
const std::vector<std::string> kPieces = {
    "'", "''", "\"", "\"\"", "`", "$", "$$", "$q$", "$tag$", "$1", "$a", "$_", "$\xc3\xa9$", "--", "/*", "*/", "\n", "\r", "\r\n", "\\",
    "\\'", "E'", "e'", "x'", "X'", "b'", "n'", "N'", "U&'", "r'", ";", "(", ")", "[", "]", "{", "}", ",", ":", "::", ":=", "=>", "=",
    "==", "<>", "!=", "<=", ">=", "||", "->", "->>", ".", "..", "1", "12", "1.5", ".5", "1e5", "1e-5", "1E+5", "1e", "1_000", "0x1F",
    "a", "A", "tok", "SELECT", "select", "password", "token", "_", "a$b", "a$q$", "a$", "\xc3\xa9", "\xe2\x82\xac", "\xf0\x9f\x98\x80",
    " ", "  ", "\t", "\v", "\f", "\xc2\xa0", "\xe2\x80\x80", "\xe2\x80\x83", "\xe2\x80\x8b", "\xe2\x80\x8c", "\xe2\x80\x8d", "\xe2\x80\xaf",
    "\xe2\x81\x9f", "\xe2\x81\xa0", "\xe2\x81\xa1", "\xe3\x80\x80", "\xef\xbb\xbf", "\xe1\x9a\x80", "\xe2\x80\xa8", "\xe2\x80\xa9",
    "\xc2\x85", "\xe1\xa0\x8e", "\xc2", "\xe2\x80", "\xff", "?", "#", "@", "+", "-", "*", "/", "%", "^", "<", ">", "~", "!", "&", "|"};

std::string RandomText(std::mt19937_64& random) {
  std::string text;
  const std::size_t pieces = std::uniform_int_distribution<std::size_t>(0, 40)(random);
  for (std::size_t i = 0; i < pieces; i++) {
    const auto roll = std::uniform_int_distribution<int>(0, 99)(random);
    if (roll < 85) {
      text += kPieces[std::uniform_int_distribution<std::size_t>(0, kPieces.size() - 1)(random)];
    } else if (roll < 97) {
      text += static_cast<char>(std::uniform_int_distribution<int>(0x20, 0x7e)(random));
    } else {
      text += static_cast<char>(std::uniform_int_distribution<int>(1, 0xff)(random));  // any byte but NUL
    }
  }
  return text;
}

}  // namespace

TEST_F(SqlLex, TokensAreDuckDBsOnAdversarialText) {
  // The disagreements the review found in the 2.13.0 lexer, and their neighbours.
  for (const std::string sql :
       {"SELECT 1 AS a$q$", "ATTACH 'f.db' AS a$q$ (ENCRYPTION_KEY 'k') -- b$q$", "UPDATE acct SET bal$usd$ = 1 WHERE bal$usd$ = 0",
        "SELECT $q$a$q$, $$b$$, $1, $name, $", "SELECT $q$never closed", "SELECT $a$x$b$", "SELECT $1$", "SELECT a$$b$$", "SELECT $ $",
        "SELECT\xe2\x81\xa0""42", "ENCRYPTION_KEY\xe2\x81\xa0'k'", "token\xe2\x81\xa0=> 'k'", "SELECT\xc2\xa0""1", "SELECT\xe1\x9a\x80""1",
        "SELECT\xe2\x80\xa8""1", "SELECT '\xe2\x81\xa0'", "SELECT \"a\xc2\xa0""b\"", "SELECT 1 -- c\xe2\x81\xa0\nFROM t", "SELECT /* \xc2\xa0 */ 1",
        "SELECT E'a\\'b\xc2\xa0'", "SELECT E'\\\\', 'x'", "SELECT e'\\", "SELECT 1e'x\\'y'", "SELECT x'ab', b'01', n'x', U&'x', r'x'",
        "SELECT 'a''b', \"a\"\"b\", `a``b`", "SELECT 'never closed", "SELECT \"never closed", "SELECT `never closed", "SELECT ''", "SELECT \"\"",
        "SELECT 1 /* a /* nested */ b */ + 2", "SELECT 1 /* never closed", "SELECT 1 /* /* half */", "SELECT 1 -- line\rSELECT 2", "SELECT 1--",
        "SELECT 1-- c", "SELECT 1 - -1, 1--1", "SELECT a.b.c, 1.5, .5, 1., 1e5, 1e-5, 1e, 1_000, 1__0, 0x1F", "SELECT 1+-2, a<>-b, a||'b', x::INT",
        "SELECT a := 1, f(x => 2), {a: 1}, m[1:2], a ? b, #1", "SELECT $--c\n1", "SELECT $/*c*/1", "SELECT $)", "(((((", "", " ", ";", ";;",
        "SELECT 1; SELECT 2;", "\xff", "SELECT '\xc3'", "SELECT 1 \xe2\x80", "a\tb\vc\fd"}) {
    EXPECT_EQ(Difference(sql), "") << "for: " << Escaped(sql);
  }
}

TEST_F(SqlLex, TokensAreDuckDBsOnSeededRandomText) {
  // Five seeds, 20,000 texts each. A failure prints the seed, the iteration and the text.
  for (const std::uint64_t seed : {20261115ull, 1ull, 0x9e3779b97f4a7c15ull, 424242ull, 7ull}) {
    std::mt19937_64 random(seed);
    for (int iteration = 0; iteration < 20000; iteration++) {
      const std::string sql = RandomText(random);
      const std::string difference = Difference(sql);
      ASSERT_EQ(difference, "") << "seed " << seed << ", iteration " << iteration << ", text: " << Escaped(sql);
    }
  }
}

TEST_F(SqlLex, KindsAndUnreadableReasons) {
  auto kinds = [](const std::string& sql) {
    std::string out;
    const SqlTokens tokens = LexSql(sql);
    for (std::size_t i = 0; i < tokens.size(); i++) out += std::string(i == 0 ? "" : " ") + Name(tokens.kind(i));
    return out;
  };
  EXPECT_EQ(kinds("SELECT a$q$, 'x' -- c"), "word space word operator space string space comment");
  EXPECT_EQ(kinds("a.b"), "word number word");
  EXPECT_EQ(kinds("f(\"x\"=>$1);"), "word operator quoted-name operator operator number operator terminator");
  EXPECT_EQ(LexSql("SELECT\xe2\x81\xa0""42").text(), "SELECT 42");  // DuckDB reads U+2060 as a space
  EXPECT_EQ(LexSql("SELECT 'x").unreadable(), std::optional<std::string>("an unterminated string literal"));
  EXPECT_EQ(LexSql("SELECT \"x").unreadable(), std::optional<std::string>("an unterminated quoted identifier"));
  EXPECT_EQ(LexSql("SELECT /* x").unreadable(), std::optional<std::string>("an unterminated /* comment"));
  EXPECT_EQ(LexSql("SELECT $q$x").unreadable(), std::optional<std::string>("an unterminated dollar-quoted string"));
  EXPECT_EQ(LexSql("SELECT '\xff'").unreadable(), std::optional<std::string>("invalid UTF-8"));
  EXPECT_FALSE(LexSql("SELECT 1 AS a$q$ -- trailing comment").unreadable());
  EXPECT_FALSE(LexSql("SELECT 1 AS \"\"").unreadable());  // tokenized; DuckDB's parser refuses it later
}
