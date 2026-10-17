"""The access-control statements' grammar, through the server: which statements
Hardcharger reads itself and which it leaves to DuckDB, quoting, keywords, and one
statement at a time."""

import pytest

from rbac import WarpError, expect_allowed, start_rbac_server

PREFIX = "Hardcharger access control: "


@pytest.fixture(scope="module")
def server(module_launcher, store):
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["CREATE TABLE t (id INT, a INT)", "CREATE TABLE users (id INT, email VARCHAR, \"Signup TS\" INT)",
                'CREATE SCHEMA "we""ird"', 'CREATE TABLE "we""ird"."T 1" (x INT)', "CREATE TABLE \"table\" (x INT)",
                "CREATE VIEW v AS SELECT 1 AS x", "CREATE ROLE r", "CREATE USER u"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def error(client, sql: str) -> str:
    try:
        client.run(sql=sql)
        return ""
    except WarpError as raised:
        return raised.detail


@pytest.mark.parametrize("sql", [
    "GRANT SELECT ON t TO r", "grant r to u", "REVOKE r FROM u", "SHOW GRANTS", "show grants for u",
    "/* hi */ -- there\n  GRANT r TO u", "/* nested /* comment */ still */ GRANT r TO u",
    "ALTER VIEW v OWNER TO r", "ALTER TABLE memory.main.t OWNER TO r",
])
def test_recognizes_access_control_statements(admin, sql):
    # Each runs as an access-control statement (DuckDB has no grammar for them).
    assert error(client=admin, sql=sql) == "", sql


@pytest.mark.parametrize("sql", [
    "SELECT 1", "CREATE TABLE users2 (id INT)", "DROP TABLE IF EXISTS roles", "CREATE VIEW grants AS SELECT 1",
    "ALTER TABLE t ADD COLUMN IF NOT EXISTS x INT", "SHOW TABLES", "DESCRIBE users", "-- GRANT r TO u\nSELECT 1",
    "/* GRANT r TO u */ SELECT 1", "SELECT 'GRANT r TO u'",
])
def test_leaves_engine_statements_to_duckdb(admin, sql):
    assert error(client=admin, sql=sql) == "", sql


def test_engine_errors_are_duckdbs(admin):
    # Statements Hardcharger does not read fail as DuckDB says.
    assert "Hardcharger" not in error(client=admin, sql="CREATE USERS_TABLE AS SELECT 1")
    assert "Hardcharger" not in error(client=admin, sql="ALTER TABLE owner RENAME TO x")


def test_create_user_passwords_and_quoting(server, admin):
    expect_allowed(client=admin, sql="CREATE USER joe WITH PASSWORD 'it''s secret'")
    assert server.password_token(user="joe", password="it's secret")
    expect_allowed(client=admin, sql='CREATE USER IF NOT EXISTS "Joe Smith";')
    assert admin.run(sql='SHOW GRANTS FOR USER "joe smith"').num_rows == 0
    expect_allowed(client=admin, sql="create user nowith password 'x'")
    assert server.password_token(user="nowith", password="x")


def test_alter_user(server, admin):
    expect_allowed(client=admin, sql="CREATE USER al WITH PASSWORD 'old'")
    expect_allowed(client=admin, sql="ALTER USER al WITH PASSWORD 'n3w'")
    assert server.password_token(user="al", password="n3w")
    expect_allowed(client=admin, sql="ALTER USER al DISABLE")
    with pytest.raises(WarpError):
        server.password_token(user="al", password="n3w")
    expect_allowed(client=admin, sql="ALTER USER al ENABLE")
    expect_allowed(client=admin, sql="ALTER USER al PASSWORD NULL")
    with pytest.raises(WarpError):
        server.password_token(user="al", password="n3w")


def test_role_membership_forms(admin):
    for sql in ["CREATE ROLE analyst", "CREATE ROLE reader", "CREATE ROLE lead", "CREATE USER jo",
                "GRANT analyst, reader TO USER jo, ROLE lead", "GRANT analyst TO lead WITH ADMIN OPTION",
                "REVOKE ADMIN OPTION FOR analyst FROM lead"]:
        expect_allowed(client=admin, sql=sql)
    granted = {(row["object"], row["granted_to"]) for row in admin.run(sql="SHOW GRANTS FOR jo").to_pylist()
               if row["privilege"] == "ROLE"}
    assert granted == {("analyst", "jo"), ("reader", "jo")}, granted
    expect_allowed(client=admin, sql="REVOKE analyst FROM jo")


def test_principals_named_like_keywords(admin):
    # USER / ROLE are kind keywords only when another name follows.
    expect_allowed(client=admin, sql='CREATE ROLE "role"')
    expect_allowed(client=admin, sql="GRANT r TO role")
    # A role named like a privilege must be quoted, as in Postgres...
    expect_allowed(client=admin, sql='CREATE ROLE "select"')
    expect_allowed(client=admin, sql='GRANT "select" TO u')
    # ...but a role whose name merely starts like a privilege phrase is fine.
    expect_allowed(client=admin, sql="CREATE ROLE manage")
    expect_allowed(client=admin, sql="GRANT manage TO u")
    expect_allowed(client=admin, sql='CREATE ROLE "create"')
    expect_allowed(client=admin, sql="GRANT create TO u")


def test_object_privileges_and_columns(admin):
    expect_allowed(client=admin, sql="CREATE ROLE analyst2")
    expect_allowed(client=admin, sql='GRANT SELECT (email, "Signup TS"), INSERT ON TABLE memory.main.users TO analyst2')
    grants = admin.run(sql="SHOW GRANTS FOR ROLE analyst2").to_pylist()
    assert {(row["privilege"], row["columns"]) for row in grants} == {("SELECT", "email, Signup TS"), ("INSERT", "")}


def test_an_object_named_table(admin):
    # "GRANT SELECT ON table TO r": a table literally named table.
    expect_allowed(client=admin, sql="GRANT SELECT ON table TO r")
    expect_allowed(client=admin, sql="GRANT SELECT ON VIEW v TO r")
    objects = {row["object"] for row in admin.run(sql="SHOW GRANTS FOR ROLE r").to_pylist()}
    assert {"memory.main.table", "memory.main.v"} <= objects, objects


def test_schema_and_catalog_scopes(admin):
    expect_allowed(client=admin, sql="CREATE ROLE loader")
    for sql in ["GRANT ALL PRIVILEGES ON SCHEMA memory.main TO loader",
                "GRANT CREATE SCHEMA, SELECT ON CATALOG memory TO loader",
                "GRANT CREATE TABLE, DROP VIEW ON SCHEMA main TO loader"]:
        expect_allowed(client=admin, sql=sql)
    kinds = {(row["privilege"], row["object_type"]) for row in admin.run(sql="SHOW GRANTS FOR loader").to_pylist()}
    # ALL stands for every table privilege, each kept on its own.
    assert {("SELECT", "SCHEMA"), ("DELETE", "SCHEMA"), ("CREATE SCHEMA", "CATALOG"), ("SELECT", "CATALOG"), ("CREATE TABLE", "SCHEMA"),
            ("DROP VIEW", "SCHEMA")} <= kinds, kinds


def test_revoke_forms(admin):
    expect_allowed(client=admin, sql="CREATE ROLE rv")
    expect_allowed(client=admin, sql="GRANT SELECT ON t TO rv WITH GRANT OPTION")
    expect_allowed(client=admin, sql="REVOKE GRANT OPTION FOR SELECT ON t FROM rv")
    assert [row["grant_option"] for row in admin.run(sql="SHOW GRANTS FOR rv").to_pylist()] == ["NO"]
    expect_allowed(client=admin, sql="GRANT SELECT (a) ON t TO rv")
    expect_allowed(client=admin, sql="REVOKE SELECT (a) ON t FROM rv RESTRICT")


def test_alter_owner_and_show_grants_for(admin):
    expect_allowed(client=admin, sql="CREATE ROLE data_eng")
    expect_allowed(client=admin, sql="ALTER VIEW v OWNER TO ROLE data_eng")
    assert admin.run(sql="SHOW GRANTS FOR USER u").num_rows >= 0


@pytest.mark.parametrize("sql", [
    "GRANT r TO u; DROP TABLE t", "GRANT r TO u;;", "CREATE ROLE a; CREATE ROLE b", "GRANT SELECT ON t TO r; SELECT 1",
    "SHOW GRANTS; ATTACH 'x.db'", "GRANT r TO u /* ok */ ; -- c\n x",
])
def test_a_second_statement_is_rejected(admin, sql):
    assert "one at a time" in error(client=admin, sql=sql), sql
    assert admin.run(sql="SELECT count(*) AS n FROM t").num_rows == 1  # t is still there


def test_a_single_trailing_semicolon_is_fine(admin):
    expect_allowed(client=admin, sql="GRANT r TO u; -- done")


@pytest.mark.parametrize(("sql", "why"), [
    ("GRANT SELECT ON t TO", "expected a user or role name"),
    ("GRANT SELECT t TO r", "expected ON"),
    ("CREATE USER joe2 PASSWORD unquoted", "quoted string"),
    ("CREATE USER 'joe'", "expected a user name"),
    ("CREATE USER joe2 PASSWORD 'x", "unterminated string"),
    ('GRANT r TO "u', "unterminated quoted identifier"),
    ("GRANT r TO u /* never closed", "unterminated comment"),
    ('GRANT r TO ""', "empty quoted identifier"),
    ("GRANT INSERT (a) ON t TO r", "only supported for SELECT"),
    ("GRANT CREATE TABLE ON t TO r", "ON SCHEMA or ON CATALOG"),
    ("GRANT CREATE SCHEMA ON SCHEMA s TO r", "ON CATALOG"),
    ("GRANT analyst ON t TO r", "is not a privilege"),
    # Characters outside the grammar (e.g. $-quoting, backslashes) are refused, never skipped.
    ("CREATE USER joe2 PASSWORD $$x$$", "unexpected character"),
    ("GRANT r TO u\\", "unexpected character"),
    ('GRANT SELECT "ON" t TO r', ""),  # a quoted keyword is a name, not the keyword
])
def test_malformed_input_is_a_clear_error(admin, sql, why):
    with pytest.raises(WarpError) as raised:
        admin.run(sql=sql)
    assert raised.value.code == "sql_error" and raised.value.sqlstate == "42601", str(raised.value)
    assert raised.value.detail.startswith(PREFIX), raised.value.detail  # every error is branded
    assert why in raised.value.detail, raised.value.detail


def test_quoting_preserves_names_exactly(admin):
    expect_allowed(client=admin, sql='CREATE ROLE "Data Team"')
    expect_allowed(client=admin, sql='GRANT SELECT ON "memory"."we""ird"."T 1" TO "Data Team"')
    grant = admin.run(sql='SHOW GRANTS FOR ROLE "Data Team"').to_pylist()[0]
    assert grant["object"] == 'memory."we""ird"."T 1"', grant


def test_keywords_are_case_insensitive(admin):
    expect_allowed(client=admin, sql="gRaNt SeLeCt On T tO r")
