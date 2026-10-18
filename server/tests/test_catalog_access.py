"""Port of GizmoSQL 1.x tests/integration/test_catalog_access.cpp (42 cases).

catalog_access rules in a sign-in token: a list of
{"catalog": pattern, "access": "read" | "write" | "none"}, first match wins, no match is
no access; '*' and '?' are globs. Access control's plan check enforces them on every
statement (reads and writes per catalog, temp included) and filters what the token's
user sees (SHOW DATABASES, information_schema, duckdb_*(), the _hardcharger_system views,
the metadata requests); the server's own catalogs (instrumentation) stay the
administrator's whatever the rules say. 1.x ignored the rules without its license
feature; Hardcharger has no license and enforces them on every server.
DuckDB 2.0's SHOW DATABASES and catalogs metadata leave out system and temp for everyone,
so the "always sees system and temp" checks read duckdb_databases().

1.x's Flight SQL calls map to Warp SQL: GetCatalogs, GetDbSchemas and GetTables are the
catalogs, db-schemas and tables metadata requests; SetSessionOptions is the session's
options. Refusals read as in 1.x ("Access denied: You do not have write access to
catalog ...").
"""

import pytest


DEFAULT = "catalog_access_test"
VISIBLE_ALWAYS = {"system", "temp", "_hardcharger_system"}


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--database", f"{DEFAULT}.db", "--enable-instrumentation"])


@pytest.fixture(scope="module")
def admin(server):
    session = server.admin()
    for sql in ["CREATE TABLE IF NOT EXISTS readonly_test (id INTEGER)",
                "CREATE TABLE IF NOT EXISTS noaccess_test (id INTEGER)",
                "CREATE TABLE IF NOT EXISTS visibility_test (id INTEGER)",
                "ATTACH IF NOT EXISTS ':memory:' AS prod_alpha", "ATTACH IF NOT EXISTS ':memory:' AS staging_beta",
                "ATTACH IF NOT EXISTS ':memory:' AS hidden_meta_cat",
                "CREATE TABLE IF NOT EXISTS hidden_meta_cat.main.secret_t (id INTEGER)",
                "ATTACH IF NOT EXISTS ':memory:' AS other_cat", "CREATE SCHEMA IF NOT EXISTS other_cat.s1",
                "CREATE OR REPLACE TABLE pivot_src AS SELECT * FROM (VALUES ('a', 1), ('b', 2), ('a', 3)) t(k, v)"]:
        session.execute(sql=sql)
    return session


def user(server, name: str, rules=None, role: str = "user"):
    claims = {} if rules is None else {"catalog_access": rules}
    return server.as_role(role=role, subject=name, **claims)


def read_only_default():
    return [{"catalog": DEFAULT, "access": "read"}, {"catalog": "*", "access": "none"}]


def denied(session, sql: str, why: str = "Access denied") -> str:
    detail = session.error(sql=sql)["detail"]
    assert why in detail, f"{sql}: {detail}"
    return detail


def catalogs_of(session, sql: str) -> set[str]:
    return set(session.query(sql=sql).column(0).to_pylist())


# ---- The administrator and tokens without rules ----

def test_system_user_has_full_access(admin):
    admin.execute(sql="CREATE TABLE sys_test_table (id INTEGER)")
    admin.query(sql="SELECT * FROM sys_test_table")
    admin.execute(sql="DROP TABLE sys_test_table")


