"""EXECUTE on a table function: GRANT EXECUTE ON TABLE FUNCTION name [(PATH ARGUMENT n, PATH
PARAMETER p, ...)] TO role (issue #24).

Under access control a table function that reads no table and no file is every user's, one
that reads files goes through the storage privileges, and any other (an extension's, say) is
the administrator's. EXECUTE opens one such function to a role. The grant declares which of
its arguments name the files it reads: their values are checked against the storage privileges
as written, before DuckDB binds (and so opens) them, and again as bound. A macro an
administrator wraps around a granted function works for the grantee. Every refusal is
asserted by its reason next to its twin."""

import sqlite3

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

ADMIN_ONLY = "under access control it is available to the administrator only"
GRANT_HINT = "or to a role granted EXECUTE ON TABLE FUNCTION"


def make_sqlite(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    connection.executemany("INSERT INTO t VALUES (?, ?)", rows)
    connection.commit()
    connection.close()
    return path


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp("files")
    return {"root": root, "allowed": make_sqlite(root / "allowed" / "x.sqlite", [(1, "a")]),
            "elsewhere": make_sqlite(root / "elsewhere" / "y.sqlite", [(2, "b")])}


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory, files):
    database = tmp_path_factory.mktemp("db") / "functions.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE ROLE tz_users", "CREATE ROLE lite_users", "CREATE ROLE nothing",
        "CREATE USER tia", "CREATE USER lou", "CREATE USER nob",
        "GRANT tz_users TO tia", "GRANT lite_users TO lou", "GRANT nothing TO nob",
        # pg_timezone_names() (the icu extension) reads no file; sqlite_scan(path, table) reads
        # the database file its first argument names.
        "GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names TO tz_users",
        "GRANT EXECUTE ON TABLE FUNCTION sqlite_scan (PATH ARGUMENT 1) TO lite_users",
        f"GRANT READ FILESYSTEM ON LOCATION '{files['allowed'].parent}/' TO lite_users",
        "CREATE MACRO tz_names() AS TABLE SELECT name FROM pg_timezone_names()",
        "CREATE MACRO lite(p) AS TABLE SELECT * FROM sqlite_scan(p, 't')",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def user(server, name):
    return server.user(name=name)


def rows(client, sql):
    return client.run(sql=sql).to_pylist()


def grant_error(admin, sql):
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql=sql)
    assert raised.value.code == "sql_error", str(raised.value)
    return raised.value.detail


# ---- calling ----

def test_a_grantee_may_call_the_function_and_nobody_else(server):
    tia, nob = user(server, "tia"), user(server, "nob")
    assert rows(tia, "SELECT count(*) > 100 AS many FROM pg_timezone_names()") == [{"many": True}]
    error = expect_blocked(client=nob, sql="SELECT count(*) FROM pg_timezone_names()", why=ADMIN_ONLY)
    assert f"{GRANT_HINT} pg_timezone_names" in error.detail


def test_an_administrators_macro_around_the_function_works_for_the_grantee(server):
    tia, nob = user(server, "tia"), user(server, "nob")
    assert rows(tia, "SELECT count(*) > 100 AS many FROM tz_names()") == [{"many": True}]
    # The macro expands to the function: the plan check refuses it to a stranger, naming the grant.
    expect_blocked(client=nob, sql="SELECT count(*) FROM tz_names()",
                   why="the table function pg_timezone_names is not available under access control (an administrator "
                       "can GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names to a role)")


def test_the_function_stays_refused_through_query(server):
    tia = user(server, "tia")
    expect_blocked(client=tia, sql="SELECT * FROM query('SELECT count(*) FROM pg_timezone_names()')",
                   why="Hardcharger blocked query()")
    expect_blocked(client=tia, sql="CALL query_table('pg_timezone_names')", why="Hardcharger blocked")


