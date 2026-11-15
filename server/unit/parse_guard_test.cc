// The parse guard's reading of SQL text (parse_guard.hpp): what DuckDB's parser hook counts in
// every text DuckDB is asked to parse, and the errors that refuse a text before DuckDB's own
// parser sees it. The hook itself, on a live server through every way text reaches the
// parser, is tested in server/tests/test_statement_intake_brackets.py.
#include <gtest/gtest.h>

#include <chrono>
#include <cstdlib>
#include <string>

#include "parse_guard.hpp"

using hardcharger::MeasureSql;
using hardcharger::ShapeRefusal;

namespace {

double TimeoutScale() {
  const char* scale = std::getenv("HARDCHARGER_TEST_TIMEOUT_SCALE");
  return scale != nullptr ? std::atof(scale) : 1.0;
}

std::string Refusal(const std::string& sql, std::size_t limit) { return ShapeRefusal(MeasureSql(sql), limit); }

}  // namespace

TEST(ParseGuard, UnclosedParenthesesAreCountedOnTokens) {
  EXPECT_EQ(MeasureSql("SELECT (1 + (2 * 3))").unclosed, 0u);
  EXPECT_EQ(MeasureSql("SELECT ((1").unclosed, 2u);
  EXPECT_EQ(MeasureSql("SELECT ((1").position, 7u);
  EXPECT_EQ(MeasureSql("SELECT abs(1), ((2 + 3)").unclosed, 1u);
  EXPECT_EQ(MeasureSql("SELECT abs(1), ((2 + 3)").position, 15u);
  // Each statement on its own; the worst one is reported.
  EXPECT_EQ(MeasureSql("SELECT (1; SELECT (((2; SELECT 3").unclosed, 3u);
  EXPECT_EQ(MeasureSql("SELECT (1; SELECT (((2; SELECT 3").position, 18u);
  EXPECT_EQ(MeasureSql("SELECT (1); SELECT (2)").unclosed, 0u);
  // A parenthesis in a literal, a dollar-quoted literal, a quoted name or a comment is no token.
  EXPECT_EQ(MeasureSql("SELECT '((((', $$((($$, $q$(($q$, \"((\", `((` -- ((\n /* (( */").unclosed, 0u);
  EXPECT_EQ(MeasureSql("SELECT E'\\'((', a$q$ FROM t WHERE x = '(' AND y = a$q$").unclosed, 0u);
  // One too many closes nothing that follows; DuckDB refuses it at once on its own.
  EXPECT_EQ(MeasureSql("SELECT 1)))) + (2").unclosed, 1u);
  // Brackets of the other kinds are not what DuckDB's parser is slow on.
  EXPECT_EQ(MeasureSql("SELECT [[[1, {a: 1").unclosed, 0u);
  // SQL in a literal is not this text's: DuckDB parses it (query(), json_serialize_sql())
  // through the hook again, when it does.
  EXPECT_EQ(MeasureSql("SELECT * FROM query('SELECT ((1')").unclosed, 0u);
  // A text that ends in an unterminated literal, quoted name or comment is DuckDB's tokenizer's
  // to refuse, at once and with the right reason (8,000 parentheses before it: 0.02 s); one
  // that ends in an unterminated dollar-quoted string goes on to the parser (17.8 s): refused.
  EXPECT_TRUE(MeasureSql("SELECT ((( 'never closed").refused_by_tokenizer);
  EXPECT_TRUE(MeasureSql("SELECT ((( \"never closed").refused_by_tokenizer);
  EXPECT_TRUE(MeasureSql("SELECT ((( /* never closed").refused_by_tokenizer);
  EXPECT_EQ(ShapeRefusal(MeasureSql("SELECT ((( 'never closed"), 1000), "");
  EXPECT_FALSE(MeasureSql("SELECT ((( $q$ never closed").refused_by_tokenizer);
  EXPECT_EQ(ShapeRefusal(MeasureSql("SELECT ((( $q$ never closed"), 1000),
            "syntax error: 3 opening parentheses are never closed (the first \"(\" at position 8)");
  EXPECT_FALSE(MeasureSql("SELECT ((( 'closed' /* closed */").refused_by_tokenizer);
}

TEST(ParseGuard, IsLinearInTheText) {
  const auto started = std::chrono::steady_clock::now();
  EXPECT_EQ(MeasureSql("SELECT " + std::string(500000, '(')).unclosed, 500000u);
  EXPECT_EQ(MeasureSql("SELECT " + std::string(16 * 1024 * 1024, '(')).unclosed, 16u * 1024 * 1024);
  EXPECT_EQ(MeasureSql("SELECT " + std::string(16 * 1024 * 1024, '[')).nesting, 16u * 1024 * 1024);
  std::string names = "SELECT a";
  for (int i = 0; i < 4 * 1024 * 1024; i++) names += ".b";
  EXPECT_EQ(MeasureSql(names).name_parts, 4u * 1024 * 1024 + 1);
  EXPECT_LT(std::chrono::steady_clock::now() - started, std::chrono::duration<double>(10.0 * TimeoutScale()));
}

