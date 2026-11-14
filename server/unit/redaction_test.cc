// Credential redaction in logged SQL and errors (LoggableSql / LoggableError): keywords
// hidden behind comments, string literals, dollar-quoting and the key=value / URL
// masking list, including idempotency. The end-to-end sinks are covered by
// server/tests/test_redaction.py.
#include <gtest/gtest.h>

#include <string>
#include <chrono>

#include "logging.hpp"
#include "redact.hpp"

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
  EXPECT_EQ(LoggableSql("SET hardcharger.query_tag = '{\"t\":1}'"), "SET hardcharger.query_tag = '{\"t\":1}'");
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

TEST(Redaction, SharedHelperMasksEveryConnectionStringForm) {
  // The store's errors, the init SQL's and the logs' masking all use RedactCredentials.
  EXPECT_EQ(hardcharger::RedactCredentials("postgresql://u:p@ss@h/db"), "postgresql://u:***@h/db");
  EXPECT_EQ(hardcharger::RedactCredentials("postgresql://u@h/db?sslpassword=k3y"), "postgresql://u@h/db?sslpassword=***");
  EXPECT_EQ(hardcharger::RedactCredentials("ATTACH 'dbname=d password=pw' AS x"), "ATTACH 'dbname=d password=***' AS x");
}

// ---- Credential values in ordinary statements ----------------------------------------
// A statement that is not credential DDL can still give a credential name a value: a
// named argument, an ATTACH option, a struct key. The value is masked on the statement's
// tokens, whatever joins the name and the value; the rest of the statement is kept.

namespace {

const std::string kSecret = "S3CR3T";  // every value below holds it

// The statement's log text has no trace of the value, says ***, and keeps `kept`.
void ExpectMasked(const std::string& sql, const std::string& kept = "") {
  const std::string logged = LoggableSql(sql);
  EXPECT_EQ(logged.find(kSecret), std::string::npos) << sql << "\n  logged: " << logged;
  EXPECT_NE(logged.find("***"), std::string::npos) << sql << "\n  logged: " << logged;
  EXPECT_FALSE(Redacted(logged)) << sql << "\n  logged: " << logged;
  if (!kept.empty()) EXPECT_NE(logged.find(kept), std::string::npos) << sql << "\n  logged: " << logged;
  EXPECT_EQ(LoggableSql(logged), logged) << "not idempotent: " << sql;
}

}  // namespace

TEST(Redaction, NamedArgumentValueIsMaskedWhateverJoinsIt) {
  EXPECT_EQ(LoggableSql("CALL serve('quack:h:1', token => 'S3CR3T')"), "CALL serve('quack:h:1', token => ***)");
  EXPECT_EQ(LoggableSql("CALL serve('quack:h:1', token := 'S3CR3T')"), "CALL serve('quack:h:1', token := ***)");
  EXPECT_EQ(LoggableSql("CALL serve('quack:h:1', token = 'S3CR3T')"), "CALL serve('quack:h:1', token = ***)");
  for (const char* sql : {"CALL serve('quack:h:1', TOKEN=>'S3CR3T')",
                          "CALL serve('quack:h:1', TOKEN:='S3CR3T')",
                          "CALL serve('quack:h:1', TOKEN='S3CR3T')",
                          "CALL serve('quack:h:1', token\n=>\n'S3CR3T')",
                          "CALL serve('quack:h:1', token\t=\t'S3CR3T')",
                          "CALL serve('quack:h:1', \"token\" => 'S3CR3T')",
                          "CALL serve('quack:h:1', \"Token\" = 'S3CR3T')",
                          "SELECT 1; CALL serve('quack:h:1', token => 'S3CR3T')"}) {
    ExpectMasked(sql, "serve('quack:h:1', ");
  }
}