def test_a_declared_path_argument_goes_through_the_storage_privileges(server, files):
    lou = user(server, "lou")
    allowed, elsewhere = files["allowed"], files["elsewhere"]
    assert rows(lou, f"SELECT * FROM sqlite_scan('{allowed}', 't')") == [{"id": 1, "name": "a"}]
    assert rows(lou, f"SELECT * FROM lite('{allowed}')") == [{"id": 1, "name": "a"}]  # through the macro too
    # Outside the location: refused before DuckDB opens the file (a 403, not sqlite's error),
    # also when the file doesn't exist, and as CALL.
    for sql in [f"SELECT * FROM sqlite_scan('{elsewhere}', 't')", f"SELECT * FROM lite('{elsewhere}')",
                f"SELECT * FROM sqlite_scan('{files['root']}/missing.sqlite', 't')", f"CALL sqlite_scan('{elsewhere}', 't')"]:
        expect_blocked(client=lou, sql=sql, why="needs the READ FILESYSTEM system privilege, everywhere or on a location that covers it")
    # A computed path can't be checked before it is read.
    expect_blocked(client=lou, sql=f"SELECT * FROM sqlite_scan(concat('{allowed}', ''), 't')",
                   why="its path is an expression or a parameter, which Hardcharger can't check before DuckDB reads it")
    # Nobody without the grant, whatever their storage privileges.
    expect_blocked(client=user(server, "nob"), sql=f"SELECT * FROM sqlite_scan('{allowed}', 't')", why=ADMIN_ONLY)


def test_every_declared_argument_is_checked_and_the_latest_declaration_stands(server, admin, files):
    lou = user(server, "lou")
    allowed = files["allowed"]
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION sqlite_scan (PATH ARGUMENTS 1, 2) TO lite_users")
    assert [r["columns"] for r in rows(admin, "SHOW GRANTS FOR ROLE lite_users") if r["object"] == "sqlite_scan"] == [
        "path argument 1, path argument 2"]
    # The table name, now declared a path, is not under the location.
    expect_blocked(client=lou, sql=f"SELECT * FROM sqlite_scan('{allowed}', 't')",
                   why="Hardcharger blocked sqlite_scan() of 't', which needs the READ FILESYSTEM system privilege")
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION sqlite_scan (PATH ARGUMENT 1) TO lite_users")
    assert rows(lou, f"SELECT * FROM sqlite_scan('{allowed}', 't')") == [{"id": 1, "name": "a"}]


def test_a_named_parameter_can_be_declared_a_path(server, admin, files):
    """st_read(path, layer := ...) of the spatial extension: declared PATH PARAMETER layer, the
    layer's value is checked as a path (the operator decides what names a file)."""
    lou = user(server, "lou")
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION st_read (PATH ARGUMENT 1, PATH PARAMETER layer) TO lite_users")
    assert [r["columns"] for r in rows(admin, "SHOW GRANTS FOR ROLE lite_users") if r["object"] == "st_read"] == [
        "path argument 1, path parameter layer"]
    expect_blocked(client=lou, sql=f"SELECT * FROM st_read('{files['allowed']}', layer := 'points')",
                   why="Hardcharger blocked st_read() of 'points', which needs the READ FILESYSTEM system privilege")
    expect_allowed(client=admin, sql="REVOKE EXECUTE ON TABLE FUNCTION st_read FROM lite_users")


def test_revoke_closes_the_function(server, admin):
    expect_allowed(client=admin, sql="CREATE ROLE temporary_tz")
    expect_allowed(client=admin, sql="GRANT temporary_tz TO nob")
    nob = user(server, "nob")
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names TO temporary_tz")
    expect_allowed(client=nob, sql="SELECT count(*) FROM pg_timezone_names()")
    assert rows(admin, "REVOKE EXECUTE ON TABLE FUNCTION pg_timezone_names FROM temporary_tz") == [
        {"result": "Revoked EXECUTE on table function pg_timezone_names from temporary_tz"}]
    expect_blocked(client=nob, sql="SELECT count(*) FROM pg_timezone_names()", why=ADMIN_ONLY)
    expect_allowed(client=admin, sql="REVOKE temporary_tz FROM nob")


# ---- granting ----

