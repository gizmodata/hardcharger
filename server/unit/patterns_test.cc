// Wildcard patterns (catalog names, authorized e-mails): ports GizmoSQL 1.x's
// tests/integration/test_catalog_wildcards.cpp, every case, against MatchesPattern
// (1.x: MatchesCatalogPattern), plus the case-insensitive form used for e-mails.
#include <gtest/gtest.h>

#include <string>

#include "patterns.hpp"

using hardcharger::MatchesPattern;
using hardcharger::MatchesPatternIgnoringCase;

TEST(CatalogWildcards, ExactMatch) {
  EXPECT_TRUE(MatchesPattern("production", "production"));
  EXPECT_FALSE(MatchesPattern("production", "staging"));
  EXPECT_FALSE(MatchesPattern("prod", "production"));   // not a prefix match
  EXPECT_FALSE(MatchesPattern("production", "prod"));
}

TEST(CatalogWildcards, ExactMatchIsCaseSensitive) {
  EXPECT_TRUE(MatchesPattern("Prod", "Prod"));
  EXPECT_FALSE(MatchesPattern("prod", "Prod"));
  EXPECT_FALSE(MatchesPattern("PROD", "prod"));
}

TEST(CatalogWildcards, EmptyStrings) {
  EXPECT_TRUE(MatchesPattern("", ""));
  EXPECT_FALSE(MatchesPattern("", "a"));
  EXPECT_TRUE(MatchesPattern("*", ""));   // '*' matches empty
  EXPECT_FALSE(MatchesPattern("?", ""));  // '?' requires one char
}

TEST(CatalogWildcards, StarMatchesEverything) {
  EXPECT_TRUE(MatchesPattern("*", "anything"));
  EXPECT_TRUE(MatchesPattern("*", "a"));
  EXPECT_TRUE(MatchesPattern("*", ""));
  EXPECT_TRUE(MatchesPattern("*", "with spaces and . dots"));
}

TEST(CatalogWildcards, PrefixGlob) {
  EXPECT_TRUE(MatchesPattern("prod_*", "prod_sales"));
  EXPECT_TRUE(MatchesPattern("prod_*", "prod_finance_eu"));
  EXPECT_TRUE(MatchesPattern("prod_*", "prod_"));  // '*' can match empty
  EXPECT_FALSE(MatchesPattern("prod_*", "prod"));  // missing the underscore
  EXPECT_FALSE(MatchesPattern("prod_*", "staging_sales"));
}

TEST(CatalogWildcards, SuffixGlob) {
  EXPECT_TRUE(MatchesPattern("*_west", "sales_west"));
  EXPECT_TRUE(MatchesPattern("*_west", "_west"));
  EXPECT_FALSE(MatchesPattern("*_west", "west"));
  EXPECT_FALSE(MatchesPattern("*_west", "sales_west_2"));
}

TEST(CatalogWildcards, InfixGlob) {
  EXPECT_TRUE(MatchesPattern("data_*_2025", "data_sales_2025"));
  EXPECT_TRUE(MatchesPattern("data_*_2025", "data__2025"));   // middle '*' empty
  EXPECT_FALSE(MatchesPattern("data_*_2025", "data_2025"));   // needs both underscores
  EXPECT_FALSE(MatchesPattern("data_*_2025", "data_sales_2024"));
}

TEST(CatalogWildcards, MultipleStars) {
  EXPECT_TRUE(MatchesPattern("*sales*", "prod_sales_eu"));
  EXPECT_TRUE(MatchesPattern("*sales*", "sales"));
  EXPECT_FALSE(MatchesPattern("*sales*", "prod_finance"));
  // Adjacent and trailing stars collapse to a single match-anything.
  EXPECT_TRUE(MatchesPattern("a**b", "axyzb"));
  EXPECT_TRUE(MatchesPattern("prod_**", "prod_x"));
}

TEST(CatalogWildcards, QuestionMark) {
  EXPECT_TRUE(MatchesPattern("tenant_?", "tenant_a"));
  EXPECT_TRUE(MatchesPattern("tenant_?", "tenant_1"));
  EXPECT_FALSE(MatchesPattern("tenant_?", "tenant_"));    // needs exactly one
  EXPECT_FALSE(MatchesPattern("tenant_?", "tenant_ab"));  // exactly one, not two
  EXPECT_TRUE(MatchesPattern("db_??", "db_99"));
}

TEST(CatalogWildcards, MixedQuestionAndStar) {
  EXPECT_TRUE(MatchesPattern("t_?_*", "t_a_anything"));
  EXPECT_TRUE(MatchesPattern("t_?_*", "t_1_"));
  EXPECT_FALSE(MatchesPattern("t_?_*", "t__x"));  // '?' needs a char before second '_'
}

TEST(CatalogWildcards, BacktrackingRequired) {
  // Naive left-to-right matching without backtracking fails these.
  EXPECT_TRUE(MatchesPattern("*abc", "zzabcabc"));
  EXPECT_TRUE(MatchesPattern("*a*b", "xaxxb"));
  EXPECT_FALSE(MatchesPattern("*abc", "zzabcx"));
  EXPECT_TRUE(MatchesPattern("a*c*e", "abcde"));
  EXPECT_FALSE(MatchesPattern("a*c*e", "abcdx"));
}

TEST(CatalogWildcards, NonWildcardSpecialCharsAreLiteral) {
  // '.' and '-' are ordinary characters, matched literally.
  EXPECT_TRUE(MatchesPattern("my-bucket.v1", "my-bucket.v1"));
  EXPECT_FALSE(MatchesPattern("my-bucket.v1", "myXbucketXv1"));
  EXPECT_TRUE(MatchesPattern("bucket.*", "bucket.prod"));
  EXPECT_FALSE(MatchesPattern("bucket.*", "bucketXprod"));
}

TEST(CatalogWildcards, EmailMatchingIgnoresCase) {
  EXPECT_TRUE(MatchesPatternIgnoringCase("*@allowed.com", "Someone@ALLOWED.com"));
  EXPECT_FALSE(MatchesPatternIgnoringCase("*@allowed.com", "someone@allowed.com.evil"));
  EXPECT_TRUE(MatchesPatternIgnoringCase("*", "anyone@anywhere"));
}

TEST(CatalogWildcards, PathologicalPatternsAreLinear) {
  const std::string text(5000, 'a');
  EXPECT_FALSE(MatchesPattern("a*a*a*a*a*a*a*a*a*a*b", text));
}
