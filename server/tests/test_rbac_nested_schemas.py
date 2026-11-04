"""Nested schemas (DuckDB 2.0: CREATE SCHEMA sales; CREATE SCHEMA sales.payroll) under access
control (issue #19).

A grant records a schema's whole path: a grant on sales.payroll covers sales.payroll.* and
nothing alike (not hr.payroll, not the top-level schema named "sales.payroll", not sales_eu); a
grant on the parent covers its children. Names in GRANT, policies and ownership are read the
way DuckDB binds them (a catalog first, else a schema path of the session's catalog), and one
that reads both ways is refused. Introspection and Chargewire metadata list nested objects to
whoever may see them, naming a nested schema by its path ("sales.payroll" quoted when that is a
top-level schema's name). A child schema is created with CREATE SCHEMA on its parent (ON
SCHEMA, ON CATALOG, or owning the parent), its creator owns it, and DROP ... CASCADE needs the
right to drop every child. Every refusal is asserted by its reason next to its twin."""

import pyarrow as pa
import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NO_SELECT = "has no SELECT privilege on table"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "nested.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA sales", "CREATE SCHEMA sales.payroll", "CREATE SCHEMA sales.payroll.deep",
        'CREATE SCHEMA "sales.payroll"', "CREATE SCHEMA hr", "CREATE SCHEMA hr.payroll", "CREATE SCHEMA sales_eu",
        "CREATE TABLE sales.orders (id INT)", "INSERT INTO sales.orders VALUES (1)",
        "CREATE TABLE sales.payroll.salaries (emp VARCHAR PRIMARY KEY, pay INT, region VARCHAR)",
        "INSERT INTO sales.payroll.salaries VALUES ('ann', 10, 'EU'), ('bob', 20, 'US')",
        "CREATE TABLE sales.payroll.deep.d (y INT)", "INSERT INTO sales.payroll.deep.d VALUES (7)",
        'CREATE TABLE "sales.payroll".t (x INT)', 'INSERT INTO "sales.payroll".t VALUES (1)',
        "CREATE TABLE hr.payroll.salaries (emp VARCHAR, pay INT)", "INSERT INTO hr.payroll.salaries VALUES ('hank', 30)",
        "CREATE TABLE sales_eu.t (x INT)",
        "CREATE SEQUENCE sales.payroll.seq",
        "CREATE ROLE analyst", "CREATE ROLE payroll_r", "CREATE ROLE quoted_r", "CREATE ROLE maker", "CREATE ROLE sub_maker",
        "CREATE USER ann", "CREATE USER pam", "CREATE USER quin", "CREATE USER mo", "CREATE USER sue", "CREATE USER nob",
        "GRANT analyst TO ann", "GRANT payroll_r TO pam", "GRANT quoted_r TO quin", "GRANT maker TO mo", "GRANT sub_maker TO sue",
        "GRANT SELECT ON SCHEMA sales TO analyst",
        "GRANT SELECT ON SCHEMA sales.payroll TO payroll_r",
        'GRANT SELECT ON SCHEMA "sales.payroll" TO quoted_r',
        "GRANT CREATE SCHEMA ON SCHEMA sales TO sub_maker",
    ]:
        expect_allowed(client=admin, sql=sql)
    catalog = admin.run(sql="SELECT current_database() AS c").column("c").to_pylist()[0]
    expect_allowed(client=admin, sql=f"GRANT CREATE SCHEMA ON CATALOG {catalog} TO maker")
    server.admin_client = admin
    server.catalog = catalog
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def user(server, name):
    return server.user(name=name)


def rows(client, sql):
    return client.run(sql=sql).to_pylist()


# ---- enforcement (regression: what already worked, with the twins) ----

def test_a_grant_on_a_nested_schema_covers_it_and_nothing_alike(server):
    pam = user(server, "pam")
    assert rows(pam, "SELECT emp FROM sales.payroll.salaries ORDER BY 1") == [{"emp": "ann"}, {"emp": "bob"}]
    assert rows(pam, "SELECT y FROM sales.payroll.deep.d") == [{"y": 7}]  # a child of the granted schema
    for sql in ["SELECT * FROM hr.payroll.salaries", 'SELECT * FROM "sales.payroll".t', "SELECT * FROM sales_eu.t",
                "SELECT * FROM sales.orders"]:
        expect_blocked(client=pam, sql=sql, why=NO_SELECT)


