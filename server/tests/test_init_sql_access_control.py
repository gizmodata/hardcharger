"""Init SQL runs Hardcharger's own access-control statements (issue #15).

CREATE USER, CREATE ROLE, GRANT and the rest aren't DuckDB's: in init SQL they run as the
administrator, in order with the DuckDB statements around them. Without access control they
fail with the error a session gets. A failing init statement never logs a credential it carries.
"""

import secrets

import pytest

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


@pytest.mark.parametrize("statement, diagnostic", [
    (f"CREATE USER IF NOT EXISTS new_user WITH PASSWORD '{PASSWORD}';",
     "[REDACTED: error of a credential statement]"),
    ("CREATE ROLE new_role;",
     "Hardcharger access control is not enabled on this server, so CREATE ROLE is not available"),
])
def test_without_access_control_init_sql_refuses_rbac_before_running_anything(launcher, statement, diagnostic):
    env = init_sql("CREATE TABLE ran_first (i INTEGER);",
                   statement)
    running = launcher.start(env=env, expect_up=False)
    assert running.process.wait(timeout=20) == 1
    log = running.log()
    assert f"init SQL failed: {diagnostic}" in log, log
    assert PASSWORD not in log, log


@pytest.mark.parametrize("statement, diagnostic", [
    (f"CREATE SECRET leaky (TYPE s3, KEY_ID 'key-id', SECRET '{PASSWORD}',, REGION 'x');",
     "[REDACTED: error of a credential statement]"),
    ("SELECT FROM;", "syntax error"),
])
def test_rejected_init_sql_masks_sensitive_errors_and_preserves_ordinary_diagnostics(launcher, statement, diagnostic):
    # DuckDB quotes the failing line in syntax errors. Suppress the entire error for
    # credential statements, while ordinary syntax errors remain useful.
    env = init_sql(statement)
    running = launcher.start(env=env, expect_up=False)
    assert running.process.wait(timeout=20) == 1
    log = running.log()
    assert "init SQL failed" in log and diagnostic in log, log
    assert PASSWORD not in log, log