TEST(Redaction, CommentsBetweenNameAndValueDoNotHideTheValue) {
  for (const char* sql : {"CALL serve('h', token /* c */ => 'S3CR3T')",
                          "CALL serve('h', token => /* c */ 'S3CR3T')",
                          "CALL serve('h', token /* c */ = 'S3CR3T')",
                          "CALL serve('h', token = /* c */ 'S3CR3T')",
                          "CALL serve('h', token =>\n-- c\n'S3CR3T')",
                          "CALL serve('h', token =\n-- c\n'S3CR3T')",
                          "CALL serve('h', token -- c\n => 'S3CR3T')",
                          "CALL serve('h', token /* => 'x' */ => 'S3CR3T')",
                          "CALL serve('h', token /* /* nested */ */ := 'S3CR3T')",
                          "CALL serve('h', -- c\r token => 'S3CR3T')",  // a line ends at a carriage return too
                          "CALL serve('h', token\xc2\xa0=>\xc2\xa0'S3CR3T')",  // no-break spaces
                          "CALL serve('h', `token` => 'S3CR3T')"}) {
    ExpectMasked(sql, "serve('h', ");
  }
  // A statement commented out is text all the same.
  for (const char* sql : {"SELECT 1 /* was: CALL serve('h', token => 'S3CR3T') */ AS kept",
                          "-- ATTACH 'postgresql://u:S3CR3T@h/db' AS d\nSELECT 1 AS kept",
                          "SELECT 1 AS kept -- password = 'S3CR3T'"}) {
    ExpectMasked(sql, "AS kept");
  }
}

TEST(Redaction, EveryKindOfLiteralAndExpressionValueIsMasked) {
  for (const char* value : {"$$S3CR3T$$", "$t$S3CR3T$t$", "E'S3CR3T'", "E'a\\'S3CR3T'", "e'S3CR3T\\\\'", "'a''S3CR3T'",
                            "'S3CR3T''b'", "'a' || 'S3CR3T'", "'S3CR3T' || 'b' || 'c'", "'a'\n'S3CR3T'",
                            "lower('S3CR3T') || 'b'", "('a' || 'S3CR3T')", "concat('a', 'S3CR3T')::VARCHAR",
                            "['S3CR3T', 'S3CR3T'][1]", "x'S3CR3T'", "VARCHAR 'S3CR3T'", "S3CR3T", "-S3CR3T",
                            "left('S3CR3T', 3)", "'S3CR3T' COLLATE nocase", "NOT 'S3CR3T'",
                            "CASE WHEN a AND b THEN 'S3CR3T' ELSE 'x' END", "(SELECT 'S3CR3T' FROM t WHERE a AND b)"}) {
    for (const char* op : {" => ", " := ", " = ", "=", " == ", " <> ", " != "}) {
      ExpectMasked(std::string("SELECT probe(1, token") + op + value + ", mode => 'fast') AS kept_alias", "kept_alias");
    }
  }
  // What follows the value stays: the next argument, the rest of a WHERE clause.
  EXPECT_EQ(LoggableSql("SELECT probe(token => 'a' || 'S3CR3T', mode => 'fast')"), "SELECT probe(token => ***, mode => 'fast')");
  EXPECT_EQ(LoggableSql("SELECT * FROM t WHERE token = 'S3CR3T' AND id = 7 ORDER BY id"),
            "SELECT * FROM t WHERE token = *** AND id = 7 ORDER BY id");
  EXPECT_EQ(LoggableSql("UPDATE users SET name = 'bob', password = 'S3CR3T' WHERE id = 7"),
            "UPDATE users SET name = 'bob', password = *** WHERE id = 7");
  EXPECT_EQ(LoggableSql("SELECT * FROM a JOIN b ON a.token = b.token JOIN c USING (id) WHERE a.pwd <> 'S3CR3T' OR a.id = 7"),
            "SELECT * FROM a JOIN b ON a.token = *** JOIN c USING (id) WHERE a.pwd <> *** OR a.id = 7");
}

