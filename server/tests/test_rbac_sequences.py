"""Sequences under access control: USAGE, CREATE SEQUENCE and DROP SEQUENCE.

nextval() and currval() need USAGE on the sequence (ON SEQUENCE, ON SCHEMA or ON CATALOG,
or owning its schema), with the rights the statement reads with (a view's owner's inside a
view). A table's own column default is part of writing it: INSERT on the table covers the
sequence its default advances, and writing such a default (CREATE TABLE, ALTER TABLE ...
DEFAULT) needs USAGE on the sequence. setval() resets a sequence, which dropping and
recreating it would do too: DROP SEQUENCE on its schema. Sequences have no owner records
(a table of the same name is another object). Every refusal is asserted by its reason, next
to the twin that succeeds."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NO_USAGE = "has no USAGE privilege on sequence"
NEEDS_CREATE = "it needs CREATE SEQUENCE on schema"
NEEDS_DROP = "it needs DROP SEQUENCE on schema"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_sequences.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA app", "CREATE SCHEMA other",
        "CREATE SEQUENCE app.ids", "CREATE SEQUENCE app.private_ids", "CREATE SEQUENCE other.x",
        "CREATE TABLE app.orders (id BIGINT DEFAULT nextval('app.ids'), item VARCHAR)",
        "CREATE TABLE app.plain (v INT)",
        # A table and a sequence of the same name: different objects.
        "CREATE TABLE app.twin (v INT)", "CREATE SEQUENCE app.twin",
        "CREATE VIEW app.next_private AS SELECT nextval('app.private_ids') AS n",
        "CREATE ROLE writers", "CREATE ROLE users", "CREATE ROLE makers", "CREATE ROLE view_readers",
        "CREATE ROLE twin_owners",
        "GRANT INSERT, SELECT ON app.orders TO writers",
        "GRANT USAGE ON SEQUENCE app.ids TO users",
        "GRANT CREATE SEQUENCE, DROP SEQUENCE ON SCHEMA app TO makers",
        "GRANT CREATE TABLE ON SCHEMA app TO makers", "GRANT SELECT ON app.next_private TO view_readers",
        "CREATE USER wes", "CREATE USER una", "CREATE USER max", "CREATE USER vic", "CREATE USER ted",
        "GRANT writers TO wes", "GRANT users TO una", "GRANT makers TO max", "GRANT view_readers TO vic",
        "GRANT twin_owners TO ted",
    ]:
        expect_allowed(client=admin, sql=sql)
    expect_allowed(client=admin, sql="ALTER TABLE app.twin OWNER TO ROLE twin_owners")
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def user(server, name):
    return server.user(name=name)


def value(client, sql):
    return expect_allowed(client=client, sql=sql).to_pylist()[0]["n"]


# ---- nextval, currval, setval ----

def test_nextval_and_currval_need_usage(server, admin):
    wes, una = user(server, "wes"), user(server, "una")
    expect_blocked(client=wes, sql="SELECT nextval('app.ids') AS n", why=NO_USAGE + " rbac_sequences.app.ids")
    expect_blocked(client=wes, sql="SELECT currval('app.ids') AS n", why=NO_USAGE)
    # Twin: with USAGE on the sequence, both work.
    first = value(una, "SELECT nextval('app.ids') AS n")
    assert value(una, "SELECT currval('app.ids') AS n") == first
    # USAGE on one sequence is not another's.
    expect_blocked(client=una, sql="SELECT nextval('app.private_ids') AS n", why=NO_USAGE)


def test_usage_on_a_schema_or_catalog_covers_its_sequences(server, admin):
    wes = user(server, "wes")
    expect_blocked(client=wes, sql="SELECT nextval('other.x') AS n", why=NO_USAGE)
    expect_allowed(client=admin, sql="GRANT USAGE ON SCHEMA other TO writers")
    try:
        assert value(wes, "SELECT nextval('other.x') AS n") >= 1
        expect_blocked(client=wes, sql="SELECT nextval('app.private_ids') AS n", why=NO_USAGE)
    finally:
        expect_allowed(client=admin, sql="REVOKE USAGE ON SCHEMA other FROM writers")
    expect_blocked(client=wes, sql="SELECT nextval('other.x') AS n", why=NO_USAGE)


def test_setval_needs_drop_sequence(server, admin):
    una, max_ = user(server, "una"), user(server, "max")
    expect_blocked(client=una, sql="SELECT setval('app.ids', 1000) AS n", why=NEEDS_DROP)
    # Twin: DROP SEQUENCE on the schema lets max reset it (he could drop and recreate it).
    assert value(max_, "SELECT setval('app.ids', 1000) AS n") == 1000


def test_a_tables_default_is_covered_by_insert_on_the_table(server, admin):
    wes = user(server, "wes")
    # wes holds INSERT on app.orders and nothing on app.ids: the table's own default
    # advances it, however the default reaches the plan.
    for sql in ["INSERT INTO app.orders (item) VALUES ('a')", "INSERT INTO app.orders VALUES (DEFAULT, 'b')",
                "INSERT INTO app.orders DEFAULT VALUES"]:
        expect_allowed(client=wes, sql=sql)
    ids = [r["id"] for r in expect_allowed(client=wes, sql="SELECT id FROM app.orders ORDER BY id").to_pylist()]
    assert len(ids) == 3 and len(set(ids)) == 3
    # But only its own default: a sequence named in the statement needs USAGE.
    expect_blocked(client=wes, sql="INSERT INTO app.orders VALUES (nextval('app.private_ids'), 'c')", why=NO_USAGE)


def test_writing_a_default_that_names_a_sequence_needs_usage(server, admin):
    max_ = user(server, "max")
    expect_blocked(client=max_, sql="CREATE TABLE app.mine (id BIGINT DEFAULT nextval('app.private_ids'))",
                   why=NO_USAGE + " rbac_sequences.app.private_ids")
    # Temporary tables too: their inserts would advance the sequence.
    expect_blocked(client=max_, sql="CREATE TEMP TABLE scratch (id BIGINT DEFAULT nextval('app.private_ids'))",
                   why=NO_USAGE)
    expect_allowed(client=admin, sql="GRANT ALTER TABLE ON SCHEMA app TO makers")
    expect_allowed(client=max_, sql="CREATE TABLE app.mine2 (v INT)")
    # DuckDB adds the column, then fills it with an UPDATE that calls nextval(): that
    # statement is refused as it runs (an execution error, with the same reason).
    with pytest.raises(ChargewireError) as raised:
        max_.run(sql="ALTER TABLE app.mine2 ADD COLUMN id BIGINT DEFAULT nextval('app.private_ids')")
    assert NO_USAGE + " rbac_sequences.app.private_ids" in raised.value.detail
    assert "id" not in [r["column_name"] for r in admin.run(sql="DESCRIBE app.mine2").to_pylist()]
    expect_blocked(client=max_, sql="ALTER TABLE app.mine2 ALTER COLUMN v SET DEFAULT nextval('app.private_ids')",
                   why=NO_USAGE)
    # Twin: a sequence the creator may use.
    expect_allowed(client=admin, sql="GRANT USAGE ON SEQUENCE app.private_ids TO makers")
    try:
        expect_allowed(client=max_, sql="CREATE TABLE app.mine (id BIGINT DEFAULT nextval('app.private_ids'))")
        expect_allowed(client=max_, sql="ALTER TABLE app.mine2 ALTER COLUMN v SET DEFAULT nextval('app.private_ids')")
    finally:
        expect_allowed(client=admin, sql="REVOKE USAGE ON SEQUENCE app.private_ids FROM makers")


def test_inside_a_view_the_owners_usage_counts(server, admin):
    vic, una = user(server, "vic"), user(server, "una")
    # The administrator's view advances a sequence vic holds nothing on: allowed, since
    # the view reads with its owner's rights.
    assert value(vic, "SELECT n FROM app.next_private") >= 1
    # Twin: called directly, vic needs USAGE himself.
    expect_blocked(client=vic, sql="SELECT nextval('app.private_ids') AS n", why=NO_USAGE)
    # A user's view reads with its owner's (the user's) rights.
    expect_allowed(client=admin, sql="GRANT CREATE VIEW ON SCHEMA app TO users")
    expect_allowed(client=una, sql="CREATE VIEW app.una_next AS SELECT nextval('app.ids') AS n")
    expect_allowed(client=admin, sql="GRANT SELECT ON app.una_next TO view_readers")
    assert value(vic, "SELECT n FROM app.una_next") >= 1
    expect_allowed(client=admin, sql="REVOKE USAGE ON SEQUENCE app.ids FROM users")
    try:
        expect_blocked(client=vic, sql="SELECT n FROM app.una_next", why="its owner lacks a privilege")
    finally:
        expect_allowed(client=admin, sql="GRANT USAGE ON SEQUENCE app.ids TO users")


def test_inside_a_macro_the_callers_usage_counts(server, admin):
    expect_allowed(client=admin, sql="CREATE MACRO app.next_id() AS nextval('app.ids')")
    wes, una = user(server, "wes"), user(server, "una")
    expect_blocked(client=wes, sql="SELECT app.next_id() AS n", why=NO_USAGE)
    assert value(una, "SELECT app.next_id() AS n") >= 1


def test_owning_a_table_of_the_same_name_grants_nothing_on_the_sequence(server, admin):
    ted = user(server, "ted")
    # ted owns the table app.twin; the sequence app.twin is another object.
    expect_allowed(client=ted, sql="INSERT INTO app.twin VALUES (1)")
    expect_blocked(client=ted, sql="SELECT nextval('app.twin') AS n", why=NO_USAGE)
    # Twin: a USAGE grant on the sequence.
    expect_allowed(client=admin, sql="GRANT USAGE ON SEQUENCE app.twin TO twin_owners")
    assert value(ted, "SELECT nextval('app.twin') AS n") >= 1
    grants = admin.run(sql="SHOW GRANTS FOR twin_owners").to_pylist()
    assert {(g["privilege"], g["object_type"]) for g in grants} >= {("USAGE", "SEQUENCE")}


def test_temporary_sequences_are_the_sessions_own(server):
    wes = user(server, "wes")
    expect_allowed(client=wes, sql="CREATE TEMP SEQUENCE mine")
    assert value(wes, "SELECT nextval('mine') AS n") == 1


# ---- CREATE / DROP SEQUENCE ----

def test_create_sequence_needs_the_privilege_on_the_schema(server, admin):
    una, max_ = user(server, "una"), user(server, "max")
    expect_blocked(client=una, sql="CREATE SEQUENCE app.unas", why=NEEDS_CREATE)
    expect_allowed(client=max_, sql="CREATE SEQUENCE app.maxs")
    # The schema privileges don't include USAGE: max grants it, or holds it on the schema.
    expect_blocked(client=max_, sql="SELECT nextval('app.maxs') AS n", why=NO_USAGE)
    expect_blocked(client=max_, sql="CREATE SEQUENCE other.maxs", why=NEEDS_CREATE)


def test_replace_and_drop_need_drop_sequence(server, admin):
    expect_allowed(client=admin, sql="CREATE ROLE creators_only")
    expect_allowed(client=admin, sql="GRANT CREATE SEQUENCE ON SCHEMA other TO creators_only")
    expect_allowed(client=admin, sql="CREATE USER cora")
    expect_allowed(client=admin, sql="GRANT creators_only TO cora")
    cora = user(server, "cora")
    expect_allowed(client=cora, sql="CREATE SEQUENCE other.coras")
    expect_blocked(client=cora, sql="CREATE OR REPLACE SEQUENCE other.coras", why="may not replace sequence")
    expect_blocked(client=cora, sql="DROP SEQUENCE other.coras", why=NEEDS_DROP)
    # Twin: with DROP SEQUENCE.
    expect_allowed(client=admin, sql="GRANT DROP SEQUENCE ON SCHEMA other TO creators_only")
    expect_allowed(client=cora, sql="CREATE OR REPLACE SEQUENCE other.coras")
    expect_allowed(client=cora, sql="DROP SEQUENCE other.coras")


def test_a_schema_owner_holds_the_sequence_privileges(server, admin):
    expect_allowed(client=admin, sql="CREATE ROLE schema_makers")
    expect_allowed(client=admin, sql="GRANT CREATE SCHEMA ON CATALOG rbac_sequences TO schema_makers")
    expect_allowed(client=admin, sql="CREATE USER sol")
    expect_allowed(client=admin, sql="GRANT schema_makers TO sol")
    sol = user(server, "sol")
    expect_allowed(client=sol, sql="CREATE SCHEMA sols")
    expect_allowed(client=sol, sql="CREATE SEQUENCE sols.s")
    assert value(sol, "SELECT nextval('sols.s') AS n") == 1
    assert value(sol, "SELECT setval('sols.s', 10) AS n") == 10
    expect_allowed(client=sol, sql="DROP SEQUENCE sols.s")


def test_a_sequence_a_default_or_view_names_cannot_be_replaced_or_stood_in_for(server, admin):
    max_ = user(server, "max")
    # app.orders' default calls nextval('app.ids'): replacing app.ids would restart its ids.
    expect_blocked(client=max_, sql="CREATE OR REPLACE SEQUENCE app.ids START 1", why="would change the numbers")
    # The view next_private names app.private_ids.
    expect_blocked(client=max_, sql="CREATE OR REPLACE SEQUENCE app.private_ids", why="would change the numbers")
    # Twin: a name nothing uses.
    expect_allowed(client=max_, sql="CREATE OR REPLACE SEQUENCE app.free_name")


def test_the_grant_takes_usage_alone_on_a_sequence(admin):
    for sql, why in [("GRANT SELECT ON SEQUENCE app.ids TO users", "on a sequence only USAGE is granted"),
                     ("GRANT USAGE ON app.plain TO users", "USAGE is granted ON SEQUENCE, ON SCHEMA or ON CATALOG"),
                     ("GRANT CREATE SEQUENCE ON app.plain TO users", "is granted ON SCHEMA or ON CATALOG"),
                     ("GRANT USAGE ON SEQUENCE app.no_such TO users", "sequence app.no_such does not exist")]:
        with pytest.raises(ChargewireError) as raised:
            admin.run(sql=sql)
        assert why in raised.value.detail, raised.value.detail


# ---- listing ----

def test_duckdb_sequences_lists_what_the_user_may_use(server, admin):
    una, wes = user(server, "una"), user(server, "wes")

    def listed(client):
        rows = client.run(sql="SELECT schema_name || '.' || sequence_name AS s FROM duckdb_sequences() "
                              "WHERE database_name = 'rbac_sequences'").to_pylist()
        return {r["s"] for r in rows}

    assert "app.ids" in listed(una) and "app.private_ids" not in listed(una)
    assert "app.ids" not in listed(wes)
    assert {"app.ids", "app.private_ids"} <= listed(admin)
