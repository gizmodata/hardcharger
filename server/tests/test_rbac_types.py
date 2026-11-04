"""Types under access control: CREATE TYPE and DROP TYPE, granted ON SCHEMA or ON CATALOG.

A type is found by name where SQL is bound (a view's query, a macro's or trigger's body, a
policy, a column default or CHECK constraint), so a user's type could stand in for one of
those, and dropping or replacing one they name changes what they compute: both are refused,
as is the name of a built-in type or of one an extension provides. Temporary types stay the
administrator's. A table's columns copy the type at CREATE TABLE, so they pin nothing. Every
refusal is asserted by its reason, next to the twin that succeeds."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NEEDS_CREATE = "it needs CREATE TYPE on schema"
NEEDS_DROP = "it needs DROP TYPE on schema"
WOULD_CHANGE = "would change what a view, a macro, a trigger, a row or masking policy, or a column default or check constraint computes"
IN_USE = "is used by a view, a macro, a trigger, a row or masking policy, or a column default or check constraint"
BUILT_IN = "would replace the built-in type"
TEMPORARY = "creating temporary types needs an administrator"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_types.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA tools", "CREATE SCHEMA reports",
        "CREATE TABLE orders (id INT, amount INT, region VARCHAR)",
        "INSERT INTO orders VALUES (1, 10, 'EU'), (2, 20, 'US'), (3, 30, 'EU')",
        "CREATE TABLE secrets (id INT, value VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        # The administrator's types, each pinned by one kind of re-bound SQL.
        "CREATE TYPE main.level AS ENUM ('low', 'high')",
        "CREATE VIEW reports.levels AS SELECT id, (CASE WHEN amount > 15 THEN 'high' ELSE 'low' END)::level AS lv "
        "FROM main.orders",
        "CREATE TYPE main.label_t AS ENUM ('a', 'b')",
        "CREATE MACRO main.as_label(x) AS x::label_t",
        "CREATE TYPE main.region_t AS ENUM ('EU', 'US')",
        "CREATE TABLE governed (id INT, region VARCHAR)",
        "INSERT INTO governed VALUES (1, 'EU'), (2, 'US')",
        "CREATE ROW POLICY eu_rows ON governed USING (region::region_t = 'EU')",
        "CREATE TYPE main.grade_t AS ENUM ('pass', 'fail')",
        "CREATE TABLE graded (id INT, grade VARCHAR DEFAULT 'pass'::grade_t)",
        "CREATE TYPE main.score_t AS INTEGER",
        "CREATE TABLE scored (id INT, score INT CHECK (score::score_t >= 0))",
        "CREATE TYPE main.status_t AS ENUM ('new', 'done')",
        "CREATE TABLE tickets (id INT)", "CREATE TABLE ticket_log (status VARCHAR)",
        "CREATE TRIGGER log_tickets AFTER INSERT ON tickets FOR EACH STATEMENT "
        "INSERT INTO ticket_log VALUES ('new'::status_t)",
        "CREATE TYPE main.masked_t AS ENUM ('x')",
        "CREATE MASKING POLICY first_letter AS (val VARCHAR) RETURNS VARCHAR -> left(val, 1)::masked_t::VARCHAR",
        # A type nothing uses.
        "CREATE TYPE main.spare_t AS ENUM ('one', 'two')",
        "CREATE ROLE readers", "CREATE ROLE type_makers", "CREATE ROLE type_creators", "CREATE ROLE schema_makers",
        "CREATE ROLE secret_readers",
        "GRANT SELECT ON orders TO readers", "GRANT SELECT ON governed TO readers",
        "GRANT SELECT ON SCHEMA reports TO readers",
        "GRANT CREATE TYPE, DROP TYPE ON SCHEMA main TO type_makers",
        "GRANT CREATE TYPE, DROP TYPE ON SCHEMA tools TO type_makers",
        "GRANT CREATE TYPE ON SCHEMA tools TO type_creators",
        "CREATE USER mia", "CREATE USER cal", "CREATE USER joe", "CREATE USER sam", "CREATE USER sid",
        "GRANT readers, type_makers TO mia", "GRANT type_creators TO cal", "GRANT readers TO joe",
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


def type_exists(admin, schema: str, name: str) -> bool:
    rows = admin.run(sql="SELECT count(*) AS n FROM duckdb_types() "
                         f"WHERE schema_name = '{schema}' AND type_name = '{name}'").to_pylist()
    return rows[0]["n"] > 0


# ---- the privileges ----

def test_create_type_needs_the_privilege_on_the_schema(admin, mia, joe):
    expect_blocked(client=joe, sql="CREATE TYPE tools.mood AS ENUM ('sad', 'ok')", why=NEEDS_CREATE)
    assert not type_exists(admin=admin, schema="tools", name="mood")
    # Twin: a role granted CREATE TYPE on the schema may, and anyone may use the type: an
    # enum, a struct and an alias.
    expect_allowed(client=mia, sql="CREATE TYPE tools.mood AS ENUM ('sad', 'ok')")
    expect_allowed(client=mia, sql="CREATE TYPE tools.pair AS STRUCT(a INT, b INT)")
    expect_allowed(client=mia, sql="CREATE TYPE tools.myint AS INTEGER")
    assert expect_allowed(client=joe, sql="SELECT 'ok'::tools.mood AS m").to_pylist() == [{"m": "ok"}]
    assert expect_allowed(client=joe, sql="SELECT {'a': 1, 'b': 2}::tools.pair AS p").to_pylist() == [{"p": {"a": 1, "b": 2}}]
    assert expect_allowed(client=joe, sql="SELECT 41::tools.myint + 1 AS v").to_pylist() == [{"v": 42}]
    # Only where it is granted: not in schema reports.
    expect_blocked(client=mia, sql="CREATE TYPE reports.mood AS ENUM ('sad')", why=NEEDS_CREATE)


def test_the_grant_is_on_a_schema_or_a_catalog_never_one_object(admin, server):
    for sql in ["GRANT CREATE TYPE ON orders TO readers", "GRANT DROP TYPE ON main.orders TO readers"]:
        with pytest.raises(ChargewireError) as raised:
            admin.run(sql=sql)
        assert "is granted ON SCHEMA or ON CATALOG, not on a single object" in raised.value.detail
    # Twin: on a catalog it is accepted, and SHOW GRANTS lists it.
    expect_allowed(client=admin, sql=f"GRANT CREATE TYPE, DROP TYPE ON CATALOG {server.catalog} TO readers")
    grants = admin.run(sql="SHOW GRANTS FOR readers").to_pylist()
    assert {"CREATE TYPE", "DROP TYPE"} <= {g["privilege"] for g in grants if g["object_type"] == "CATALOG"}
    expect_allowed(client=admin, sql=f"REVOKE CREATE TYPE, DROP TYPE ON CATALOG {server.catalog} FROM readers")


def test_a_schema_owner_may_create_types_in_it(server, admin):
    sam = server.user(name="sam")
    expect_allowed(client=sam, sql="CREATE SCHEMA sams")
    expect_allowed(client=sam, sql="CREATE TYPE sams.size AS ENUM ('s', 'l')")
    assert expect_allowed(client=sam, sql="SELECT 'l'::sams.size AS v").to_pylist() == [{"v": "l"}]
    expect_allowed(client=sam, sql="DROP TYPE sams.size")
    # But not in a schema it doesn't own.
    expect_blocked(client=sam, sql="CREATE TYPE tools.sams_type AS INTEGER", why=NEEDS_CREATE)


def test_drop_and_replace_need_drop_type(admin, cal, mia):
    expect_allowed(client=cal, sql="CREATE TYPE tools.cals AS ENUM ('one')")
    expect_blocked(client=cal, sql="CREATE OR REPLACE TYPE tools.cals AS ENUM ('two')", why="may not replace type")
    expect_blocked(client=cal, sql="DROP TYPE tools.cals", why=NEEDS_DROP)
    assert type_exists(admin=admin, schema="tools", name="cals")
    # Twin: a role that also holds DROP TYPE may.
    expect_allowed(client=mia, sql="CREATE OR REPLACE TYPE tools.cals AS ENUM ('two')")
    assert expect_allowed(client=cal, sql="SELECT 'two'::tools.cals AS v").to_pylist() == [{"v": "two"}]
    expect_allowed(client=mia, sql="DROP TYPE tools.cals")
    assert not type_exists(admin=admin, schema="tools", name="cals")
    # IF NOT EXISTS of an existing type creates nothing, so needs only CREATE TYPE.
    expect_allowed(client=cal, sql="CREATE TYPE tools.kept AS ENUM ('kept')")
    expect_allowed(client=cal, sql="CREATE TYPE IF NOT EXISTS tools.kept AS ENUM ('other')")
    assert expect_allowed(client=cal, sql="SELECT 'kept'::tools.kept AS v").to_pylist() == [{"v": "kept"}]


def test_temporary_types_stay_the_administrators(admin, mia):
    # A session's type is found before every other type, also inside a view that reads
    # with its owner's rights: level here would make reports.levels compute something else.
    expect_blocked(client=mia, sql="CREATE TEMP TYPE level AS ENUM ('other')", why=TEMPORARY)
    # Twin: the same user creates the type in a schema; the administrator may make a
    # temporary one.
    expect_allowed(client=mia, sql="CREATE TYPE tools.session_free AS ENUM ('x')")
    expect_allowed(client=admin, sql="CREATE TEMP TYPE admins_temp AS ENUM ('y')")


def test_an_enum_from_a_query_reads_with_its_creators_rights(admin, mia, server):
    expect_blocked(client=mia, sql="CREATE TYPE tools.from_secrets AS ENUM (SELECT value FROM main.secrets)",
                   why="no SELECT privilege on table")
    assert not type_exists(admin=admin, schema="tools", name="from_secrets")
    # Twin: over a table the creator may read.
    expect_allowed(client=mia, sql="CREATE TYPE tools.regions AS ENUM (SELECT DISTINCT region FROM main.orders)")
    rows = expect_allowed(client=mia, sql="SELECT enum_range(NULL::tools.regions) AS r").to_pylist()
    assert sorted(rows[0]["r"]) == ["EU", "US"]
    # And the creator may not read through query(): the gate refuses it at creation.
    expect_blocked(client=mia, sql="CREATE TYPE tools.q AS ENUM (SELECT * FROM query('SELECT value FROM main.secrets'))",
                   why="query()")


# ---- what a type may not change ----

@pytest.mark.parametrize("name", ["integer", "varchar", "uuid", "timestamp"])
def test_a_built_in_types_name_is_refused(admin, mia, name):
    expect_blocked(client=mia, sql=f"CREATE TYPE main.{name} AS ENUM ('mine')", why=BUILT_IN)
    expect_blocked(client=mia, sql=f"CREATE TYPE tools.{name} AS ENUM ('mine')", why=BUILT_IN)
    # Twin: the type still answers as itself.
    assert expect_allowed(client=mia, sql="SELECT typeof(1::integer) AS t").to_pylist() == [{"t": "INTEGER"}]


@pytest.mark.parametrize("name", ["json", "inet"])
def test_an_extensions_type_name_is_refused(admin, mia, name):
    # DuckDB itself lets a schema's json type be created and then finds it before the json
    # extension's: '{}'::json would become the user's type in every query and view. The
    # refusal names the extension, or the built-in type when the extension is loaded.
    error = expect_blocked(client=mia, sql=f"CREATE TYPE main.{name} AS VARCHAR", why="would replace the")
    assert f"of the {name} extension" in error.detail or f"built-in type {name}" in error.detail
    # Twin: a name no extension has.
    expect_allowed(client=mia, sql=f"CREATE TYPE tools.{name}_of_mine AS VARCHAR")


def test_a_type_a_view_uses_cannot_be_replaced_or_dropped(admin, mia, joe):
    expect_blocked(client=mia, sql="CREATE OR REPLACE TYPE main.level AS ENUM ('other')", why=IN_USE)
    expect_blocked(client=mia, sql="DROP TYPE main.level", why=IN_USE)
    expect_blocked(client=mia, sql="DROP TYPE main.level CASCADE", why=IN_USE)
    # The view still computes what its owner wrote.
    rows = expect_allowed(client=joe, sql="SELECT lv FROM reports.levels ORDER BY id").to_pylist()
    assert rows == [{"lv": "low"}, {"lv": "high"}, {"lv": "high"}]
    # Twin: a type nothing uses can be replaced and dropped by the same user.
    expect_allowed(client=mia, sql="CREATE OR REPLACE TYPE main.spare_t AS ENUM ('three')")
    expect_allowed(client=mia, sql="DROP TYPE main.spare_t")
    expect_allowed(client=admin, sql="CREATE TYPE main.spare_t AS ENUM ('one', 'two')")


def test_a_type_cannot_stand_in_for_one_a_view_finds_earlier_in_its_search_path(admin, mia):
    # reports.levels names level unqualified: a view looks in its own schema first, so
    # reports.level would be found before main's. mia may create types in main and tools,
    # not reports.
    expect_allowed(client=admin, sql="GRANT CREATE TYPE, DROP TYPE ON SCHEMA reports TO type_makers")
    try:
        expect_blocked(client=mia, sql="CREATE TYPE reports.level AS ENUM ('other')", why=WOULD_CHANGE)
        # Twin: another name in the same schema.
        expect_allowed(client=mia, sql="CREATE TYPE reports.other_level AS ENUM ('other')")
        expect_allowed(client=mia, sql="DROP TYPE reports.other_level")
    finally:
        expect_allowed(client=admin, sql="REVOKE CREATE TYPE, DROP TYPE ON SCHEMA reports FROM type_makers")


@pytest.mark.parametrize("name, used_by", [
    ("label_t", "a macro"), ("region_t", "a row policy"), ("grade_t", "a column default"),
    ("score_t", "a check constraint"), ("status_t", "a trigger"), ("masked_t", "a masking policy"),
])
def test_a_type_a_macro_policy_default_check_or_trigger_uses_cannot_be_replaced(admin, mia, name, used_by):
    expect_blocked(client=mia, sql=f"CREATE OR REPLACE TYPE main.{name} AS ENUM ('mine')", why=IN_USE)
    expect_blocked(client=mia, sql=f"DROP TYPE main.{name}", why=IN_USE)
    # A type of the same name in another schema of the search path would stand in for it
    # where the name is unqualified.
    expect_blocked(client=mia, sql=f"CREATE TYPE tools.{name} AS ENUM ('mine')", why=WOULD_CHANGE)


def test_a_type_a_macro_uses_keeps_its_meaning(admin, mia, joe):
    # The macro resolves label_t where it is called: mia's own type of that name could not be
    # created, so the macro still answers as the administrator wrote it.
    assert expect_allowed(client=joe, sql="SELECT main.as_label('a') AS v").to_pylist() == [{"v": "a"}]
    with pytest.raises(ChargewireError) as raised:
        joe.run(sql="SELECT main.as_label('mine') AS v")
    assert "Could not convert" in raised.value.detail


def test_a_table_column_pins_nothing(admin, mia, joe):
    # A column copies its type: dropping the type changes nothing for the table.
    expect_allowed(client=mia, sql="CREATE TYPE tools.copied AS ENUM ('kept')")
    expect_allowed(client=admin, sql="CREATE TABLE tools.keeps (v tools.copied)")
    expect_allowed(client=admin, sql="INSERT INTO tools.keeps VALUES ('kept')")
    expect_allowed(client=mia, sql="DROP TYPE tools.copied")
    assert expect_allowed(client=admin, sql="SELECT v FROM tools.keeps").to_pylist() == [{"v": "kept"}]


# ---- what users see ----

def test_types_follow_schema_visibility(admin, mia, joe, server):
    # joe may read main.orders and schema reports, nothing in tools: the listing shows
    # main's types and the built-in ones, not tools' (mia's mood from the first test).
    expect_allowed(client=mia, sql="CREATE TYPE IF NOT EXISTS tools.mood AS ENUM ('sad', 'ok')")
    rows = expect_allowed(client=joe, sql="SELECT schema_name, type_name, internal FROM duckdb_types()").to_pylist()
    listed = {(r["schema_name"], r["type_name"]) for r in rows if not r["internal"]}
    assert ("main", "level") in listed
    assert ("tools", "mood") not in listed
    assert any(r["internal"] and r["type_name"].lower() == "integer" for r in rows)
    # Twin: mia, who may create types in tools, sees them; so does the administrator.
    for client in [mia, admin]:
        rows = expect_allowed(client=client, sql="SELECT schema_name, type_name FROM duckdb_types() WHERE NOT internal").to_pylist()
        assert ("tools", "mood") in {(r["schema_name"], r["type_name"]) for r in rows}


def test_comment_on_type_needs_drop_type(admin, mia, joe):
    expect_blocked(client=joe, sql="COMMENT ON TYPE main.spare_t IS 'joes'", why="may not comment on type")
    expect_allowed(client=mia, sql="COMMENT ON TYPE main.spare_t IS 'a spare type'")
    rows = admin.run(sql="SELECT comment FROM duckdb_types() WHERE type_name = 'spare_t'").to_pylist()
    assert rows == [{"comment": "a spare type"}]


def test_the_administrator_is_unaffected(admin):
    # Built-in names DuckDB allows, in-use types, temporary ones: the administrator may.
    expect_allowed(client=admin, sql="CREATE OR REPLACE TYPE main.spare_t AS ENUM ('one', 'two', 'three')")
    expect_allowed(client=admin, sql="CREATE TEMP TYPE admins_other AS INTEGER")
    expect_allowed(client=admin, sql="CREATE TYPE main.json AS VARCHAR")
    expect_allowed(client=admin, sql="DROP TYPE main.json")
