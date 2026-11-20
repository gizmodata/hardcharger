"""What DuckDB runs while it binds a statement, before access control's plan check sees a plan.

Found while porting GizmoSQL 1.42.3's catalog-isolation fixes (2026-11-20): DuckDB evaluates a
table function's arguments, a PRAGMA's parameters, an AT clause, EXECUTE's values and a setting's
value while it binds, and folds the result into a constant, so a nextval() / setval() there moved
the sequence before (and without) the plan check refusing it: a catalog_access token with only read
access set a sequence of its catalog with `SELECT * FROM range(setval('c.main.seq', 11))`. And an
administrator's macro handed the caller's string to query() or a table function, which DuckDB bound
(a remote postgres_query prepare as another catalog's role) before the plan check refused the
expansion. The pre-bind gate now checks each of them as the plan check would, on the parse tree, and
refuses a statement kind it has no rule for; every refusal here has a twin that works.
"""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server

READER_RULES = [{"catalog": "bind", "access": "read"}, {"catalog": "*", "access": "none"}]
WRITER_RULES = [{"catalog": "bind", "access": "write"}, {"catalog": "*", "access": "none"}]


@pytest.fixture(scope="module")
def server(module_launcher):
    server = module_launcher.start(args=["--database", "bind.db"])
    admin = server.admin()
    for sql in ["CREATE SEQUENCE seq", "CREATE TABLE t (x INTEGER)", "INSERT INTO t VALUES (1), (2)",
                "PREPARE nothing AS SELECT 1",
                "CREATE MACRO route_sql(s) AS TABLE SELECT * FROM query(s)",
                "CREATE MACRO route_scalar(s) AS (SELECT count(*) FROM query(s))",
                "CREATE MACRO first_n(n) AS TABLE SELECT * FROM range(n)",
                "CREATE MACRO twice(n) AS TABLE SELECT * FROM first_n(n * 2)"]:
        admin.execute(sql=sql)
    return server


def last_value(server):
    return server.admin().scalar(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq'")


# Each form DuckDB evaluates while binding: a read-only token is refused, and the sequence stays put.
BIND_TIME_FORMS = [
    "SELECT * FROM range(setval('bind.main.seq', 11))",
    "SELECT * FROM range(nextval('bind.main.seq'))",
    "CALL range(setval('bind.main.seq', 12))",
    "EXPLAIN SELECT * FROM range(setval('bind.main.seq', 13))",
    "DESCRIBE SELECT * FROM range(setval('bind.main.seq', 14))",
    "SUMMARIZE SELECT * FROM range(setval('bind.main.seq', 15))",
    "SET VARIABLE v = (SELECT * FROM range(setval('bind.main.seq', 16)) LIMIT 1)",
    "PIVOT (SELECT * FROM range(setval('bind.main.seq', 17))) ON range USING count(*)",
    "PRAGMA table_info(setval('bind.main.seq', 18)::VARCHAR)",
    "SELECT * FROM t AT (VERSION => setval('bind.main.seq', 19))",
    "EXECUTE nothing(setval('bind.main.seq', 20))",
    # Through an administrator's macros: their bodies are bound with the caller's arguments.
    "SELECT * FROM route_sql('SELECT * FROM range(setval(''bind.main.seq'', 21))')",
    "SELECT route_scalar('SELECT * FROM range(setval(''bind.main.seq'', 22))')",
    "SELECT * FROM first_n(setval('bind.main.seq', 23))",
    "SELECT * FROM twice(setval('bind.main.seq', 24))",
]


@pytest.mark.parametrize("sql", BIND_TIME_FORMS)
def test_a_read_only_token_cannot_move_a_sequence_while_binding(server, sql):
    before = last_value(server)
    reader = server.as_role(role="user", subject="reader", catalog_access=READER_RULES)
    if sql.startswith("EXECUTE"):
        reader.execute(sql="PREPARE nothing AS SELECT ?::BIGINT AS v")
    problem = reader.error(sql=sql)
    assert problem["code"] == "permission_denied", problem
    assert "You do not have write access to catalog 'bind'" in problem["detail"], problem
    assert last_value(server) == before


def test_the_writer_twin_moves_it(server):
    writer = server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES)
    assert writer.query(sql="SELECT * FROM range(setval('bind.main.seq', 40))").num_rows == 40
    assert last_value(server) == 40
    assert writer.query(sql="SELECT * FROM first_n(setval('bind.main.seq', 3))").num_rows == 3
    assert last_value(server) == 3
    # currval needs read access only.
    reader = server.as_role(role="user", subject="reader", catalog_access=READER_RULES)
    assert reader.query(sql="SELECT * FROM range(currval('bind.main.seq'))").num_rows == 3