def test_system_user_can_read_instrumentation(admin):
    admin.query(sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")


def test_system_user_cannot_write_instrumentation(admin):
    assert "read-only" in admin.error(sql="DELETE FROM _hardcharger_instr.sessions WHERE 1=0")["detail"]


def test_token_without_catalog_access_has_full_access(server, admin):
    session = user(server=server, name="test_user")
    session.execute(sql="CREATE TABLE test_no_rules (id INTEGER)")
    session.query(sql="SELECT * FROM test_no_rules")
    session.execute(sql="DROP TABLE test_no_rules")


# ---- Rules ----

def test_token_with_read_only_access_cannot_write(server, admin):
    session = user(server=server, name="readonly_user", rules=[{"catalog": DEFAULT, "access": "read"}])
    session.query(sql="SELECT * FROM readonly_test")
    denied(session=session, sql="INSERT INTO readonly_test VALUES (1)",
           why=f"You do not have write access to catalog '{DEFAULT}'")


def test_token_with_no_access_cannot_read(server, admin):
    session = user(server=server, name="noaccess_user", rules=[{"catalog": DEFAULT, "access": "none"}])
    denied(session=session, sql="SELECT * FROM noaccess_test", why="read access to catalog")


def test_token_with_write_access_can_read_and_write(server, admin):
    session = user(server=server, name="write_user", rules=[{"catalog": DEFAULT, "access": "write"}])
    session.execute(sql="CREATE TABLE write_access_test (id INTEGER)")
    session.query(sql="SELECT * FROM write_access_test")
    session.execute(sql="DROP TABLE write_access_test")


def test_wildcard_read_only_access_denies_write(server, admin):
    session = user(server=server, name="wildcard_readonly", rules=[{"catalog": "*", "access": "read"}])
    denied(session=session, sql="CREATE TABLE wildcard_test (id INTEGER)")


def test_wildcard_write_access_allows_all(server, admin):
    session = user(server=server, name="wildcard_write", rules=[{"catalog": "*", "access": "write"}])
    session.execute(sql="CREATE TABLE wildcard_write_test (id INTEGER)")
    session.query(sql="SELECT * FROM wildcard_write_test")
    session.execute(sql="DROP TABLE wildcard_write_test")


def test_first_match_wins_specific_before_wildcard(server, admin):
    session = user(server=server, name="first_match_user",
                   rules=[{"catalog": DEFAULT, "access": "write"}, {"catalog": "*", "access": "read"}])
    session.execute(sql="CREATE TABLE first_match_test (id INTEGER)")
    session.execute(sql="DROP TABLE first_match_test")


def test_first_match_wins_wildcard_denies_specific_after(server, admin):
    session = user(server=server, name="wildcard_first_user",
                   rules=[{"catalog": "*", "access": "read"}, {"catalog": DEFAULT, "access": "write"}])
    denied(session=session, sql="CREATE TABLE wildcard_first_test (id INTEGER)")


# ---- The instrumentation catalog ----

def test_non_admin_cannot_read_instrumentation(server, admin):
    session = user(server=server, name="non_admin_user", rules=[{"catalog": "*", "access": "write"}])
    denied(session=session, sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")


def test_an_unrestricted_admin_reads_instrumentation(server, admin):
    # 1.x let an admin token with rules read it; Hardcharger does not (a restricted admin's
    # read would leak every session's SQL, so it is hidden — see test_catalog_access_admin.py).
    # An admin token without rules reads it, like the password administrator.
    session = user(server=server, name="admin_norules", role="admin")
    session.query(sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")


def test_admin_without_rules_cannot_write_instrumentation(server, admin):
    session = user(server=server, name="admin_writer", role="admin")
    denied(session=session, sql="DELETE FROM _hardcharger_instr.sessions WHERE 1=0", why="read-only")


def test_instrumentation_protection_ignores_token_rules(server, admin):
    session = user(server=server, name="instr_hacker", rules=[{"catalog": "_hardcharger_instr", "access": "write"}])
    denied(session=session, sql="SELECT * FROM _hardcharger_instr.sessions LIMIT 1")
    denied(session=session, sql="DELETE FROM _hardcharger_instr.sessions WHERE 1=0")


# ---- What the user sees ----

def test_show_databases_filters_unauthorized_catalogs(server, admin):
    session = user(server=server, name="visibility_user", rules=read_only_default())
    names = catalogs_of(session=session, sql="SHOW DATABASES")
    assert DEFAULT in names, names
    assert not names & {"prod_alpha", "staging_beta", "hidden_meta_cat", "other_cat", "_hardcharger_instr"}, names
    # DuckDB 2.0's SHOW DATABASES leaves out system and temp for everyone; they stay listed
    # where DuckDB lists them.
    assert {"system", "temp"} <= catalogs_of(session=session, sql="SELECT database_name FROM duckdb_databases()")


def test_show_schemas_filters_unauthorized_catalogs(server, admin):
    session = user(server=server, name="show_schemas_user", rules=read_only_default())
    names = catalogs_of(session=session, sql="SHOW SCHEMAS")
    assert names <= {DEFAULT} | VISIBLE_ALWAYS and DEFAULT in names, names


def test_use_unauthorized_catalog_is_denied(server, admin):
    session = user(server=server, name="use_denied_user", rules=read_only_default())
    denied(session=session, sql="USE _hardcharger_instr")


def test_use_unauthorized_regular_catalog_is_denied(server, admin):
    session = user(server=server, name="use_regular_denied_user", rules=[{"catalog": "*", "access": "none"}])
    denied(session=session, sql=f"USE {DEFAULT}", why="read access to catalog")


def test_use_quoted_unauthorized_catalog_is_denied(server, admin):
    session = user(server=server, name="use_quoted_denied_user", rules=[{"catalog": "*", "access": "none"}])
    denied(session=session, sql=f'USE "{DEFAULT}"', why="read access to catalog")


def test_wildcard_prefix_grants_and_denies_by_pattern(server, admin):
    session = user(server=server, name="wildcard_prefix_user",
                   rules=[{"catalog": "prod_*", "access": "write"}, {"catalog": "*", "access": "none"}])
    session.execute(sql="CREATE TABLE prod_alpha.wildcard_t (id INTEGER)")
    denied(session=session, sql="CREATE TABLE staging_beta.wildcard_t (id INTEGER)")
    names = catalogs_of(session=session, sql="SHOW DATABASES")
    assert "prod_alpha" in names and "staging_beta" not in names, names


def test_set_session_options_unauthorized_catalog_is_denied(server, admin):
    session = user(server=server, name="set_catalog_denied_user", rules=[{"catalog": "*", "access": "none"}])
    assert session.scalar(sql="SELECT 1") == 1
    response = session.set_options(options={"catalog": DEFAULT})
    assert response.status_code >= 400 or "catalog" in response.json().get("errors", {}), response.text
    assert "read access to catalog" in response.text, response.text


@pytest.mark.parametrize("sql", [
    "SELECT table_catalog, table_name FROM information_schema.tables",
    "SELECT table_catalog, table_name FROM system.information_schema.tables",
    'SELECT table_catalog, table_name FROM "information_schema"."tables"',
    'SELECT DISTINCT table_catalog FROM "information_schema"."tables" ORDER BY 1',
    'SELECT table_catalog, table_name FROM "system"."information_schema"."tables"',
    'SELECT DISTINCT table_catalog FROM "system"."information_schema"."tables" ORDER BY 1',
    "SELECT database_name, table_name FROM duckdb_tables()",
    'SELECT "TABLE_CAT" FROM _hardcharger_system.main.hardcharger_index_info',
    'SELECT "TABLE_CAT" FROM _hardcharger_system.main.hardcharger_view_definition',
], ids=["information_schema", "catalog_qualified", "quoted", "quoted_order_by", "quoted_catalog_qualified",
        "quoted_catalog_qualified_order_by", "duckdb_tables", "hardcharger_index_info", "hardcharger_view_definition"])
def test_catalog_functions_filter_unauthorized_catalogs(server, admin, sql):
    """1.x's seven information_schema / duckdb_tables cases and the two
    _hardcharger_system view cases."""
    admin.execute(sql="CREATE OR REPLACE VIEW other_cat.main.v AS SELECT 1 AS x")
    admin.execute(sql="CREATE TABLE IF NOT EXISTS other_cat.main.t (id INTEGER PRIMARY KEY)")
    session = user(server=server, name="visibility_user2", rules=read_only_default())
    names = catalogs_of(session=session, sql=sql)
    assert names <= {DEFAULT} | VISIBLE_ALWAYS, f"{sql}: {names}"
    # The administrator sees the other catalogs there, so the filter is what hid them.
    assert catalogs_of(session=admin, sql=sql) - ({DEFAULT} | VISIBLE_ALWAYS), sql


def test_wildcard_none_only_shows_system_and_temp(server, admin):
    session = user(server=server, name="none_user", rules=[{"catalog": "*", "access": "none"}])
    names = catalogs_of(session=session, sql="SHOW DATABASES")
    assert names <= VISIBLE_ALWAYS, names
    assert catalogs_of(session=session, sql="SELECT database_name FROM duckdb_databases()") == VISIBLE_ALWAYS


def test_wildcard_read_shows_all_catalogs(server, admin):
    session = user(server=server, name="read_all_user", rules=[{"catalog": "*", "access": "read"}])
    names = catalogs_of(session=session, sql="SHOW DATABASES")
    assert {DEFAULT, "prod_alpha"} <= names, names
    assert "_hardcharger_instr" not in names, names  # the server's own, whatever the rules


def test_no_catalog_access_means_no_filtering(server, admin):
    session = user(server=server, name="no_rules_user")
    assert {DEFAULT, "prod_alpha", "other_cat"} <= catalogs_of(session=session, sql="SHOW DATABASES")


def test_catalog_access_rules_apply_on_every_server(server, admin, launcher):
    # 1.x ignored the rules without its license feature; Hardcharger enforces them on any
    # server, here one started with no options but its database.
    restricted = user(server=server, name="core_user", rules=[{"catalog": "*", "access": "none"}])
    assert DEFAULT not in catalogs_of(session=restricted, sql="SHOW DATABASES")
    plain = launcher.start(args=["--database", "core.db"])
    session = user(server=plain, name="core_user", rules=[{"catalog": "*", "access": "none"}])
    assert "core" not in catalogs_of(session=session, sql="SHOW DATABASES")
    open_user = user(server=plain, name="open_user")
    assert "core" in catalogs_of(session=open_user, sql="SHOW DATABASES")


def test_db_schemas_and_tables_requests_respect_filtering(server, admin):
    session = user(server=server, name="meta_rpc_filter_user", rules=read_only_default())
    assert session.metadata(kind="db-schemas", catalog="hidden_meta_cat").num_rows == 0
    assert session.metadata(kind="db-schemas", catalog=DEFAULT).num_rows > 0
    assert session.metadata(kind="tables", catalog="hidden_meta_cat").num_rows == 0
    tables = session.metadata(kind="tables").to_pylist()
    assert all(t["catalog_name"] != "hidden_meta_cat" for t in tables), tables
    assert all(t["table_name"] != "secret_t" for t in tables), tables


def test_db_schemas_without_catalog_lists_all_catalogs(admin):
    admin.execute(sql=f"USE {DEFAULT}")
    pairs = {(r["catalog_name"], r["db_schema_name"]) for r in admin.metadata(kind="db-schemas").to_pylist()}
    assert {("other_cat", "s1"), ("other_cat", "main"), (DEFAULT, "main")} <= pairs, pairs
    assert all(c == "other_cat" for c, _ in
               {(r["catalog_name"], r["db_schema_name"])
                for r in admin.metadata(kind="db-schemas", catalog="other_cat").to_pylist()})


def test_catalogs_request_respects_filtering(server, admin):
    session = user(server=server, name="rpc_filter_user", rules=read_only_default())
    names = set(session.metadata(kind="catalogs").column(0).to_pylist())
    assert DEFAULT in names and names <= {DEFAULT} | VISIBLE_ALWAYS, names
    assert {"prod_alpha", "other_cat"} <= set(admin.metadata(kind="catalogs").column(0).to_pylist())


# ---- Embedded queries (adversarial review, finding 1) ----

def test_set_variable_subquery_cannot_read_a_denied_catalog(server, admin):
    # A rules-only (non-admin) user: SET VARIABLE's value is a query, and getvariable()
    # hands it to anything. It must be plan-checked against the rules, not skipped.
    admin.execute(sql="CREATE OR REPLACE TABLE noaccess_test_secret AS SELECT 'leak-me' AS pw")
    session = user(server=server, name="setvar_user", rules=[{"catalog": DEFAULT, "access": "none"}])
    denied(session=session,
           sql="SET VARIABLE stolen = (SELECT string_agg(pw, ',') FROM noaccess_test_secret)",
           why="read access to catalog")
    assert session.scalar(sql="SELECT getvariable('stolen')") is None
    # The positive twin: a constant works, and getvariable() returns it.
    session.execute(sql="SET VARIABLE ok = 5")
    assert session.scalar(sql="SELECT getvariable('ok')") == 5


# ---- Batches and read-only principals ----

def test_multi_statement_batch_cannot_bypass_read_only_catalog_access(server, admin):
    admin.execute(sql="CREATE OR REPLACE TABLE ro_multi (id INTEGER)")
    session = user(server=server, name="readonly_user", rules=[{"catalog": DEFAULT, "access": "read"}])
    session.query(sql="SELECT 1; SELECT count(*) FROM ro_multi")
    for sql in ["SELECT 1; INSERT INTO ro_multi VALUES (2)", "INSERT INTO ro_multi VALUES (3); SELECT 1"]:
        denied(session=session, sql=sql, why="You do not have write access to catalog")
    assert admin.scalar(sql="SELECT count(*) FROM ro_multi") == 0


def test_multi_statement_batch_cannot_bypass_readonly_role(server, admin):
    admin.execute(sql="CREATE OR REPLACE TABLE ro_role_multi (id INTEGER)")
    session = user(server=server, name="readonly_role_user", role="readonly")
    session.query(sql="SELECT 1; SELECT 2")
    denied(session=session, sql="SELECT 1; INSERT INTO ro_role_multi VALUES (1)",
           why="has a readonly session and cannot run statements that modify state")
    assert admin.scalar(sql="SELECT count(*) FROM ro_role_multi") == 0


def test_multi_statement_batch_runs_exactly_once(admin):
    admin.execute(sql="CREATE OR REPLACE TABLE once_multi (id INTEGER)")
    admin.execute(sql="SELECT 1; INSERT INTO once_multi VALUES (1)")
    assert admin.scalar(sql="SELECT count(*) FROM once_multi") == 1
    admin.execute(sql="INSERT INTO once_multi VALUES (2); SELECT count(*) FROM once_multi")
    assert admin.scalar(sql="SELECT count(*) FROM once_multi") == 2


def test_read_only_principals_can_run_pivot(server, admin):
    for session in [user(server=server, name="readonly_user", rules=[{"catalog": DEFAULT, "access": "read"}]),
                    user(server=server, name="readonly_role_user", role="readonly")]:
        assert session.query(sql="PIVOT pivot_src ON k USING sum(v)").num_rows == 1
        for sql in ["SELECT 1; CREATE TEMPORARY TABLE pivot_tmp AS SELECT 1 AS x",
                    "SELECT 1; CREATE TEMPORARY TYPE pivot_mood AS ENUM ('a', 'b')"]:
            detail = session.error(sql=sql)["detail"]
            # Hardcharger refuses a non-admin CREATE TEMPORARY TABLE/TYPE either as a temp
            # write it lacks or (for a macro/type, whose body runs unchecked) as needing an
            # administrator; 1.x gave only the first two. Any of them refuses it.
            assert ("cannot run statements that modify state" in detail
                    or "do not have write access to catalog 'temp'" in detail
                    or "needs an administrator" in detail), f"{sql}: {detail}"
