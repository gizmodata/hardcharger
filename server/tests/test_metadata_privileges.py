"""Metadata follows authorization (Chargewire §9.4), object by object.

Inside a catalog a principal can see, the metadata calls (catalogs, db-schemas, tables,
primary-keys, imported-keys, ...) and every SQL way to list the catalog
(duckdb_tables(), duckdb_columns(), duckdb_views(), information_schema, SHOW TABLES,
DESCRIBE, pragma_table_info) show only what the principal may access, by the rules
SELECT itself follows:

- a table or view is listed when the principal holds SELECT on it or on any of its
  columns, or INSERT, UPDATE or DELETE on it (owning it holds them all);
- a column, when the principal may SELECT it or holds INSERT, UPDATE or DELETE on its
  table;
- a schema, when the principal holds a grant on or in it, or owns something in it;
- a catalog_access rules principal (rules-only server, or an administrator token with
  rules) sees whole catalogs, as its rules let it read them;
- a tenant's user, its tenant's catalogs only (as before), and in them its grants;
- the unrestricted administrator, everything.

DESCRIBE and pragma_table_info of a table the principal may not access are refused with
the reason; DESCRIBE of a table whose columns it may read only some of is refused too
(DESCRIBE shows them all), and pragma_table_info lists the readable ones.

Before this, only other tenants' catalogs were hidden: joe below saw pair.b, its columns,
the view over it and its definition, through every one of these. Each negative test has
a positive twin (the administrator, or the same user on what it may access).
"""

import time

import pyarrow as pa
import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, mint, start_rbac_server

SCHEMA_TABLES = {"a", "b", "bv", "a_child", "b_child", "wide", "inbox"}


@pytest.fixture(scope="module")
def server(module_launcher, store):
    # In memory: the default catalog is memory.
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA pair", "CREATE SCHEMA other",
        "CREATE TABLE pair.a (id INT PRIMARY KEY, x INT)",
        "CREATE TABLE pair.b (id INT PRIMARY KEY, secret VARCHAR)",
        "INSERT INTO pair.a VALUES (1, 10)", "INSERT INTO pair.b VALUES (1, 'top-secret')",
        "CREATE VIEW pair.bv AS SELECT id, secret FROM pair.b",
        "CREATE TABLE pair.a_child (aid INT REFERENCES pair.a (id))",
        "CREATE TABLE pair.b_child (bid INT REFERENCES pair.b (id))",
        "CREATE TABLE pair.wide (id INT, salary INT)",
        "CREATE TABLE pair.inbox (id INT, body VARCHAR)",
        "CREATE TABLE other.t (id INT)",
        "ATTACH ':memory:' AS acme_db", "ATTACH ':memory:' AS beta_db",
        "CREATE TABLE acme_db.main.a_seen (id INT)", "CREATE TABLE acme_db.main.a_hidden (id INT)",
        "CREATE TABLE beta_db.main.beta_t (id INT)",
        "CREATE TENANT acme", "CREATE TENANT beta",
        "ALTER CATALOG acme_db SET TENANT acme", "ALTER CATALOG beta_db SET TENANT beta",
        "ALTER TENANT acme SET DEFAULT CATALOG acme_db",
        "CREATE ROLE reader", "GRANT SELECT ON pair.a TO reader", "GRANT SELECT ON pair.a_child TO reader",
        "GRANT SELECT ON pair.b_child TO reader",
        "CREATE ROLE colreader", "GRANT SELECT (id) ON pair.wide TO colreader",
        "CREATE ROLE writer", "GRANT INSERT ON pair.inbox TO writer",
        "CREATE USER joe", "GRANT reader TO joe",
        "CREATE USER carl", "GRANT colreader TO carl",
        "CREATE USER wanda", "GRANT writer TO wanda",
        "CREATE USER nora",
        "CREATE USER acme/boss", "GRANT acme/tenant_admin TO acme/boss",
    ]:
        expect_allowed(client=admin, sql=sql)
    boss = server.user(name="acme/boss")
    for sql in ["CREATE USER acme/ann", "CREATE ROLE acme/readers",
                "GRANT SELECT ON acme_db.main.a_seen TO acme/readers", "GRANT acme/readers TO acme/ann"]:
        expect_allowed(client=boss, sql=sql)
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def joe(server):
    return server.user(name="joe")


def names(table: pa.Table, column: str) -> set:
    return set(table.column(column).to_pylist())


def where(table: pa.Table, **equal) -> pa.Table:
    rows = [row for row in table.to_pylist() if all(row[k] == v for k, v in equal.items())]
    return pa.Table.from_pylist(rows, schema=table.schema)


