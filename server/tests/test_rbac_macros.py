"""Macros under access control: CREATE MACRO and DROP MACRO, granted ON SCHEMA or ON CATALOG.

A macro is expanded into the statement that calls it, so what its body reads is checked as
part of that statement, with the caller's rights. What a user's macro must not do is change
what anyone else's SQL computes: it may not take the name of a built-in function or one an
extension autoloads, nor replace or stand in for a function that a view, a row or masking
policy, or a macro one of them calls uses (a view reads with its owner's rights). Temporary
macros stay the administrator's. Every refusal is asserted by its reason, next to the twin
that succeeds."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NEEDS_CREATE = "it needs CREATE MACRO on schema"
NEEDS_DROP = "it needs DROP MACRO on schema"
IN_USE = "would change what a view, a trigger, a row or masking policy, or a macro one of them uses computes"
BUILT_IN = "would replace the built-in function"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_macros.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA tools", "CREATE SCHEMA reports",
        "CREATE TABLE orders (id INT, amount INT, region VARCHAR)",
        "INSERT INTO orders VALUES (1, 10, 'EU'), (2, 20, 'US'), (3, 30, 'EU')",
        "CREATE TABLE secrets (id INT, value VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        # An administrator's macro that a view uses, one a row policy uses, and a chain
        # (view -> outer -> inner) whose inner macro the view reaches only through outer.
        "CREATE MACRO main.fmt_amount(a) AS a * 100",
        "CREATE VIEW reports.amounts AS SELECT id, fmt_amount(amount) AS cents FROM main.orders",
        "CREATE MACRO main.inner_label(r) AS lower(r)",
        "CREATE MACRO main.outer_label(r) AS inner_label(r) || '!'",
        "CREATE VIEW reports.labels AS SELECT outer_label(region) AS label FROM main.orders",
        "CREATE TABLE governed (id INT, region VARCHAR)",
        "INSERT INTO governed VALUES (1, 'EU'), (2, 'US')",
        "CREATE MACRO main.region_ok(r) AS r = 'EU'",
        "CREATE ROW POLICY eu_rows ON governed USING (region_ok(region))",
        # A macro nothing uses.
        "CREATE MACRO main.spare(x) AS x + 1",
        "CREATE ROLE readers", "CREATE ROLE macro_makers", "CREATE ROLE macro_creators", "CREATE ROLE schema_makers",
        "CREATE ROLE secret_readers",
        "GRANT SELECT ON orders TO readers", "GRANT SELECT ON governed TO readers",
        "GRANT SELECT ON SCHEMA reports TO readers",
        "GRANT CREATE MACRO, DROP MACRO ON SCHEMA main TO macro_makers",
        "GRANT CREATE MACRO, DROP MACRO ON SCHEMA tools TO macro_makers",
        "GRANT CREATE MACRO ON SCHEMA tools TO macro_creators",
        "CREATE USER mia", "CREATE USER cal", "CREATE USER joe", "CREATE USER sam", "CREATE USER sid",
        "GRANT readers, macro_makers TO mia", "GRANT macro_creators TO cal", "GRANT readers TO joe",
        "GRANT SELECT ON secrets TO secret_readers", "GRANT secret_readers TO sid",
    ]:
        expect_allowed(client=admin, sql=sql)
    catalog = admin.run(sql="SELECT current_database() AS c").column("c").to_pylist()[0]
    expect_allowed(client=admin, sql=f"GRANT CREATE SCHEMA ON CATALOG {catalog} TO schema_makers")
    expect_allowed(client=admin, sql="GRANT schema_makers TO sam")
    server.admin_client = admin
    server.catalog = catalog
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def mia(server):
    return server.user(name="mia")


@pytest.fixture
def cal(server):
    return server.user(name="cal")


@pytest.fixture
def joe(server):
    return server.user(name="joe")


def macro_exists(admin, schema: str, name: str) -> bool:
    rows = admin.run(sql="SELECT count(*) AS n FROM duckdb_functions() "
                         f"WHERE schema_name = '{schema}' AND function_name = '{name}'").to_pylist()
    return rows[0]["n"] > 0


# ---- the privileges ----

def test_create_macro_needs_the_privilege_on_the_schema(admin, mia, joe):
    expect_blocked(client=joe, sql="CREATE MACRO tools.plus_one(x) AS x + 1", why=NEEDS_CREATE)
    assert not macro_exists(admin=admin, schema="tools", name="plus_one")
    # Twin: a role granted CREATE MACRO on the schema may, and anyone may call it.
    expect_allowed(client=mia, sql="CREATE MACRO tools.plus_one(x) AS x + 1")
    assert expect_allowed(client=joe, sql="SELECT tools.plus_one(41) AS v").to_pylist() == [{"v": 42}]
    expect_allowed(client=mia, sql="CREATE MACRO tools.first_n(n) AS TABLE SELECT * FROM range(n)")
    assert expect_allowed(client=joe, sql="SELECT count(*) AS c FROM tools.first_n(3)").to_pylist() == [{"c": 3}]
    # Only where it is granted: not in schema reports.
    expect_blocked(client=mia, sql="CREATE MACRO reports.plus_two(x) AS x + 2", why=NEEDS_CREATE)


def test_the_grant_is_on_a_schema_or_a_catalog_never_one_object(admin, server):
    for sql in ["GRANT CREATE MACRO ON orders TO readers", "GRANT DROP MACRO ON main.orders TO readers"]:
        with pytest.raises(ChargewireError) as raised:
            admin.run(sql=sql)
        assert "is granted ON SCHEMA or ON CATALOG, not on a single object" in raised.value.detail
    # Twin: on a catalog it is accepted, and SHOW GRANTS lists it.
    expect_allowed(client=admin, sql=f"GRANT CREATE MACRO ON CATALOG {server.catalog} TO readers")
    grants = admin.run(sql="SHOW GRANTS FOR readers").to_pylist()
    assert {"CREATE MACRO"} <= {g["privilege"] for g in grants if g["object_type"] == "CATALOG"}
    expect_allowed(client=admin, sql=f"REVOKE CREATE MACRO ON CATALOG {server.catalog} FROM readers")


def test_a_schema_owner_may_create_macros_in_it(server, admin):
    sam = server.user(name="sam")
    expect_allowed(client=sam, sql="CREATE SCHEMA sams")
    expect_allowed(client=sam, sql="CREATE MACRO sams.double_it(x) AS x * 2")
    assert expect_allowed(client=sam, sql="SELECT sams.double_it(4) AS v").to_pylist() == [{"v": 8}]
    expect_allowed(client=sam, sql="DROP MACRO sams.double_it")
    # But not in a schema it doesn't own.
    expect_blocked(client=sam, sql="CREATE MACRO tools.sams_macro(x) AS x", why=NEEDS_CREATE)


def test_owning_a_table_of_that_name_grants_nothing(server, admin):
    # sam owns the table sams.t, so the plan check's owner rule would cover the path
    # sams.t if it were asked of the object rather than its schema.
    sam = server.user(name="sam")
    expect_allowed(client=admin, sql="CREATE SCHEMA IF NOT EXISTS shared")
    expect_allowed(client=admin, sql="GRANT CREATE TABLE ON SCHEMA shared TO schema_makers")
    expect_allowed(client=sam, sql="CREATE TABLE shared.t (id INT)")
    expect_blocked(client=sam, sql="CREATE MACRO shared.t(x) AS x", why=NEEDS_CREATE)
    # Twin: with the privilege, the same statement works.
    expect_allowed(client=admin, sql="GRANT CREATE MACRO ON SCHEMA shared TO schema_makers")
    expect_allowed(client=sam, sql="CREATE MACRO shared.t(x) AS x")


def test_drop_and_replace_need_drop_macro(admin, cal, mia):
    expect_allowed(client=cal, sql="CREATE MACRO tools.cals(x) AS x + 1")
    expect_blocked(client=cal, sql="CREATE OR REPLACE MACRO tools.cals(x) AS x + 2", why="may not replace macro")
    expect_blocked(client=cal, sql="DROP MACRO tools.cals", why=NEEDS_DROP)
    assert macro_exists(admin=admin, schema="tools", name="cals")
    # Twin: a role that also holds DROP MACRO may.
    expect_allowed(client=mia, sql="CREATE OR REPLACE MACRO tools.cals(x) AS x + 2")
    assert expect_allowed(client=cal, sql="SELECT tools.cals(1) AS v").to_pylist() == [{"v": 3}]
    expect_allowed(client=mia, sql="DROP MACRO tools.cals")
    assert not macro_exists(admin=admin, schema="tools", name="cals")
    # IF NOT EXISTS of an existing macro creates nothing, so needs only CREATE MACRO.
    expect_allowed(client=cal, sql="CREATE MACRO tools.kept(x) AS x")
    expect_allowed(client=cal, sql="CREATE MACRO IF NOT EXISTS tools.kept(x) AS x + 100")
    assert expect_allowed(client=cal, sql="SELECT tools.kept(1) AS v").to_pylist() == [{"v": 1}]


def test_drop_macro_table_needs_drop_macro(admin, cal, mia):
    expect_allowed(client=cal, sql="CREATE MACRO tools.some_rows() AS TABLE SELECT 1 AS one")
    expect_blocked(client=cal, sql="DROP MACRO TABLE tools.some_rows", why=NEEDS_DROP)
    expect_allowed(client=mia, sql="DROP MACRO TABLE tools.some_rows")


def test_temporary_macros_stay_the_administrators(admin, mia):
    # A session's macro is found before every other function, also inside a view that
    # reads with its owner's rights: lower() here would make reports.labels read secrets.
    expect_blocked(client=mia, sql="CREATE TEMP MACRO lower(x) AS (SELECT max(value) FROM secrets)",
                   why="creating temporary macros needs an administrator")
    # Twin: the same user creates the macro in a schema.
    expect_allowed(client=mia, sql="CREATE MACRO tools.helper(x) AS x")


# ---- what a macro reads ----

def test_a_macros_body_reads_with_the_callers_rights(server, admin, mia, joe):
    # Created over a table its creator can't read: creating reads nothing.
    expect_allowed(client=mia, sql="CREATE MACRO tools.secret_rows() AS TABLE SELECT * FROM main.secrets")
    expect_allowed(client=mia, sql="CREATE MACRO tools.secret_count() AS (SELECT count(*) FROM main.secrets)")
    # Called, it reads with the caller's rights: refused to those without SELECT on it ...
    for client in [mia, joe]:
        expect_blocked(client=client, sql="SELECT * FROM tools.secret_rows()", why="no SELECT privilege on table")
        expect_blocked(client=client, sql="SELECT tools.secret_count() AS n", why="no SELECT privilege on table")
    # ... and the twin: a user who may read the table gets its rows through it.
    sid = server.user(name="sid")
    rows = expect_allowed(client=sid, sql="SELECT value FROM tools.secret_rows()").to_pylist()
    assert rows == [{"value": "top-secret-value"}]
    assert expect_allowed(client=sid, sql="SELECT tools.secret_count() AS n").to_pylist() == [{"n": 1}]


def test_a_macros_body_may_call_only_what_its_creator_may(admin, mia, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("word\nfile-secret-word\n")
    for sql, why in [
        ("CREATE MACRO tools.q() AS TABLE SELECT * FROM query('SELECT * FROM main.secrets')", "query()"),
        ("CREATE MACRO tools.qt() AS TABLE SELECT * FROM query_table('main.secrets')", "query_table()"),
        (f"CREATE MACRO tools.f() AS TABLE SELECT * FROM read_csv('{secret}')", "READ FILESYSTEM"),
        (f"CREATE MACRO tools.fs() AS (SELECT count(*) FROM read_csv('{secret}'))", "READ FILESYSTEM"),
        ("CREATE MACRO tools.fp(p) AS TABLE SELECT * FROM read_csv(p)", "READ FILESYSTEM"),
        (f"CREATE MACRO tools.fd(p := '{secret}') AS (SELECT count(*) FROM read_csv(p))", "READ FILESYSTEM"),
    ]:
        expect_blocked(client=mia, sql=sql, why=why)
    assert not macro_exists(admin=admin, schema="tools", name="q")
    # Twin: a body over the table functions anyone may call, and over a table, is fine.
    expect_allowed(client=mia, sql="CREATE MACRO tools.squares(n) AS TABLE SELECT range * range AS sq FROM range(n)")
    expect_allowed(client=mia, sql="CREATE MACRO tools.order_rows() AS TABLE SELECT id FROM main.orders")


# ---- what a macro may not change ----

@pytest.mark.parametrize("name", ["lower", "upper", "sum", "range", "read_csv", "list_reverse", "current_user"])
def test_a_built_in_functions_name_is_refused(admin, mia, name):
    expect_blocked(client=mia, sql=f"CREATE MACRO main.{name}(x) AS 'mine'", why=BUILT_IN)
    expect_blocked(client=mia, sql=f"CREATE MACRO tools.{name}(x) AS 'mine'", why=BUILT_IN)
    # Twin: the function still answers as itself.
    assert expect_allowed(client=mia, sql="SELECT lower('AB') AS v").to_pylist() == [{"v": "ab"}]


def test_an_autoloadable_functions_name_is_refused(admin, mia):
    # st_area is the spatial extension's, autoloaded where it is first called: a macro
    # of that name would be found instead.
    error = expect_blocked(client=mia, sql="CREATE MACRO main.st_area(x) AS 0", why="of the spatial extension")
    assert "would replace the function st_area" in error.detail
    # Twin: a name no extension has.
    expect_allowed(client=mia, sql="CREATE MACRO tools.st_area_of_mine(x) AS 0")


def test_a_macro_a_view_uses_cannot_be_replaced(admin, mia, joe):
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO main.fmt_amount(a) AS (SELECT max(id) FROM main.secrets)",
                   why=IN_USE)
    # The view still computes what its owner wrote.
    rows = expect_allowed(client=joe, sql="SELECT cents FROM reports.amounts ORDER BY id").to_pylist()
    assert rows == [{"cents": 1000}, {"cents": 2000}, {"cents": 3000}]
    # Twin: a macro nothing uses can be replaced by the same user.
    expect_allowed(client=mia, sql="CREATE OR REPLACE MACRO main.spare(x) AS x + 2")


def test_a_macro_cannot_stand_in_for_one_a_view_finds_later_in_its_search_path(admin, mia):
    # reports.amounts calls fmt_amount unqualified: a view looks in its own schema first,
    # so reports.fmt_amount (or tools.fmt_amount, read as a schema-qualified name) would
    # be found before main's. mia may create macros in main and tools, not reports.
    expect_allowed(client=admin, sql="GRANT CREATE MACRO, DROP MACRO ON SCHEMA reports TO macro_makers")
    try:
        expect_blocked(client=mia, sql="CREATE MACRO reports.fmt_amount(a) AS 0", why=IN_USE)
        # Twin: another name in the same schema.
        expect_allowed(client=mia, sql="CREATE MACRO reports.fmt_other(a) AS 0")
        expect_allowed(client=mia, sql="DROP MACRO reports.fmt_other")
    finally:
        expect_allowed(client=admin, sql="REVOKE CREATE MACRO, DROP MACRO ON SCHEMA reports FROM macro_makers")


def test_a_macro_a_view_reaches_through_another_macro_cannot_be_replaced(admin, mia, joe):
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO main.inner_label(r) AS (SELECT max(value) FROM secrets)",
                   why=IN_USE)
    labels = expect_allowed(client=joe, sql="SELECT DISTINCT label FROM reports.labels ORDER BY label").to_pylist()
    assert labels == [{"label": "eu!"}, {"label": "us!"}]


def test_a_macro_a_row_policy_uses_cannot_be_replaced(admin, mia, joe):
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO main.region_ok(r) AS true", why=IN_USE)
    assert expect_allowed(client=joe, sql="SELECT id FROM governed").to_pylist() == [{"id": 1}]


def test_dropping_a_macro_a_row_policy_uses_closes_the_table_and_it_cannot_come_back(admin, server):
    # Its own setup: drop and recreate, as an attacker with DROP MACRO would try.
    for sql in ["CREATE TABLE fenced (id INT, region VARCHAR)", "INSERT INTO fenced VALUES (1, 'EU'), (2, 'US')",
                "CREATE MACRO main.fence_ok(r) AS r = 'EU'", "CREATE ROW POLICY fence_rows ON fenced USING (fence_ok(region))",
                "GRANT SELECT ON fenced TO readers"]:
        expect_allowed(client=admin, sql=sql)
    mia = server.user(name="mia")
    expect_allowed(client=mia, sql="DROP MACRO main.fence_ok")
    # Without its macro the policy can't be applied, and the table is not read at all.
    with pytest.raises(ChargewireError) as raised:
        mia.run(sql="SELECT id FROM fenced")
    assert "fence_ok" in raised.value.detail
    expect_blocked(client=mia, sql="CREATE MACRO main.fence_ok(r) AS true", why=IN_USE)
    # Twin: the administrator restores it, and the policy applies again.
    expect_allowed(client=admin, sql="CREATE MACRO main.fence_ok(r) AS r = 'EU'")
    assert expect_allowed(client=mia, sql="SELECT id FROM fenced").to_pylist() == [{"id": 1}]


def test_a_macro_a_masking_policy_uses_cannot_be_replaced(admin, server):
    for sql in ["CREATE TABLE people (id INT, email VARCHAR)", "INSERT INTO people VALUES (1, 'ann@example.com')",
                "CREATE MACRO main.hide_email(e) AS '***@' || split_part(e, '@', 2)",
                "CREATE MASKING POLICY email_mask AS (e VARCHAR) RETURNS VARCHAR -> hide_email(e)",
                "ALTER TABLE people MODIFY COLUMN email SET MASKING POLICY email_mask",
                "GRANT SELECT ON people TO readers"]:
        expect_allowed(client=admin, sql=sql)
    mia = server.user(name="mia")
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO main.hide_email(e) AS e", why=IN_USE)
    assert expect_allowed(client=mia, sql="SELECT email FROM people").to_pylist() == [{"email": "***@example.com"}]


def test_a_view_its_creator_makes_over_its_own_macro(server, admin, mia, joe):
    # The user's own view over the user's own macro: once the view uses it, it is pinned
    # for everyone but the administrator, the creator included.
    expect_allowed(client=admin, sql="GRANT CREATE VIEW ON SCHEMA tools TO macro_makers")
    expect_allowed(client=mia, sql="CREATE MACRO tools.region_upper(r) AS upper(r)")
    expect_allowed(client=mia, sql="CREATE VIEW tools.order_regions AS SELECT tools.region_upper(region) AS r FROM main.orders")
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO tools.region_upper(r) AS r", why=IN_USE)
    # The administrator may.
    expect_allowed(client=admin, sql="CREATE OR REPLACE MACRO tools.region_upper(r) AS lower(r)")