def test_a_sequence_named_by_an_expression_is_refused_while_binding(server):
    writer = server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES)
    problem = writer.error(sql="SELECT * FROM range(nextval('bind.main.' || 'seq'))")
    assert problem["code"] == "permission_denied", problem
    assert "its sequence is not named by a string literal" in problem["detail"], problem


def test_a_macro_hands_its_argument_to_query_only_as_the_caller_could_run_it(server):
    user = server.as_role(role="user", subject="plain")
    # The SQL handed to query() is checked before DuckDB binds it.
    for sql, why in [
        ("SELECT * FROM route_sql('SELECT * FROM postgres_query(''b'', ''select 1'')')",
         "Hardcharger blocked the table function postgres_query"),
        ("SELECT route_scalar('SELECT count(*) FROM duckdb_secrets()')", "duckdb_secrets()"),
        ("SELECT * FROM route_sql('COPY (SELECT 1) TO ''x.csv''')", "COPY"),
        ("SELECT * FROM route_sql(concat('SELECT ', '1'))", "query() with SQL that is not a string literal"),
    ]:
        problem = user.error(sql=sql)
        assert problem["code"] == "permission_denied" and why in problem["detail"], (sql, problem)
    # Twins: what the caller may run itself works through the macro.
    assert user.scalar(sql="SELECT * FROM route_sql('SELECT 42 AS answer')") == 42
    assert user.scalar(sql="SELECT route_scalar('SELECT * FROM range(3)')") == 3
    assert user.query(sql="SELECT * FROM twice(2)").num_rows == 4


def test_pragma_json_execute_serialized_sql_is_the_administrators(server):
    admin = server.admin()
    serialized = admin.scalar(sql="SELECT json_serialize_sql('SELECT x FROM t')").replace("'", "''")
    sql = f"PRAGMA json_execute_serialized_sql('{serialized}')"
    for principal in [server.as_role(role="user", subject="plain"),
                      server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES)]:
        problem = principal.error(sql=sql)
        assert problem["code"] == "permission_denied", problem
        assert "Hardcharger blocked json_execute_serialized_sql()" in problem["detail"], problem
    assert admin.query(sql=sql).num_rows == 2


# ---- Store users: the same calls are held to their grants (USAGE, DROP SEQUENCE) ----

@pytest.fixture(scope="module")
def rbac(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("bind_rbac")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "bind_rbac.duckdb")])
    admin = server.admin()
    for sql in ["CREATE SEQUENCE seq", "CREATE ROLE users", "CREATE USER una", "CREATE USER uri",
                "GRANT users TO uri", "GRANT USAGE ON SEQUENCE main.seq TO users"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def rbac_last_value(rbac):
    rows = rbac.admin_client.run(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq'")
    return rows.column(0).to_pylist()[0]


def test_a_store_user_needs_usage_for_a_sequence_moved_while_binding(rbac):
    una, uri = rbac.user(name="una"), rbac.user(name="uri")
    before = rbac_last_value(rbac)
    expect_blocked(client=una, sql="SELECT * FROM range(nextval('seq'))",
                   why="user una has no USAGE privilege on sequence")
    assert rbac_last_value(rbac) == before
    # Twin: with USAGE it runs (and moves the sequence); setval still needs DROP SEQUENCE.
    expect_allowed(client=uri, sql="SELECT * FROM range(nextval('seq'))")
    assert rbac_last_value(rbac) == (before or 0) + 1
    expect_blocked(client=uri, sql="SELECT * FROM range(setval('seq', 50))", why="may not setval() of sequence")
    expect_allowed(client=rbac.admin_client, sql="GRANT DROP SEQUENCE ON SCHEMA main TO users")
    try:
        expect_allowed(client=uri, sql="SELECT * FROM range(setval('seq', 50))")
        assert rbac_last_value(rbac) == 50
    finally:
        expect_allowed(client=rbac.admin_client, sql="REVOKE DROP SEQUENCE ON SCHEMA main FROM users")