def metadata_tables(client, schema: str = "pair", **params) -> set:
    return names(client.metadata(kind="tables", catalog="memory", db_schema_filter_pattern=schema, **params),
                 "table_name")


def schema_columns(client, table: str) -> list:
    """The columns tables?include_schema=true describes `table` with."""
    rows = client.metadata(kind="tables", catalog="memory", db_schema_filter_pattern="pair",
                           table_name_filter_pattern=table, include_schema="true").to_pylist()
    assert len(rows) == 1, rows
    return pa.ipc.read_schema(pa.py_buffer(rows[0]["table_schema"])).names


# Every way a client can list the tables of schema pair: (name, how).
TABLE_LISTINGS = {
    "metadata tables": lambda c: metadata_tables(c),
    "metadata tables with schemas": lambda c: metadata_tables(c, include_schema="true"),
    "duckdb_tables() and duckdb_views()": lambda c: names(c.run(
        sql="SELECT table_name AS n FROM duckdb_tables() WHERE schema_name = 'pair' "
            "UNION ALL SELECT view_name FROM duckdb_views() WHERE schema_name = 'pair'"), "n"),
    "duckdb_columns()": lambda c: names(c.run(
        sql="SELECT table_name FROM duckdb_columns() WHERE schema_name = 'pair'"), "table_name"),
    "information_schema.tables": lambda c: names(c.run(
        sql="SELECT table_name FROM information_schema.tables WHERE table_schema = 'pair'"), "table_name"),
    "information_schema.columns": lambda c: names(c.run(
        sql="SELECT table_name FROM information_schema.columns WHERE table_schema = 'pair'"), "table_name"),
    "SHOW TABLES FROM": lambda c: names(c.run(sql="SHOW TABLES FROM memory.pair"), "name"),
    "SHOW ALL TABLES": lambda c: names(where(c.run(sql="SHOW ALL TABLES"), schema="pair"), "name"),
}


@pytest.mark.parametrize("how", list(TABLE_LISTINGS))
def test_a_user_sees_only_the_tables_it_may_access(server, admin, joe, how):
    listed = TABLE_LISTINGS[how]
    # joe holds SELECT on a (and on the two child tables), none on b or its view.
    assert listed(joe) == {"a", "a_child", "b_child"}, how
    # The twin: the administrator sees every one.
    assert listed(admin) == SCHEMA_TABLES, how


def test_one_table_of_two_through_each_metadata_call(admin, joe):
    # The two tables of a pair, as the metadata calls name them one by one.
    for kind, params in [("tables", {"table_name_filter_pattern": "_"}),
                         ("tables", {"table_name_filter_pattern": "_", "include_schema": "true"}),
                         ("primary-keys", {})]:
        seen = names(joe.metadata(kind=kind, catalog="memory", **params), "table_name") & {"a", "b"}
        assert seen == {"a"}, (kind, params)
        assert names(admin.metadata(kind=kind, catalog="memory", **params), "table_name") & {"a", "b"} == {"a", "b"}
    # Constraints of a table joe can't access aren't listed either.
    constrained = names(joe.run(sql="SELECT table_name FROM duckdb_constraints() WHERE schema_name = 'pair'"),
                        "table_name")
    assert "b" not in constrained and "a" in constrained, constrained
    assert "b" in names(admin.run(sql="SELECT table_name FROM duckdb_constraints() WHERE schema_name = 'pair'"),
                        "table_name")


def test_a_foreign_key_to_a_table_the_user_cannot_access_is_not_listed(admin, joe):
    # joe may read both child tables; only a_child's key names a table he may access.
    assert joe.metadata(kind="imported-keys", catalog="memory", db_schema="pair", table="a_child").num_rows == 1
    assert joe.metadata(kind="imported-keys", catalog="memory", db_schema="pair", table="b_child").num_rows == 0
    assert joe.metadata(kind="exported-keys", catalog="memory", db_schema="pair", table="b").num_rows == 0
    # The twin: the administrator sees both keys.
    assert admin.metadata(kind="imported-keys", catalog="memory", db_schema="pair", table="b_child").num_rows == 1
    assert admin.metadata(kind="exported-keys", catalog="memory", db_schema="pair", table="b").num_rows == 1


