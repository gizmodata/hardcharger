"""Views read with their owner's rights, so users can be given a view without the
tables under it."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server

OWNER_LACKS = "its owner lacks a privilege on an object the view reads"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("views")
    parquet = work / "answer.parquet"
    # Init SQL runs as Hardcharger itself, so this view gets no owner record: it stands
    # for a view made before access control was turned on.
    init = ("CREATE SCHEMA legacy; CREATE TABLE legacy.base AS SELECT 1 AS id, 'legacy-secret' AS s; "
            "CREATE VIEW legacy.v AS SELECT id, s FROM legacy.base;")
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "rbac_views.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": init, "HARDCHARGER_PRINT_QUERIES": "false"})
    admin = server.admin()
    for sql in [
        "CREATE TABLE raw_orders (id INT, amount INT, customer VARCHAR, ssn VARCHAR)",
        "INSERT INTO raw_orders VALUES (1, 10, 'ann', '111'), (2, 20, 'bob', '222'), (3, 30, 'cara', '333')",
        "CREATE TABLE secrets (id INT, value VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        "CREATE TABLE allowed (tenant VARCHAR)",
        "INSERT INTO allowed VALUES ('ann')",
        f"COPY (SELECT 42 AS answer) TO '{parquet}' (FORMAT parquet)",
        "CREATE SCHEMA curated",
        "CREATE VIEW curated.orders_v AS SELECT id, amount, customer FROM main.raw_orders",
        "CREATE VIEW curated.ssn_v AS SELECT id, ssn FROM main.raw_orders",
        "CREATE VIEW curated.nested_v AS SELECT id, amount FROM curated.orders_v",
        # `allowed` is unqualified on purpose: a session's temporary table of that name
        # would be found first.
        "CREATE VIEW curated.tenant_v AS SELECT id, customer FROM main.raw_orders "
        "WHERE customer IN (SELECT tenant FROM allowed)",
        f"CREATE VIEW curated.file_v AS SELECT * FROM read_parquet('{parquet}')",
        "CREATE ROLE analyst", "CREATE ROLE builder", "CREATE ROLE publisher",
        "GRANT SELECT ON curated.orders_v TO analyst",
        "GRANT SELECT ON curated.nested_v TO analyst",
        "GRANT SELECT ON curated.tenant_v TO analyst",
        "GRANT SELECT ON curated.file_v TO analyst",
        "GRANT SELECT (id) ON curated.ssn_v TO analyst",
        "GRANT CREATE VIEW ON SCHEMA main TO builder",
        "GRANT SELECT (id, amount) ON main.raw_orders TO builder",
        "CREATE USER ann", "CREATE USER bob", "CREATE USER dan",
        "GRANT analyst TO ann", "GRANT builder TO bob",
    ]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    server.parquet = parquet
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def ann(server):
    return server.user(name="ann")


@pytest.fixture
def bob(server):
    return server.user(name="bob")


@pytest.fixture
def dan(server):
    return server.user(name="dan")


def test_an_administrators_view_publishes_what_it_selects(ann):
    assert expect_allowed(client=ann, sql="SELECT * FROM curated.orders_v").num_rows == 3
    assert expect_allowed(client=ann, sql="SELECT customer, sum(amount) FROM curated.orders_v GROUP BY 1").num_rows == 3
    # BI tools wrap everything in a subquery.
    assert expect_allowed(client=ann, sql="SELECT * FROM (SELECT * FROM curated.orders_v) t LIMIT 100").num_rows == 3
    expect_blocked(client=ann, sql="SELECT * FROM raw_orders", why="no SELECT privilege on table")


def test_the_caller_still_needs_select_on_the_view(ann, dan):
    expect_blocked(client=dan, sql="SELECT * FROM curated.orders_v", why="no SELECT privilege on view")
    expect_blocked(client=dan, sql="SELECT * FROM (SELECT id FROM curated.orders_v) t",
                   why="no SELECT privilege on view")
    assert expect_allowed(client=ann, sql="SELECT id FROM curated.orders_v").num_rows == 3


def test_nested_views_check_each_hop_against_its_owner(admin, ann, bob):
    assert expect_allowed(client=ann, sql="SELECT * FROM curated.nested_v").num_rows == 3
    # nested_v is readable without SELECT on orders_v, which it reads...
    expect_allowed(client=admin, sql="REVOKE SELECT ON curated.orders_v FROM analyst")
    assert expect_allowed(client=ann, sql="SELECT * FROM curated.nested_v").num_rows == 3
    # ...but naming orders_v directly is the caller's own reference.
    expect_blocked(client=ann, sql="SELECT * FROM curated.orders_v", why="no SELECT privilege on view")
    expect_allowed(client=admin, sql="GRANT SELECT ON curated.orders_v TO analyst")
    expect_blocked(client=bob, sql="SELECT * FROM curated.nested_v", why="no SELECT privilege on view")


def test_column_grants_on_a_view_cover_every_use_of_a_column(ann):
    assert expect_allowed(client=ann, sql="SELECT id FROM curated.ssn_v").num_rows == 3
    expect_allowed(client=ann, sql="SELECT count(*) FROM curated.ssn_v")
    ssn = "no SELECT on column(s) ssn"
    for sql in ["SELECT ssn FROM curated.ssn_v", "SELECT * FROM curated.ssn_v",
                "SELECT id FROM curated.ssn_v WHERE ssn = '111'", "SELECT id FROM curated.ssn_v ORDER BY ssn",
                "SELECT id, length(ssn) FROM curated.ssn_v", "SELECT * FROM (SELECT ssn FROM curated.ssn_v) t",
                "SELECT id FROM curated.ssn_v UNION ALL SELECT ssn::INT FROM curated.ssn_v",
                "SELECT a.id FROM curated.ssn_v a JOIN curated.ssn_v b ON a.ssn = b.ssn",
                "SELECT max(ssn) OVER () FROM curated.ssn_v",
                # Renaming the view's columns in FROM does not rename the grant: "id" here
                # is the view's ssn column.
                "SELECT id FROM curated.ssn_v AS x(other, id)"]:
        expect_blocked(client=ann, sql=sql, why=ssn)
    assert expect_allowed(client=ann, sql="SELECT renamed FROM curated.ssn_v AS x(renamed)").num_rows == 3


def test_a_direct_reference_beside_a_view_is_checked_against_the_caller(ann):
    raw = "no SELECT privilege on table"
    for sql in ["SELECT * FROM curated.orders_v v JOIN raw_orders r USING (id)",
                "SELECT (SELECT max(ssn) FROM raw_orders), * FROM curated.orders_v",
                "SELECT * FROM curated.orders_v WHERE id IN (SELECT id FROM secrets)",
                "WITH r AS (SELECT * FROM raw_orders) SELECT * FROM curated.orders_v, r",
                "SELECT * FROM curated.orders_v UNION ALL SELECT id, amount, ssn FROM raw_orders"]:
        expect_blocked(client=ann, sql=sql, why=raw)
    assert expect_allowed(client=ann, sql="SELECT * FROM curated.orders_v v JOIN curated.nested_v n USING (id)"
                          ).num_rows == 3


def test_a_view_cannot_reach_what_its_owner_cannot(admin, ann, bob):
    # bob may create views in main and read two columns of raw_orders.
    expect_allowed(client=bob, sql="CREATE VIEW main.bob_ok AS SELECT id, amount FROM raw_orders")
    expect_allowed(client=bob, sql="CREATE VIEW main.bob_ssn AS SELECT id, ssn FROM raw_orders")
    expect_allowed(client=bob, sql="CREATE VIEW main.bob_secret AS SELECT * FROM secrets")
    assert expect_allowed(client=bob, sql="SELECT * FROM main.bob_ok").num_rows == 3  # he owns it
    expect_blocked(client=bob, sql="SELECT * FROM main.bob_ssn", why=OWNER_LACKS)
    error = expect_blocked(client=bob, sql="SELECT * FROM main.bob_secret", why=OWNER_LACKS)
    assert "secrets" not in error.detail, f"the refusal named what the view reads: {error.detail}"
    # Granting the view does not lend the reader more than the owner has.
    expect_allowed(client=admin, sql="GRANT SELECT ON main.bob_ok TO analyst")
    expect_allowed(client=admin, sql="GRANT SELECT ON main.bob_secret TO analyst")
    assert expect_allowed(client=ann, sql="SELECT * FROM main.bob_ok").num_rows == 3
    expect_blocked(client=ann, sql="SELECT * FROM main.bob_secret", why=OWNER_LACKS)
    expect_blocked(client=ann, sql="SELECT * FROM raw_orders", why="no SELECT privilege on table")


def test_the_owners_current_rights_apply(admin, ann, bob):
    expect_allowed(client=bob, sql="CREATE VIEW main.bob_now AS SELECT id, amount FROM raw_orders")
    expect_allowed(client=admin, sql="GRANT SELECT ON main.bob_now TO analyst")
    assert expect_allowed(client=ann, sql="SELECT * FROM main.bob_now").num_rows == 3
    prepared = ann.prepare(sql="SELECT * FROM main.bob_now")
    assert ann.execute_prepared(prepared_id=prepared).num_rows == 3

    expect_allowed(client=admin, sql="REVOKE SELECT (amount) ON main.raw_orders FROM builder")
    expect_blocked(client=ann, sql="SELECT * FROM main.bob_now", why="its owner lacks a privilege")
    with pytest.raises(Exception) as raised:
        ann.execute_prepared(prepared_id=prepared)
    assert "its owner lacks a privilege" in str(raised.value), "a statement prepared before the REVOKE still ran"
    expect_allowed(client=admin, sql="GRANT SELECT (amount) ON main.raw_orders TO builder")
    assert expect_allowed(client=ann, sql="SELECT * FROM main.bob_now").num_rows == 3


def test_a_disabled_owner_closes_the_view(admin, ann, bob):
    expect_allowed(client=bob, sql="CREATE VIEW main.bob_disabled AS SELECT id FROM raw_orders")
    expect_allowed(client=admin, sql="GRANT SELECT ON main.bob_disabled TO analyst")
    assert expect_allowed(client=ann, sql="SELECT * FROM main.bob_disabled").num_rows == 3
    expect_allowed(client=admin, sql="ALTER USER bob DISABLE")
    expect_blocked(client=ann, sql="SELECT * FROM main.bob_disabled", why="its owner is disabled or was dropped")
    expect_allowed(client=admin, sql="ALTER USER bob ENABLE")
    assert expect_allowed(client=ann, sql="SELECT * FROM main.bob_disabled").num_rows == 3


def test_a_view_without_an_owner_reads_with_the_callers_rights(admin, ann):
    expect_allowed(client=admin, sql="GRANT SELECT ON legacy.v TO analyst")
    expect_blocked(client=ann, sql="SELECT * FROM legacy.v", why="which has no owner, so it reads with your rights")
    # The fix for a view made before access control: give it an owner.
    expect_allowed(client=admin, sql="GRANT SELECT ON legacy.base TO publisher")
    expect_allowed(client=admin, sql="ALTER VIEW legacy.v OWNER TO publisher")
    assert expect_allowed(client=ann, sql="SELECT * FROM legacy.v").num_rows == 1
    expect_blocked(client=ann, sql="SELECT * FROM legacy.base", why="no SELECT privilege on table")
    # A role owner's rights are the role's, checked at query time.
    expect_allowed(client=admin, sql="REVOKE SELECT ON legacy.base FROM publisher")
    expect_blocked(client=ann, sql="SELECT * FROM legacy.v", why="its owner lacks a privilege")
    # The administrator can take it back, under its own name.
    expect_allowed(client=admin, sql="ALTER VIEW legacy.v OWNER TO admin")
    assert expect_allowed(client=ann, sql="SELECT * FROM legacy.v").num_rows == 1


def test_a_sessions_temporary_objects_cannot_stand_in_for_what_a_view_reads(server):
    ann_t = server.user(name="ann")
    assert expect_allowed(client=ann_t, sql="SELECT customer FROM curated.tenant_v").num_rows == 1
    expect_allowed(client=ann_t, sql="CREATE TEMP TABLE allowed AS SELECT 'bob' AS tenant UNION ALL SELECT 'cara'")
    assert expect_allowed(client=ann_t, sql="SELECT * FROM allowed").num_rows == 2  # her own table
    expect_blocked(client=ann_t, sql="SELECT customer FROM curated.tenant_v",
                   why="cannot read a temporary table of this session")
    expect_allowed(client=ann_t, sql="DROP TABLE temp.main.allowed")
    assert expect_allowed(client=ann_t, sql="SELECT customer FROM curated.tenant_v").num_rows == 1

    ann_v = server.user(name="ann")
    expect_allowed(client=ann_v, sql="CREATE TEMP VIEW allowed AS SELECT 'bob' AS tenant")
    expect_blocked(client=ann_v, sql="SELECT customer FROM curated.tenant_v",
                   why="cannot read a temporary view of this session")


def test_session_macros_and_variables_are_refused(ann):
    # A session's macro is found before the catalog's functions, also inside a view: it
    # could make an administrator's view compute something else.
    expect_blocked(client=ann, sql="CREATE TEMP MACRO lower(x) AS (SELECT max(value) FROM secrets)",
                   why="creating macros needs an administrator")
    expect_blocked(client=ann, sql="CREATE TEMP TYPE mood AS ENUM ('a', 'b')",
                   why="creating types needs an administrator")
    # SET VARIABLE runs a query of its own, and getvariable() hands the value to any view.
    expect_blocked(client=ann, sql="SET VARIABLE leak = (SELECT max(value) FROM secrets)",
                   why="SET VARIABLE is not available")
    expect_blocked(client=ann, sql="SET VARIABLE x = 1", why="SET VARIABLE is not available")
    row = expect_allowed(client=ann, sql="SELECT lower('A') AS l, getvariable('leak') AS v").to_pylist()[0]
    assert row == {"l": "a", "v": None}


def test_an_administrators_view_may_read_a_file(server, ann):
    assert expect_allowed(client=ann, sql="SELECT answer FROM curated.file_v").num_rows == 1
    expect_blocked(client=ann, sql=f"SELECT * FROM read_parquet('{server.parquet}')", why="READ FILESYSTEM")


def test_a_refusal_is_always_recorded(server, ann, dan):
    # Every refusal is logged, also when queries are not (this server does not log them).
    expect_blocked(client=dan, sql="SELECT * FROM curated.orders_v", why="no SELECT privilege on view")
    lines = [line for line in server.log().splitlines() if "access control refused a statement" in line]
    assert lines and "user=dan" in lines[-1] and "no SELECT privilege on view" in lines[-1], lines
    # So is what an allowed statement read, including through a view's owner.
    expect_allowed(client=ann, sql="SELECT id FROM curated.nested_v")
    lines = [line for line in server.log().splitlines() if "access control allowed a statement" in line]
    assert lines and "user=ann" in lines[-1], lines
    assert "SELECT on view rbac_views.curated.nested_v (id)" in lines[-1], lines[-1]
    assert "as the owner of view rbac_views.curated.nested_v" in lines[-1], lines[-1]


def test_an_owner_record_applies_only_to_the_definition_it_was_recorded_for(launcher, store, tmp_path):
    # The administrator's view is owned by the administrator (the record carries a hash
    # of its definition). Replaced outside access control (here: by init SQL on a
    # restart), the record no longer applies, and the view reads with the caller's rights.
    database = tmp_path / "rbac_hash.duckdb"
    uri = store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)])
    admin = first.admin()
    for sql in ["CREATE TABLE secret AS SELECT 'TOP' AS s", "CREATE VIEW v AS SELECT s FROM secret",
                "CREATE ROLE r", "CREATE USER joe", "GRANT r TO joe", "GRANT SELECT ON v TO r"]:
        expect_allowed(client=admin, sql=sql)
    assert first.user(name="joe").run(sql="SELECT * FROM v").to_pylist() == [{"s": "TOP"}]
    first.running.stop()
    replaced = start_rbac_server(launcher=launcher, store_uri=uri, args=["--database", str(database)],
                                 env={"HARDCHARGER_INIT_SQL_COMMANDS":
                                      "CREATE OR REPLACE VIEW v AS SELECT s || '!' AS s FROM secret"})
    expect_blocked(client=replaced.user(name="joe"), sql="SELECT * FROM v",
                   why="which has no owner, so it reads with your rights")
    # Recording an owner again (as the administrator) restores it.
    expect_allowed(client=replaced.admin(), sql="ALTER VIEW v OWNER TO admin")
    assert replaced.user(name="joe").run(sql="SELECT * FROM v").to_pylist() == [{"s": "TOP!"}]