TEST(ParseGuard, BracketNestingTablesAndNames) {
  EXPECT_EQ(MeasureSql("SELECT 1").nesting, 0u);
  EXPECT_EQ(MeasureSql("SELECT (1), (2), [3], {a: 4}").nesting, 1u);
  EXPECT_EQ(MeasureSql("SELECT f(g([{a: (1)}]))").nesting, 5u);
  EXPECT_EQ(MeasureSql("SELECT ((1)); SELECT (((2)))").nesting, 3u);
  EXPECT_EQ(MeasureSql("SELECT '((((((' -- (((((").nesting, 0u);
  EXPECT_EQ(MeasureSql("SELECT * FROM (SELECT * FROM query('SELECT ((1))'))").nesting, 2u);
  // The tables one FROM clause names: joins and commas, at the clause's own level.
  EXPECT_EQ(MeasureSql("SELECT 1").tables, 0u);
  EXPECT_EQ(MeasureSql("SELECT a, b, c FROM t").tables, 1u);
  EXPECT_EQ(MeasureSql("SELECT a, b FROM t, u JOIN v ON t.i = v.i, w WHERE a IN (1, 2, 3) ORDER BY 1, 2, 3").tables, 4u);
  EXPECT_EQ(MeasureSql("SELECT f(a, b, c) FROM t JOIN u USING (a, b, c) JOIN v ON g(1, 2, 3) = 1").tables, 3u);
  EXPECT_EQ(MeasureSql("SELECT * FROM (SELECT 1 FROM a, b, c, d, e) s JOIN t ON true").tables, 5u);
  EXPECT_EQ(MeasureSql("SELECT 1 FROM a UNION ALL SELECT 2 FROM b, c UNION ALL SELECT 3, 4, 5, 6, 7").tables, 2u);
  EXPECT_EQ(MeasureSql("SELECT 1 IS DISTINCT FROM 2, 3, 4, 5, 6").tables, 0u);
  EXPECT_EQ(MeasureSql("SELECT extract(year FROM d), 1, 2, 3, 4, 5 FROM t").tables, 1u);
  EXPECT_EQ(MeasureSql("SELECT 'FROM a, b, c, d' -- FROM a, b, c, d").tables, 0u);
  EXPECT_EQ(MeasureSql("SELECT 1 FROM a, b; SELECT 2 FROM c, d, e; SELECT 3, 4, 5, 6").tables, 3u);
  // Dotted names: the most parts of one.
  EXPECT_EQ(MeasureSql("SELECT 1").name_parts, 1u);  // (a keyword is read as a one-part name)
  EXPECT_EQ(MeasureSql("SELECT a, b FROM t").name_parts, 1u);
  EXPECT_EQ(MeasureSql("SELECT c.s.t.col.f FROM c.s.t").name_parts, 5u);
  EXPECT_EQ(MeasureSql("SELECT \"a\".\"b\".c, 1.5, t.*").name_parts, 3u);
  EXPECT_EQ(MeasureSql("SELECT a.b /* c */ .c").name_parts, 3u);
  EXPECT_EQ(MeasureSql("SELECT 'a.b.c.d.e', x.upper().lower()").name_parts, 2u);
  EXPECT_EQ(MeasureSql("SELECT a.b.c; SELECT d.e").name_parts, 3u);
}

TEST(ParseGuard, TheErrorsThatRefuseAText) {
  EXPECT_EQ(Refusal("SELECT ((1))", 2), "");
  EXPECT_EQ(Refusal("SELECT ((1))", 0), "");
  EXPECT_EQ(Refusal("SELECT (((1)))", 2),
            "the statement's brackets nest 3 deep, and this server takes 2 (its max_sql_nesting option)");
  EXPECT_EQ(Refusal("SELECT 1 FROM a, b, c, d, e, f, g, h", 2), "");
  EXPECT_EQ(Refusal("SELECT 1 FROM a, b, c, d JOIN e ON true JOIN f ON true, g, h, i", 2),
            "a FROM clause of the statement names 9 tables, and this server takes 8 in one (four times its "
            "max_sql_nesting option)");
  EXPECT_EQ(Refusal("SELECT a.b.c", 3), "");
  EXPECT_EQ(Refusal("SELECT a.b.c.d", 3),
            "a name in the statement has 4 dotted parts, and this server takes 3 (its max_sql_nesting option)");
  EXPECT_EQ(Refusal("SELECT ((1", 1000),
            "syntax error: 2 opening parentheses are never closed (the first \"(\" at position 8)");
  EXPECT_EQ(Refusal("SELECT (1", 1000), "syntax error: 1 opening parenthesis is never closed (the first \"(\" at position 8)");
  // Parentheses never closed are refused whatever the limit, 0 included.
  EXPECT_EQ(Refusal("SELECT (1", 0), "syntax error: 1 opening parenthesis is never closed (the first \"(\" at position 8)");
}
