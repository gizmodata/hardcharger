"""The names in a trigger's body, under access control.

DuckDB keeps a trigger's action as written and binds it again in every statement that fires
it, in the writer's session: a table, view or sequence the action names without its catalog
and schema follows the writer's search path, the writer's temporary objects first. Before
this was checked, a writer with a temporary table named like the one an audit trigger writes
had the audit row land in its own table (the real one got nothing), and one named like the
table a user's trigger writes read there what the trigger's owner may read.

Now each name in a body must be, in the writer's session, the object it is for the trigger's
own table, or the statement is refused, saying which trigger and which name; and CREATE
TRIGGER stores the action with its names in full, so a new trigger depends on no session.
The triggers of before are in a fixture a 2.13.0 server wrote (the lists below are what it
ran: fixtures/make_trigger_names_fixture.py). Every refusal is asserted by its reason, next
to the twin that succeeds."""

import gzip
import pathlib

import pytest

from rbac import STORE_KIND, ChargewireError, expect_allowed, expect_blocked, start_rbac_server

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
LEGACY = "trigger_names_213"  # the fixture database's file name, so its catalog's name

# ---- what the 2.13.0 fixture holds ----

LEGACY_OBJECTS = [
    "CREATE SCHEMA app", "CREATE SCHEMA other", "CREATE SCHEMA ws",
    # An audit trail in main. Its triggers name their table, view and sequence bare.
    "CREATE TABLE main.audit_log (what VARCHAR, n BIGINT)", "CREATE SEQUENCE main.audit_seq START 100",
    "CREATE VIEW main.rates AS SELECT 7 AS rate",
    "CREATE TABLE main.payments (id INT)",
    "CREATE TRIGGER pay_audit AFTER INSERT ON main.payments FOR EACH STATEMENT "
    "INSERT INTO audit_log SELECT 'payment ' || rate, nextval('audit_seq') FROM rates",
    "CREATE TABLE main.accounts (id INT, balance INT)", "INSERT INTO main.accounts VALUES (1, 10), (2, 20)",
    "CREATE TRIGGER acct_before BEFORE UPDATE ON main.accounts FOR EACH STATEMENT INSERT INTO audit_log VALUES ('before update', 0)",
    "CREATE TRIGGER acct_upd AFTER UPDATE ON main.accounts REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
    "INSERT INTO audit_log SELECT 'updated', id FROM fresh",
    "CREATE TRIGGER acct_del AFTER DELETE ON main.accounts FOR EACH ROW INSERT INTO audit_log VALUES ('deleted', OLD.id)",
    # A chain: this trigger's body writes payments, which fires pay_audit.
    "CREATE TABLE main.inbox (id INT)",
    "CREATE TRIGGER inbox_ins AFTER INSERT ON main.inbox REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
    "INSERT INTO payments SELECT id FROM fresh",
    # A table in another schema. Its trigger's table named bare, in the table's own schema ...
    "CREATE TABLE app.orders (id INT)", "CREATE TABLE app.order_log (what VARCHAR)",
    # ... named with its schema ...
    "CREATE TABLE app.orders2 (id INT)",
    "CREATE TRIGGER orders2_audit AFTER INSERT ON app.orders2 FOR EACH STATEMENT INSERT INTO app.order_log VALUES ('order2')",
    # ... bare, where both that schema and main have one ...
    "CREATE TABLE app.dups (id INT)", "CREATE TABLE app.dup_log (what VARCHAR)", "CREATE TABLE main.dup_log (what VARCHAR)",
    # ... and bare, in neither (the creator's own search path found it).
    "CREATE TABLE main.elsewhere (id INT)", "CREATE TABLE other.elsewhere_log (what VARCHAR)",
    "USE app",
    "CREATE TRIGGER orders_audit AFTER INSERT ON app.orders FOR EACH STATEMENT INSERT INTO order_log VALUES ('order')",
    "CREATE TRIGGER dups_audit AFTER INSERT ON app.dups FOR EACH STATEMENT INSERT INTO dup_log VALUES ('dup')",
    "USE other",
    "CREATE TRIGGER elsewhere_audit AFTER INSERT ON main.elsewhere FOR EACH STATEMENT "
    "INSERT INTO elsewhere_log VALUES ('elsewhere')",
    "USE main",
    # What a user's trigger reads with its owner's rights, and where it puts it.
    "CREATE TABLE main.other_secret (note VARCHAR)", "INSERT INTO main.other_secret VALUES ('other-secret')",
    "CREATE TABLE main.msink (note VARCHAR)",
]
LEGACY_ACCESS = [
    "CREATE ROLE writers", "CREATE ROLE makers", "CREATE USER wes", "CREATE USER mia",
    "GRANT writers TO wes", "GRANT makers TO mia",
    "GRANT CREATE TABLE, CREATE TRIGGER ON SCHEMA app TO makers", "GRANT SELECT ON main.other_secret TO makers",
    "GRANT INSERT ON main.msink TO makers",
    # The administrator's triggers run with the writer's rights: the writers may write the trail.
    "GRANT INSERT ON main.audit_log TO writers", "GRANT SELECT ON main.rates TO writers",
    "GRANT USAGE ON SEQUENCE main.audit_seq TO writers",
    "GRANT INSERT ON main.payments TO writers", "GRANT INSERT ON main.inbox TO writers",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON main.accounts TO writers",
    "GRANT INSERT ON app.orders TO writers", "GRANT INSERT ON app.orders2 TO writers", "GRANT INSERT ON app.order_log TO writers",
    "GRANT INSERT ON app.dups TO writers", "GRANT INSERT ON app.dup_log TO writers", "GRANT INSERT ON main.dup_log TO writers",
    "GRANT INSERT ON main.elsewhere TO writers", "GRANT INSERT ON other.elsewhere_log TO writers",
    "GRANT CREATE TABLE, CREATE VIEW, CREATE SEQUENCE ON SCHEMA ws TO writers",
]
# mia's own tables and triggers (she owns them: her triggers run with her rights) ...
LEGACY_MIA = [
    "CREATE TABLE app.mine (id INT)", "CREATE TABLE app.mine_rows (id INT)",
    "CREATE TRIGGER mine_ins AFTER INSERT ON app.mine FOR EACH STATEMENT INSERT INTO msink SELECT note FROM main.other_secret",
    "CREATE TRIGGER mine_row AFTER INSERT ON app.mine_rows FOR EACH ROW INSERT INTO msink VALUES ('row ' || NEW.id)",
]
# ... and who may write them.
LEGACY_MIA_ACCESS = ["GRANT INSERT ON app.mine TO writers", "GRANT INSERT ON app.mine_rows TO writers"]

