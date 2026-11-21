"""What the plan check must see in every operator: a table function's input, and PIVOT's enum.

Review findings 12 and 13 (2026-11-20, both also in 2.13.3). The plan check checked a table
function call but never what was planned beneath it, so a subquery handed to one that takes its
input as rows (`range((SELECT ...))`, `unnest((SELECT list(v) FROM t))`, `generate_series(1,
(SELECT ...))`, `CALL range((SELECT ...))`) read a table the user held nothing on, or moved a
sequence through `(SELECT setval(...))`. And a temporary type named like the enum DuckDB makes
for a PIVOT without an IN list (`__pivot_enum_*`) was let through without its query being
checked, so `CREATE TEMP TYPE __pivot_enum_x AS ENUM (SELECT v FROM secret)` read any column.
Every refusal here has a twin that works: the same statement over a table the user may read.
"""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server

NO_SECRET = "has no SELECT privilege on table app.s.secret"
# Each reads s.secret through a table function's input; s.open is the twin's table.
INPUTS = ["SELECT * FROM unnest((SELECT list(v) FROM s.{t}))",
          "SELECT * FROM range((SELECT count(*) FROM s.{t}))",
          "SELECT * FROM generate_series(1, (SELECT max(n) FROM s.{t}))",
          "CALL range((SELECT count(*) FROM s.{t}))",
          "SELECT count(*) FROM (SELECT * FROM range((SELECT count(*) FROM s.{t})))",
          "WITH c AS (SELECT * FROM unnest((SELECT list(v) FROM s.{t}))) SELECT count(*) FROM c"]


