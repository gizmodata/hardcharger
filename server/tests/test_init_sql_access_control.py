"""Init SQL runs Hardcharger's own access-control statements (issue #15).

CREATE USER, CREATE ROLE, GRANT and the rest aren't DuckDB's: in init SQL they run as the
administrator, in order with the DuckDB statements around them. Without access control they
fail with the error a session gets. A failing init statement never logs a credential it carries,
and always says which statement failed and why.
"""

import secrets

import pytest

from conftest import TIMEOUT_SCALE
from rbac import rbac_env, start_rbac_server

PASSWORD = "pa$$w0rd-" + secrets.token_hex(nbytes=6)


def init_sql(*statements: str) -> dict[str, str]:
    return {"HARDCHARGER_INIT_SQL_COMMANDS": "\n".join(statements)}


def test_init_sql_creates_users_and_grants_in_order_with_duckdb_statements(launcher, store):
    uri = store.new_database()
    # The README's example (Access control), then a row to read.
    env = init_sql("CREATE TABLE IF NOT EXISTS sales (amount DECIMAL(12, 2));",
                   "CREATE ROLE IF NOT EXISTS readers;",
                   "GRANT SELECT ON sales TO readers;",  # a table the init SQL has just created
                   f"CREATE USER IF NOT EXISTS analyst WITH PASSWORD '{PASSWORD}';",
                   "GRANT readers TO analyst;",
                   "INSERT INTO sales VALUES (42);")
    server = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    analyst = server.user(name="analyst", password=PASSWORD)
    assert analyst.run(sql="SELECT amount::INTEGER AS amount FROM sales").to_pylist() == [{"amount": 42}]
    assert "init SQL ran" in server.log()
    assert PASSWORD not in server.log()
    server.running.stop()
    # The init SQL runs at every start (and on every replica of a shared store): with
    # IF NOT EXISTS, a second start over the same store comes up and the user still signs in.
    again = start_rbac_server(launcher=launcher, store_uri=uri, env=env)
    assert again.user(name="analyst", password=PASSWORD).run(
        sql="SELECT amount::INTEGER AS amount FROM sales").to_pylist() == [{"amount": 42}]


def test_a_failing_access_control_statement_stops_the_server_without_logging_its_password(launcher, store):
    uri = store.new_database()
    env = init_sql(f"CREATE USER twice WITH PASSWORD '{PASSWORD}';",
                   f"CREATE USER twice WITH PASSWORD '{PASSWORD}';")
    running = launcher.start(env={**rbac_env(store_uri=uri), **env}, expect_up=False, startup_seconds=60)
    assert running.process.wait(timeout=60) == 1
    log = running.log()
    assert "init SQL failed" in log and "twice" in log and "already exists" in log, log
    assert PASSWORD not in log, log


@pytest.mark.parametrize("statement, diagnostic, which", [
    (f"CREATE USER IF NOT EXISTS new_user WITH PASSWORD '{PASSWORD}';",
     "Hardcharger access control is not enabled on this server, so CREATE USER is not available",
     "(init SQL statement 2 of 2: [REDACTED: user DDL])"),
    ("CREATE ROLE new_role;",
     "Hardcharger access control is not enabled on this server, so CREATE ROLE is not available",
     "(init SQL statement 2 of 2: CREATE ROLE new_role)"),
], ids=["credential-create-user", "ordinary-create-role"])
def test_without_access_control_init_sql_refuses_rbac_before_running_anything(launcher, statement, diagnostic, which):
    env = init_sql("CREATE TABLE ran_first (i INTEGER);",
                   statement)
    running = launcher.start(env=env, expect_up=False)
    assert running.process.wait(timeout=20 * TIMEOUT_SCALE) == 1
    log = running.log()
    assert f"init SQL failed: {diagnostic}" in log, log  # the reason survives, for a credential statement too
    assert which in log, log
    assert PASSWORD not in log, log


@pytest.mark.parametrize("statement, which", [
    (f"CREATE SECRET leaky (TYPE s3, KEY_ID 'key-id', SECRET '{PASSWORD}',, REGION 'x');",
     "(init SQL statement 1 of 1: [REDACTED: secret DDL])"),
    ("SELECT FROM;", "(init SQL statement 1 of 1: SELECT FROM)"),
    (f"SELECT 1; ATTACH '/no-such-dir/x.duckdb' AS a (ENCRYPTION_KEY '{PASSWORD}' BOGUS); SELECT 2",
     "(init SQL statement 2 of 3: ATTACH '/no-such-dir/x.duckdb' AS a (ENCRYPTION_KEY ***))"),
], ids=["credential-syntax-error", "ordinary-syntax-error", "credential-attach-syntax-error"])
def test_rejected_init_sql_masks_sensitive_errors_and_preserves_ordinary_diagnostics(launcher, statement, which):
    # DuckDB quotes the failing line ("LINE 1: ...") in a syntax error, a CREATE SECRET's values
    # included: the excerpt is dropped and the values are replaced, and the reason survives.
    env = init_sql(statement)
    running = launcher.start(env=env, expect_up=False)
    assert running.process.wait(timeout=20 * TIMEOUT_SCALE) == 1
    log = running.log()
    assert "init SQL failed" in log and "syntax error" in log, log  # the reason survives
    assert which in log, log
    assert "LINE 1" not in log, log
    assert PASSWORD not in log, log


@pytest.mark.parametrize("statement, reason, which", [
    # The usual init statements that carry a credential, each failing for a reason an operator must be able to read.
    ("ATTACH 'host=127.0.0.1 port=1 user=u password={p} dbname=d' AS pg (TYPE postgres)",
     ("Connection refused", "connection to server"),
     "ATTACH 'host=127.0.0.1 port=1 user=u password=*** dbname=d' AS pg (TYPE postgres)"),
    ("ATTACH '/no/such/dir/lake.db' AS lake (ENCRYPTION_KEY '{p}')",
     ("No such file or directory", "Cannot open file"), "ATTACH '/no/such/dir/lake.db' AS lake (ENCRYPTION_KEY ***)"),
    ("CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET '{p}', REGIN 'us-east-1')",
     ("regin",), "[REDACTED: secret DDL]"),
    ("SELECT * /* {{\"app\": \"dbt\"}} */ FROM missing_table",
     ("Table with name missing_table does not exist",), "SELECT * /* {\"app\": \"dbt\"} */ FROM missing_table"),
    ("CREATE TABLE a (access_token VARCHAR); SELECT nofn(1) FROM a JOIN a b ON a.access_token = b.access_token",
     ("nofn",), "SELECT nofn(1) FROM a JOIN a b ON a.access_token = b.access_token"),
], ids=["attach-postgres-refused", "attach-missing-directory", "create-secret-typo", "ordinary-with-comment", "token-join"])
def test_a_failed_init_statement_says_which_and_why(launcher, statement, reason, which):
    # At start-up there is no client that still has the error: the log line is all there is.
    password = "Zk7;CANARY)init" + secrets.token_hex(nbytes=4)
    running = launcher.start(env=init_sql(statement.format(p=password)), expect_up=False, startup_seconds=60)
    assert running.process.wait(timeout=60 * TIMEOUT_SCALE) == 1
    log = running.log()
    assert "init SQL failed: " in log, log
    assert any(text in log for text in reason), log  # why
    assert f": {which})" in log, log               # which
    assert "CANARY" not in log and "Zk7" not in log, log
    assert "[REDACTED: error" not in log, log