TEST(Redaction, OptionListsStructKeysAndKeyFunctions) {
  EXPECT_EQ(LoggableSql("ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN 'S3CR3T')"), "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN ***)");
  for (const char* sql : {"attach 'quack:h:1' as q (type quack, token 'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN /* c */ 'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN $$S3CR3T$$)",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN E'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TOKEN 'S3CR3T', TYPE quack)",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, PASSWORD 'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, META_PASSWORD 'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (ENCRYPTION_KEY 'S3CR3T')",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN S3CR3T)",
                          "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN \"S3CR3T\", READ_ONLY)",
                          "SELECT 1;\nattach 'quack:h:1' (token S3CR3T)",
                          "SELECT 1; ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN 'S3CR3T')"}) {
    ExpectMasked(sql, "'quack:h:1'");
  }
  for (const char* sql : {"SELECT struct_pack(password := 'S3CR3T') AS kept", "SELECT {password: 'S3CR3T'} AS kept",
                          "SELECT {'password': 'S3CR3T'} AS kept", "SELECT {\"password\": 'S3CR3T'} AS kept",
                          "SELECT MAP {'api_key': 'S3CR3T'} AS kept", "SELECT {password:'S3CR3T', n: 1} AS kept",
                          "SET VARIABLE my_api_key = 'S3CR3T'; SELECT 1 AS kept",
                          "PRAGMA add_parquet_key('name', 'S3CR3T'); SELECT 1 AS kept",
                          "CALL add_parquet_key('name', 'S3CR3T'); SELECT 1 AS kept"}) {
    ExpectMasked(sql, "kept");
  }
  EXPECT_EQ(LoggableSql("PRAGMA add_parquet_key('k', 'S3CR3T')"), "PRAGMA add_parquet_key(***)");
}