@pytest.fixture(scope="module")
def rbac(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("plan_inputs")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "app.duckdb")])
    admin = server.admin()
    for sql in ["CREATE SCHEMA s", "CREATE SEQUENCE s.seq",
                "CREATE TABLE s.open (v VARCHAR, n INT)", "INSERT INTO s.open VALUES ('open-1', 3)",
                "CREATE TABLE s.secret (v VARCHAR, n INT)",
                "INSERT INTO s.secret SELECT 'top-secret-' || range, 7 FROM range(41)",
                "CREATE ROLE r", "CREATE USER rita", "GRANT r TO rita", "GRANT SELECT ON s.open TO r"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


def last_value(rbac):
    return rbac.admin_client.run(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq'"
                                 ).column(0).to_pylist()[0]


@pytest.mark.rbac_store
@pytest.mark.parametrize("role", ["user", "readonly"])
def test_a_table_functions_input_is_checked_as_any_query(rbac, role):
    rita = rbac.user(name="rita", role=role)
    expect_blocked(client=rita, sql="SELECT count(*) FROM s.secret", why=NO_SECRET)
    for sql in INPUTS:
        if role == "readonly" and sql.startswith("CALL"):
            continue  # a readonly session runs no CALL at all
        expect_blocked(client=rita, sql=sql.format(t="secret"), why=NO_SECRET)
        expect_allowed(client=rita, sql=sql.format(t="open"))  # the twin
    assert rita.run(sql="SELECT * FROM unnest((SELECT list(v) FROM s.open))").column(0).to_pylist() == ["open-1"]


@pytest.mark.rbac_store
def test_a_table_functions_input_moves_no_sequence_without_the_privilege(rbac):
    rita = rbac.user(name="rita")
    before = last_value(rbac)
    for sql, why in [("SELECT setval('s.seq', 5)", "may not setval() of sequence app.s.seq"),
                     ("SELECT * FROM range((SELECT setval('s.seq', 9)))", "may not setval() of sequence app.s.seq"),
                     ("SELECT * FROM range((SELECT nextval('s.seq')))", "has no USAGE privilege on sequence app.s.seq"),
                     ("CALL range((SELECT nextval('s.seq')))", "has no USAGE privilege on sequence app.s.seq"),
                     ("SELECT * FROM generate_series(1, (SELECT nextval('s.seq')))",
                      "has no USAGE privilege on sequence app.s.seq")]:
        expect_blocked(client=rita, sql=sql, why=why)
        assert last_value(rbac) == before, sql
    # The twin: with USAGE the same statement moves it.
    expect_allowed(client=rbac.admin_client, sql="CREATE ROLE seq_users")
    expect_allowed(client=rbac.admin_client, sql="GRANT USAGE ON SEQUENCE s.seq TO seq_users")
    expect_allowed(client=rbac.admin_client, sql="CREATE USER sam")
    expect_allowed(client=rbac.admin_client, sql="GRANT seq_users TO sam")
    expect_allowed(client=rbac.user(name="sam"), sql="SELECT * FROM range((SELECT nextval('s.seq')))")
    assert last_value(rbac) == (before or 0) + 1


@pytest.mark.rbac_store
def test_a_type_named_like_pivots_enum_is_not_a_way_around_select(rbac):
    rita = rbac.user(name="rita")
    for sql in ["CREATE TEMP TYPE __pivot_enum_x AS ENUM (SELECT v FROM s.secret)",
                "CREATE TYPE temp.main.__pivot_enum_y AS ENUM (SELECT v FROM s.secret WHERE n = 7)",
                "CREATE TEMP TYPE __PIVOT_ENUM_Z AS ENUM (SELECT v FROM s.open)",
                "EXPLAIN CREATE TEMP TYPE __pivot_enum_w AS ENUM (SELECT v FROM s.secret)"]:
        expect_blocked(client=rita, sql=sql, why="type names starting with __pivot_enum_ are DuckDB's own, for PIVOT")
    expect_blocked(client=rita, sql="CREATE TEMP TYPE my_enum AS ENUM (SELECT v FROM s.open)",
                   why="creating temporary types needs an administrator")
    # PIVOT's own enum reads the ON column: checked as the user's SELECT, so it is not made
    # (an enum of the secret values left in the session would be readable by its name).
    expect_blocked(client=rita, sql="PIVOT s.secret ON v USING count(*)", why=NO_SECRET)
    enums = rita.run(sql="SELECT type_name FROM duckdb_types() WHERE type_name LIKE '__pivot_enum%'").column(0).to_pylist()
    for name in enums:
        values = rita.run(sql=f'SELECT unnest(enum_range(NULL::"{name}")) AS v').column(0).to_pylist()
        assert not any(str(v).startswith("top-secret") for v in values), (name, values)
    # The twins: PIVOT of a table the user may read, without and with an IN list.
    assert rita.run(sql="PIVOT s.open ON v USING count(*)").column("open-1").to_pylist() == [1]
    assert rita.run(sql="PIVOT s.open ON v IN ('open-1') USING count(*)").column("open-1").to_pylist() == [1]


SEQ_READ_RULES = [{"catalog": "home", "access": "read"}, {"catalog": "*", "access": "none"}]
SEQ_WRITE_RULES = [{"catalog": "home", "access": "write"}, {"catalog": "*", "access": "none"}]


@pytest.fixture(scope="module")
def rules_server(module_launcher):
    server = module_launcher.start(args=["--database", "home.duckdb"])
    admin = server.admin()
    for sql in ["CREATE SEQUENCE seq", "CREATE TABLE t (i INT)", "INSERT INTO t VALUES (1), (2)"]:
        admin.execute(sql=sql)
    return server


def rules_last_value(server):
    return server.admin().scalar(sql="SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq'")


@pytest.mark.parametrize("role, rules", [("user", SEQ_READ_RULES), ("readonly", SEQ_WRITE_RULES)])
def test_a_token_moves_no_sequence_through_a_table_functions_input(rules_server, role, rules):
    session = rules_server.as_role(role=role, subject=f"in_{role}", catalog_access=rules)
    before = rules_last_value(rules_server)
    for sql in ["SELECT * FROM range((SELECT setval('home.main.seq', 9)))",
                "SELECT * FROM range((SELECT nextval('home.main.seq')))",
                "CALL range((SELECT nextval('home.main.seq')))"]:
        detail = session.error(sql=sql)["detail"]
        assert ("You do not have write access to catalog 'home'" in detail
                or "has a readonly session and cannot run statements that modify state" in detail), f"{sql}: {detail}"
        assert rules_last_value(rules_server) == before, sql
    # EXPLAIN only plans (a readonly session may run it): the sequence stays.
    if role == "readonly":
        session.query(sql="EXPLAIN SELECT * FROM range((SELECT nextval('home.main.seq')))")
        assert rules_last_value(rules_server) == before
    # The twins: reading through the input works for both; a writer's token moves the sequence.
    assert session.query(sql="SELECT count(*) AS n FROM range((SELECT count(*) FROM home.main.t))"
                         ).column("n").to_pylist() == [2]
    writer = rules_server.as_role(role="user", subject=f"in_writer_{role}", catalog_access=SEQ_WRITE_RULES)
    writer.query(sql="SELECT * FROM range((SELECT nextval('home.main.seq')))")
    assert rules_last_value(rules_server) == (before or 0) + 1
