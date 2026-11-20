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
                "CREATE MACRO twice(n) AS TABLE SELECT * FROM first_n(n * 2)",
                # Scalar macros that move the sequence (review finding 03).
                "CREATE MACRO bump() AS nextval('bind.main.seq')",
                "CREATE MACRO bump_to(n) AS setval('bind.main.seq', n)",
                "CREATE MACRO ident(x) AS x",
                "CREATE MACRO bump_via(n) AS ident(bump_to(n))",
                # Table macros named like delta_*() / iceberg_*() (review finding 04).
                "CREATE MACRO delta_summary() AS TABLE SELECT 1 AS x",
                "CREATE MACRO iceberg_report(n) AS TABLE SELECT * FROM range(n)",
                # A chain of scalar macros, each calling the one before (depth).
                "CREATE MACRO chain_0(n) AS n",
                *[f"CREATE MACRO chain_{i}(n) AS chain_{i - 1}(n)" for i in range(1, 18)]]:
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


# Through a user's scalar macro: DuckDB expands it in place and evaluates its body while binding.
MACRO_FORMS = [
    "SELECT * FROM range(bump())",
    "SELECT * FROM range(bump_to(77))",
    "SELECT * FROM range(ident(bump_to(78)))",
    "SELECT * FROM range(bump_via(79))",
    "CALL range(bump_to(80))",
    "SELECT * FROM generate_series(1, bump_to(81)::BIGINT)",
    "SELECT * FROM first_n(bump_to(82))",
    "PRAGMA table_info(bump_to(83)::VARCHAR)",
    "EXPLAIN SELECT * FROM range(bump_to(84))",
]
BIND_TIME_FORMS += MACRO_FORMS


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
                "GRANT users TO uri", "GRANT USAGE ON SEQUENCE main.seq TO users",
                # delta_scan / iceberg_scan granted with a location-only storage grant, and with the
                # storage privileges everywhere.
                "CREATE ROLE lakes_at", "CREATE ROLE lakes_all", "CREATE USER loc", "CREATE USER all_over",
                "GRANT lakes_at TO loc", "GRANT lakes_all TO all_over",
                "GRANT EXECUTE ON TABLE FUNCTION delta_scan (PATH ARGUMENT 1) TO lakes_at",
                "GRANT EXECUTE ON TABLE FUNCTION iceberg_scan (PATH ARGUMENT 1) TO lakes_at",
                "GRANT READ OBJECT STORAGE ON LOCATION 's3://lake-bucket/tables/' TO lakes_at",
                "GRANT EXECUTE ON TABLE FUNCTION delta_scan (PATH ARGUMENT 1) TO lakes_all",
                "GRANT EXECUTE ON TABLE FUNCTION iceberg_scan (PATH ARGUMENT 1) TO lakes_all",
                "GRANT READ FILESYSTEM TO lakes_all", "GRANT READ OBJECT STORAGE TO lakes_all",
                "GRANT READ HTTP TO lakes_all",
                # Nothing listens there: the twin's read fails at once, without the network.
                "CREATE SECRET lake_nowhere (TYPE s3, KEY_ID 'k', SECRET 's', ENDPOINT '127.0.0.1:9', USE_SSL false, "
                "URL_STYLE 'path', REGION 'us-east-1')",
                # Review finding 03: a user who may create macros, and holds nothing on the sequence.
                "CREATE ROLE makers", "CREATE USER mack", "GRANT makers TO mack",
                "GRANT CREATE MACRO, DROP MACRO ON SCHEMA main TO makers",
                "CREATE MACRO main.bump_to(n) AS setval('seq', n)",
                # Review finding 04.
                "CREATE MACRO main.delta_summary() AS TABLE SELECT 1 AS x"]:
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


# delta_scan() / iceberg_scan() read, with a storage client of their own, the files their table's
# log or manifest names, wherever they are: a location grant can't bound that (GizmoSQL 1.42.3 refuses
# them to catalog-scoped sessions for the same reason). So a store user needs the storage privileges
# everywhere for them, as for a computed path, whatever path it hands them.
@pytest.mark.parametrize("sql", ["SELECT * FROM delta_scan('s3://lake-bucket/tables/t')",
                                 "SELECT * FROM iceberg_scan('s3://lake-bucket/tables/t')",
                                 "CALL delta_scan('s3://lake-bucket/tables/t')",
                                 "DESCRIBE SELECT * FROM delta_scan('s3://lake-bucket/tables/t')"])
def test_delta_and_iceberg_need_the_storage_privileges_everywhere(rbac, sql):
    expect_blocked(client=rbac.user(name="loc"), sql=sql, why="no location can bound it")
    # The twin: with the privileges everywhere, access control lets it through to DuckDB (which then
    # fails for want of the table, or of the extension, but not for access).
    try:
        rbac.user(name="all_over").run(sql=sql)
    except Exception as error:  # noqa: BLE001 - any failure but access control's
        assert "Hardcharger blocked" not in str(error) and "permission_denied" not in str(error), error


# ---- Review 2026-11-20 ----

READONLY_FORMS = [
    "SELECT * FROM range(setval('bind.main.seq', 200))",
    "SELECT * FROM range(nextval('bind.main.seq'))",
    "SELECT * FROM generate_series(1, setval('bind.main.seq', 400))",
    "EXPLAIN SELECT * FROM range(setval('bind.main.seq', 500))",
    "DESCRIBE SELECT * FROM range(setval('bind.main.seq', 600))",
    "SELECT * FROM range(bump_to(700))",
]