TEST(Redaction, CredentialNamesAreRecognisedByTheirEnding) {
  for (const char* name : {"password", "passwd", "pwd", "passphrase", "secret", "client_secret", "token", "bearer_token",
                           "session_token", "s3_secret_access_key", "account_key", "accountkey", "api_key", "apikey",
                           "private_key", "encryption_key", "sig", "signature", "t.api_key", "API_KEY"}) {
    ExpectMasked(std::string("SELECT probe(") + name + " => 'S3CR3T') AS kept", "kept");
    ExpectMasked(std::string("SELECT probe(") + name + " = 'S3CR3T') AS kept", "kept");
  }
  // Names that are not credentials keep their values.
  for (const char* sql : {"SELECT probe(name => 'bob', mode := 'fast', id = 7, key_id => 'AKIDEXAMPLE')",
                          "SELECT * FROM t WHERE tokens = 'a' AND secret_id = 'b'",
                          "SELECT read_parquet('f', encryption_config => {footer_key: 'key_name'})"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(Redaction, PlaceholdersAndUsesWithoutAValueAreKept) {
  for (const char* sql : {"SELECT probe(1, token => ?)", "SELECT probe(1, token => $1)", "SELECT * FROM t WHERE token = $token",
                          "CREATE TABLE t (token VARCHAR, password VARCHAR NOT NULL)", "SELECT token, password FROM t ORDER BY token",
                          "SELECT token::VARCHAR, t.password || 'x' FROM t WHERE token IS NOT NULL",
                          "SELECT 'the token: ' || name, 'password' FROM t", "GRANT SELECT ON t TO secret",
                          "ATTACH 'x.db' AS token (READ_ONLY); CREATE TABLE token.t (password VARCHAR, id INT)"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(Redaction, ConnectionStringsAndUrlsInsideLiterals) {
  // A libpq value in quotes, inside a SQL literal whose quotes are doubled, dollar-quoted or escaped.
  EXPECT_EQ(LoggableSql("ATTACH 'dbname=x password=''S3CR3T S3CR3T'' host=h' AS d (TYPE postgres)"),
            "ATTACH 'dbname=x password=*** host=h' AS d (TYPE postgres)");
  for (const char* sql : {"ATTACH $$dbname=x password='S3CR3T S3CR3T' host=h$$ AS d (TYPE postgres)",
                          "ATTACH E'dbname=x password=\\'S3CR3T S3CR3T\\' host=h' AS d (TYPE postgres)",
                          "ATTACH 'dbname=x password=''S3CR3T\\'' S3CR3T'' host=h' AS d (TYPE postgres)"}) {
    ExpectMasked(sql, "AS d (TYPE postgres)");
  }
  for (const char* sql : {"SELECT * FROM read_csv('https://h/f.csv?api_key=S3CR3T')",
                          "SELECT * FROM read_csv('https://h/f.csv?v=1&key=S3CR3T')",
                          "SELECT * FROM read_csv('https://h/f.csv?X-Goog-Credential=c&X-Goog-Signature=S3CR3T')",
                          "SELECT * FROM \"https://u:S3CR3T@h/f.csv\"", "SELECT * FROM postgres_query('postgresql://u:S3CR3T@h/f.csv', 'SELECT 1')"}) {
    ExpectMasked(sql, "h/f.csv");
  }
}

TEST(Redaction, UnreadableStatementIsRedactedWhole) {
  // A string, quoted name or comment that never ends: nothing says where the value is.
  for (const char* sql : {"CALL serve('h', token => 'S3CR3T", "CALL serve('h', token = 'S3CR3T", "SELECT 'S3CR3T",
                          "SELECT 1 AS \"S3CR3T", "SELECT 1 /* S3CR3T", "SELECT 1 /* /* S3CR3T */", "SELECT $$S3CR3T",
                          "SELECT $a$S3CR3T$b$", "SELECT E'S3CR3T\\'", "ATTACH 'quack:h:1' AS q (TOKEN 'S3CR3T''",
                          "SELECT 1 AS `S3CR3T"}) {
    const std::string logged = LoggableSql(sql);
    EXPECT_TRUE(Redacted(logged)) << sql << "\n  logged: " << logged;
    EXPECT_EQ(logged.find(kSecret), std::string::npos) << sql;
    EXPECT_EQ(LoggableSql(logged), logged) << sql;
    const std::string error = LoggableError(sql, "Parser Error: unterminated quoted string at or near \"'S3CR3T\"");
    EXPECT_TRUE(Redacted(error)) << sql << "\n  logged: " << error;
  }
}

TEST(Redaction, EveryPrefixOfAStatementIsMaskedOrRedacted) {
  // A statement cut anywhere (a client's, or one a proxy truncated) shows no part of the value:
  // "S3" appears nowhere else in these statements.
  for (const std::string sql : {"CALL serve('h', token => E'S3CR3T\\'S3' || $q$S3CR3T$q$, mode => 'fast')",
                                "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN /* c */ 'S3CR3T''S3', MODE 'x')",
                                "SELECT {'password': 'S3CR3T', \"api_key\": $$S3CR3T$$} /* c */ FROM t WHERE token = 'S3CR3T' AND i = 1",
                                "ATTACH 'dbname=x password=''S3CR3T S3'' host=h' AS d (TYPE postgres, SECRET \"S3CR3T\")",
                                "PRAGMA add_parquet_key('k', 'S3CR3T'); SET VARIABLE pwd = -- c\n 'S3CR3T'"}) {
    for (std::size_t length = 0; length <= sql.size(); length++) {
      const std::string prefix = sql.substr(0, length);
      const std::string logged = LoggableSql(prefix);
      EXPECT_EQ(logged.find("S3"), std::string::npos) << prefix << "\n  logged: " << logged;
      EXPECT_EQ(LoggableSql(logged), logged) << prefix;
    }
  }
}

TEST(Redaction, ValuesAreMaskedBeforeTheTextIsShortened) {
  // The value begins before the 4000-byte cut and ends after it.
  std::string value;
  while (value.size() < 400) value += kSecret;
  const std::string sql = "SELECT '" + std::string(3800, 'x') + "' AS kept_alias, probe(1, token = '" + value + "') AS after_value";
  ExpectMasked(sql, "kept_alias, probe(1, token = ***) AS after_value");
  // Text past the cut is dropped at a token's edge, and the result reads the same again.
  const std::string longer = "SELECT '" + std::string(3900, 'x') + "' AS kept_alias, probe(1, token => 'S3CR3T'), '" +
                             std::string(500, 'y') + "'";
  const std::string logged = LoggableSql(longer);
  EXPECT_EQ(logged.find(kSecret), std::string::npos) << logged;
  EXPECT_TRUE(logged.ends_with("\xe2\x80\xa6")) << logged.substr(logged.size() - 40);
  EXPECT_NE(logged.find("kept_alias"), std::string::npos);
  EXPECT_LE(logged.size(), 4003u);
  EXPECT_EQ(LoggableSql(logged), logged);
}

TEST(Redaction, ErrorQuotingAMaskedValueIsMasked) {
  const std::string sql = "CALL serve('h', token => 'S3CR3T-1' 'S3CR3T-1b')";
  const std::string logged = LoggableError(sql, "Parser Error: syntax error at or near \"'S3CR3T-1b'\"\nLINE 1: " + sql);
  EXPECT_EQ(logged.find(kSecret), std::string::npos) << logged;
  EXPECT_EQ(logged, hardcharger::kCredentialError);
  const std::string converted = LoggableError("ATTACH 'x.db' AS d (ENCRYPTION_KEY 'S3CR3T')", "Invalid Input Error: bad key S3CR3T");
  EXPECT_EQ(converted, hardcharger::kCredentialError);
  // An error of a statement without credential values is kept.
  EXPECT_EQ(LoggableError("SELECT nofn(1)", "Catalog Error: Scalar Function with name nofn does not exist!"),
            "Catalog Error: Scalar Function with name nofn does not exist!");
}

TEST(Redaction, SharedHelperReadsEveryAssignmentForm) {
  // Text that is not a statement (an error that quotes one, a connection string).
  for (const char* text : {"near: serve(token => 'S3CR3T')", "near: serve(token := 'S3CR3T')", "near: serve(token = E'S3CR3T')",
                           "host=h password='S3CR3T \\' S3CR3T' port=1", "host=h password='S3CR3T", "https://h/f?api_key=S3CR3T",
                           "https://h/f?a=1&key=S3CR3T", "{'api_key': 'S3CR3T'}"}) {
    const std::string masked = hardcharger::RedactCredentials(text);
    EXPECT_EQ(masked.find(kSecret), std::string::npos) << text << " -> " << masked;
    EXPECT_EQ(hardcharger::RedactCredentials(masked), masked) << text;
  }
  EXPECT_EQ(hardcharger::RedactCredentials("serve(token => 'S3CR3T')"), "serve(token => ***)");
}

TEST(Redaction, AStatementWithACredentialValueIsACredentialStatement) {
  // What the init SQL's failure line goes by: such a statement's error loses DuckDB's excerpt.
  EXPECT_TRUE(hardcharger::IsCredentialStatement("ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN 'S3CR3T')"));
  EXPECT_TRUE(hardcharger::IsCredentialStatement("CALL serve('h', token => 'S3CR3T')"));
  EXPECT_TRUE(hardcharger::IsCredentialStatement("SELECT 'S3CR3T"));
  EXPECT_FALSE(hardcharger::IsCredentialStatement("ATTACH 'x.db' AS d"));
  EXPECT_FALSE(hardcharger::IsCredentialStatement("SELECT token FROM t WHERE id = 7"));
}

TEST(Redaction, ReviewCredentialClassificationIncludesEveryMaskedForm) {
  for (const std::string sql : std::vector<std::string>{"SELECT f(token => ?)", "SELECT f(token => $1)",
                                "SELECT f(token => bare_value)", "SELECT f(token => \"quoted_value\")",
                                "SELECT f(token => 'xy')", "SELECT f(token => upper('secret_value'))",
                                "SELECT 'postgresql://u:secret_value@h'", "SELECT 'password=secret_value'",
                                "SELECT 1 /* TOKEN 'secret_value' */",
                                "SELECT '" + std::string(5000, 'x') + "', f(token => ?)"}) {
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql)) << sql;
    EXPECT_EQ(LoggableError(sql, "arbitrarily transformed credential"),
              "[REDACTED: error of a credential statement]") << sql;
  }
}

TEST(Redaction, ReviewCommentLongNamesAndUnicodeSpaces) {
  for (const std::string sql : std::vector<std::string>{"SELECT 1 /* ATTACH 'a' (TOKEN 'S3CR3T') */ AS kept",
                                "SELECT 1 /* f(token => 'a' || 'S3CR3T') */ AS kept",
                                "SELECT struct_pack(\"" + std::string(100, 'x') + "token\" := 'S3CR3T') AS kept",
                                "SELECT struct_pack(\"token\"\xc2\xa0:= 'S3CR3T') AS kept"}) {
    ExpectMasked(sql, "kept");
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql));
  }
}

TEST(Redaction, ReviewMalformedAuthoritiesHaveBoundedCost) {
  std::string text;
  for (int i = 0; i < 10000; i++) text += "://u:x";
  const auto started = std::chrono::steady_clock::now();
  EXPECT_EQ(hardcharger::RedactCredentials(text), text);
  EXPECT_LT(std::chrono::steady_clock::now() - started, std::chrono::seconds(2));
  EXPECT_EQ(hardcharger::RedactCredentials("postgres://u:p@ss@h/x https://a:b@host/y"),
            "postgres://u:***@h/x https://a:***@host/y");
}

TEST(Redaction, JsonCredentialsInsideSqlLiterals) {
  for (const char* sql : {R"(SELECT '{"password": "S3CR3T"}' AS kept)",
                          R"(SELECT $j${"api_key": "S3CR3T"}$j$ AS kept)",
                          R"(SELECT error(json_extract_string('{"token":"S3CR3T"}', '$.token')) AS kept)"}) {
    ExpectMasked(sql, "kept");
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql));
    EXPECT_EQ(LoggableError(sql, "decoded value S3CR3T"), hardcharger::kCredentialError);
  }
  EXPECT_EQ(LoggableSql(R"(SELECT '{"ordinary": "value"}' AS kept)"),
            R"(SELECT '{"ordinary": "value"}' AS kept)");
}