# The fixture's access-control store is a DuckDB one. With another store (PostgreSQL) the
# database is the fixture's and the grants are made again; who made which trigger is on record
# in the fixture's store alone, so there mia's triggers run with the writer's rights, like one
# with no owner on record, and the writers get what her triggers' bodies need.
OWNERS_ON_RECORD = STORE_KIND == "duckdb"
WITHOUT_OWNERS = ["GRANT SELECT ON main.other_secret TO writers", "GRANT INSERT ON main.msink TO writers"]

TEMPORARY = "in this session that is a temporary object of the session's own"
ANOTHER = "in this session that is another object than it is for the trigger's own table"
QUALIFY = "The trigger's owner can name it in full (catalog.schema.name) with CREATE OR REPLACE TRIGGER"


@pytest.fixture
def legacy(launcher, store, tmp_path):
    """A server on a copy of the 2.13.0 fixture."""
    database = tmp_path / f"{LEGACY}.duckdb"
    database.write_bytes(gzip.decompress(data=(FIXTURES / f"{LEGACY}.duckdb.gz").read_bytes()))
    if OWNERS_ON_RECORD:
        rbac = tmp_path / f"{LEGACY}.rbac.duckdb"
        rbac.write_bytes(gzip.decompress(data=(FIXTURES / f"{LEGACY}.rbac.duckdb.gz").read_bytes()))
        rbac.chmod(mode=0o600)
        return start_rbac_server(launcher=launcher, store_uri=f"duckdb:{rbac}", args=["--database", str(database)])
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), args=["--database", str(database)])
    admin = server.admin()
    for sql in LEGACY_ACCESS + LEGACY_MIA_ACCESS + WITHOUT_OWNERS:
        expect_allowed(client=admin, sql=sql)
    return server


def values(client, sql: str) -> list:
    table = expect_allowed(client=client, sql=sql)
    return [str(v) for v in table.column(0).to_pylist()]


def count(client, table: str, where: str = "true") -> int:
    return int(values(client=client, sql=f"SELECT count(*) FROM {table} WHERE {where}")[0])