def test_a_grant_on_the_parent_covers_its_children_but_not_the_quoted_twin(server):
    ann = user(server, "ann")
    for sql in ["SELECT * FROM sales.orders", "SELECT * FROM sales.payroll.salaries", "SELECT * FROM sales.payroll.deep.d"]:
        expect_allowed(client=ann, sql=sql)
    expect_blocked(client=ann, sql='SELECT * FROM "sales.payroll".t', why=f'no SELECT privilege on table {server.catalog}."sales.payroll".t')
    expect_blocked(client=ann, sql="SELECT * FROM hr.payroll.salaries", why=f"no SELECT privilege on table {server.catalog}.hr.payroll.salaries")
    # The quoted top-level twin's role sees that table and no nested one.
    quin = user(server, "quin")
    assert rows(quin, 'SELECT x FROM "sales.payroll".t') == [{"x": 1}]
    expect_blocked(client=quin, sql="SELECT * FROM sales.payroll.salaries", why=f"no SELECT privilege on table {server.catalog}.sales.payroll.salaries")


def test_a_nested_schema_is_reached_only_by_its_whole_path(server):
    ann = user(server, "ann")
    with pytest.raises(ChargewireError) as raised:
        ann.run(sql="SELECT * FROM payroll.salaries")
    assert 'schema "payroll" does not exist' in raised.value.detail


# ---- names in GRANT, policies and ownership (gap 1) ----

def test_names_without_the_catalog_resolve_as_duckdb_binds_them(server, admin):
    nob = user(server, "nob")
    expect_allowed(client=admin, sql="CREATE ROLE nested_readers")
    expect_allowed(client=admin, sql="GRANT nested_readers TO nob")
    expect_blocked(client=nob, sql="SELECT * FROM sales.payroll.salaries", why=NO_SELECT)
    # A table, a sequence, a row policy, a masking policy and ownership, each by the path as
    # a query writes it (no catalog), land on the nested object.
    result = expect_allowed(client=admin, sql="GRANT SELECT ON sales.payroll.salaries TO nested_readers").to_pylist()
    assert result == [{"result": f"Granted SELECT on {server.catalog}.sales.payroll.salaries to nested_readers"}]
    expect_allowed(client=admin, sql="GRANT USAGE ON SEQUENCE sales.payroll.seq TO nested_readers")
    expect_allowed(client=admin, sql="CREATE ROW POLICY eu_only ON sales.payroll.salaries TO nested_readers USING (region = 'EU')")
    expect_allowed(client=admin, sql="CREATE MASKING POLICY hide_pay AS (val INT) RETURNS INT -> 0")
    expect_allowed(client=admin, sql="ALTER TABLE sales.payroll.salaries MODIFY COLUMN pay SET MASKING POLICY hide_pay")
    assert rows(nob, "SELECT emp, pay FROM sales.payroll.salaries") == [{"emp": "ann", "pay": 0}]
    assert rows(nob, "SELECT nextval('sales.payroll.seq') AS n") == [{"n": 1}]
    # Undo what the other tests don't expect, and the revoke by the same path closes the table.
    for sql in ["ALTER TABLE sales.payroll.salaries MODIFY COLUMN pay UNSET MASKING POLICY", "DROP MASKING POLICY hide_pay",
                "DROP ROW POLICY eu_only ON sales.payroll.salaries", "REVOKE SELECT ON sales.payroll.salaries FROM nested_readers"]:
        expect_allowed(client=admin, sql=sql)
    expect_blocked(client=nob, sql="SELECT * FROM sales.payroll.salaries", why=NO_SELECT)
    # Ownership by that path too (a nested table has an owner like any other).
    expect_allowed(client=admin, sql="CREATE TABLE sales.payroll.owned (x INT)")
    expect_allowed(client=admin, sql="ALTER TABLE sales.payroll.owned OWNER TO nested_readers")
    assert rows(admin, "SELECT schema_name, object_name FROM hardcharger_grants() WHERE privilege = 'OWNERSHIP' AND grantee = 'nested_readers'") == [
        {"schema_name": "sales.payroll", "object_name": "owned"}]
    assert rows(nob, "SELECT count(*) AS n FROM sales.payroll.owned") == [{"n": 0}]
    expect_allowed(client=admin, sql="DROP TABLE sales.payroll.owned")  # the listing tests expect the fixture's tables