@pytest.mark.parametrize("rules", [WRITER_RULES, None], ids=["write rules", "no rules"])
@pytest.mark.parametrize("sql", READONLY_FORMS)
def test_a_readonly_token_moves_no_sequence_while_binding_whatever_its_rules(server, rules, sql):
    # 02: a readonly principal's session changes nothing; its write rules (or none, which is '*': write)
    # don't make a sequence call DuckDB evaluates while binding a write it may do. EXPLAIN included.
    claims = {} if rules is None else {"catalog_access": rules}
    readonly = server.as_role(role="readonly", subject="ro_w", **claims)
    before = last_value(server)
    problem = readonly.error(sql=sql)
    assert problem["code"] == "permission_denied", problem
    assert "has a readonly session and cannot run statements that modify state" in problem["detail"], problem
    assert last_value(server) == before


def test_a_readonly_token_reads_a_sequence_while_binding(server):
    # The twin: currval() changes nothing.
    server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES).execute(
        sql="SELECT * FROM range(nextval('bind.main.seq'))")
    before = last_value(server)
    readonly = server.as_role(role="readonly", subject="ro_w", catalog_access=WRITER_RULES)
    assert readonly.query(sql="SELECT * FROM range(currval('bind.main.seq'))").num_rows == before
    assert last_value(server) == before


def test_a_scalar_macro_moves_the_sequence_for_whoever_may(server):
    # 03's twin: with write access the same macro forms run, and move the sequence.
    writer = server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES)
    assert writer.query(sql="SELECT * FROM range(bump_to(5))").num_rows == 5
    assert last_value(server) == 5
    assert writer.query(sql="SELECT * FROM range(bump_via(6))").num_rows == 6
    assert last_value(server) == 6


def test_macros_nested_too_deep_fail_closed_while_binding(server):
    writer = server.as_role(role="user", subject="writer", catalog_access=WRITER_RULES)
    problem = writer.error(sql="SELECT * FROM range(chain_17(3))")
    assert problem["code"] == "permission_denied", problem
    assert "macros that call macros more than 16 deep" in problem["detail"], problem
    assert writer.query(sql="SELECT * FROM range(chain_3(3))").num_rows == 3  # the twin


@pytest.mark.parametrize("who", ["token without rules", "catalog_access token"])
def test_table_macros_named_like_delta_or_iceberg_run_as_macros(server, who):
    # 04: a user's table macro is a macro, whatever its name; only DuckDB's delta_*() / iceberg_*()
    # functions need the storage privileges everywhere.
    claims = {} if who == "token without rules" else {"catalog_access": WRITER_RULES}
    session = server.as_role(role="user", subject="macro_user", **claims)
    assert session.query(sql="SELECT * FROM delta_summary()").to_pylist() == [{"x": 1}]
    assert session.query(sql="SELECT * FROM iceberg_report(2)").num_rows == 2
    assert session.query(sql="CALL iceberg_report(3)").num_rows == 3
    problem = session.error(sql="SELECT * FROM delta_scan('s3://lake-bucket/tables/t')")
    assert problem["code"] == "permission_denied" and "delta_scan" in problem["detail"], problem


def test_unit_gate_refuses_a_statement_kind_without_a_rule():
    # 10 (mutation M1): GatedCategory's refusal of a kind it has no rule for, which no SQL reaches.
    from conftest import run_unit_tests
    results = run_unit_tests(gtest_filter="AuthzGate.*")
    assert results == {"AuthzGate.AStatementKindWithoutARuleIsRefused": True,
                       "AuthzGate.KindsLeftToThePlanCheckAreNotRefusedForTheirKind": True}, results


def test_a_store_user_moves_no_sequence_through_a_scalar_macro(rbac):
    # 03: mack may create macros, and holds no USAGE / DROP SEQUENCE on seq.
    mack, uri = rbac.user(name="mack"), rbac.user(name="uri")
    expect_allowed(client=mack, sql="CREATE OR REPLACE MACRO main.mine_to(n) AS setval('seq', n)")
    expect_allowed(client=mack, sql="CREATE OR REPLACE MACRO main.mine_next() AS nextval('seq')")
    before = rbac_last_value(rbac)
    for sql, why in [("SELECT * FROM range(main.mine_to(501))", "may not setval() of sequence"),
                     ("SELECT * FROM range(main.mine_next())", "has no USAGE privilege on sequence"),
                     ("SELECT * FROM range(main.bump_to(77))", "may not setval() of sequence")]:
        expect_blocked(client=mack, sql=sql, why=why)
        assert rbac_last_value(rbac) == before, sql
    # The twin: uri holds USAGE, so nextval() through the macro runs (and moves the sequence).
    expect_allowed(client=uri, sql="SELECT * FROM range(main.mine_next())")
    assert rbac_last_value(rbac) == (before or 0) + 1


def test_a_store_users_table_macro_named_like_delta_runs(rbac):
    # 04: a user's table macro named delta_* is a macro (v2.13.3 ran it), not delta_scan().
    rows = rbac.user(name="una").run(sql="SELECT * FROM main.delta_summary()")
    assert rows.to_pylist() == [{"x": 1}]