def test_schemas_are_listed_only_where_the_user_holds_something(admin, joe, server):
    for client, expected in [(joe, {"pair"}), (admin, {"main", "pair", "other"}),
                             (server.user(name="nora"), set())]:
        assert names(client.metadata(kind="db-schemas", catalog="memory"), "db_schema_name") == expected
        assert names(client.run(sql="SELECT schema_name FROM information_schema.schemata "
                                    "WHERE catalog_name = 'memory'"), "schema_name") == expected
        assert names(client.run(sql="SELECT schema_name FROM duckdb_schemas() WHERE database_name = 'memory'"),
                     "schema_name") == expected
    # DuckDB's own schemas and the session's temp catalog stay listed for everyone.
    nora = server.user(name="nora")
    schemas = {(r["catalog_name"], r["db_schema_name"]) for r in nora.metadata(kind="db-schemas").to_pylist()}
    assert {("system", "information_schema"), ("system", "pg_catalog"), ("temp", "main")} <= schemas, schemas


def test_a_user_without_grants_lists_no_tables(server, admin):
    nora = server.user(name="nora")
    assert nora.metadata(kind="tables", catalog="memory").num_rows == 0
    assert nora.run(sql="SELECT * FROM duckdb_tables() WHERE database_name = 'memory'").num_rows == 0
    # Its own temporary table is listed; the twin, the administrator's view of memory.
    expect_allowed(client=nora, sql="CREATE TEMP TABLE scratch (x INT)")
    assert "scratch" in names(nora.metadata(kind="tables", catalog="temp"), "table_name")
    assert admin.metadata(kind="tables", catalog="memory").num_rows >= len(SCHEMA_TABLES) + 1


def test_a_hidden_views_definition_is_not_listed(admin, joe):
    assert joe.run(sql="SELECT sql FROM duckdb_views() WHERE view_name = 'bv'").num_rows == 0
    assert joe.run(sql="SELECT * FROM information_schema.views WHERE table_name = 'bv'").num_rows == 0
    # The twin.
    definition = admin.run(sql="SELECT sql FROM duckdb_views() WHERE view_name = 'bv'").column("sql")[0].as_py()
    assert "pair.b" in definition


@pytest.mark.parametrize("sql", ["DESCRIBE pair.b", "DESCRIBE SELECT * FROM pair.b", "SHOW pair.b",
                                 "DESCRIBE pair.bv", "SELECT * FROM pragma_table_info('pair.b')",
                                 "SELECT * FROM pragma_show('pair.b')"])
def test_describing_a_table_the_user_cannot_access_is_refused(admin, joe, sql):
    what = "view memory.pair.bv" if "bv" in sql else "table memory.pair.b"
    expect_blocked(client=joe, sql=sql, why=f"user joe holds no privilege on {what}, so it cannot describe it")
    # The twins: joe describes a table he may read, the administrator this one.
    assert expect_allowed(client=joe, sql=sql.replace("pair.bv", "pair.a").replace("pair.b", "pair.a")).num_rows == 2
    assert expect_allowed(client=admin, sql=sql).num_rows == 2


def test_column_grants_list_only_the_granted_columns(server, admin):
    carl = server.user(name="carl")
    assert metadata_tables(carl) == {"wide"}
    assert schema_columns(client=carl, table="wide") == ["id"]
    assert schema_columns(client=admin, table="wide") == ["id", "salary"]
    for sql, column in [("SELECT column_name FROM duckdb_columns() WHERE table_name = 'wide'", "column_name"),
                        ("SELECT column_name FROM information_schema.columns WHERE table_name = 'wide'",
                         "column_name"),
                        ("SELECT name FROM pragma_table_info('pair.wide')", "name"),
                        ("SELECT column_name FROM pragma_show('pair.wide')", "column_name")]:
        assert names(carl.run(sql=sql), column) == {"id"}, sql
        assert names(admin.run(sql=sql), column) == {"id", "salary"}, sql
    # DESCRIBE shows every column: refused, saying where the readable ones are.
    for sql in ["DESCRIBE pair.wide", "DESCRIBE SELECT id FROM pair.wide"]:
        expect_blocked(client=carl, sql=sql,
                       why="user carl may read only some columns of table memory.pair.wide, and DESCRIBE shows "
                           "them all; pragma_table_info('memory.pair.wide') lists the ones it may read")
    # The twins: carl reads his column; the administrator describes the table.
    assert expect_allowed(client=carl, sql="SELECT id FROM pair.wide").num_rows == 0
    assert expect_allowed(client=admin, sql="DESCRIBE pair.wide").num_rows == 2