def trail(admin) -> list:
    """The audit trail so far, emptied."""
    rows = sorted(values(client=admin, sql="SELECT what FROM main.audit_log"))
    expect_allowed(client=admin, sql="DELETE FROM main.audit_log")
    return rows


def refused(client, sql: str, trigger: str, name: str, why: str) -> ChargewireError:
    error = expect_blocked(client=client, sql=sql, why=why)
    assert f"trigger {trigger} on table" in error.detail and f" names {name}" in error.detail, error.detail
    assert QUALIFY in error.detail
    return error


# The writes that fire the fixture's audit triggers, the trigger whose body names audit_log
# first in each (a BEFORE trigger's body comes first; inbox's body fires pay_audit), and the
# rows each leaves in the trail.
AUDITED = [
    ("INSERT INTO main.payments VALUES (1)", "pay_audit", ["payment 7"]),
    ("UPDATE main.accounts SET balance = balance + 1", "acct_before", ["before update", "updated", "updated"]),
    ("DELETE FROM main.accounts WHERE id = 2", "acct_del", ["deleted"]),
    ("INSERT INTO main.inbox VALUES (5)", "pay_audit", ["payment 7"]),
]


# ---- triggers from before: names without their catalog and schema ----

def test_triggers_from_before_keep_firing_where_their_names_are_what_they_were(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    stored = values(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name = 'pay_audit'")[0]
    assert "INSERT INTO audit_log " in stored and "nextval('audit_seq')" in stored and "FROM rates" in stored
    # Whatever the writer's search path: a name that is nowhere ahead in it is still main's.
    for use in ["USE main", "USE ws", "USE other"]:
        expect_allowed(client=wes, sql=use)
        for sql, _, rows in AUDITED:
            expect_allowed(client=wes, sql=sql.replace("main.", f"{LEGACY}.main."))
            assert trail(admin=admin) == rows, (use, sql)
        expect_allowed(client=admin, sql="INSERT INTO main.accounts VALUES (2, 20)")
    assert count(client=admin, table="main.payments") == 6
    # A name in the trigger table's own schema is that schema's, for a session that finds it
    # (one that does not never could fire this trigger: DuckDB finds no such table).
    expect_blocked(client=wes, sql="INSERT INTO app.orders VALUES (1)", code="sql_error", why="order_log does not exist")
    expect_allowed(client=wes, sql="USE app")
    expect_allowed(client=wes, sql="INSERT INTO app.orders VALUES (1)")
    expect_allowed(client=wes, sql="USE main")
    expect_allowed(client=wes, sql="INSERT INTO app.orders2 VALUES (1)")
    assert sorted(values(client=admin, sql="SELECT what FROM app.order_log")) == ["order", "order2"]
    # A user's triggers.
    expect_allowed(client=wes, sql="INSERT INTO app.mine VALUES (1)")
    expect_allowed(client=wes, sql="INSERT INTO app.mine_rows VALUES (7)")
    assert sorted(values(client=admin, sql="SELECT note FROM main.msink")) == ["other-secret", "row 7"]


def test_a_temporary_table_does_not_take_an_audit_triggers_rows(legacy):
    # The reproduction: the writer makes a temporary table named like the audit trail. Its
    # write went through and the audit row landed in the temporary table; the trail got none.
    admin, wes = legacy.admin(), legacy.user(name="wes")
    expect_allowed(client=wes, sql="CREATE TEMP TABLE audit_log (what VARCHAR, n BIGINT)")
    for sql, trigger, _ in AUDITED:
        refused(client=wes, sql=sql, trigger=trigger, name="audit_log", why=TEMPORARY)
    assert count(client=wes, table="temp.main.audit_log") == 0 and trail(admin=admin) == []
    assert count(client=admin, table="main.payments") == 0 and count(client=admin, table="main.inbox") == 0
    assert values(client=admin, sql="SELECT balance FROM main.accounts ORDER BY id") == ["10", "20"]
    # Twin: without it, each write is audited in the trail.
    expect_allowed(client=wes, sql="DROP TABLE temp.main.audit_log")
    for sql, _, rows in AUDITED:
        expect_allowed(client=wes, sql=sql)
        assert trail(admin=admin) == rows, sql
    # A temporary table named like the table a body writes on the way (the chain's payments).
    expect_allowed(client=wes, sql="CREATE TEMP TABLE payments (id INT)")
    refused(client=wes, sql="INSERT INTO main.inbox VALUES (6)", trigger="inbox_ins", name="payments", why=TEMPORARY)
    assert count(client=wes, table="temp.main.payments") == 0 and count(client=admin, table="main.inbox") == 1
    # An administrator's own statements are not held to this: it is the writer it guards against.
    expect_allowed(client=admin, sql="CREATE TEMP TABLE audit_log (what VARCHAR, n BIGINT)")
    expect_allowed(client=admin, sql="INSERT INTO main.payments VALUES (9)")
    assert count(client=admin, table="temp.main.audit_log") == 1


def test_a_temporary_view_or_sequence_does_not_stand_in_either(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    insert = "INSERT INTO main.payments VALUES (1)"
    for create, drop, name in [
        ("CREATE TEMP VIEW rates AS SELECT 999 AS rate", "DROP VIEW temp.main.rates", "rates"),
        ("CREATE TEMP SEQUENCE audit_seq START 9000", "DROP SEQUENCE temp.main.audit_seq", "audit_seq"),
    ]:
        expect_allowed(client=wes, sql=create)
        refused(client=wes, sql=insert, trigger="pay_audit", name=name, why=TEMPORARY)
        assert trail(admin=admin) == [] and count(client=admin, table="main.payments") == 0
        expect_allowed(client=wes, sql=drop)
    # Twin: the trail's own view and sequence.
    expect_allowed(client=wes, sql=insert)
    assert values(client=admin, sql="SELECT what || ' ' || n FROM main.audit_log") == ["payment 7 100"]


def test_a_table_ahead_in_the_writers_search_path_does_not_take_them(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    expect_allowed(client=wes, sql="CREATE TABLE ws.audit_log (what VARCHAR, n BIGINT)")
    for use in ["USE ws", f"SET search_path = '{LEGACY}.ws,{LEGACY}.main'"]:
        expect_allowed(client=wes, sql=use)
        for sql, trigger, _ in AUDITED:
            refused(client=wes, sql=sql.replace("main.", f"{LEGACY}.main."), trigger=trigger, name="audit_log", why=ANOTHER)
        # And the table a body writes on the way (the chain's payments).
        expect_allowed(client=wes, sql="CREATE TABLE ws.payments (id INT)")
        refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.inbox VALUES (5)", trigger="inbox_ins", name="payments", why=ANOTHER)
        assert count(client=wes, table="ws.payments") == 0
        expect_allowed(client=wes, sql="DROP TABLE ws.payments")
    assert count(client=wes, table="ws.audit_log") == 0
    assert trail(admin=admin) == [] and count(client=admin, table="main.payments") == 0
    # Twin: the same session, its search path back at main: the tables in ws are not ahead.
    expect_allowed(client=wes, sql="USE main")
    for sql, _, rows in AUDITED:
        expect_allowed(client=wes, sql=sql)
        assert trail(admin=admin) == rows, sql
    assert count(client=wes, table="ws.audit_log") == 0


def test_another_attached_catalog_does_not_take_them(legacy):
    # A name without its catalog is the trigger table's catalog's. (A user may ATTACH with
    # MANAGE CATALOGS, and has in the new catalog only what it is granted there.)
    admin, wes = legacy.admin(), legacy.user(name="wes")
    for sql in ["ATTACH ':memory:' AS side", "CREATE TABLE side.main.audit_log (what VARCHAR, n BIGINT)",
                "CREATE SEQUENCE side.main.audit_seq", "CREATE VIEW side.main.rates AS SELECT 1 AS rate",
                "GRANT INSERT ON side.main.audit_log TO writers", "GRANT SELECT ON side.main.rates TO writers",
                "GRANT USAGE ON SEQUENCE side.main.audit_seq TO writers"]:
        expect_allowed(client=admin, sql=sql)
    expect_allowed(client=wes, sql="USE side")
    refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.payments VALUES (1)", trigger="pay_audit", name="audit_log", why=ANOTHER)
    assert count(client=admin, table="side.main.audit_log") == 0 and trail(admin=admin) == []
    # Twin.
    expect_allowed(client=wes, sql=f"USE {LEGACY}")
    expect_allowed(client=wes, sql="INSERT INTO main.payments VALUES (1)")
    assert trail(admin=admin) == ["payment 7"] and count(client=admin, table="side.main.audit_log") == 0


def test_a_writer_does_not_read_through_a_users_trigger(legacy):
    # The other reproduction: mia's trigger copies what she may read into a table of hers;
    # the writer made a temporary table of that name and read the copy there.
    admin, wes = legacy.admin(), legacy.user(name="wes")
    if OWNERS_ON_RECORD:
        for sql, why in [("SELECT note FROM main.other_secret", "no SELECT privilege"), ("INSERT INTO main.msink VALUES ('x')", "no INSERT privilege")]:
            expect_blocked(client=wes, sql=sql, why=why)
    expect_allowed(client=wes, sql="CREATE TEMP TABLE msink (note VARCHAR)")
    refused(client=wes, sql="INSERT INTO app.mine VALUES (1)", trigger="mine_ins", name="msink", why=TEMPORARY)
    refused(client=wes, sql="INSERT INTO app.mine_rows VALUES (1)", trigger="mine_row", name="msink", why=TEMPORARY)
    assert count(client=wes, table="temp.main.msink") == 0 and count(client=admin, table="main.msink") == 0
    assert count(client=admin, table="app.mine") == 0
    expect_allowed(client=wes, sql="DROP TABLE temp.main.msink")
    # And through its search path.
    expect_allowed(client=wes, sql="CREATE TABLE ws.msink (note VARCHAR)")
    expect_allowed(client=wes, sql="GRANT INSERT ON ws.msink TO makers")
    expect_allowed(client=wes, sql="USE ws")
    refused(client=wes, sql="INSERT INTO app.mine VALUES (1)", trigger="mine_ins", name="msink", why=ANOTHER)
    assert count(client=wes, table="ws.msink") == 0
    # Twin: the copy goes to mia's table.
    expect_allowed(client=wes, sql="USE main")
    expect_allowed(client=wes, sql="INSERT INTO app.mine VALUES (1)")
    expect_allowed(client=wes, sql="INSERT INTO app.mine_rows VALUES (2)")
    assert sorted(values(client=admin, sql="SELECT note FROM main.msink")) == ["other-secret", "row 2"]
    assert count(client=wes, table="ws.msink") == 0


def test_a_name_that_is_not_one_table_for_the_triggers_own_is_refused(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    # In both the table's schema and main: nobody's to choose, whatever the session.
    for use in ["USE main", "USE app"]:
        expect_allowed(client=wes, sql=use)
        error = refused(client=wes, sql="INSERT INTO app.dups VALUES (1)", trigger="dups_audit", name="dup_log",
                        why="both the table's own schema and its catalog's main schema have one")
        assert "for any session" in error.detail
    # In neither (the creator's search path found it in a third schema).
    expect_blocked(client=wes, sql="INSERT INTO main.elsewhere VALUES (1)", code="sql_error", why="elsewhere_log does not exist")
    expect_allowed(client=wes, sql="USE other")
    refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.elsewhere VALUES (1)", trigger="elsewhere_audit", name="elsewhere_log", why=ANOTHER)
    assert count(client=admin, table="app.dup_log") + count(client=admin, table="main.dup_log") == 0
    assert count(client=admin, table="other.elsewhere_log") == 0
    # Twin: its owner (here the administrator) names the table in full, and it fires for all.
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER dups_audit AFTER INSERT ON app.dups FOR EACH STATEMENT "
                                     "INSERT INTO app.dup_log VALUES ('dup')")
    expect_allowed(client=admin, sql="USE other")
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER elsewhere_audit AFTER INSERT ON main.elsewhere "
                                     "FOR EACH STATEMENT INSERT INTO elsewhere_log VALUES ('elsewhere')")
    for use in ["USE main", "USE app", "USE other"]:
        expect_allowed(client=wes, sql=use)
        expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.app.dups VALUES (1)")
        expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.main.elsewhere VALUES (1)")
    assert count(client=admin, table="app.dup_log") == 3 and count(client=admin, table="main.dup_log") == 0
    assert count(client=admin, table=f"{LEGACY}.other.elsewhere_log") == 3


def test_replacing_a_trigger_from_before_stores_its_names_in_full(legacy):
    admin, wes, mia = legacy.admin(), legacy.user(name="wes"), legacy.user(name="mia")

    def stored(trigger: str) -> str:
        return values(client=admin, sql=f"SELECT sql FROM duckdb_triggers() WHERE trigger_name = '{trigger}'")[0]

    assert stored(trigger="pay_audit") == (
        "CREATE TRIGGER pay_audit AFTER INSERT ON payments FOR EACH STATEMENT "
        "INSERT INTO audit_log SELECT ('payment ' || rate), nextval('audit_seq') FROM rates;")
    assert "INSERT INTO msink SELECT note FROM main.other_secret" in stored(trigger="mine_ins")
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER pay_audit AFTER INSERT ON main.payments FOR EACH STATEMENT "
                                     "INSERT INTO audit_log SELECT 'payment ' || rate, nextval('audit_seq') FROM rates")
    assert stored(trigger="pay_audit") == (
        "CREATE TRIGGER pay_audit AFTER INSERT ON payments FOR EACH STATEMENT "
        f"INSERT INTO {LEGACY}.main.audit_log SELECT ('payment ' || rate), nextval('{LEGACY}.main.audit_seq') "
        f"FROM {LEGACY}.main.rates;")
    owner = mia if OWNERS_ON_RECORD else admin
    expect_allowed(client=owner, sql="CREATE OR REPLACE TRIGGER mine_ins AFTER INSERT ON app.mine FOR EACH STATEMENT "
                                     "INSERT INTO msink SELECT note FROM main.other_secret")
    assert f"INSERT INTO {LEGACY}.main.msink SELECT note FROM {LEGACY}.main.other_secret" in stored(trigger="mine_ins")
    # No session's own objects stand in for them now, and the rows land where they belong.
    for sql in ["CREATE TEMP TABLE audit_log (what VARCHAR, n BIGINT)", "CREATE TEMP VIEW rates AS SELECT 999 AS rate",
                "CREATE TEMP SEQUENCE audit_seq START 9000", "CREATE TEMP TABLE msink (note VARCHAR)",
                "CREATE TABLE ws.audit_log (what VARCHAR, n BIGINT)", "CREATE TABLE ws.msink (note VARCHAR)", "USE ws"]:
        expect_allowed(client=wes, sql=sql)
    expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.main.payments VALUES (1)")
    expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.app.mine VALUES (1)")
    assert values(client=admin, sql="SELECT what || ' ' || n FROM main.audit_log") == ["payment 7 100"]
    assert values(client=admin, sql="SELECT note FROM main.msink") == ["other-secret"]
    for table in ["temp.main.audit_log", "temp.main.msink", "ws.audit_log", "ws.msink"]:
        assert count(client=wes, table=table) == 0, table
    if OWNERS_ON_RECORD:
        # mia's trigger is still hers (its owner record is of the definition as stored): wes,
        # who may not write her table, fires it.
        expect_blocked(client=wes, sql=f"INSERT INTO {LEGACY}.main.msink VALUES ('x')", why="no INSERT privilege")


# ---- new triggers: stored with their names in full ----

@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "tn.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(), args=["--database", str(database)])
    admin = server.admin()
    for sql in ["CREATE SCHEMA app", "CREATE SCHEMA ws", "CREATE ROLE writers", "CREATE ROLE makers",
                "CREATE USER wes", "CREATE USER mia", "GRANT writers TO wes", "GRANT makers TO mia",
                "GRANT CREATE TABLE, CREATE VIEW, CREATE SEQUENCE ON SCHEMA ws TO writers",
                "GRANT CREATE TABLE, CREATE TRIGGER, CREATE VIEW, CREATE SEQUENCE, USAGE, SELECT, INSERT, UPDATE, DELETE "
                "ON SCHEMA app TO makers"]:
        expect_allowed(client=admin, sql=sql)
    server.admin_client = admin
    return server


# Every test leaves the server as it found it: pytest-xdist runs the tests in any order.


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def wes(server):
    return server.user(name="wes")


@pytest.fixture
def mia(server):
    return server.user(name="mia")


def drop(client, statements: list[str]) -> None:
    for sql in statements:
        expect_allowed(client=client, sql=sql)


def test_create_trigger_stores_the_names_in_its_action_in_full(admin, mia, wes):
    # mia works in app (USE app): her names are bare. What is stored names every table, view
    # and sequence in full; a CTE's name and a transition table's stay as they are.
    for sql in ["USE app", "CREATE TABLE src (id INT, note VARCHAR)", "CREATE TABLE log (id BIGINT, what VARCHAR)",
                "CREATE TABLE state (id INT, seen INT)", "INSERT INTO state VALUES (1, 0)",
                "CREATE SEQUENCE log_ids START 50", "CREATE VIEW labels AS SELECT 'L' AS label",
                "CREATE TRIGGER src_ins AFTER INSERT ON src REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
                "INSERT INTO log WITH picked AS (SELECT note FROM fresh WHERE id IN (SELECT id FROM state)) "
                "SELECT nextval('log_ids'), label || note FROM picked, labels",
                "CREATE TRIGGER src_upd BEFORE UPDATE ON src FOR EACH STATEMENT UPDATE state SET seen = seen + 1 WHERE id = 1",
                "CREATE TRIGGER src_del AFTER DELETE ON src FOR EACH ROW DELETE FROM app.state WHERE id = OLD.id",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON src TO writers", "USE main"]:
        expect_allowed(client=mia, sql=sql)
    try:
        rows = admin.run(sql="SELECT trigger_name, sql FROM duckdb_triggers() WHERE table_name = 'src'").to_pylist()
        stored = {row["trigger_name"]: row["sql"] for row in rows}
        assert stored["src_ins"] == (
            "CREATE TRIGGER src_ins AFTER INSERT ON src REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
            "INSERT INTO tn.app.log WITH picked AS (SELECT note FROM fresh WHERE (id = ANY(SELECT id FROM tn.app.state))) "
            "SELECT nextval('tn.app.log_ids'), (\"label\" || note) FROM picked , tn.app.labels;")
        assert "UPDATE tn.app.state SET seen = (seen + 1)" in stored["src_upd"]
        assert "DELETE FROM tn.app.state WHERE " in stored["src_del"]
        # The writer's session has its own objects of every one of those names, ahead in its
        # search path and temporary: none of them stands in, and the triggers run with mia's
        # rights (wes may write none of her other tables), so their owner records are of
        # what is stored.
        for sql in ["CREATE TABLE ws.log (id BIGINT, what VARCHAR)", "CREATE TABLE ws.state (id INT, seen INT)",
                    "CREATE VIEW ws.labels AS SELECT 'X' AS label", "CREATE SEQUENCE ws.log_ids START 9000",
                    "CREATE TEMP TABLE log (id BIGINT, what VARCHAR)", "CREATE TEMP TABLE state (id INT, seen INT)",
                    "CREATE TEMP VIEW labels AS SELECT 'T' AS label", "CREATE TEMP SEQUENCE log_ids START 7000", "USE ws"]:
            expect_allowed(client=wes, sql=sql)
        expect_blocked(client=wes, sql="INSERT INTO tn.app.log VALUES (0, 'wes')", why="no INSERT privilege")
        expect_allowed(client=wes, sql="INSERT INTO tn.app.src VALUES (1, 'one'), (2, 'two')")
        expect_allowed(client=wes, sql="UPDATE tn.app.src SET note = 'uno' WHERE id = 1")
        assert values(client=mia, sql="SELECT id || ' ' || what FROM app.log") == ["50 Lone"]
        assert values(client=mia, sql="SELECT seen FROM app.state") == ["1"]
        expect_allowed(client=wes, sql="DELETE FROM tn.app.src WHERE id = 1")
        assert count(client=mia, table="app.state") == 0
        for table in ["ws.log", "ws.state", "temp.main.log", "temp.main.state"]:
            assert count(client=wes, table=table) == 0, table
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS ws.log", "DROP TABLE IF EXISTS ws.state", "DROP VIEW IF EXISTS ws.labels",
                                       "DROP SEQUENCE IF EXISTS ws.log_ids", "DROP TABLE IF EXISTS app.src",
                                       "DROP TABLE IF EXISTS app.log", "DROP TABLE IF EXISTS app.state",
                                       "DROP VIEW IF EXISTS app.labels", "DROP SEQUENCE IF EXISTS app.log_ids"])


def test_what_a_body_reaches_that_no_name_in_it_stands_for(admin, wes):
    # A table's own defaults take numbers from a sequence the body never names, bound by name
    # in the statement all the same: the table's is fine, a session's temporary one is not.
    # A table reached through a macro is looked up in the writer's session too, and no name in
    # the body says which is meant: refused, for every writer.
    drop(client=admin, statements=[
        "CREATE SEQUENCE main.nm_ids START 10", "CREATE TABLE main.nm_log (id BIGINT DEFAULT nextval('nm_ids'), what VARCHAR)",
        "CREATE TABLE main.nm_src (v INT)", "INSERT INTO main.nm_src VALUES (4)",
        "CREATE MACRO main.nm_rows() AS TABLE SELECT v FROM nm_src",
        "CREATE TABLE main.nm_t (id INT)", "CREATE TABLE main.nm_m (id INT)",
        "CREATE TRIGGER nm_t_ins AFTER INSERT ON main.nm_t FOR EACH STATEMENT INSERT INTO nm_log (what) VALUES ('default')",
        "CREATE TRIGGER nm_m_ins AFTER INSERT ON main.nm_m FOR EACH STATEMENT INSERT INTO nm_log (what) SELECT 'macro ' || v FROM nm_rows()",
        "GRANT INSERT ON main.nm_t TO writers", "GRANT INSERT ON main.nm_m TO writers", "GRANT INSERT ON main.nm_log TO writers",
        "GRANT SELECT ON main.nm_src TO writers", "GRANT USAGE ON SEQUENCE main.nm_ids TO writers",
    ])
    try:
        expect_allowed(client=wes, sql="INSERT INTO main.nm_t VALUES (1)")
        expect_allowed(client=wes, sql="CREATE TEMP SEQUENCE nm_ids START 7000")
        error = expect_blocked(client=wes, sql="INSERT INTO main.nm_t VALUES (2)", why="a temporary object of this session named nm_ids")
        assert "trigger nm_t_ins on table" in error.detail and "none of the names in its body stands for" in error.detail
        expect_allowed(client=wes, sql="DROP SEQUENCE temp.main.nm_ids")
        expect_allowed(client=wes, sql="INSERT INTO main.nm_t VALUES (3)")
        assert values(client=admin, sql="SELECT id || ' ' || what FROM main.nm_log ORDER BY id") == ["10 default", "11 default"]
        error = expect_blocked(client=wes, sql="INSERT INTO main.nm_m VALUES (1)", why="an object named nm_src that none of the names")
        assert "trigger nm_m_ins on table" in error.detail
        # Twin: the table named in the body itself.
        expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER nm_m_ins AFTER INSERT ON main.nm_m FOR EACH STATEMENT "
                                         "INSERT INTO nm_log (what) SELECT 'inline ' || v FROM nm_src")
        expect_allowed(client=wes, sql="INSERT INTO main.nm_m VALUES (1)")
        assert values(client=admin, sql="SELECT what FROM main.nm_log WHERE id = 12") == ["inline 4"]
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS main.nm_t", "DROP TABLE IF EXISTS main.nm_m",
                                       "DROP MACRO TABLE IF EXISTS main.nm_rows", "DROP TABLE IF EXISTS main.nm_log",
                                       "DROP TABLE IF EXISTS main.nm_src", "DROP SEQUENCE IF EXISTS main.nm_ids"])


