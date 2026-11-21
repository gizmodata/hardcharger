// Credential masking in logged SQL and errors (LoggableSql / LoggableError, MaskSql /
// RedactError): one test per form README.md lists under "What the statement log masks",
// each with the neighbouring ordinary text that must stay as written. The end-to-end sinks
// are covered by server/tests/test_redaction.py and test_log_masking_forms.py.
#include <gtest/gtest.h>

#include <chrono>
#include <cstdlib>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "logging.hpp"
#include "redact.hpp"

using hardcharger::LoggableError;
using hardcharger::LoggableSql;
using hardcharger::MaskSql;
using hardcharger::RedactCredentials;
using hardcharger::RedactError;

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

TEST(Redaction, CredentialDdlWordsInACommentOrALiteralDoNotHideAStatement) {
  // What a statement is, is decided on its own first words as DuckDB tokenizes them. The
  // words in a comment or a string are text: the statement is logged as written.
  for (const char* sql : {"SELECT 'CREATE SECRET s' AS note",
                          "DELETE FROM victim WHERE i = 2 -- create secret",
                          "DELETE FROM victim WHERE i = 3 AND 'alter user' <> ''",
                          "DELETE FROM victim WHERE i = 4 AND no_such_column = 1 -- create user",
                          "/* CREATE SECRET */ SELECT 1", "SELECT 1 /* ALTER USER bob */",
                          "SELECT 'SET s3_secret_access_key' AS note", "SELECT create_secret FROM t",
                          "CREATE TABLE secret (id INT)", "CREATE USER MAPPING FOR x SERVER s"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
    EXPECT_EQ(LoggableError(sql, "Binder Error: no_such_column"), "Binder Error: no_such_column") << sql;
  }
  // In a request of several statements only the credential DDL is replaced.
  EXPECT_EQ(LoggableSql("SELECT 1; CREATE SECRET s (TYPE s3, SECRET 'pw'); SELECT 2"),
            "SELECT 1; [REDACTED: secret DDL]; SELECT 2");
  EXPECT_EQ(LoggableSql("SELECT 1 AS a; ALTER USER bob PASSWORD 'pw'"), "SELECT 1 AS a; [REDACTED: user DDL]");
  // The statement a function is given as text is one all the same: it is replaced inside the literal.
  EXPECT_EQ(LoggableSql("SELECT * FROM query('CREATE SECRET s (TYPE s3, SECRET ''pw'')')"),
            "SELECT * FROM query('[REDACTED: secret DDL]')");
  // CREATE / ALTER USER without a password carries nothing.
  EXPECT_EQ(LoggableSql("ALTER USER bob RENAME TO rob"), "ALTER USER bob RENAME TO rob");
}

TEST(Redaction, CredentialSettings) {
  EXPECT_TRUE(Redacted(LoggableSql("SET GLOBAL s3_secret_access_key = 'shh'")));
  EXPECT_TRUE(Redacted(LoggableSql("SET s3_secret_access_key='shh'")));
  EXPECT_TRUE(Redacted(LoggableSql("PRAGMA http_proxy_password = 'shh'")));
  // TO as well as =, a bare word as well as a literal, and every credential word.
  for (const char* sql : {"SET my_api_key TO 'shh'", "SET s3_secret_access_key TO 'shh'", "SET GLOBAL ext_private_key = 'shh'",
                          "SET SESSION x_encryption_key TO shh", "SET ext_passphrase = shh", "SET LOCAL azure_account_key='shh'",
                          "set /* c */ motherduck_token = 'shh'", "SET ext.signature_value TO 'shh'"}) {
    const std::string logged = LoggableSql(sql);
    EXPECT_EQ(logged, "[REDACTED: a credential setting]") << sql;
    EXPECT_EQ(LoggableError(sql, "Catalog Error: unrecognized configuration parameter \"x\", did you mean shh?"),
              "Catalog Error: unrecognized configuration parameter \"x\", did you mean ***?") << sql;
  }
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

TEST(Redaction, ErrorOfCredentialDdlKeepsItsReasonWithoutTheValues) {
  const std::string error =
      "Parser Error: syntax error at or near \"BOGUS\"\nLINE 1: CREATE SECRET s (SECRET 'M10x' BOGUS)\n  ^";
  EXPECT_EQ(LoggableError("CREATE SECRET s (SECRET 'M10x' BOGUS)", error), "Parser Error: syntax error at or near \"BOGUS\"");
  // The value itself, wherever the error puts it and in whatever case.
  EXPECT_EQ(LoggableError("CREATE SECRET s (TYPE s3, KEY_ID AKIDEXAMPLE1, SECRET 'M10x-Zq', REGIN 'eu')",
                          "Invalid Input Error: Unknown parameter 'regin' for secret type 's3' (key AKIDEXAMPLE1, secret m10X-zq, eu)"),
            "Invalid Input Error: Unknown parameter 'regin' for secret type 's3' (key ***, secret ***, ***)");
  EXPECT_EQ(LoggableError("CREATE USER dana PASSWORD M10xBare", "Parser Error: syntax error at or near \"M10xBare\""),
            "Parser Error: syntax error at or near \"***\"");
  EXPECT_EQ(LoggableError("CREATE USER app WITH PASSWORD 'pw-0123456789'",
                          "Hardcharger access control is not enabled on this server, so CREATE USER is not available"),
            "Hardcharger access control is not enabled on this server, so CREATE USER is not available");
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
  EXPECT_EQ(LoggableSql("SELECT * FROM t WHERE password = 'S3CR3T' AND id = 7 ORDER BY id"),
            "SELECT * FROM t WHERE password = *** AND id = 7 ORDER BY id");
  EXPECT_EQ(LoggableSql("UPDATE users SET name = 'bob', password = 'S3CR3T' WHERE id = 7"),
            "UPDATE users SET name = 'bob', password = *** WHERE id = 7");
  // A comparison with another column hides nothing; one with a literal does.
  EXPECT_EQ(LoggableSql("SELECT * FROM a JOIN b ON a.api_key = b.api_key JOIN c USING (id) WHERE a.passwd <> 'S3CR3T' OR a.id = 7"),
            "SELECT * FROM a JOIN b ON a.api_key = b.api_key JOIN c USING (id) WHERE a.passwd <> *** OR a.id = 7");
  // A column whose name ordinary data has too (pwd, auth, sig, ...) is compared and set
  // as written: only a credential setting, option, named argument or connection string masks it.
  for (const char* sql : {"SELECT * FROM t WHERE page_token = 'S3CR3T' AND id = 7 ORDER BY id",
                          "SELECT * FROM a JOIN b ON a.token = b.token WHERE a.pwd <> 'S3CR3T' OR a.id = 7",
                          "UPDATE t SET auth = 'S3CR3T', sig = 'S3CR3T' WHERE credential = 'S3CR3T' OR bearer = 'S3CR3T'",
                          "SELECT * FROM t WHERE first_pass IN (1, 2) AND page_token LIKE 'S3CR3T%' AND signature BETWEEN 'a' AND 'S3CR3T'",
                          "INSERT INTO tokens (doc, page_token, cookie) VALUES (1, 'S3CR3T', 'S3CR3T')",
                          "SELECT {'token': 'S3CR3T'}, MAP {'jwt': 'S3CR3T'}, json_object('auth', 'S3CR3T')",
                          "SET VARIABLE page_token = 'S3CR3T'"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(Redaction, TokenColumnMasksValues) {
  EXPECT_EQ(LoggableSql("INSERT INTO t (id, token) VALUES (1, 'example')"), "INSERT INTO t (id, token) VALUES (1, ***)");
  EXPECT_EQ(LoggableSql("UPDATE t SET token = 'example'"), "UPDATE t SET token = ***");
  EXPECT_EQ(LoggableSql("SELECT * FROM t WHERE length(token) > 2"), "SELECT * FROM t WHERE length(token) > ***");
  EXPECT_EQ(LoggableSql("SELECT id FROM t WHERE t.token = 'example'"), "SELECT id FROM t WHERE t.token = ***");
}

TEST(Redaction, OmitsErrorDetailsOverBudget) {
  const hardcharger::MaskedStatement ordinary;
  EXPECT_EQ(RedactError("ordinary diagnostic", ordinary), "ordinary diagnostic");
  EXPECT_EQ(RedactError(std::string(65537, 'a'), ordinary), "[REDACTED: error details omitted]");
  EXPECT_EQ(RedactError(std::string(4096, 'a'), ordinary, {std::string(2048, 'b')}), "[REDACTED: error details omitted]");
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
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
  // The placeholder says why, in the statement's place and in its error's.
  EXPECT_EQ(LoggableSql("SELECT 'S3CR3T"), "[REDACTED: a statement that could not be read: an unterminated string literal]");
  EXPECT_EQ(LoggableSql("SELECT 1 AS \"S3CR3T"),
            "[REDACTED: a statement that could not be read: an unterminated quoted identifier]");
  EXPECT_EQ(LoggableSql("SELECT 1 /* S3CR3T"), "[REDACTED: a statement that could not be read: an unterminated /* comment]");
  EXPECT_EQ(LoggableSql("SELECT $q$S3CR3T"),
            "[REDACTED: a statement that could not be read: an unterminated dollar-quoted string]");
  EXPECT_EQ(LoggableSql("SELECT 'S3CR3T\xff'"), "[REDACTED: a statement that could not be read: invalid UTF-8]");
  EXPECT_EQ(LoggableError("SELECT 'S3CR3T", "Parser Error: unterminated string literal"),
            "[REDACTED: error of a statement that could not be read: an unterminated string literal]");
}

TEST(Redaction, EveryPrefixOfAStatementIsMaskedOrRedacted) {
  // A statement cut anywhere (a client's, or one a proxy truncated) shows no part of the value:
  // "S3" appears nowhere else in these statements.
  for (const std::string sql : {"CALL serve('h', token => E'S3CR3T\\'S3' || $q$S3CR3T$q$, mode => 'fast')",
                                "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN /* c */ 'S3CR3T''S3', MODE 'x')",
                                "SELECT {'password': 'S3CR3T', \"api_key\": $$S3CR3T$$} /* c */ FROM t WHERE password = 'S3CR3T' AND i = 1",
                                "ATTACH 'dbname=x password=''S3CR3T S3'' host=h' AS d (TYPE postgres, SECRET \"S3CR3T\")",
                                "PRAGMA add_parquet_key('k', 'S3CR3T'); SET VARIABLE api_key = -- c\n 'S3CR3T'"}) {
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

TEST(Redaction, ErrorQuotingAMaskedValueIsMaskedByValue) {
  const std::string sql = "CALL serve('h', token => 'S3CR3T-1' 'S3CR3T-1b')";
  EXPECT_EQ(LoggableError(sql, "Parser Error: syntax error at or near \"'S3CR3T-1b'\"\nLINE 1: " + sql),
            "Parser Error: syntax error at or near \"'***'\"");
  EXPECT_EQ(LoggableError("ATTACH 'x.db' AS d (ENCRYPTION_KEY 'S3CR3T')", "Invalid Input Error: bad key S3CR3T"),
            "Invalid Input Error: bad key ***");
  // An error of a statement without credential values is kept.
  EXPECT_EQ(LoggableError("SELECT nofn(1)", "Catalog Error: Scalar Function with name nofn does not exist!"),
            "Catalog Error: Scalar Function with name nofn does not exist!");
  // Whatever kind of value was masked, and however its case changed on the way to the error.
  for (const auto& [value, shown] : std::vector<std::pair<std::string, std::string>>{
           {"'S3CR3T'", "S3CR3T"}, {"'s3cr3t'", "S3CR3T"}, {"$$S3CR3T$$", "S3CR3T"}, {"E'S3\\x43R3T'", "S3CR3T"},
           {"E'S3CR\\063T'", "S3CR3T"}, {"'it''s S3CR3T'", "it's S3CR3T"}, {"S3CR3T", "S3CR3T"}, {"\"S3CR3T\"", "S3CR3T"},
           {"'S3' || 'CR3T'", "S3CR3T"}, {"'S3'\n'CR3T'", "S3CR3T"}, {"('S3CR3T')", "S3CR3T"}, {"-12345678", "12345678"},
           {"'S3CR3T'::VARCHAR", "S3CR3T"}}) {
    const std::string statement = "SELECT probe(token => " + value + ")";
    EXPECT_EQ(LoggableError(statement, "Invalid Input Error: could not use \"" + shown + "\" here"),
              "Invalid Input Error: could not use \"***\" here") << statement;
  }
  // A value the statement computes cannot be listed: its error is not shown, and says so.
  for (const char* value : {"upper('s3cr3t')", "left('S3CR3T', 3)", "(SELECT 'S3CR3T')", "CASE WHEN a THEN 'S3CR3T' END",
                            "['S3CR3T'][1]", "'S3CR3T' + 1", "concat('a', ?)"}) {
    const std::string statement = std::string("SELECT probe(token => ") + value + ")";
    EXPECT_EQ(LoggableError(statement, "Invalid Input Error: S3C"),
              "[REDACTED: error of a statement that computes a credential value]") << statement;
  }
  // A short value is replaced where it stands alone, not inside other words.
  EXPECT_EQ(LoggableError("SELECT probe(token => 'ab')", "Conversion Error: Could not convert string 'ab' to INT32 in table"),
            "Conversion Error: Could not convert string '***' to INT32 in table");
  // An empty value hides nothing and replaces nothing.
  EXPECT_EQ(LoggableError("SELECT probe(token => '')", "Conversion Error: Could not convert string '' to INT32"),
            "Conversion Error: Could not convert string '' to INT32");
}

TEST(Redaction, ErrorDoesNotQuoteWhatIsBoundToAMaskedPlaceholder) {
  const hardcharger::MaskedSql masked = MaskSql("SELECT probe(a => ?, token => ?, b => $3, password => $pw)");
  ASSERT_EQ(masked.parameters.size(), 2u);
  EXPECT_EQ(masked.parameters[0].ordinal, 2u);
  EXPECT_EQ(masked.parameters[1].name, "pw");
  EXPECT_TRUE(masked.credentials);
  EXPECT_EQ(masked.text, "SELECT probe(a => ?, token => ?, b => $3, password => $pw)");
  EXPECT_EQ(RedactError("Conversion Error: Could not convert string 'B0UND-value' to INT32", masked, {"B0UND-value"}),
            "Conversion Error: Could not convert string '***' to INT32");
  EXPECT_EQ(MaskSql("SELECT probe(token => $2, x => $1)").parameters[0].ordinal, 2u);
  // A placeholder that is not in a masked place is not a credential.
  EXPECT_TRUE(MaskSql("SELECT CAST(? AS INTEGER)").parameters.empty());
  EXPECT_FALSE(MaskSql("SELECT CAST(? AS INTEGER)").credentials);
  // A bound value that is a URL or a connection string has its own credentials found.
  EXPECT_EQ(hardcharger::CredentialsIn("https://h/f.csv?auth=B0UND-value&x=1"), std::vector<std::string>{"B0UND-value"});
  EXPECT_EQ(hardcharger::CredentialsIn("plain text"), std::vector<std::string>{});
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
  // What decides whether a query profile (which holds the statement's text and constants) is kept.
  for (const std::string sql : std::vector<std::string>{
           "ATTACH 'quack:h:1' AS q (TYPE quack, TOKEN 'S3CR3T')", "CALL serve('h', token => 'S3CR3T')", "SELECT 'S3CR3T",
           "SELECT f(token => ?)", "SELECT f(token => $1)", "SELECT f(token => bare_value)",
           "SELECT f(token => \"quoted_value\")", "SELECT f(token => 'xy')", "SELECT f(token => upper('secret_value'))",
           "SELECT 'postgresql://u:secret_value@h'", "SELECT 'password=secret_value'",
           "SELECT 1 /* TOKEN 'secret_value' */", "SELECT '" + std::string(5000, 'x') + "', f(token => ?)",
           "CREATE SECRET s (TYPE s3)", "SET s3_secret_access_key = 'x'"}) {
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
  for (const char* sql : {"ATTACH 'x.db' AS d", "SELECT token FROM t WHERE id = 7",
                          // finding 4: a comment, a join on a credential-named column, an escaped literal
                          "/* {\"app\": \"dbt\", \"node_id\": \"model.x.y\"} */ SELECT * FROM t",
                          "SELECT * FROM t -- Metabase:: userID: 5 queryType: native queryHash: 0a1b",
                          "SELECT count(*) FROM a JOIN b ON a.access_token = b.access_token WHERE a.id > 0",
                          "SELECT * FROM a, b WHERE a.api_key = b.\"api_key\" AND a.secret <> b.secret",
                          "SELECT E'tab\\there' AS s", "SELECT password IS NOT NULL FROM users",
                          "CREATE TABLE users (name VARCHAR, password VARCHAR NOT NULL)"}) {
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_EQ(LoggableError(sql, "Catalog Error: Table with name t does not exist!"),
              "Catalog Error: Table with name t does not exist!") << sql;
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
  // A comment's own text stays; only the credential in it goes.
  EXPECT_EQ(LoggableSql("SELECT 1 /* was: CALL serve('h', token => 'S3CR3T'), see ticket 42 */ AS kept"),
            "SELECT 1 /* was: CALL serve('h', token => ***), see ticket 42 */ AS kept");
  EXPECT_EQ(LoggableSql("SELECT 1 -- see ticket 42: password = 'S3CR3T'\nFROM t"),
            "SELECT 1 -- see ticket 42: password = ***\nFROM t");
  EXPECT_EQ(LoggableSql("SELECT 1 /* don't: token => 'S3CR3T' */ AS kept"), "SELECT 1 /* don't: token => *** */ AS kept");
  // A comment DuckDB's tokenizer cannot read as SQL, with a credential name before a quote: all of it.
  EXPECT_EQ(LoggableSql("SELECT 1 /* don't: ATTACH 'a' (TOKEN 'S3CR3T') */ AS kept"), "SELECT 1 /* *** */ AS kept");
  EXPECT_EQ(LoggableSql("SELECT 1 -- don't: ATTACH 'a' (TOKEN 'S3CR3T')\nFROM t"), "SELECT 1 -- *** \nFROM t");
  EXPECT_EQ(LoggableSql("SELECT 1 -- it's fine, no credential here\nFROM t"),
            "SELECT 1 -- it's fine, no credential here\nFROM t");
}

TEST(Redaction, ReviewMalformedAuthoritiesHaveBoundedCost) {
  std::string text;
  for (int i = 0; i < 10000; i++) text += "://u:x";
  const auto started = std::chrono::steady_clock::now();
  EXPECT_EQ(hardcharger::RedactCredentials(text), text);
  EXPECT_LT(std::chrono::steady_clock::now() - started, std::chrono::seconds(2));
  EXPECT_EQ(hardcharger::RedactCredentials("postgres://u:p@ss@h/x https://a:b@host/y"),
            "postgres://u:***@h/x https://***@host/y");
}

TEST(Redaction, JsonCredentialsInsideSqlLiterals) {
  for (const char* sql : {R"(SELECT '{"password": "S3CR3T"}' AS kept)",
                          R"(SELECT $j${"api_key": "S3CR3T"}$j$ AS kept)",
                          R"(SELECT error(json_extract_string('{"token":"S3CR3T"}', '$.token')) AS kept)"}) {
    ExpectMasked(sql, "kept");
    EXPECT_TRUE(hardcharger::IsCredentialStatement(sql));
    EXPECT_EQ(LoggableError(sql, "decoded value S3CR3T"), "decoded value ***");
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

// ---- Finding 1: a password with punctuation in a connection string or a URL ----------

namespace {

// A password with each printable ASCII character in it, the ones the review names, and one
// that is not ASCII. "Zk7" and "CANARY" appear nowhere else in the texts below.
std::vector<std::string> HardPasswords() {
  std::vector<std::string> passwords = {"Zk7;CANARY", "Zk7)CANARY", "Zk7\"CANARY", "Zk7'CANARY", "Zk7@CANARY", "Zk7:CANARY",
                                        "Zk7/CANARY", "Zk7%CANARY", "Zk7 CANARY", "Zk7\\CANARY", "Zk7\xc3\xa9" "CANARY",
                                        "Zk7;)\"'@:/% \\CANARY", ";Zk7CANARY;", "\"Zk7CANARY\"", "'Zk7CANARY'", ")Zk7CANARY(",
                                        "Zk7CANARY\\", "Zk7=CANARY=", "Zk7&CANARY", "Zk7#CANARY", "Zk7?CANARY", "Zk7{CANARY}"};
  for (char c = '!'; c <= '~'; c++) passwords.push_back(std::string("Zk7") + c + "CANARY" + c);
  return passwords;
}

// The password as libpq's keyword form writes it without quotes: a backslash before a
// space, a quote and a backslash.
std::string LibpqBare(const std::string& password) {
  std::string out;
  for (const char c : password) {
    if (c == ' ' || c == '\'' || c == '\\') out += '\\';
    out += c;
  }
  return out;
}
std::string LibpqQuoted(const std::string& password) {
  std::string out = "'";
  for (const char c : password) {
    if (c == '\'' || c == '\\') out += '\\';
    out += c;
  }
  return out + "'";
}
std::string SqlLiteral(const std::string& text) {
  std::string out = "'";
  for (const char c : text) {
    out += c;
    if (c == '\'') out += '\'';
  }
  return out + "'";
}

void ExpectNoPassword(const std::string& shown, const std::string& from) {
  EXPECT_EQ(shown.find("Zk7"), std::string::npos) << from << "\n  shown: " << shown;
  EXPECT_EQ(shown.find("CANARY"), std::string::npos) << from << "\n  shown: " << shown;
}

}  // namespace

TEST(RedactionPasswords, KeywordFormMasksTheWholePasswordWhateverIsInIt) {
  for (const std::string& password : HardPasswords()) {
    for (const std::string& value : {LibpqBare(password), LibpqQuoted(password)}) {
      // First, in the middle and last in the string: what follows it must stay.
      const std::string middle = "host=127.0.0.1 port=1 user=u password=" + value + " dbname=d";
      const std::string last = "host=127.0.0.1 port=1 dbname=d user=u password=" + value;
      const std::string first = "password=" + value + " host=127.0.0.1 port=1 dbname=d";
      EXPECT_EQ(RedactCredentials(middle), "host=127.0.0.1 port=1 user=u password=*** dbname=d") << middle;
      EXPECT_EQ(RedactCredentials(last), "host=127.0.0.1 port=1 dbname=d user=u password=***") << last;
      EXPECT_EQ(RedactCredentials(first), "password=*** host=127.0.0.1 port=1 dbname=d") << first;
      for (const std::string& connection : {middle, last, first}) {
        for (const std::string& prefix : {"", "postgres:", "ducklake:postgres:"}) {
          const std::string sql = "ATTACH " + SqlLiteral(prefix + connection) + " AS p (TYPE postgres)";
          const std::string logged = LoggableSql(sql);
          ExpectNoPassword(logged, sql);
          EXPECT_NE(logged.find("host=127.0.0.1"), std::string::npos) << sql << "\n  logged: " << logged;
          EXPECT_TRUE(logged.ends_with(" AS p (TYPE postgres)")) << sql << "\n  logged: " << logged;
          EXPECT_EQ(LoggableSql(logged), logged) << sql;
          // The error DuckDB's postgres scanner gives quotes the string: with the statement
          // (its value is known), and without it (ATTACH getvariable('dsn')).
          const std::string error = "IO Error: Unable to connect to Postgres at \"" + prefix + connection +
                                    "\": connection to server at \"127.0.0.1\", port 1 failed: Connection refused\n"
                                    "\tIs the server running on that host and accepting TCP/IP connections?";
          for (const std::string& stored : {LoggableError(sql, error), LoggableError("ATTACH getvariable('dsn') AS p", error),
                                            LoggableError("", error)}) {
            ExpectNoPassword(stored, error);
            EXPECT_NE(stored.find("Connection refused"), std::string::npos) << error << "\n  stored: " << stored;
            EXPECT_NE(stored.find("Is the server running"), std::string::npos) << stored;
          }
        }
      }
    }
  }
  // libpq's complaint about a string it could not parse quotes the piece it stopped at, with no key near it.
  EXPECT_EQ(RedactCredentials("Invalid Input Error: Failed to parse connection string: \"invalid percent-encoded token: \"Zk7%CANARY\"\n\""),
            "Invalid Input Error: Failed to parse connection string: \"invalid percent-encoded token: \"***\"\n\"");
  EXPECT_EQ(RedactCredentials("Invalid Input Error: Failed to parse connection string: \"unexpected spaces found in \"Zk7 \"CANARY\", "
                              "use percent-encoded spaces (%20) instead\n\""),
            "Invalid Input Error: Failed to parse connection string: \"unexpected spaces found in \"***\", "
            "use percent-encoded spaces (%20) instead\n\"");
  EXPECT_EQ(RedactCredentials("Invalid Input Error: Failed to parse connection string: \"missing \"=\" after \"Zk7CANARY\" in connection info string\n\""),
            "Invalid Input Error: Failed to parse connection string: \"missing \"***\" in connection info string\n\"");
  // An empty password: nothing to hide, and nothing else is taken for it.
  EXPECT_EQ(RedactCredentials("host=h password='' dbname=d"), "host=h password=*** dbname=d");
  EXPECT_EQ(LoggableSql("ATTACH 'host=h password='''' dbname=d' AS p (TYPE postgres)"),
            "ATTACH 'host=h password=*** dbname=d' AS p (TYPE postgres)");
  EXPECT_EQ(LoggableSql("ATTACH 'host=h dbname=d password=' AS p (TYPE postgres)"),
            "ATTACH 'host=h dbname=d password=***' AS p (TYPE postgres)");
}

TEST(RedactionPasswords, UrlFormMasksTheWholePasswordUpToTheLastAt) {
  for (const std::string& password : HardPasswords()) {
    const std::string url = "postgresql://u:" + password + "@127.0.0.1:1/db";
    EXPECT_EQ(RedactCredentials(url), "postgresql://u:***@127.0.0.1:1/db") << url;
    EXPECT_EQ(RedactCredentials("see " + url + " and mysql://root:" + password + "@db.example:3306/x?ssl=1 too"),
              "see postgresql://u:***@127.0.0.1:1/db and mysql://root:***@db.example:3306/x?ssl=1 too") << url;
    const std::string sql = "ATTACH " + SqlLiteral(url) + " AS p (TYPE postgres)";
    const std::string logged = LoggableSql(sql);
    EXPECT_EQ(logged, "ATTACH 'postgresql://u:***@127.0.0.1:1/db' AS p (TYPE postgres)") << sql;
    EXPECT_EQ(LoggableSql(logged), logged);
    const std::string error = "IO Error: Unable to connect to Postgres at \"" + url +
                              "\": connection to server at \"127.0.0.1\", port 1 failed: Connection refused";
    for (const std::string& stored : {LoggableError(sql, error), LoggableError("ATTACH getvariable('dsn') AS p", error)}) {
      ExpectNoPassword(stored, error);
      EXPECT_NE(stored.find("Connection refused"), std::string::npos) << error << "\n  stored: " << stored;
    }
  }
  // A password with a space and an '@' before it: whole wherever the URL's end is known (a
  // literal that is the URL, a message that quotes it).
  EXPECT_EQ(LoggableSql("ATTACH 'postgresql://u:Zk7@x CANARY@127.0.0.1:1/db' AS p (TYPE postgres)"),
            "ATTACH 'postgresql://u:***@127.0.0.1:1/db' AS p (TYPE postgres)");
  EXPECT_EQ(RedactCredentials("IO Error: invalid URI \"postgresql://u:Zk7@x CANARY@127.0.0.1:1/db\": unexpected spaces found"),
            "IO Error: invalid URI \"postgresql://u:***@127.0.0.1:1/db\": unexpected spaces found");
  // An empty password, an empty user, IPv6, a port that is no password.
  EXPECT_EQ(RedactCredentials("postgresql://u:@h/db"), "postgresql://u:***@h/db");
  EXPECT_EQ(RedactCredentials("redis://:Zk7CANARY@h:6379/0"), "redis://:***@h:6379/0");
  EXPECT_EQ(RedactCredentials("postgresql://u:Zk7CANARY@[::1]:5432/db"), "postgresql://u:***@[::1]:5432/db");
  for (const char* plain : {"postgresql://h:5432/db", "http://[::1]:8080/x", "https://example.com/a?b=c", "s3://bucket/key.parquet",
                            "abfss://container@account.dfs.core.windows.net/f.parquet", "postgresql://user@h/db",
                            "see https://example.com:8443/docs for more"}) {
    EXPECT_EQ(RedactCredentials(plain), plain);
    const std::string sql = std::string("SELECT * FROM read_parquet('") + plain + "')";
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
}

TEST(RedactionPasswords, TheOtherConnectionStringGrammars) {
  for (const std::string& password : HardPasswords()) {
    // MySQL (DuckDB's mysql extension): "..", a backslash before " and backslash.
    std::string quoted = "\"";
    for (const char c : password) {
      if (c == '"' || c == '\\') quoted += '\\';
      quoted += c;
    }
    quoted += '"';
    EXPECT_EQ(RedactCredentials("host=h user=u password=" + quoted + " database=d"), "host=h user=u password=*** database=d")
        << quoted;
    ExpectNoPassword(LoggableSql("ATTACH " + SqlLiteral("host=h user=u password=" + quoted + " database=d") + " AS m (TYPE mysql)"),
                     quoted);
    // ODBC: {..}, with }} for a }.
    std::string braced = "{";
    for (const char c : password) {
      braced += c;
      if (c == '}') braced += '}';
    }
    braced += '}';
    const std::string odbc = "Driver={ODBC Driver 18};Server=h;UID=u;PWD=" + braced + ";Encrypt=yes";
    ExpectNoPassword(RedactCredentials(odbc), odbc);
    EXPECT_TRUE(RedactCredentials(odbc).starts_with("Driver={ODBC Driver 18};Server=h;UID=u;PWD=***")) << RedactCredentials(odbc);
    // ADO and Azure: ;-separated, a value runs to the next ';' with its spaces (it has no ';').
    if (password.find(';') == std::string::npos) {
      EXPECT_EQ(RedactCredentials("Server=h;User Id=u;Password=" + password + ";Database=d"),
                "Server=h;User Id=u;Password=***;Database=d") << password;
      ExpectNoPassword(RedactCredentials("Password=" + password + ";Server=h;Database=d"), password);
      ExpectNoPassword(RedactCredentials("Server=h;Database=d;Password=" + password), password);
      ExpectNoPassword(RedactCredentials("failed to open \"Server=h;Database=d;Password=" + password + "\": refused"), password);
    }
  }
  EXPECT_EQ(RedactCredentials("DefaultEndpointsProtocol=https;AccountName=a;AccountKey=Zk7+CANARY/x==;EndpointSuffix=core.windows.net"),
            "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=***;EndpointSuffix=core.windows.net");
  EXPECT_EQ(RedactCredentials("BlobEndpoint=https://a.blob.core.windows.net;SharedAccessSignature=sv=2024&sig=Zk7CANARY"),
            "BlobEndpoint=https://a.blob.core.windows.net;SharedAccessSignature=***");
}

// ---- Finding 2: where DuckDB's tokens are ---------------------------------------------

TEST(RedactionTokens, ADollarInANameIsNoDollarQuote) {
  EXPECT_EQ(LoggableSql("ATTACH 'f.db' AS a$q$ (ENCRYPTION_KEY 'S3CR3T') -- b$q$"),
            "ATTACH 'f.db' AS a$q$ (ENCRYPTION_KEY ***) -- b$q$");
  EXPECT_EQ(LoggableSql("SELECT 1 AS a$q$; ATTACH 'f.db' AS d2 (ENCRYPTION_KEY 'S3CR3T'); SELECT 2 AS b$q$"),
            "SELECT 1 AS a$q$; ATTACH 'f.db' AS d2 (ENCRYPTION_KEY ***); SELECT 2 AS b$q$");
  EXPECT_EQ(LoggableSql("UPDATE acct SET bal$usd$ = 1, password = lower('S3CR3T') WHERE bal$usd$ = 0"),
            "UPDATE acct SET bal$usd$ = 1, password = *** WHERE bal$usd$ = 0");
  EXPECT_EQ(LoggableSql("SELECT * FROM t$x$ WHERE password = 'S3CR3T' AND c$x$ = 1"), "SELECT * FROM t$x$ WHERE password = *** AND c$x$ = 1");
  // A statement with such a name is logged as written (it was hidden whole as "unreadable").
  for (const char* sql : {"DELETE FROM victim WHERE i = 1 AND 0 = (SELECT 0 AS a$q$)", "SELECT 1 AS a$b$", "SELECT a$ FROM t$",
                          "SELECT $1, $name, a$1 FROM t"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
  // A real dollar-quoted literal is still a literal.
  ExpectMasked("SELECT probe(token => $q$S3CR3T$q$) AS kept", "kept");
  ExpectMasked("SELECT probe(token => $$S3CR3T$$) AS kept", "kept");
}

TEST(RedactionTokens, EverySpaceDuckDBReadsSeparatesANameFromItsValue) {
  // The code points DuckDB 2.0 takes for white space (Parser::StripUnicodeSpaces), U+2060 among them.
  for (const char* space : {" ", "\t", "\n", "\v", "\f", "\r", "\xc2\xa0", "\xe2\x80\x80", "\xe2\x80\x81", "\xe2\x80\x82",
                            "\xe2\x80\x83", "\xe2\x80\x84", "\xe2\x80\x85", "\xe2\x80\x86", "\xe2\x80\x87", "\xe2\x80\x88",
                            "\xe2\x80\x89", "\xe2\x80\x8a", "\xe2\x80\x8b", "\xe2\x80\xaf", "\xe2\x81\x9f", "\xe2\x81\xa0",
                            "\xe3\x80\x80", "\xef\xbb\xbf"}) {
    for (const std::string sql : {std::string("ATTACH 'f.db' AS wj (ENCRYPTION_KEY") + space + "'S3CR3T') -- kept",
                                  std::string("SELECT * FROM nofn(token") + space + "=> 'S3CR3T') -- kept",
                                  std::string("SELECT * FROM nofn(token =>") + space + "'S3CR3T') -- kept",
                                  std::string("CREATE") + space + "SECRET s (TYPE s3, SECRET 'S3CR3T'); SELECT 1 -- kept"}) {
      const std::string logged = LoggableSql(sql);
      EXPECT_EQ(logged.find(kSecret), std::string::npos) << sql << "\n  logged: " << logged;
      EXPECT_NE(logged.find("kept"), std::string::npos) << sql << "\n  logged: " << logged;
    }
  }
  // A character DuckDB reads as part of the word makes it another name, and DuckDB refuses
  // the statement ("Unrecognized option encryption_key<U+180E>"). The value typed after it
  // is the credential all the same: a name is matched without its invisible characters.
  for (const char* glued : {"\xc2\x85", "\xe1\xa0\x8e", "\xe2\x80\x8c", "\xe1\x9a\x80", "\x1f", "\xe2\x80\xa8"}) {
    for (const std::string sql : {std::string("ATTACH 'f.db' AS wj (ENCRYPTION_KEY") + glued + "'S3CR3T') -- kept",
                                  std::string("SELECT * FROM nofn(token") + glued + " => 'S3CR3T') -- kept",
                                  std::string("SELECT * FROM nofn(to") + glued + "ken => 'S3CR3T') -- kept"}) {
      const std::string logged = LoggableSql(sql);
      EXPECT_EQ(logged.find(kSecret), std::string::npos) << sql << "\n  logged: " << logged;
      EXPECT_NE(logged.find("kept"), std::string::npos) << sql << "\n  logged: " << logged;
    }
  }
  EXPECT_EQ(LoggableSql("SELECT 1 AS token\xe1\x9a\x80"), "SELECT 1 AS token\xe1\x9a\x80");
}

// ---- Finding 3: the forms the list names ------------------------------------------------

TEST(RedactionForms, HttpHeaderMaps) {
  for (const char* header : {"Authorization", "authorization", "AUTHORIZATION", "Proxy-Authorization", "proxy-authorization",
                             "X-API-Key", "x-api-key", "X-Api-Key", "Cookie", "cookie", "Set-Cookie", "X-Auth-Token"}) {
    for (const std::string sql : {
             std::string("SELECT * FROM read_json('https://h/x', headers => MAP {'") + header + "': 'Bearer S3CR3T'}) AS kept",
             std::string("SELECT * FROM read_json('https://h/x', headers => {'") + header + "': 'Bearer S3CR3T'}) AS kept",
             std::string("SELECT * FROM read_json('https://h/x', headers => MAP(['Accept', '") + header +
                 "'], ['text/csv', 'Bearer S3CR3T'])) AS kept",
             std::string("SELECT * FROM read_json('https://h/x', headers => map_from_entries([('Accept', 'text/csv'), ('") + header +
                 "', 'Bearer S3CR3T')])) AS kept",
             std::string("SELECT http_get('https://h/x', headers => MAP {'Accept': 'text/csv', '") + header +
                 "': 'sessionid=' || 'S3CR3T'}) AS kept"}) {
      ExpectMasked(sql, "kept");
      const std::string logged = LoggableSql(sql);
      EXPECT_NE(logged.find("https://h/x"), std::string::npos) << logged;
      EXPECT_EQ(LoggableError(sql, "HTTP Error: 401 for Bearer S3CR3T"), LoggableError(sql, "HTTP Error: 401 for Bearer S3CR3T"));
      EXPECT_EQ(LoggableError(sql, "HTTP Error: 401 for S3CR3T").find(kSecret), std::string::npos) << sql;
    }
    EXPECT_EQ(RedactCredentials(std::string("request failed\n") + header + ": Bearer S3CR3T\nAccept: text/csv"),
              std::string("request failed\n") + header + ": ***\nAccept: text/csv");
  }
  // The neighbouring ordinary headers keep their values.
  EXPECT_EQ(LoggableSql("SELECT * FROM read_json('https://h/x', headers => MAP {'Accept': 'text/csv', 'User-Agent': 'x/1'})"),
            "SELECT * FROM read_json('https://h/x', headers => MAP {'Accept': 'text/csv', 'User-Agent': 'x/1'})");
  EXPECT_EQ(LoggableSql("SELECT * FROM read_json('https://h/x', headers => MAP {'Accept': 'text/csv', 'Authorization': 'Bearer S3CR3T'})"),
            "SELECT * FROM read_json('https://h/x', headers => MAP {'Accept': 'text/csv', 'Authorization': ***})");
}

TEST(RedactionForms, ATokenAsTheUrlUser) {
  EXPECT_EQ(LoggableSql("SELECT * FROM read_csv('https://S3CR3T@h.example/f.csv')"), "SELECT * FROM read_csv('https://***@h.example/f.csv')");
  EXPECT_EQ(LoggableSql("SELECT * FROM read_csv('https://S3CR3T:x-oauth-basic@h.example/f.csv')"),
            "SELECT * FROM read_csv('https://***@h.example/f.csv')");
  EXPECT_EQ(LoggableSql("SELECT * FROM read_csv('http://oauth2:S3CR3T@h.example/f.csv')"), "SELECT * FROM read_csv('http://***@h.example/f.csv')");
  EXPECT_EQ(LoggableSql("INSTALL x FROM 'HTTPS://S3CR3T@h.example/repo'"), "INSTALL x FROM 'HTTPS://***@h.example/repo'");
  for (const char* error : {"HTTP Error: 401 (https://S3CR3T@h.example/f.csv)", "IO Error: could not resolve s3cr3t"}) {
    EXPECT_EQ(LoggableError("SELECT * FROM read_csv('https://S3CR3T:x-oauth-basic@h.example/f.csv')", error).find("3CR3"),
              std::string::npos) << error;
  }
  // An '@' that is in the path or the query is no userinfo.
  for (const char* sql : {"SELECT * FROM read_csv('https://h.example/f.csv?to=a@b.example')", "SELECT * FROM read_csv('https://h.example/@x/f.csv')"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(RedactionForms, UrlQueryParameters) {
  for (const char* name : {"token", "access_token", "id_token", "refresh_token", "sig", "signature", "key", "api_key", "apiKey",
                           "api-key", "apikey", "password", "passwd", "pwd", "secret", "client_secret", "auth", "jwt",
                           "credential", "subscription-key", "sessionid", "session_id", "X-Amz-Signature", "X-Amz-Credential",
                           "X-Amz-Security-Token", "X-Goog-Signature", "x-goog-credential", "AUTH", "Api-Key"}) {
    for (const std::string url : {std::string("https://h.example/f.csv?") + name + "=S3CR3T", std::string("https://h.example/f.csv?v=1&") + name + "=S3CR3T&z=9",
                                  std::string("s3://b/k.parquet?") + name + "=S3CR3T#frag"}) {
      const std::string sql = "SELECT * FROM read_csv('" + url + "') AS kept";
      ExpectMasked(sql, "kept");
      EXPECT_NE(LoggableSql(sql).find("?"), std::string::npos);
      EXPECT_EQ(RedactCredentials("HTTP Error: 403 for " + url).find(kSecret), std::string::npos) << url;
    }
  }
  EXPECT_EQ(RedactCredentials("https://h/f?v=1&auth=S3CR3T&z=9"), "https://h/f?v=1&auth=***&z=9");
  EXPECT_EQ(RedactCredentials("https://h/f?jwt=S3CR3T#frag"), "https://h/f?jwt=***#frag");
  // Parameters that are no credentials (`code` is a product's or a country's as often as
  // OAuth's), and `key` outside a query string, are kept.
  for (const char* plain : {"https://h/f.csv?version=3&format=csv&keyword=x&encode=1&code=US", "SELECT * FROM t WHERE code = 'US' AND key = 'k1'",
                            "SELECT sessionid, session_id FROM hardcharger_sessions() WHERE session_id = 'abc'"}) {
    EXPECT_EQ(LoggableSql(plain), plain);
    EXPECT_EQ(RedactCredentials(plain), plain);
  }
}

TEST(RedactionForms, SqlGivenToAFunctionAsText) {
  for (const char* function : {"postgres_execute", "postgres_query", "mysql_execute", "mysql_query", "sqlite_query", "query", "read_adbc",
                               "adbc_scan", "adbc_execute", "anything_else"}) {
    for (const char* inner : {"ALTER ROLE app PASSWORD 'S3CR3T'", "CREATE ROLE app LOGIN PASSWORD 'S3CR3T'", "ALTER USER app IDENTIFIED BY 'S3CR3T'",
                              "CREATE USER app IDENTIFIED WITH mysql_native_password BY 'S3CR3T'", "ALTER USER app WITH PASSWORD 'S3CR3T'",
                              "CREATE USER app IDENTIFIED BY S3CR3T", "SET PASSWORD = 'S3CR3T'", "UPDATE users SET password = 'S3CR3T' WHERE id = 1",
                              "SELECT * FROM dblink('host=h password=S3CR3T', 'SELECT 1')", "CALL login(token => 'S3CR3T')",
                              "CREATE SECRET s (TYPE s3, SECRET 'S3CR3T')", "SELECT * FROM query('ALTER ROLE app PASSWORD ''S3CR3T''')"}) {
      for (const std::string literal : {SqlLiteral(inner), "$$" + std::string(inner) + "$$", "$sql$" + std::string(inner) + "$sql$"}) {
        const std::string sql = std::string("CALL ") + function + "('pg', " + literal + ") /* kept */";
        const std::string logged = LoggableSql(sql);
        EXPECT_EQ(logged.find(kSecret), std::string::npos) << sql << "\n  logged: " << logged;
        // The value is ***, or the statement in the literal is one that is replaced whole.
        EXPECT_TRUE(logged.find("***") != std::string::npos || logged.find("'[REDACTED: ") != std::string::npos) << logged;
        EXPECT_TRUE(logged.starts_with(std::string("CALL ") + function + "('pg', '")) << logged;
        EXPECT_TRUE(logged.ends_with(") /* kept */")) << logged;
        EXPECT_EQ(LoggableSql(logged), logged) << sql;
        EXPECT_EQ(LoggableError(sql, "Invalid Error: role app: password S3CR3T too short"), "Invalid Error: role app: password *** too short")
            << sql;
      }
    }
  }
  EXPECT_EQ(LoggableSql("CALL postgres_execute('pg', 'ALTER ROLE app PASSWORD ''S3CR3T'' VALID UNTIL ''2027-01-01''')"),
            "CALL postgres_execute('pg', 'ALTER ROLE app PASSWORD ***')");
  EXPECT_EQ(LoggableSql("CALL mysql_execute('my', $$ALTER USER app IDENTIFIED BY 'S3CR3T' PASSWORD EXPIRE NEVER$$)"),
            "CALL mysql_execute('my', '[REDACTED: user DDL]')");
  EXPECT_EQ(LoggableSql("CALL mysql_execute('my', $$GRANT ALL ON *.* TO app IDENTIFIED BY 'S3CR3T' WITH GRANT OPTION$$)"),
            "CALL mysql_execute('my', 'GRANT ALL ON *.* TO app IDENTIFIED BY ***')");
  EXPECT_EQ(LoggableSql(R"(CALL mysql_execute('my', $$GRANT ALL ON *.* TO app IDENTIFIED BY 'S3\'CR3T\'x' WITH GRANT OPTION$$))").find("CR3T"),
            std::string::npos);
  // SQL another engine reads differently (MySQL's backslash before a quote) is not read at all: all of it goes.
  EXPECT_EQ(LoggableSql(R"(CALL mysql_execute('my', 'ALTER USER app IDENTIFIED BY ''S3\''CR3T''') AS kept)").find("CR3T"), std::string::npos);
  // The neighbour: ordinary SQL given as text is kept.
  for (const char* sql : {"CALL postgres_execute('pg', 'ALTER ROLE app VALID UNTIL ''2027-01-01''')",
                          "SELECT * FROM postgres_query('pg', 'SELECT name, password_changed_at FROM users WHERE id = 7')",
                          "SELECT * FROM query('SELECT token FROM t ORDER BY 1')"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
}

TEST(RedactionForms, ACredentialBuiltFromPieces) {
  EXPECT_EQ(LoggableSql("ATTACH ('host=127.0.0.1 port=1 password=' || 'S3CR3T') AS p4 (TYPE postgres)"),
            "ATTACH ('host=127.0.0.1 port=1 password=***' ***) AS p4 (TYPE postgres)");
  EXPECT_EQ(LoggableSql("SELECT * FROM read_csv(format('https://h/f?token={}', 'S3CR3T')) AS kept"),
            "SELECT * FROM read_csv(format('https://h/f?token=***', ***)) AS kept");
  for (const char* sql : {"ATTACH ('host=h password=' || 'S3CR3T' || ' dbname=d') AS kept (TYPE postgres)",
                          "ATTACH ('host=h password=S3' || 'CR3T') AS kept (TYPE postgres)",
                          "ATTACH ('postgresql://u:' || 'S3CR3T' || '@h/db') AS kept (TYPE postgres)",
                          "ATTACH concat('host=h password=', 'S3CR3T') AS kept (TYPE postgres)",
                          "SELECT * FROM read_csv('https://h/f?token=' || 'S3CR3T') AS kept",
                          "SELECT * FROM read_csv(printf('https://h/f?token=%s', 'S3CR3T')) AS kept",
                          "SELECT * FROM read_csv(format('https://{}@h/f', 'S3CR3T')) AS kept",
                          "SELECT * FROM read_csv(format('https://u:{}@h/f', 'S3CR3T')) AS kept",
                          "SELECT * FROM read_json('https://h/x', headers => MAP {'Authorization': 'Bearer ' || 'S3CR3T'}) AS kept",
                          "SELECT * FROM read_json('https://h/x', headers => MAP {'Authorization': format('Bearer {}', 'S3CR3T')}) AS kept",
                          "SELECT probe(token => 'S3' || 'CR3T') AS kept", "SELECT probe(token => format('{}', 'S3CR3T')) AS kept",
                          "SELECT probe(password := concat('S3', 'CR3T'), mode => 1) AS kept"}) {
    ExpectMasked(sql, "kept");
  }
  // The neighbours: joining text that holds no credential.
  for (const char* sql : {"SELECT * FROM read_csv('https://h:8443/dir/' || name || '.csv')", "SELECT format('https://h/f?version={}', 3)",
                          "ATTACH ('host=h dbname=' || 'd') AS p (TYPE postgres)"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(RedactionForms, EveryComparisonOfACredentialColumnAndInsertedValues) {
  for (const char* sql : {"SELECT * FROM users WHERE password IN ('S3CR3T', 'S3CR3T2') AND kept = 1",
                          "SELECT * FROM users WHERE password NOT IN ('S3CR3T') AND kept = 1",
                          "SELECT * FROM users WHERE password LIKE 'S3CR3T%' AND kept = 1",
                          "SELECT * FROM users WHERE password NOT LIKE 'S3CR3T%' ESCAPE '!' AND kept = 1",
                          "SELECT * FROM users WHERE password ILIKE 'S3CR3T%' AND kept = 1",
                          "SELECT * FROM users WHERE password SIMILAR TO 'S3CR3T.*' AND kept = 1",
                          "SELECT * FROM users WHERE password GLOB 'S3CR3T*' AND kept = 1",
                          "SELECT * FROM users WHERE password BETWEEN 'S3CR3T' AND 'S3CR3Tz' AND kept = 1",
                          "SELECT * FROM users WHERE password IS DISTINCT FROM 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE password IS NOT DISTINCT FROM 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE password ~ 'S3CR3T' AND kept = 1", "SELECT * FROM users WHERE password !~ 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE 'S3CR3T' = password AND kept = 1", "SELECT * FROM users WHERE 'S3CR3T' <> u.password AND kept = 1",
                          "SELECT * FROM users WHERE lower(password) = 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE password::VARCHAR = 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE CAST(password AS VARCHAR) = 'S3CR3T' AND kept = 1",
                          "SELECT * FROM users WHERE lower(u.\"password\")::VARCHAR(40) IN ('S3CR3T') AND kept = 1",
                          "INSERT INTO users (name, password) VALUES ('kept', 'S3CR3T')",
                          "INSERT INTO users (name, password, age) VALUES ('kept', 'S3CR3T', 3), ('eve', 'S3CR3T2', 4)",
                          "INSERT OR REPLACE INTO main.users (\"password\", name) VALUES ('S3CR3T', 'kept')",
                          "INSERT INTO users (name, api_key, note) VALUES ('kept', upper('S3CR3T'), 'x') RETURNING name",
                          "INSERT INTO cfg VALUES ('api_key', 'S3CR3T') /* kept */", "SELECT json_object('api_key', 'S3CR3T') AS kept",
                          "SELECT * FROM t WHERE (k, v) = ('password', 'S3CR3T') AND kept = 1"}) {
    ExpectMasked(sql, "kept");
  }
  EXPECT_EQ(LoggableSql("INSERT INTO users (name, password, age) VALUES ('eve', 'S3CR3T', 3), ('bob', 'S3CR3T2', 4)"),
            "INSERT INTO users (name, password, age) VALUES ('eve', ***, 3), ('bob', ***, 4)");
  EXPECT_EQ(LoggableSql("SELECT * FROM users WHERE password IN ('S3CR3T', 'S3CR3T2') AND id = 7"),
            "SELECT * FROM users WHERE password IN (***) AND id = 7");
  EXPECT_EQ(LoggableError("INSERT INTO users (name, password) VALUES ('eve', 'S3CR3T')", "Constraint Error: (eve, S3CR3T) violates CHECK"),
            "Constraint Error: (eve, ***) violates CHECK");
  // The neighbours: the same statements on columns that are no credentials, and comparisons that give no value.
  for (const char* sql : {"SELECT * FROM users WHERE name IN ('eve', 'bob') AND status LIKE 'a%'", "INSERT INTO users (name, age) VALUES ('eve', 3)",
                          "INSERT INTO users VALUES ('eve', 'x')", "SELECT * FROM users WHERE password IS NULL OR password IS NOT NULL",
                          "SELECT * FROM users u JOIN logins l ON u.password IS NOT DISTINCT FROM l.password",
                          "SELECT * FROM a WHERE a.token IN (SELECT token FROM b)",
                          // A subquery or a function of columns holds no value either (review of 2.14.0, finding 50).
                          "SELECT * FROM a WHERE a.api_key IN (SELECT api_key FROM b)",
                          "SELECT * FROM a WHERE a.api_key NOT IN (SELECT api_key FROM revoked WHERE user_id = 3) AND a.id = 7",
                          "SELECT * FROM a WHERE a.password = (SELECT max(password) FROM s WHERE id = 3)",
                          "SELECT * FROM a WHERE password = upper(other_col)",
                          "SELECT * FROM a JOIN b ON lower(a.password) = lower(b.password)"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
}

// ---- Finding 6: cost ----------------------------------------------------------------------

// ---- The review of 2.14.0's masking (hc-v214-notes/review-masking-fix), findings 01-06 ----
//
// Each test is the finding's own repro. The passwords carry the punctuation a password has:
// ; ) " ' @ : / %.

namespace {

const std::string kHard = "Zk7;)\"'@:/%CANARY";  // what a password may hold

// `value` written as a SQL string literal.
std::string Lit(const std::string& value) {
  std::string out = "'";
  for (const char c : value) {
    out += c;
    if (c == '\'') out += '\'';
  }
  return out + "'";
}

}  // namespace

TEST(RedactionReview, F01AStatementKeepsTheValuesItHidWhenTheOneBeforeHidTheSame) {
  // In a request of several statements, each statement's error is redacted with its own list of
  // hidden values; the request's list keeps a value once when two statements in a row hid it.
  for (const std::string value : {std::string("CANARYlive1"), kHard}) {
    for (const std::string sql : {"INSERT INTO s (id, password) VALUES (7, " + Lit(value) + "); UPDATE u SET password = " + Lit(value) + " WHERE id = 1",
                                  "SELECT * FROM s WHERE password = " + Lit(value) + "; SELECT * FROM u WHERE password = " + Lit(value),
                                  "SET s3_secret_access_key = " + Lit(value) + "; SELECT * FROM u WHERE password = " + Lit(value),
                                  "SELECT f(token => 'A', password => " + Lit(value) + "); SELECT g(password => " + Lit(value) + ", token => 'B')",
                                  "SELECT * FROM s WHERE password = " + Lit(value) + "; SELECT 1; SELECT * FROM u WHERE password = " + Lit(value)}) {
      const hardcharger::MaskedSql masked = MaskSql(sql);
      ASSERT_GE(masked.statements.size(), 2u) << sql;
      for (const auto& statement : masked.statements) {
        if (!statement.credentials) continue;
        EXPECT_NE(std::find(statement.secrets.begin(), statement.secrets.end(), value), statement.secrets.end()) << sql;
        const std::string error = "Conversion Error: Could not convert string '" + value + "' to INT32";
        const std::string stored = RedactError(error, statement);
        EXPECT_EQ(stored, "Conversion Error: Could not convert string '***' to INT32") << sql;
      }
    }
  }
  // Two ATTACHes with one password: the second's error is redacted, each piece of it too.
  const hardcharger::MaskedSql attaches = MaskSql(
      "ATTACH 'postgresql://u:CANARY@same2@h1/db' AS a (TYPE postgres); ATTACH 'postgresql://u:CANARY@same2@h2/db' AS b (TYPE postgres)");
  ASSERT_EQ(attaches.statements.size(), 2u);
  EXPECT_EQ(RedactError("IO Error: could not translate host name \"same2@h2\" to address", attaches.statements[1]),
            "IO Error: could not translate host name \"***@h2\" to address");
}

TEST(RedactionReview, F02SqlGivenAsTextTwoOrMoreLevelsDownHasItsValueFound) {
  auto as_text = [](std::string sql, int depth) {
    for (int level = 0; level < depth; level++) sql = "SELECT * FROM query(" + Lit(sql) + ")";
    return sql;
  };
  for (const std::string value : {std::string("CANARYnest"), std::string("CANARY;)@:/%nest")}) {
    for (const std::string inner : {"SELECT * FROM u WHERE password = " + Lit(value), "SELECT fm(1, token => " + Lit(value) + ")",
                                    "SELECT fm(1, token := " + Lit(value) + ")", "UPDATE u SET password = " + Lit(value),
                                    "INSERT INTO u (id, password) VALUES (1, " + Lit(value) + ")", "ALTER ROLE app PASSWORD " + Lit(value),
                                    "SELECT {'password': " + Lit(value) + "}", "SELECT " + Lit("host=h password=" + value + " dbname=d")}) {
      for (int depth = 0; depth <= 4; depth++) {
        const std::string sql = as_text(inner, depth);
        const hardcharger::MaskedSql masked = MaskSql(sql);
        EXPECT_EQ(masked.text.find("CANARY"), std::string::npos) << sql << "\n  logged: " << masked.text;
        const std::string stored = RedactError("Conversion Error: Could not convert string '" + value + "' to INT32", masked);
        EXPECT_EQ(stored.find("CANARY"), std::string::npos) << sql << "\n  stored: " << stored;
        EXPECT_TRUE(stored.starts_with("Conversion Error: Could not convert string '")) << stored;
      }
    }
  }
  // No string of quotes is taken for a hidden value (it garbled the error's punctuation).
  const hardcharger::MaskedSql nested = MaskSql(as_text("SELECT * FROM u WHERE password = 'CANARYnest'", 3));
  for (const auto& secret : nested.secrets) EXPECT_NE(secret.find_first_not_of("'\" "), std::string::npos) << secret;
  EXPECT_EQ(RedactError("Binder Error: 'x' and \"y\": ''", nested), "Binder Error: 'x' and \"y\": ''");
  // The twin: SQL given as text with nothing in it is kept as written, three levels down.
  const std::string plain = as_text("SELECT * FROM u WHERE name = 'kept'", 3);
  EXPECT_EQ(LoggableSql(plain), plain);
}

TEST(RedactionReview, F03EveryFormTheReadmeListsIsMasked) {
  for (const std::string sql : {
           // (a) an INSERT behind WITH, EXPLAIN or PREPARE, and MERGE's INSERT
           "WITH x AS (SELECT 1) INSERT INTO s (id, name, password) VALUES (3, 'kept', " + Lit(kHard) + ")",
           "EXPLAIN INSERT INTO s (id, name, password) VALUES (4, 'kept', " + Lit(kHard) + ")",
           "PREPARE gp AS INSERT INTO s (id, name, password) VALUES (5, 'kept', " + Lit(kHard) + ")",
           "MERGE INTO s USING t ON s.id = t.id WHEN NOT MATCHED THEN INSERT (id, password) VALUES (t.id, " + Lit(kHard) + ") -- kept",
           // (b) an expression on the left of `= password`
           "SELECT * FROM s WHERE upper(" + Lit(kHard) + ") = password AND kept = 1",
           "SELECT * FROM s WHERE (SELECT " + Lit(kHard) + ") = password AND kept = 1",
           // (c) a function of a function of the column
           "SELECT * FROM s WHERE trim(lower(password)) = " + Lit(kHard) + " AND kept = 1",
           "SELECT * FROM s WHERE ((password)) = " + Lit(kHard) + " AND kept = 1",
           // (d) an option whose value is computed
           "COPY t TO 'f.parquet' (FORMAT parquet, PASSWORD upper(" + Lit(kHard) + ")) -- kept",
           "ATTACH 'x.db' AS e (ENCRYPTION_KEY (SELECT " + Lit(kHard) + ")) -- kept"}) {
    const std::string logged = LoggableSql(sql);
    EXPECT_EQ(logged.find("CANARY"), std::string::npos) << sql << "\n  logged: " << logged;
    EXPECT_EQ(logged.find("Zk7"), std::string::npos) << sql << "\n  logged: " << logged;
    EXPECT_NE(logged.find("kept"), std::string::npos) << sql << "\n  logged: " << logged;
    EXPECT_EQ(LoggableSql(logged), logged) << sql;
  }
  EXPECT_EQ(LoggableSql("EXPLAIN INSERT INTO s (id, password) VALUES (4, 'x')"), "EXPLAIN INSERT INTO s (id, password) VALUES (4, ***)");
  EXPECT_EQ(LoggableSql("SELECT * FROM s WHERE upper('x') = password"), "SELECT * FROM s WHERE *** = password");
  EXPECT_EQ(LoggableSql("SELECT * FROM s WHERE trim(lower(password)) = 'x'"), "SELECT * FROM s WHERE trim(lower(password)) = ***");
  // The twins, kept as written.
  for (const char* sql : {"WITH x AS (SELECT 1) INSERT INTO s (id, name) VALUES (3, 'w')", "EXPLAIN INSERT INTO s (id, name) VALUES (4, 'e')",
                          "PREPARE gp AS INSERT INTO s (id, name) VALUES (5, 'p')", "SELECT * FROM s WHERE upper('w') = name",
                          "SELECT * FROM s WHERE (SELECT 'w') = name", "SELECT * FROM s WHERE trim(lower(name)) = 'w'",
                          "COPY t TO 'f.parquet' (FORMAT parquet, COMPRESSION upper('zstd'))", "SELECT insert, values FROM t",
                          "COPY t TO 'f.csv' (FORMAT csv, HEADER true)"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
  }
}

TEST(RedactionReview, F04AUrlWithAPortHasNoUserinfo) {
  // A credential with an '@' in it, in a query parameter or a later field, after a URL whose
  // authority is host:port: the whole credential is masked, and nothing else.
  for (const std::string value : {std::string("P@ssCANARY"), std::string("ab@CANARY"), std::string("Zk7;)@:/%CANARY")}) {
    std::string encoded;  // (in a query string, a ';' or '%' would end or encode the value: percent-encode them)
    for (const char c : value) encoded += c == ';' ? std::string("%3B") : c == '%' ? std::string("%25") : std::string(1, c);
    for (const std::string text :
         {"postgresql://127.0.0.1:1/app?user=svc&password=" + encoded + "&connect_timeout=2",
          "jdbc:postgresql://db.example.com:5432/app?user=svc&password=" + encoded,
          "jdbc:mysql://db.example.com:3306/app?user=svc&password=" + encoded + "&useSSL=true",
          "http://127.0.0.1:1/data.csv?access_token=" + encoded,
          "http://minio.local:9000/b/f.parquet?X-Amz-Signature=" + encoded,
          "{\"url\": \"http://db.example.com:8080/api\", \"user\": \"svc\", \"password\": \"" + encoded + "\"}",
          "host=h sslrootcert=https://ca.example.com:443/root.pem password=" + encoded + " dbname=d",
          "{'endpoint': 'http://minio.local:9000', 'secret': '" + encoded + "'}",
          "POST http://api.example.com:8080/v1\nAuthorization: Bearer " + encoded}) {
      for (const std::string& shown : {RedactCredentials("IO Error: cannot open \"" + text + "\""),
                                       LoggableSql("SELECT * FROM read_text(" + Lit(text) + ")"),
                                       LoggableError("SELECT 1", "IO Error: cannot open \"" + text + "\"")}) {
        EXPECT_EQ(shown.find("CANARY"), std::string::npos) << text << "\n  shown: " << shown;
        EXPECT_EQ(shown.find("ss@"), std::string::npos) << text << "\n  shown: " << shown;
      }
    }
  }
  EXPECT_EQ(LoggableSql("ATTACH 'postgresql://127.0.0.1:1/app?user=svc&password=P@ssw0rd&connect_timeout=2' AS pg1 (TYPE postgres)"),
            "ATTACH 'postgresql://127.0.0.1:1/app?user=svc&password=***&connect_timeout=2' AS pg1 (TYPE postgres)");
  EXPECT_EQ(LoggableSql("SELECT * FROM read_csv('http://127.0.0.1:1/data.csv?access_token=abc@CANARY')"),
            "SELECT * FROM read_csv('http://127.0.0.1:1/data.csv?access_token=***')");
  // A userinfo is still one, with a port after its host, and with an '@' or a ':' in its password.
  EXPECT_EQ(RedactCredentials("postgresql://u:12345@h:5432/db"), "postgresql://u:***@h:5432/db");
  EXPECT_EQ(RedactCredentials("postgresql://u:Zk7:9@x@h:5432/db"), "postgresql://u:***@h:5432/db");
  EXPECT_EQ(RedactCredentials("at \"postgresql://u:Zk7\"x@h:1/db\": refused"), "at \"postgresql://u:***@h:1/db\": refused");
}

TEST(RedactionReview, F05OrdinaryStatementsAreKeptAsWritten) {
  // The review's false-positive list: none of these holds a credential.
  for (const char* sql : {"SELECT * FROM customers WHERE first_pass = 3", "SELECT * FROM customers WHERE first_pass IN (1, 2, 3)",
                          "UPDATE customers SET first_pass = first_pass + 1 WHERE id = 3", "SELECT * FROM s WHERE auth = 'basic'",
                          "SELECT CASE WHEN auth = 'basic' THEN 1 END FROM s", "SELECT * FROM s WHERE auth IS DISTINCT FROM 'none'",
                          "SELECT * FROM s WHERE sig = 'x' AND bearer = 'b' AND credential = 'cert'",
                          "SELECT * FROM tokens WHERE page_token IN ('cat', 'mat')", "SELECT * FROM tokens WHERE page_token LIKE 'c%'",
                          "SELECT * FROM tokens WHERE page_token BETWEEN 'a' AND 'm'", "SELECT * FROM tokens WHERE token NOT IN (SELECT token FROM stopwords)",
                          "SELECT count(*) FROM sessions WHERE session_token IN (SELECT session_token FROM sessions WHERE user_id = 3)",
                          "SELECT * FROM t JOIN s ON lower(t.token) = lower(s.token)", "SELECT * FROM tokens WHERE length(page_token) > 2",
                          "INSERT INTO tokens (doc_id, pos, page_token, tag) VALUES (99, 1, 'dog', 'NOUN'), (99, 2, 'ran', 'VERB')",
                          "SELECT json_object('token', 'cat', 'pos', 3)", "SELECT * FROM notes WHERE note IN ('password', 'token', 'api_key')",
                          "SELECT * FROM s WHERE auth = 'password'", "SELECT * FROM events WHERE url LIKE 'https://example.com/p/%?code=%'",
                          "SELECT * FROM events WHERE url = 'https://example.com/cb?state=1&code=4/0Ab'",
                          "SELECT count(*) FROM events WHERE url LIKE 'https://%@%'",
                          "SELECT 'see http://wiki.local:8080/page or mail ops@example.com' AS note",
                          "SELECT 'http://api.local:8080/users/bob@example.com/orders' AS u",
                          "SELECT 'docker://registry.local:5000/img@sha256:abc' AS image", "SELECT * FROM s WHERE pwd = '/home'",
                          "SELECT * FROM s WHERE is_secret = false", "SET VARIABLE page_token = 'p2'"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
}

TEST(Redaction, SingleTokenStatementsAreKeptAsWritten) {
  for (const char* sql : {"BEGIN", "COMMIT", "ROLLBACK", "BEGIN /* comment */", "BEGIN; COMMIT", "SET"}) {
    EXPECT_EQ(LoggableSql(sql), sql);
    EXPECT_FALSE(hardcharger::IsCredentialStatement(sql)) << sql;
  }
}

TEST(RedactionReview, F06AnOrdinaryErrorKeepsItsWords) {
  // 1. A hidden value that is a word is replaced only where it is a word of its own.
  EXPECT_EQ(LoggableError("SELECT * FROM s WHERE password = 'error' AND nope = 1",
                          "Binder Error: Referenced column \"nope\" not found in FROM clause!\nCandidate bindings: \"pos\""),
            "Binder Error: Referenced column \"nope\" not found in FROM clause!\nCandidate bindings: \"pos\"");
  EXPECT_EQ(LoggableError("SELECT * FROM s WHERE password = 'error'", "Conversion Error: Could not convert string 'error' to INT32"),
            "Conversion Error: Could not convert string '***' to INT32");
  // 2. libpq's split of a password: the pieces are hidden, the message's own words are not.
  EXPECT_EQ(LoggableError("ATTACH 'postgresql://u:Qx7@Zt9@127.0.0.1:1/db' AS p (TYPE postgres)",
                          "IO Error: Unable to connect to Postgres at \"postgresql://u:Qx7@Zt9@127.0.0.1:1/db\": could not translate host name "
                          "\"Zt9@127.0.0.1\" to address: nodename nor servname provided, or not known"),
            "IO Error: Unable to connect to Postgres at \"postgresql://u:***@127.0.0.1:1/db\": could not translate host name "
            "\"***@127.0.0.1\" to address: nodename nor servname provided, or not known");
  // 3. A URL with a port, and an '@' later on the line: no credential, nothing replaced.
  for (const char* error : {"Invalid Input Error: sync from http://idp.local:8080/users failed for user bob@example.com: 404",
                            "IO Error: cannot open \"http://minio.local:9000/bucket/exports/2024/bob@example.com.csv\": No such file"}) {
    EXPECT_EQ(LoggableError("SELECT 1", error), error);
    EXPECT_EQ(RedactCredentials(error), error);
  }
}

namespace {

double TimeoutScale() {
  const char* scale = std::getenv("HARDCHARGER_TEST_TIMEOUT_SCALE");
  const double value = scale != nullptr ? std::atof(scale) : 1.0;
  return value > 0 ? value : 1.0;
}

std::chrono::duration<double> Timed(const std::function<void()>& work) {
  const auto started = std::chrono::steady_clock::now();
  work();
  return std::chrono::steady_clock::now() - started;
}

}  // namespace

TEST(RedactionCost, ASixteenMegabyteStatementIsMaskedOnceWithinBounds) {
  // What dbt seed and ORMs send: one INSERT with its rows inline. The value at its very end
  // is still found (the masking reads all of it, whatever the log keeps).
  std::string sql = "INSERT INTO t VALUES ";
  for (int row = 0; sql.size() < 16u * 1024 * 1024; row++) {
    sql += "(" + std::to_string(row) + ", 'name " + std::to_string(row) + "', 3.25, NULL, 'a longer piece of text for row'), ";
  }
  sql += "(0, 'last', 0, NULL, 'x'); CALL serve('h', token => 'S3CR3T')";
  hardcharger::MaskedSql masked;
  const auto took = Timed([&] { masked = MaskSql(sql); });
  EXPECT_LT(took.count(), 8.0 * TimeoutScale()) << "masking 16 MB took " << took.count() << " s";
  EXPECT_TRUE(masked.credentials);
  ASSERT_EQ(masked.statements.size(), 2u);
  EXPECT_EQ(masked.statements[1].text, "CALL serve('h', token => ***)");
  EXPECT_EQ(masked.secrets, std::vector<std::string>{"S3CR3T"});
  EXPECT_LE(masked.text.size(), 4003u);
  EXPECT_TRUE(masked.text.starts_with("INSERT INTO t VALUES (0, 'name 0', 3.25, NULL, 'a longer piece of text for row'), (1, "));
  // Every sink of the request takes that one reading (MaskedSqlScope): asking again costs nothing.
  {
    const auto shared = std::make_shared<const hardcharger::MaskedSql>(std::move(masked));
    const hardcharger::MaskedSqlScope scope(sql, shared);
    const auto again = Timed([&] {
      for (int i = 0; i < 100; i++) {
        EXPECT_EQ(LoggableSql(sql).size(), shared->text.size());
        EXPECT_TRUE(hardcharger::IsCredentialStatement(sql));
        EXPECT_EQ(LoggableError(sql, "Invalid Input Error: S3CR3T"), "Invalid Input Error: ***");
      }
    });
    EXPECT_LT(again.count(), 0.5 * TimeoutScale()) << "300 reads of the same request took " << again.count() << " s";
  }
  // Text that is not a statement (an error message of that size) is read in one pass too.
  const auto text_took = Timed([&] { EXPECT_EQ(RedactCredentials(sql).find(kSecret), std::string::npos); });
  EXPECT_LT(text_took.count(), 8.0 * TimeoutScale()) << "reading 16 MB of text took " << text_took.count() << " s";
}

TEST(RedactionCost, InputsBuiltToCostMoreStayLinear) {
  // One megabyte of each shape that could make a scan start over; each is read in bounded time.
  const std::size_t size = 1024 * 1024;
  auto repeated = [&](const std::string& piece) {
    std::string out;
    while (out.size() < size) out += piece;
    return out;
  };
  const std::vector<std::string> inputs = {
      repeated("://u:x"), repeated("://u:x "), repeated("password="), repeated("password=x;"), repeated("token => "),
      repeated("a$q$ "), repeated("$q"), repeated("/*"), repeated("'password': "), repeated("\"password\": \""),
      repeated("https://a:b@c "), repeated("x=1;"), repeated("password=a b;"), repeated("("), repeated("token => ("),
      "SELECT " + repeated("password = 'x' AND "), "SELECT '" + repeated("password=x ") + "'",
      "SELECT f(token => " + repeated("'a' || ") + "'b')", "INSERT INTO u (a, password) VALUES " + repeated("(1, 'x'), ") + "(1, 'x')",
      "SELECT " + repeated("'ALTER ROLE a PASSWORD ''x''', "), "SET " + std::string(size, 'a') + " = 1", std::string(size, ' ') + "SELECT 1",
      "SELECT MAP([" + repeated("'password', ") + "'a'], [" + repeated("'x', ") + "'y'])", repeated("-- password = 'x'\n")};
  for (const std::string& input : inputs) {
    const auto took = Timed([&] {
      MaskSql(input);
      RedactCredentials(input);
    });
    EXPECT_LT(took.count(), 4.0 * TimeoutScale()) << input.substr(0, 40) << "... took " << took.count() << " s";
  }
}

TEST(RedactionCost, DocumentsAndPairsAreReadInLinearTime) {
  // Review of 2.14.0, finding 40: a look-ahead that searched the rest of the text once per URL or
  // per pair made a 4 MB JSON document with a URL per record take 18 s (16 MB: 5 minutes), in
  // statement text, error text and bound text parameters alike. Each shape is timed at 1 MB and
  // 4 MB: linear is 4 times as long, quadratic 16.
  auto fill = [](const std::string& unit, std::size_t size) {
    std::string out;
    out.reserve(size + unit.size());
    while (out.size() < size) out += unit;
    return out;
  };
  auto json_doc = [](std::size_t size) {
    std::string out = "[";
    for (int i = 0; out.size() < size; i++) {
      out += "{\"id\": " + std::to_string(i) + ", \"url\": \"https://example.com/items/" + std::to_string(i) +
             "\", \"title\": \"Item number " + std::to_string(i) + "\"}, ";
    }
    return out + "{}]";
  };
  auto html_doc = [](std::size_t size) {
    std::string out = "<ul>";
    for (int i = 0; out.size() < size; i++) out += "<li><a href=\"https://example.com/p/" + std::to_string(i) + "\">Page</a> text</li>";
    return out + "</ul>";
  };
  struct Shape {
    const char* name;
    std::function<std::string(std::size_t)> text;
  };
  const std::vector<Shape> shapes = {
      {"a JSON document with a URL per record", json_doc},
      {"an HTML list of links", html_doc},
      {"x://h repeated", [&](std::size_t n) { return fill("x://h ", n); }},
      {"x://h:1 repeated", [&](std::size_t n) { return fill("x://h:1 ", n); }},
      {"a quote, then x://h repeated", [&](std::size_t n) { return "\"" + fill("x://h ", n); }},
      {";password=v pairs, no white space", [&](std::size_t n) { return fill(";password=v", n); }},
      {"password=v and a word", [&](std::size_t n) { return fill("password=v x ", n); }},
      {"Failed to parse connection string, repeated", [&](std::size_t n) { return fill("Failed to parse connection string ", n); }},
      {"token words and a quote at the end", [&](std::size_t n) { return fill("token ", n) + "'"; }},
  };
  // (The best of three runs, on a machine that may be busy with other work.)
  auto best_of_three = [](const std::function<void()>& work) {
    return std::min({Timed(work).count(), Timed(work).count(), Timed(work).count()});
  };
  for (const Shape& shape : shapes) {
    std::vector<double> took;
    for (const std::size_t size : {std::size_t{1} << 20, std::size_t{4} << 20}) {
      const std::string text = shape.text(size);
      std::string literal = "SELECT '";
      for (const char c : text) literal += c == '\'' ? std::string("''") : std::string(1, c);
      literal += "'";
      const hardcharger::MaskedSql statement = MaskSql("SELECT * FROM t WHERE password = 'abcdefgh'");
      took.push_back(best_of_three([&] {
        MaskSql(literal);                                           // statement text
        RedactError("Invalid Input Error: " + text, statement);    // error text
        hardcharger::CredentialsIn(text);                          // a bound text parameter
      }));
    }
    EXPECT_LT(took[1], 10.0 * std::max(took[0], 0.05)) << shape.name << ": 1 MB " << took[0] << " s, 4 MB " << took[1] << " s";
    EXPECT_LT(took[1], 4.0 * TimeoutScale()) << shape.name << ": 4 MB took " << took[1] << " s";
  }
  // And the review's own case at 16 MB (2.13.1: 2.6 s; before the fix: over 5 minutes).
  std::string doc = json_doc(16u << 20);
  const auto sixteen = Timed([&] { MaskSql("INSERT INTO docs VALUES ('" + doc + "')"); });
  EXPECT_LT(sixteen.count(), 8.0 * TimeoutScale()) << "a 16 MB JSON document took " << sixteen.count() << " s";
}