def test_a_table_the_user_may_only_insert_into_is_listed_with_its_columns(server):
    wanda = server.user(name="wanda")
    assert metadata_tables(wanda) == {"inbox"}
    assert schema_columns(client=wanda, table="inbox") == ["id", "body"]
    expect_allowed(client=wanda, sql="INSERT INTO pair.inbox VALUES (1, 'hi')")
    expect_blocked(client=wanda, sql="SELECT * FROM pair.inbox", why="no SELECT privilege on table")


def eventually(listing, expected: set) -> set:
    """`listing()` once it equals `expected` (a grant reaches sessions within the store's
    poll), or what it last returned."""
    deadline = time.monotonic() + 10
    while True:
        seen = listing()
        if seen == expected or time.monotonic() > deadline:
            return seen
        time.sleep(0.2)


def test_a_grant_and_a_revoke_apply_to_the_next_listing_and_to_prepared_ones(admin, joe):
    prepared = joe.prepare(sql="SELECT table_name FROM duckdb_tables() WHERE schema_name = 'pair'")
    run_prepared = lambda: names(joe.execute_prepared(prepared_id=prepared), "table_name")  # noqa: E731
    before = {"a", "a_child", "b_child"}
    assert run_prepared() == before
    expect_allowed(client=admin, sql="GRANT SELECT ON pair.b TO reader")
    try:
        assert eventually(listing=run_prepared, expected=before | {"b"}) == before | {"b"}
        assert "b" in metadata_tables(joe)
    finally:
        expect_allowed(client=admin, sql="REVOKE SELECT ON pair.b FROM reader")
    assert eventually(listing=run_prepared, expected=before) == before
    assert "b" not in metadata_tables(joe)


def test_a_tenants_user_sees_its_grants_in_its_tenants_catalog(server, admin):
    ann = server.user(name="acme/ann")
    assert names(ann.metadata(kind="tables", catalog="acme_db"), "table_name") == {"a_seen"}
    assert names(ann.run(sql="SELECT table_name FROM duckdb_tables()"), "table_name") == {"a_seen"}
    assert ann.metadata(kind="tables", catalog="beta_db").num_rows == 0
    assert "beta_db" not in names(ann.metadata(kind="catalogs"), "catalog_name")
    expect_allowed(client=ann, sql="SELECT * FROM a_seen")
    # The twin: the administrator sees both of acme's tables.
    assert names(admin.metadata(kind="tables", catalog="acme_db"), "table_name") == {"a_seen", "a_hidden"}


def test_an_administrator_token_with_rules_sees_what_its_rules_let_it_read(server):
    restricted = server.client(token=mint(user="admin", role="admin",
                                          catalog_access=[{"catalog": "memory", "access": "read"},
                                                          {"catalog": "*", "access": "none"}]))
    # Its rules read all of memory, so it sees every table there, b included, and can read b.
    assert metadata_tables(restricted) == SCHEMA_TABLES
    assert expect_allowed(client=restricted, sql="SELECT secret FROM pair.b").num_rows == 1
    # Nothing of the catalogs its rules deny.
    assert restricted.metadata(kind="tables", catalog="acme_db").num_rows == 0
    assert "acme_db" not in names(restricted.metadata(kind="catalogs"), "catalog_name")


# ---- catalog_access rules without a store (rules-only server) ----

@pytest.fixture(scope="module")
def rules_server(module_launcher):
    running = module_launcher.start(args=["--database", "metadata_rules.db"])
    session = running.admin()
    for sql in ["ATTACH ':memory:' AS rx", "ATTACH ':memory:' AS ry",
                "CREATE TABLE rx.main.one (id INT)", "CREATE TABLE rx.main.two (id INT)",
                "CREATE TABLE ry.main.three (id INT)"]:
        session.execute(sql=sql)
    return running


def test_a_rules_principal_sees_the_catalogs_its_rules_let_it_read(rules_server):
    reader = rules_server.as_role(role="user", subject="rita",
                                  catalog_access=[{"catalog": "rx", "access": "read"}, {"catalog": "*", "access": "none"}])
    assert names(reader.metadata(kind="tables", catalog="rx"), "table_name") == {"one", "two"}
    assert reader.metadata(kind="tables", catalog="ry").num_rows == 0
    assert {r["table_name"] for r in reader.rows(sql="SELECT table_name FROM duckdb_tables()")} == {"one", "two"}
    assert reader.scalar(sql="SELECT count(*) FROM rx.main.two") == 0  # it may read what it sees
    # The twin: the administrator sees ry's table.
    assert names(rules_server.admin().metadata(kind="tables", catalog="ry"), "table_name") == {"three"}