def test_a_name_that_reads_as_a_catalog_and_as_a_schema_path_is_refused(server, admin):
    # A catalog named like the top-level schema, with the same path inside it.
    for sql in ["ATTACH ':memory:' AS sales", "CREATE SCHEMA sales.payroll", "CREATE TABLE sales.payroll.salaries (emp VARCHAR)"]:
        expect_allowed(client=admin, sql=sql)
    try:
        for sql in ["GRANT SELECT ON sales.payroll.salaries TO analyst", "GRANT SELECT ON SCHEMA sales.payroll TO analyst",
                    "CREATE ROW POLICY amb ON sales.payroll.salaries USING (true)"]:
            with pytest.raises(ChargewireError) as raised:
                admin.run(sql=sql)
            assert "is ambiguous" in raised.value.detail and "write the nested one with its catalog" in raised.value.detail, (sql, raised.value.detail)
        # Twin: with its catalog the nested one is a grant of its own (a query's
        # sales.payroll.salaries reads the catalog's, as DuckDB binds it).
        expect_allowed(client=admin, sql=f"GRANT SELECT ON {server.catalog}.sales.payroll.salaries TO analyst")
        expect_allowed(client=admin, sql=f"REVOKE SELECT ON {server.catalog}.sales.payroll.salaries FROM analyst")
    finally:
        admin.run(sql="DETACH sales")


# ---- introspection and metadata (gaps 2 and 3) ----

def test_listings_show_the_nested_objects_a_user_may_see(server):
    ann, quin, pam = user(server, "ann"), user(server, "quin"), user(server, "pam")
    tables = lambda c: {(r["schema_name"], r["table_name"]) for r in rows(c, "SELECT schema_name, table_name FROM duckdb_tables()")}
    assert tables(ann) == {("sales", "orders"), ("payroll", "salaries"), ("deep", "d")}
    assert tables(pam) == {("payroll", "salaries"), ("deep", "d")}
    assert tables(quin) == {("sales.payroll", "t")}
    schemas = lambda c: {r["schema_name"] for r in rows(c, "SELECT schema_name FROM duckdb_schemas() WHERE database_name = current_database()")}
    assert schemas(ann) == {"sales", "payroll", "deep"}
    assert schemas(quin) == {"sales.payroll"}
    assert {r["table_schema"] for r in rows(ann, "SELECT table_schema FROM information_schema.tables WHERE table_catalog = current_database()")} == {"sales", "payroll", "deep"}
    assert {r["column_name"] for r in rows(pam, "SELECT column_name FROM duckdb_columns() WHERE table_name = 'salaries'")} == {"emp", "pay", "region"}
    assert rows(pam, "SELECT column_name FROM duckdb_columns() WHERE table_name = 't'") == []
    # DuckDB's listings name a schema by its leaf; two of them say payroll. The parent tells them apart.
    assert sorted((r["schema_name"], r["parent_schema"]) for r in rows(server.admin_client, "SELECT schema_name, parent_schema FROM duckdb_schemas() WHERE schema_name = 'payroll'")) == [("payroll", "hr"), ("payroll", "sales")]


def test_metadata_names_a_nested_schema_by_its_path(server):
    """Chargewire's metadata calls (and the web UI's browser through them) name a nested schema
    sales.payroll, and a top-level schema with a dot in its name "sales.payroll", and take that
    text back in their filters."""
    ann, quin, admin = user(server, "ann"), user(server, "quin"), server.admin_client
    cat = server.catalog
    names = lambda t, col: [r[col] for r in t.to_pylist()]
    assert names(ann.metadata("db-schemas", catalog=cat), "db_schema_name") == ["sales", "sales.payroll", "sales.payroll.deep"]
    assert names(quin.metadata("db-schemas", catalog=cat), "db_schema_name") == ['"sales.payroll"']
    assert [(r["db_schema_name"], r["table_name"]) for r in ann.metadata("tables", catalog=cat).to_pylist()] == [
        ("sales", "orders"), ("sales.payroll", "salaries"), ("sales.payroll.deep", "d")]
    assert names(ann.metadata("tables", catalog=cat, db_schema_filter_pattern="sales.payroll"), "table_name") == ["salaries"]
    assert names(quin.metadata("tables", catalog=cat, db_schema_filter_pattern='"sales.payroll"'), "table_name") == ["t"]
    assert names(admin.metadata("tables", catalog=cat, db_schema_filter_pattern="hr.payroll"), "table_name") == ["salaries"]
    # The per-table schema carries the same name, and the columns a user may see.
    described = ann.metadata("tables", catalog=cat, db_schema_filter_pattern="sales.payroll", include_schema="true").to_pylist()
    schema = pa.ipc.read_schema(pa.BufferReader(described[0]["table_schema"]))
    assert schema.names == ["emp", "pay", "region"]
    assert schema.field("emp").metadata[b"ARROW:FLIGHT:SQL:DB_SCHEMA_NAME"] == b"sales.payroll"
    assert names(ann.metadata("primary-keys", catalog=cat, db_schema="sales.payroll", table="salaries"), "column_name") == ["emp"]
    assert ann.metadata("primary-keys", catalog=cat, db_schema="payroll", table="salaries").num_rows == 0  # a leaf is no name
    assert quin.metadata("tables", catalog=cat, db_schema_filter_pattern="sales.payroll").num_rows == 0


