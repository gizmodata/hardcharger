"""A user may create a view only over what it could SELECT itself.

The view's query is checked when the view is created, as the creator's own SELECT of it
would be: every table and view it reads (views with their owners' rights), the columns it
reads of each, through CTEs, subqueries, set operations and table macros, under the
session's SET ROLE (and scoped token: the same check). Before this, CREATE VIEW checked only CREATE VIEW on
the schema; the body was checked when the view was read, and since metadata follows
privileges the creator (who owns the view) could list the view's columns, and so the
column names of a table it can't read (test_rbac_views.py covers the owner's side)."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

CREATOR_LACKS = "may create a view only over what it can SELECT itself"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("view_creation")
    # Init SQL runs as Hardcharger itself: this view has no owner record, so it reads with
    # the caller's rights.
    init = ("CREATE SCHEMA legacy; CREATE TABLE legacy.base AS SELECT 1 AS id, 'legacy-secret' AS s; "
            "CREATE VIEW legacy.v AS SELECT id, s FROM legacy.base;")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "view_creation.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": init, "HARDCHARGER_PRINT_QUERIES": "false"})
    admin = server.admin()
    for sql in [
        "CREATE TABLE raw_orders (id INT, amount INT, customer VARCHAR, ssn VARCHAR)",
        "INSERT INTO raw_orders VALUES (1, 10, 'ann', '111'), (2, 20, 'bob', '222'), (3, 30, 'cara', '333')",
        "CREATE TABLE secrets (id INT, hidden_column_name VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        "CREATE SCHEMA curated",
        "CREATE VIEW curated.orders_v AS SELECT id, amount, customer FROM main.raw_orders",
        "CREATE VIEW curated.ssn_v AS SELECT id, ssn FROM main.raw_orders",
        "CREATE VIEW curated.secret_v AS SELECT * FROM main.secrets",
        "CREATE VIEW curated.query_v AS SELECT * FROM query('SELECT id, customer FROM main.raw_orders')",
        "CREATE MACRO main.secret_rows() AS TABLE SELECT * FROM main.secrets",
        "CREATE MACRO main.order_rows() AS TABLE SELECT id, amount FROM main.raw_orders",
        "CREATE ROLE builder", "CREATE ROLE reader", "CREATE ROLE analyst",
        "GRANT CREATE VIEW ON SCHEMA main TO builder",
        "GRANT SELECT (id, amount) ON main.raw_orders TO builder",
        "GRANT SELECT ON curated.orders_v TO builder",
        "GRANT SELECT ON curated.query_v TO builder",
        "GRANT SELECT (id) ON curated.ssn_v TO builder",
        "GRANT SELECT ON legacy.v TO builder",
        "GRANT SELECT ON main.secrets TO reader",
        "CREATE USER bob", "CREATE USER rita", "CREATE USER ann",
        "GRANT builder TO bob", "GRANT builder TO rita", "GRANT reader TO rita", "GRANT analyst TO ann",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def bob(server):
    return server.user(name="bob")


def view_exists(admin, name: str) -> bool:
    rows = admin.run(sql=f"SELECT count(*) AS n FROM duckdb_views() WHERE view_name = '{name}'").to_pylist()
    return rows[0]["n"] > 0


def outcome(client, sql: str):
    """('ok', '') or (code, detail): what the user is told."""
    try:
        client.run(sql=sql)
        return ("ok", "")
    except ChargewireError as error:
        return (error.code, error.detail)


def refusal_reason(detail: str) -> str:
    """What a refusal says was missing, without the branding around it."""
    reason = detail.split("blocked this statement: ", 1)[-1]
    reason = reason.split(". Access control on this server", 1)[0]
    return reason.split(" reads more: ", 1)[-1]


def test_a_view_over_a_table_the_creator_cannot_read_is_refused(server, admin, bob):
    error = expect_blocked(client=bob, sql="CREATE VIEW main.peek AS SELECT * FROM secrets", why=CREATOR_LACKS)
    assert "the query of view view_creation.main.peek reads more: user bob has no SELECT privilege on " \
           "table view_creation.main.secrets" in error.detail, error.detail
    assert error.sqlstate == "42501", error.sqlstate
    assert not view_exists(admin=admin, name="peek")
    # So bob's metadata never shows the table's column names.
    columns = bob.run(sql="SELECT column_name FROM duckdb_columns()").to_pylist()
    assert "hidden_column_name" not in str(columns), columns
    # Preparing it is refused the same way.
    with pytest.raises(Exception) as raised:
        bob.prepare(sql="CREATE VIEW main.peek AS SELECT * FROM secrets")
    assert CREATOR_LACKS in str(raised.value), raised.value
    assert not view_exists(admin=admin, name="peek")


def test_a_view_over_what_the_creator_can_read_is_created(server, admin, bob):
    expect_allowed(client=bob, sql="CREATE VIEW main.mine AS SELECT id, amount FROM raw_orders")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.mine").num_rows == 3
    columns = bob.run(sql="SELECT column_name FROM duckdb_columns() WHERE table_name = 'mine'").to_pylist()
    assert [c["column_name"] for c in columns] == ["id", "amount"], columns


def test_the_audit_record_says_what_a_new_views_query_read(server, bob):
    expect_allowed(client=bob, sql="CREATE VIEW main.audited AS SELECT id, amount FROM raw_orders")
    lines = [line for line in server.log().splitlines() if "access control allowed a statement" in line]
    assert lines and "user=bob" in lines[-1], lines[-3:]
    assert "SELECT on table view_creation.main.raw_orders (id, amount)" in lines[-1], lines[-1]
    assert "as the owner of view" not in lines[-1], lines[-1]  # bob's own rights


def test_the_bar_is_what_a_select_of_the_same_query_needs(admin, bob):
    # Each query's CREATE VIEW refusal gives the reason its SELECT refusal gives, so it
    # says nothing about a table the SELECT would not say.
    for query in ["SELECT id, ssn FROM raw_orders", "SELECT id FROM raw_orders WHERE ssn = '111'",
                  "SELECT id FROM raw_orders ORDER BY customer", "SELECT max(ssn) OVER () FROM raw_orders",
                  "SELECT * FROM raw_orders", "SELECT * FROM secrets", "SELECT count(*) FROM secrets",
                  "SELECT ssn FROM curated.ssn_v", "SELECT * FROM curated.secret_v"]:
        selected = expect_blocked(client=bob, sql=query)
        created = expect_blocked(client=bob, sql=f"CREATE VIEW main.bar_v AS {query}", why=CREATOR_LACKS)
        assert refusal_reason(created.detail) == refusal_reason(selected.detail), (query, created.detail, selected.detail)
    assert not view_exists(admin=admin, name="bar_v")
    # A missing table: CREATE VIEW fails exactly as the SELECT does.
    for query in ["SELECT * FROM no_such_table", "SELECT * FROM main.no_such_table"]:
        # (The same message, but for the statement text DuckDB quotes after it.)
        selected = outcome(client=bob, sql=query)
        created = outcome(client=bob, sql=f"CREATE VIEW main.bar_v AS {query}")
        assert selected[0] == "sql_error" and "does not exist" in selected[1], selected
        assert (created[0], created[1].split("\n")[0]) == (selected[0], selected[1].split("\n")[0]), created
    # And what the SELECT allows, the view may read: the columns granted, count(*) over a
    # column grant, a filter on a granted column.
    for i, query in enumerate(["SELECT id FROM raw_orders", "SELECT count(*) FROM raw_orders",
                               "SELECT id FROM raw_orders WHERE amount > 10", "SELECT id FROM curated.ssn_v"]):
        expect_allowed(client=bob, sql=query)
        expect_allowed(client=bob, sql=f"CREATE VIEW main.bar_ok_{i} AS {query}")


@pytest.mark.parametrize("query", [
    "WITH s AS (SELECT * FROM secrets) SELECT * FROM s",
    "WITH s AS MATERIALIZED (SELECT * FROM secrets) SELECT id FROM s",
    "SELECT * FROM (SELECT hidden_column_name FROM secrets) t",
    "SELECT id, (SELECT max(hidden_column_name) FROM secrets) AS x FROM raw_orders",
    "SELECT id FROM raw_orders WHERE id IN (SELECT id FROM secrets)",
    "SELECT id FROM raw_orders WHERE EXISTS (SELECT 1 FROM secrets)",
    "SELECT id FROM raw_orders UNION ALL SELECT id FROM secrets",
    "SELECT r.id FROM raw_orders r JOIN secrets s USING (id)",
    "SELECT r.id, l.x FROM raw_orders r, LATERAL (SELECT hidden_column_name AS x FROM secrets) l",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < (SELECT count(*) FROM secrets)) "
    "SELECT n FROM r",
])
def test_ctes_and_subqueries_in_the_view_are_checked(admin, bob, query):
    expect_blocked(client=bob, sql=f"CREATE VIEW main.nested_q AS {query}", why=CREATOR_LACKS)
    assert not view_exists(admin=admin, name="nested_q")


def test_ctes_and_subqueries_over_what_the_creator_can_read_are_fine(bob):
    expect_allowed(client=bob, sql="CREATE OR REPLACE VIEW main.cte_ok AS "
                                   "WITH r AS (SELECT id, amount FROM raw_orders) "
                                   "SELECT id FROM r WHERE amount IN (SELECT max(amount) FROM raw_orders)")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.cte_ok").to_pylist() == [{"id": 3}]


def test_views_over_views_are_checked_as_a_select_of_them_is(server, admin, bob):
    # A view the creator may read, which reads (with its owner's rights) a column the
    # creator may not: allowed, as SELECT FROM curated.orders_v is.
    expect_allowed(client=bob, sql="CREATE VIEW main.over_admin AS SELECT id, customer FROM curated.orders_v")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.over_admin").num_rows == 3
    # Owner's rights all the way: granted to someone who reads neither table nor view.
    expect_allowed(client=admin, sql="GRANT SELECT ON main.over_admin TO analyst")
    ann = server.user(name="ann")
    assert expect_allowed(client=ann, sql="SELECT customer FROM main.over_admin").num_rows == 3
    expect_blocked(client=ann, sql="SELECT * FROM curated.orders_v", why="no SELECT privilege on view")
    # Column grants on a view hold for a view over it.
    expect_allowed(client=bob, sql="CREATE VIEW main.over_ssn_ok AS SELECT id FROM curated.ssn_v")
    expect_blocked(client=bob, sql="CREATE VIEW main.over_ssn AS SELECT id, ssn FROM curated.ssn_v",
                   why="no SELECT on column(s) ssn of view_creation.curated.ssn_v")
    # A view the creator holds nothing on.
    expect_blocked(client=bob, sql="CREATE VIEW main.over_secret AS SELECT * FROM curated.secret_v",
                   why="no SELECT privilege on view view_creation.curated.secret_v")
    # A view without an owner reads with the caller's rights: so does a view over it.
    expect_blocked(client=bob, sql="CREATE VIEW main.over_legacy AS SELECT * FROM legacy.v",
                   why="which has no owner, so it reads with your rights")
    # Its own view, over its own view.
    expect_allowed(client=bob, sql="CREATE VIEW main.over_mine AS SELECT id FROM main.over_ssn_ok")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.over_mine").num_rows == 3
    for name in ["over_ssn", "over_secret", "over_legacy"]:
        assert not view_exists(admin=admin, name=name), name


def test_create_or_replace_and_temporary_views_are_checked_too(admin, server):
    bob = server.user(name="bob")
    expect_allowed(client=bob, sql="CREATE VIEW main.replace_me AS SELECT id FROM raw_orders")
    expect_blocked(client=bob, sql="CREATE OR REPLACE VIEW main.replace_me AS SELECT * FROM secrets",
                   why=CREATOR_LACKS)
    # The refusal changed nothing.
    assert expect_allowed(client=bob, sql="SELECT * FROM main.replace_me").column_names == ["id"]
    expect_allowed(client=bob, sql="CREATE OR REPLACE VIEW main.replace_me AS SELECT id, amount FROM raw_orders")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.replace_me").column_names == ["id", "amount"]
    # Session-local views list their columns to their session too.
    expect_blocked(client=bob, sql="CREATE TEMP VIEW tv AS SELECT * FROM secrets", why=CREATOR_LACKS)
    expect_blocked(client=bob, sql="CREATE TEMP VIEW tv AS SELECT ssn FROM raw_orders",
                   why="no SELECT on column(s) ssn")
    expect_allowed(client=bob, sql="CREATE TEMP VIEW tv AS SELECT id, amount FROM raw_orders")
    assert expect_allowed(client=bob, sql="SELECT * FROM tv").num_rows == 3
    # IF NOT EXISTS of an existing view creates nothing, and so reads nothing.
    expect_allowed(client=bob, sql="CREATE VIEW IF NOT EXISTS main.replace_me AS SELECT * FROM secrets")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.replace_me").column_names == ["id", "amount"]


def test_a_view_created_without_binding_is_checked_all_the_same(admin, server):
    bob = server.user(name="bob")
    # DuckDB creates a view WITH (defer_binding) without binding its query: under access
    # control it is bound and checked anyway.
    expect_blocked(client=bob, sql="CREATE VIEW main.deferred WITH (defer_binding = true) AS SELECT * FROM secrets",
                   why=CREATOR_LACKS)
    expect_blocked(client=bob, sql="CREATE TEMP VIEW deferred WITH (defer_binding = true) AS SELECT * FROM secrets",
                   why=CREATOR_LACKS)
    assert not view_exists(admin=admin, name="deferred")
    # So one over what doesn't exist yet fails as its SELECT does.
    created = outcome(client=bob, sql="CREATE VIEW main.deferred WITH (defer_binding = true) AS SELECT * FROM later")
    assert created[0] == "sql_error" and "Table with name later does not exist" in created[1], created
    expect_allowed(client=bob, sql="CREATE VIEW main.deferred WITH (defer_binding = true) AS "
                                   "SELECT id FROM raw_orders")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.deferred").num_rows == 3
    # IF NOT EXISTS of an existing temporary view creates (and reads) nothing.
    expect_allowed(client=bob, sql="CREATE TEMP VIEW tv_exists AS SELECT id FROM raw_orders")
    expect_allowed(client=bob, sql="CREATE TEMP VIEW IF NOT EXISTS tv_exists AS SELECT * FROM secrets")
    assert expect_allowed(client=bob, sql="SELECT * FROM tv_exists").column_names == ["id"]


def test_query_and_query_table_in_a_view_stay_the_administrators(admin, bob):
    for sql in ["CREATE VIEW main.q AS SELECT * FROM query('SELECT * FROM secrets')",
                "CREATE VIEW main.q AS SELECT * FROM query('SELECT id FROM raw_orders')",
                "CREATE VIEW main.q AS SELECT * FROM query_table('secrets')"]:
        expect_blocked(client=bob, sql=sql, why="available to the administrator only")
    assert not view_exists(admin=admin, name="q")
    # An administrator's view that uses query(), granted to the creator, is read with the
    # administrator's rights, so a view over it is fine.
    expect_allowed(client=bob, sql="CREATE VIEW main.over_query_v AS SELECT id, customer FROM curated.query_v")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.over_query_v").num_rows == 3


def test_table_macros_in_a_view_are_checked_as_what_they_read(admin, bob):
    expect_blocked(client=bob, sql="CREATE VIEW main.via_macro AS SELECT * FROM secret_rows()", why=CREATOR_LACKS)
    assert not view_exists(admin=admin, name="via_macro")
    expect_allowed(client=bob, sql="CREATE VIEW main.via_macro AS SELECT * FROM order_rows()")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.via_macro").num_rows == 3


def test_set_role_narrows_what_a_view_may_read(server, admin):
    rita = server.user(name="rita")
    expect_allowed(client=rita, sql="SET ROLE builder")
    error = expect_blocked(client=rita, sql="CREATE VIEW main.rita_v AS SELECT * FROM secrets", why=CREATOR_LACKS)
    assert "acts with role builder only (SET ROLE)" in error.detail, error.detail
    expect_allowed(client=rita, sql="RESET ROLE")
    expect_allowed(client=rita, sql="CREATE VIEW main.rita_v AS SELECT * FROM secrets")
    assert expect_allowed(client=rita, sql="SELECT * FROM main.rita_v").num_rows == 1
    expect_allowed(client=admin, sql="DROP VIEW main.rita_v")


def test_a_scoped_token_narrows_what_a_view_may_read(server):
    rita = server.user(name="rita")  # SELECT on secrets, through role reader

    def token(sql: str) -> str:
        rows = expect_allowed(client=rita, sql=sql).to_pylist()
        return rows[0]["token"]

    no_select = server.client(token=token(sql="CREATE SCOPED TOKEN view_no_select ALLOW INSERT"))
    error = expect_blocked(client=no_select, sql="CREATE TEMP VIEW scoped_v AS SELECT * FROM secrets",
                           why=CREATOR_LACKS)
    assert "scoped token view_no_select of user rita" in error.detail, error.detail
    with_select = server.client(token=token(sql="CREATE SCOPED TOKEN view_select ALLOW SELECT"))
    expect_allowed(client=with_select, sql="CREATE TEMP VIEW scoped_v AS SELECT * FROM secrets")
    assert expect_allowed(client=with_select, sql="SELECT * FROM scoped_v").num_rows == 1


def test_an_administrator_still_creates_views_over_anything(admin):
    expect_allowed(client=admin, sql="CREATE VIEW main.admin_peek AS SELECT * FROM secrets")
    assert expect_allowed(client=admin, sql="SELECT * FROM main.admin_peek").num_rows == 1