TEST(Redaction, ConnectionPasswordsWithEscapedDelimiters) {
  for (const char* body : {R"(dbname=x password=prefix\ S3CR3T host=h)",
                           R"(dbname=x password=prefix\'S3CR3T host=h)",
                           R"(dbname=x password=prefix\"S3CR3T host=h)",
                           R"(dbname=x password=prefix\;S3CR3T host=h)",
                           R"(dbname=x password=prefix\)S3CR3T host=h)"}) {
    const std::string masked = hardcharger::RedactCredentials(body);
    EXPECT_EQ(masked, "dbname=x password=*** host=h");
    EXPECT_EQ(hardcharger::RedactCredentials(masked), masked);
    ExpectMasked("SELECT $q$" + std::string(body) + "$q$ AS kept", "kept");
  }
}

TEST(Redaction, QuotedAndQualifiedKeyFunctions) {
  for (const char* sql : {R"(PRAGMA "add_parquet_key"('k', 'S3CR3T'); SELECT 1 AS kept)",
                          R"(CALL "AdD_PaRqUeT_KeY"('k', 'S3CR3T'); SELECT 1 AS kept)",
                          R"(CALL main.add_parquet_key('k', 'S3CR3T'); SELECT 1 AS kept)",
                          R"(CALL "main"."add_parquet_key"('k', 'S3CR3T'); SELECT 1 AS kept)"}) {
    ExpectMasked(sql, "kept");
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql));
  }
}
