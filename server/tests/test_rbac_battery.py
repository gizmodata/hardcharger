"""Access control together with the GizmoSQL 1.x battery's features: the server's own
catalogs (instrumentation, catalog logs, _hardcharger_system), the SQL functions and
settings, KILL SESSION, the statement queue and the statement guards, on a server whose
users come from the access-control store.

A store user sees none of the server's own catalogs and can't write them: neither in
SQL nor in metadata, whatever grants it holds. The administrator reads them and can't
write them either. Logs and instrumentation never keep the password of CREATE USER.
"""

import time

import pytest

from rbac import BLOCKED, WarpError, expect_allowed, expect_blocked, start_rbac_server

MANAGED = ("_hardcharger_instr", "_hardcharger_logs")


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(
        launcher=module_launcher, store_uri=store.new_database(),
        args=["--enable-instrumentation", "--enable-catalog-logging"])
    admin = server.admin()
    for sql in ["CREATE TABLE sales (id INT, amount INT)", "INSERT INTO sales VALUES (1, 10), (2, 20)",
                "CREATE ROLE analyst", "GRANT SELECT ON sales TO analyst",
                "CREATE USER joe WITH PASSWORD 'joe-Secret-1'", "GRANT analyst TO joe",
                # Even a grant on the whole catalog reaches none of the server's own.
                "GRANT SELECT ON CATALOG memory TO analyst"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def joe(server):
    return server.user(name="joe", password="joe-Secret-1")


def eventually(client, sql: str, predicate, timeout: float = 10):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = client.run(sql=sql).to_pylist()
        if predicate(value):
            return value
        time.sleep(0.1)
    raise AssertionError(f"{sql} -> {value!r}")


def test_store_users_cannot_read_or_list_the_servers_catalogs(joe):
    assert expect_allowed(client=joe, sql="SELECT sum(amount) AS s FROM sales").to_pylist() == [{"s": 30}]
    for sql in ["SELECT count(*) FROM _hardcharger_instr.main.sessions", "SELECT count(*) FROM _hardcharger_logs.main.logs"]:
        with pytest.raises(WarpError) as raised:
            joe.run(sql=sql)
        assert raised.value.code == "permission_denied" and "Only administrators can read" in raised.value.detail, \
            raised.value
    catalogs = joe.metadata(kind="catalogs").column(0).to_pylist()
    assert not set(MANAGED) & set(catalogs), catalogs
    # Nor in SQL's own catalog functions and views.
    for sql in ["SELECT database_name FROM duckdb_databases()", "SELECT DISTINCT table_catalog FROM information_schema.tables",
                "SELECT database_name FROM duckdb_tables()"]:
        names = set(joe.run(sql=sql).column(0).to_pylist())
        assert not set(MANAGED) & names, f"{sql}: {names}"


def test_store_users_cannot_write_or_detach_them(joe):
    for sql in ["DELETE FROM _hardcharger_logs.main.logs", "DETACH _hardcharger_instr",
                "INSERT INTO _hardcharger_instr.main.sessions SELECT * FROM _hardcharger_instr.main.sessions",
                "USE _hardcharger_logs"]:
        with pytest.raises(WarpError) as raised:
            joe.run(sql=sql)
        assert raised.value.code == "permission_denied", f"{sql}: {raised.value}"


def test_the_administrator_reads_them_and_cannot_write_them(admin):
    eventually(admin, "SELECT count(*) AS n FROM _hardcharger_instr.main.sessions WHERE username = 'joe'",
               lambda rows: rows[0]["n"] >= 1)
    eventually(admin, "SELECT count(*) AS n FROM _hardcharger_logs.main.logs WHERE username = 'joe'",
               lambda rows: rows[0]["n"] >= 1)
    for sql, why in [("DELETE FROM _hardcharger_logs.main.logs", "read-only"),
                     ("DETACH _hardcharger_instr", "Cannot DETACH")]:
        with pytest.raises(WarpError) as raised:
            admin.run(sql=sql)
        assert raised.value.code == "permission_denied" and why in raised.value.detail, raised.value


def test_passwords_never_reach_logs_or_instrumentation(server, admin):
    expect_allowed(client=admin, sql="CREATE USER secretive WITH PASSWORD 'never-Logged-42'")
    expect_allowed(client=admin, sql="ALTER USER secretive WITH PASSWORD 'never-Logged-43'")
    eventually(admin, "SELECT count(*) AS n FROM _hardcharger_instr.main.sql_statements "
                      "WHERE sql_text LIKE '%REDACTED: user DDL%'", lambda rows: rows[0]["n"] >= 2)
    for sql in ["SELECT count(*) AS n FROM _hardcharger_instr.main.sql_statements WHERE sql_text LIKE '%never-' || 'Logged%'",
                "SELECT count(*) AS n FROM _hardcharger_logs.main.logs WHERE fields::VARCHAR LIKE '%never-' || 'Logged%'"]:
        assert admin.run(sql=sql).to_pylist() == [{"n": 0}], sql
    assert "never-Logged" not in server.log()


def test_sql_functions_and_settings_for_store_users(joe):
    row = expect_allowed(client=joe, sql="SELECT HARDCHARGER_USER() AS u, HARDCHARGER_ROLE() AS r").to_pylist()[0]
    assert row == {"u": "joe", "r": "user"}, row
    settings = expect_allowed(client=joe, sql="SELECT name FROM hardcharger_settings() "
                                              "WHERE name LIKE 'hardcharger.%instrumentation%'").to_pylist()
    assert settings == [], settings  # admin-only rows
    expect_allowed(client=joe, sql="SET hardcharger.query_tag = '{\"team\": \"finance\"}'")
    with pytest.raises(WarpError) as raised:
        joe.run(sql="SET GLOBAL hardcharger.query_timeout = 5")
    assert raised.value.code == "permission_denied", raised.value


def test_the_admin_kills_a_store_users_session(server, admin, joe):
    session_id = joe.run(sql="SELECT HARDCHARGER_CURRENT_SESSION() AS s").to_pylist()[0]["s"]
    expect_allowed(client=admin, sql=f"KILL SESSION '{session_id}'")
    with pytest.raises(WarpError) as raised:
        joe.run(sql="SELECT 1")
    assert raised.value.code == "session_ended", raised.value
    other = server.user(name="joe", password="joe-Secret-1")
    with pytest.raises(WarpError) as raised:
        other.run(sql=f"KILL SESSION '{admin.run(sql='SELECT HARDCHARGER_CURRENT_SESSION() AS s').to_pylist()[0]['s']}'")
    assert raised.value.code == "permission_denied" and "Only admin users" in raised.value.detail, raised.value


def test_system_privileges_decide_for_store_users_not_the_admin_guard(server, admin):
    # The statement guard's admin-only list (files, ATTACH, ...) is access control's to
    # decide under RBAC: a store user with the system privilege may read files.
    for sql in ["CREATE USER reader WITH PASSWORD 'reader-Secret-1'", "CREATE ROLE file_readers",
                "GRANT file_readers TO reader"]:
        expect_allowed(client=admin, sql=sql)
    reader = server.user(name="reader", password="reader-Secret-1")
    error = expect_blocked(client=reader, sql="SELECT * FROM read_csv('/tmp/hc-no-such.csv')")
    assert "READ FILESYSTEM" in error.detail or BLOCKED in error.detail, error
    expect_allowed(client=admin, sql="GRANT READ FILESYSTEM TO file_readers")
    reader = server.user(name="reader", password="reader-Secret-1")
    with pytest.raises(WarpError) as raised:
        reader.run(sql="SELECT * FROM read_csv('/tmp/hc-no-such.csv')")
    assert BLOCKED not in raised.value.detail and "No files found" in raised.value.detail, raised.value
    # The sensitive path guard holds for everyone, privilege or not.
    with pytest.raises(WarpError) as raised:
        reader.run(sql="SELECT * FROM read_csv('/etc/shadow')")
    assert "blocked access to" in raised.value.detail, raised.value