def test_grant_listings_tell_the_nested_schema_from_the_quoted_one(server, admin):
    grants = {(r["grantee"], r["schema_name"]) for r in rows(admin, "SELECT grantee, schema_name FROM hardcharger_grants() WHERE privilege = 'SELECT' AND object_type = 'SCHEMA'")}
    assert {("payroll_r", "sales.payroll"), ("quoted_r", '"sales.payroll"'), ("analyst", "sales")} <= grants
    assert [r["object"] for r in rows(admin, "SHOW GRANTS FOR ROLE quoted_r")] == [f'{server.catalog}."sales.payroll"']
    assert [r["object"] for r in rows(admin, "SHOW GRANTS FOR ROLE payroll_r")] == [f"{server.catalog}.sales.payroll"]
    pam = user(server, "pam")
    assert [r["schema_name"] for r in rows(pam, "SELECT schema_name FROM hardcharger_effective_privileges() WHERE privilege = 'SELECT'")] == ["sales.payroll"]


# ---- child schemas (gap 4) ----

def test_create_schema_on_the_parent_or_owning_it_creates_a_child(server, admin):
    sue, mo, nob = user(server, "sue"), user(server, "mo"), user(server, "nob")
    expect_blocked(client=nob, sql="CREATE SCHEMA sales.nobs", why=f"no CREATE SCHEMA privilege on schema {server.catalog}.sales (its parent)")
    expect_blocked(client=sue, sql="CREATE SCHEMA hr.sues", why=f"no CREATE SCHEMA privilege on schema {server.catalog}.hr (its parent)")
    expect_blocked(client=sue, sql="CREATE SCHEMA sues", why=f"no CREATE SCHEMA privilege on catalog {server.catalog}")
    # ON SCHEMA sales: a child of sales, and of its children; the creator owns it and so may nest further.
    expect_allowed(client=sue, sql="CREATE SCHEMA sales.sues")
    expect_allowed(client=sue, sql="CREATE SCHEMA sales.payroll.sues")
    expect_allowed(client=sue, sql="CREATE SCHEMA sales.sues.inner")
    owned = {r["schema_name"] for r in rows(admin, "SELECT schema_name FROM hardcharger_grants() WHERE privilege = 'OWNERSHIP' AND grantee = 'sue'")}
    assert owned == {"sales.sues", "sales.payroll.sues", "sales.sues.inner"}
    # ON CATALOG covers every level; owning the parent too (mo owns tops, so tops.child is his to make).
    expect_allowed(client=mo, sql="CREATE SCHEMA tops")
    expect_allowed(client=mo, sql="CREATE SCHEMA tops.child")
    expect_allowed(client=mo, sql="CREATE SCHEMA sales.mos")
    # The owner of a child drops it; the parent's owner too; a stranger not.
    expect_blocked(client=nob, sql="DROP SCHEMA sales.sues.inner", why=f"may not drop schema {server.catalog}.sales.sues.inner")
    expect_allowed(client=sue, sql="DROP SCHEMA sales.sues.inner")
    expect_allowed(client=admin, sql="CREATE SCHEMA tops.child.admins")
    expect_allowed(client=mo, sql="DROP SCHEMA tops.child.admins")  # mo owns tops.child, the parent
    expect_allowed(client=mo, sql="DROP SCHEMA tops.child")
    expect_allowed(client=mo, sql="DROP SCHEMA tops")
    expect_allowed(client=mo, sql="DROP SCHEMA sales.mos")


def test_drop_schema_cascade_needs_the_right_to_drop_every_child(server, admin):
    sue = user(server, "sue")
    expect_allowed(client=sue, sql="CREATE SCHEMA sales.tree")             # sue owns it
    expect_allowed(client=admin, sql="CREATE SCHEMA sales.tree.theirs")    # a child sue may drop: she owns its parent
    expect_allowed(client=admin, sql="CREATE SCHEMA sales.tree.theirs.deeper")  # not this one: its parent is the administrator's
    expect_allowed(client=admin, sql="CREATE TABLE sales.tree.theirs.deeper.t (x INT)")
    with pytest.raises(ChargewireError) as raised:
        sue.run(sql="DROP SCHEMA sales.tree")
    assert "depend on it" in raised.value.detail  # DuckDB: a schema with children needs CASCADE
    expect_blocked(client=sue, sql="DROP SCHEMA sales.tree CASCADE",
                   why=f"may not drop schema {server.catalog}.sales.tree.theirs.deeper (only its owner, or a role granted DROP SCHEMA)")
    assert rows(admin, "SELECT count(*) AS n FROM duckdb_schemas() WHERE schema_name = 'deeper'") == [{"n": 1}]
    # Twin: DROP SCHEMA on the tree covers every schema under it.
    expect_allowed(client=admin, sql="GRANT DROP SCHEMA ON SCHEMA sales.tree TO sub_maker")
    expect_allowed(client=sue, sql="DROP SCHEMA sales.tree CASCADE")
    assert rows(admin, "SELECT count(*) AS n FROM duckdb_schemas() WHERE schema_name IN ('tree', 'theirs', 'deeper')") == [{"n": 0}]