def test_the_init_sqls_triggers_are_stored_with_their_names_in_full(launcher, store, tmp_path):
    # The init SQL's DuckDB statements run outside access control, as the operator's
    # configuration; a trigger they create is stored like a session's all the same.
    init = ("CREATE SCHEMA IF NOT EXISTS ops; USE ops; CREATE TABLE IF NOT EXISTS jobs (id INT); "
            "CREATE TABLE IF NOT EXISTS job_log (what VARCHAR); "
            "CREATE OR REPLACE TRIGGER jobs_ins AFTER INSERT ON jobs FOR EACH STATEMENT INSERT INTO job_log VALUES ('job'); "
            "CREATE ROLE IF NOT EXISTS writers; CREATE USER IF NOT EXISTS wes; GRANT writers TO wes; "
            "GRANT INSERT ON ops.jobs TO writers; GRANT INSERT ON ops.job_log TO writers;")
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), args=["--database", str(tmp_path / "init.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": init})
    admin, wes = server.admin(), server.user(name="wes")
    assert values(client=admin, sql="SELECT sql FROM duckdb_triggers()") == [
        "CREATE TRIGGER jobs_ins AFTER INSERT ON jobs FOR EACH STATEMENT INSERT INTO init.ops.job_log (VALUES ('job'));"]
    # A writer whose search path never finds job_log fires it, a temporary table or not.
    expect_allowed(client=wes, sql="CREATE TEMP TABLE job_log (what VARCHAR)")
    expect_allowed(client=wes, sql="INSERT INTO ops.jobs VALUES (1)")
    assert count(client=admin, table="ops.job_log") == 1 and count(client=wes, table="temp.main.job_log") == 0
