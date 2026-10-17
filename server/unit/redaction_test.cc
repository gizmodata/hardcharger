// Credential redaction in logged SQL and errors (LoggableSql / LoggableError): keywords
// hidden behind comments, string literals, dollar-quoting and the key=value / URL
// masking list, including idempotency. The end-to-end sinks are covered by
// server/tests/test_redaction.py.
#include <gtest/gtest.h>

#include <string>

#include "logging.hpp"

using hardcharger::LoggableError;
using hardcharger::LoggableSql;

namespace {

bool Redacted(const std::string& text) { return text.rfind("[REDACTED", 0) == 0; }

}  // namespace

TEST(Redaction, CredentialDdlWholeStatement) {
  EXPECT_TRUE(Redacted(LoggableSql("CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'pw')")));
  EXPECT_TRUE(Redacted(LoggableSql("CREATE OR REPLACE PERSISTENT SECRET s (TYPE s3)")));
  EXPECT_TRUE(Redacted(LoggableSql("CREATE USER bob WITH PASSWORD 'pw'")));
  EXPECT_TRUE(Redacted(LoggableSql("ALTER USER bob WITH PASSWORD 'pw'")));
}

TEST(Redaction, CommentsAndCaseDoNotHideTheKeyword) {
  for (const char* sql : {"CREATE /* x */ SECRET s (SECRET 'pw')",
                          "CREATE -- x\n SECRET s (SECRET 'pw')",
                          "CREATE OR/**/REPLACE SECRET s (SECRET 'pw')",
                          "CREATE /* /* nested */ */ SECRET s (SECRET 'pw')",
                          "create /**/ user bob password 'pw'",
                          "ALTER--x\nUSER bob PASSWORD 'pw'"}) {
    EXPECT_TRUE(Redacted(LoggableSql(sql))) << sql;
  }
}

TEST(Redaction, StringLiteralKeywordsAreOverRedactedByTheBackstop) {
  // The tokenizer ignores string bodies, but the raw-word backstop does not: a
  // statement that merely names these keywords in a literal is redacted rather than
  // risk letting a crafted one through. Over-redaction of a log line is safe.
  EXPECT_TRUE(Redacted(LoggableSql("SELECT 'CREATE SECRET s' AS note")));
}

TEST(Redaction, CredentialSettings) {
  EXPECT_TRUE(Redacted(LoggableSql("SET GLOBAL s3_secret_access_key = 'shh'")));
  EXPECT_TRUE(Redacted(LoggableSql("SET s3_secret_access_key='shh'")));
  EXPECT_TRUE(Redacted(LoggableSql("PRAGMA http_proxy_password = 'shh'")));
  // Ordinary settings are kept.
  EXPECT_EQ(LoggableSql("SET gizmosql.query_tag = '{\"t\":1}'"), "SET gizmosql.query_tag = '{\"t\":1}'");
  EXPECT_EQ(LoggableSql("SET threads = 4"), "SET threads = 4");
}

TEST(Redaction, MasksConnectionAndKeyValueCredentials) {
  EXPECT_EQ(LoggableSql("ATTACH 'postgres:dbname=x user=u password=M14x host=h' AS d (TYPE postgres)"),
            "ATTACH 'postgres:dbname=x user=u password=*** host=h' AS d (TYPE postgres)");
  const std::string masked = LoggableSql("ATTACH 'postgresql://u:topsecret@h:5432/db' AS d");
  EXPECT_EQ(masked.find("topsecret"), std::string::npos) << masked;
  EXPECT_NE(masked.find("://u:***@"), std::string::npos) << masked;
  const std::string amz = LoggableSql("SELECT * FROM read_csv('s3://b/k?x-amz-security-token=ABC123')");
  EXPECT_EQ(amz.find("ABC123"), std::string::npos) << amz;
}

TEST(Redaction, MaskingIsIdempotent) {
  const std::string once = LoggableSql("ATTACH 'postgres:password=M14x host=h' AS d (TYPE postgres)");
  EXPECT_EQ(LoggableSql(once), once);
}

TEST(Redaction, OrdinaryStatementKept) {
  EXPECT_EQ(LoggableSql("SELECT count(*) FROM orders WHERE id = 7"),
            "SELECT count(*) FROM orders WHERE id = 7");
}

TEST(Redaction, ErrorOfCredentialStatementDropped) {
  const std::string error =
      "Parser Error: syntax error at or near \"BOGUS\"\nLINE 1: CREATE SECRET s (SECRET 'M10x' BOGUS)\n  ^";
  const std::string logged = LoggableError("CREATE SECRET s (SECRET 'M10x' BOGUS)", error);
  EXPECT_TRUE(Redacted(logged)) << logged;
  EXPECT_EQ(logged.find("M10x"), std::string::npos);
}

TEST(Redaction, ErrorOfAttachMasksCredentialAndDropsLineExcerpt) {
  const std::string error =
      "IO Error: Failed to attach ... password=M14x host=h\nLINE 1: ATTACH 'postgres:password=M14x host=h'\n ^";
  const std::string logged = LoggableError("ATTACH 'postgres:password=M14x host=h' AS d (TYPE postgres)", error);
  EXPECT_EQ(logged.find("M14x"), std::string::npos) << logged;
  EXPECT_EQ(logged.find("LINE "), std::string::npos) << logged;
}