def test_the_schema_privileges_are_granted_on_a_schema_too(admin):
    expect_allowed(client=admin, sql="GRANT CREATE SCHEMA, DROP SCHEMA ON SCHEMA hr TO maker")
    assert {(r["privilege"], r["object_type"]) for r in rows(admin, "SHOW GRANTS FOR ROLE maker") if r["object"].endswith(".hr")} == {
        ("CREATE SCHEMA", "SCHEMA"), ("DROP SCHEMA", "SCHEMA")}
    expect_allowed(client=admin, sql="REVOKE CREATE SCHEMA, DROP SCHEMA ON SCHEMA hr FROM maker")
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="GRANT CREATE SCHEMA ON sales.orders TO maker")
    assert "ON CATALOG, or ON SCHEMA for the schemas nested in it" in raised.value.detail


# ---- the macro and sequence guards (gap 5) ----

def test_the_macro_guard_reads_a_macros_whole_path(server, admin):
    for sql in ["CREATE SCHEMA tools", "CREATE SCHEMA tools.inner", 'CREATE SCHEMA "tools.inner"', "CREATE SCHEMA other",
                "CREATE SCHEMA other.inner",
                "CREATE MACRO tools.inner.fmt(x) AS x * 100",
                "CREATE VIEW sales.cents AS SELECT tools.inner.fmt(id) AS c FROM sales.orders",
                "CREATE ROLE macro_makers", "CREATE USER mia", "GRANT macro_makers TO mia",
                "GRANT CREATE MACRO, DROP MACRO ON SCHEMA tools TO macro_makers",
                'GRANT CREATE MACRO, DROP MACRO ON SCHEMA "tools.inner" TO macro_makers',
                "GRANT CREATE MACRO, DROP MACRO ON SCHEMA other TO macro_makers"]:
        expect_allowed(client=admin, sql=sql)
    mia = user(server, "mia")
    # The view calls tools.inner.fmt: replacing it would change what the view computes.
    expect_blocked(client=mia, sql="CREATE OR REPLACE MACRO tools.inner.fmt(x) AS 0",
                   why="would change what a view, a trigger, a row or masking policy, or a macro one of them uses computes")
    # Twins: the quoted top-level "tools.inner" and the same leaf under another parent are other macros.
    expect_allowed(client=mia, sql='CREATE MACRO "tools.inner".fmt(x) AS 0')
    expect_allowed(client=mia, sql="CREATE MACRO other.inner.fmt(x) AS 0")
    assert rows(admin, "SELECT c FROM sales.cents") == [{"c": 100}]


def test_the_sequence_guard_reads_a_sequences_whole_path(server, admin):
    for sql in ["CREATE SCHEMA app", "CREATE SCHEMA app.ids", "CREATE SCHEMA elsewhere", "CREATE SCHEMA elsewhere.ids",
                "CREATE SEQUENCE app.ids.order_ids",
                "CREATE TABLE app.orders (id INT DEFAULT nextval('app.ids.order_ids'))",
                "CREATE ROLE seq_makers", "CREATE USER sid", "GRANT seq_makers TO sid",
                "GRANT CREATE SEQUENCE, DROP SEQUENCE ON SCHEMA app TO seq_makers",
                "GRANT CREATE SEQUENCE, DROP SEQUENCE ON SCHEMA elsewhere TO seq_makers"]:
        expect_allowed(client=admin, sql=sql)
    sid = user(server, "sid")
    expect_blocked(client=sid, sql="CREATE OR REPLACE SEQUENCE app.ids.order_ids START 1000", why="would change the numbers")
    expect_allowed(client=sid, sql="CREATE SEQUENCE elsewhere.ids.order_ids START 1000")  # the same leaf elsewhere
    expect_allowed(client=sid, sql="CREATE SEQUENCE app.ids.free_name")