def test_what_cannot_be_granted(admin):
    for sql, why in [
        ("GRANT EXECUTE ON TABLE FUNCTION range TO nothing", "table function range needs no grant: every user may call it"),
        ("GRANT EXECUTE ON TABLE FUNCTION hardcharger_sessions TO nothing", "needs no grant: every user may call it"),
        ("GRANT EXECUTE ON TABLE FUNCTION read_parquet TO nothing",
         "table function read_parquet reads files: the storage privileges decide who may call it"),
        ("GRANT EXECUTE ON TABLE FUNCTION query TO nothing", "table function query stays the administrator's"),
        ("GRANT EXECUTE ON TABLE FUNCTION duckdb_secrets TO nothing", "stays the administrator's"),
        ("GRANT EXECUTE ON TABLE FUNCTION hardcharger_monitor_info TO nothing", "needs no grant"),
        ("GRANT EXECUTE ON TABLE FUNCTION chargewire_upload TO nothing", "needs no grant"),
        ("GRANT EXECUTE ON TABLE FUNCTION no_such_function TO nothing",
         "table function no_such_function does not exist: load the extension that provides it first"),
        ("GRANT EXECUTE ON TABLE FUNCTION main.pg_timezone_names TO nothing", "expected TO but found '.'"),
        ("GRANT ALL ON TABLE FUNCTION pg_timezone_names TO nothing", "on a table function only EXECUTE is granted"),
        ("GRANT EXECUTE ON SCHEMA main TO nothing", "EXECUTE is granted ON TABLE FUNCTION name"),
    ]:
        assert why in grant_error(admin, sql), sql
    # Twin: the grant that works, and GRANT ALL ON a schema or catalog never includes EXECUTE.
    expect_allowed(client=admin, sql="GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names TO nothing")
    expect_allowed(client=admin, sql="GRANT ALL ON SCHEMA main TO nothing")
    assert "EXECUTE" not in {r["privilege"] for r in rows(admin, "SHOW GRANTS FOR ROLE nothing") if r["object_type"] == "SCHEMA"}
    expect_allowed(client=admin, sql="REVOKE ALL ON SCHEMA main FROM nothing")
    expect_allowed(client=admin, sql="REVOKE EXECUTE ON TABLE FUNCTION pg_timezone_names FROM nothing")


def test_the_grant_shows_in_every_listing(server, admin):
    grants = rows(admin, "SELECT grantee, object_type, catalog_name, schema_name, object_name, column_name FROM hardcharger_grants() WHERE privilege = 'EXECUTE' ORDER BY grantee")
    assert grants == [
        {"grantee": "lite_users", "object_type": "TABLE FUNCTION", "catalog_name": None, "schema_name": None,
         "object_name": "sqlite_scan", "column_name": "path argument 1"},
        {"grantee": "tz_users", "object_type": "TABLE FUNCTION", "catalog_name": None, "schema_name": None,
         "object_name": "pg_timezone_names", "column_name": None}]
    assert rows(admin, "SHOW GRANTS FOR ROLE tz_users") == [
        {"privilege": "EXECUTE", "object_type": "TABLE FUNCTION", "object": "pg_timezone_names", "columns": "",
         "granted_to": "tz_users", "grantee_type": "ROLE", "grant_option": "NO", "granted_by": "administrator"}]
    tia = user(server, "tia")
    assert rows(tia, "SELECT privilege, object_type, object_name, via FROM hardcharger_effective_privileges() WHERE privilege = 'EXECUTE'") == [
        {"privilege": "EXECUTE", "object_type": "TABLE FUNCTION", "object_name": "pg_timezone_names", "via": "tz_users"}]
    # A user sees the grants of its own roles only.
    assert rows(user(server, "nob"), "SELECT count(*) AS n FROM hardcharger_grants() WHERE privilege = 'EXECUTE'") == [{"n": 0}]


def test_init_sql_can_make_the_grant(launcher, store):
    env = {"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE ROLE IF NOT EXISTS tz; CREATE USER IF NOT EXISTS tim; GRANT tz TO tim; "
                                           "GRANT EXECUTE ON TABLE FUNCTION pg_timezone_names TO tz;"}
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), env=env)
    tim = server.user(name="tim")
    assert rows(tim, "SELECT count(*) > 100 AS many FROM pg_timezone_names()") == [{"many": True}]
