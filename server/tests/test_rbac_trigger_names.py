"""The names in a trigger's body, under access control.

DuckDB keeps a trigger's action as written and binds it again in every statement that fires
it, in the writer's session: a table, view or sequence the action names without its catalog
and schema follows the writer's search path, the writer's temporary objects first. Before
this was checked, a writer with a temporary table named like the one an audit trigger writes
had the audit row land in its own table (the real one got nothing), and one named like the
table a user's trigger writes read there what the trigger's owner may read.

Now each name in a body must be, in the writer's session, the object it is for the trigger's
own table, and nothing from outside the body (the writer's WITH) may stand in for one, or the
statement is refused, saying which trigger and which name; and CREATE TRIGGER stores the action
with its names given their schema (not their catalog: a renamed database file keeps its
triggers), so a new trigger depends on no session's search path.
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
    # Bodies that READ a table by a bare name: a row trigger, an UPDATE's (with a transition
    # table) and a BEFORE DELETE's.
    "CREATE TABLE main.lookup (id INT, label VARCHAR)", "INSERT INTO main.lookup VALUES (1, 'one'), (2, 'two')",
    "CREATE TABLE main.lk_r (id INT)",
    "CREATE TRIGGER lk_row AFTER INSERT ON main.lk_r FOR EACH ROW "
    "INSERT INTO audit_log SELECT 'lk ' || label, NEW.id FROM lookup WHERE id = NEW.id",
    "CREATE TABLE main.lk_u (id INT, v INT)", "INSERT INTO main.lk_u VALUES (1, 0), (2, 0)",
    "CREATE TRIGGER lk_upd AFTER UPDATE ON main.lk_u REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
    "INSERT INTO audit_log SELECT 'lk upd ' || label, fresh.id FROM fresh JOIN lookup USING (id)",
    "CREATE TRIGGER lk_del BEFORE DELETE ON main.lk_u FOR EACH STATEMENT INSERT INTO audit_log SELECT 'lk del', count(*) FROM lookup",
    # Macros a body calls: a table macro naming its table bare, a scalar one with a subquery
    # naming it with its schema, and one around nextval.
    "CREATE MACRO main.lookup_rows() AS TABLE SELECT id, label FROM lookup",
    "CREATE MACRO main.label_of(i) AS (SELECT label FROM main.lookup WHERE id = i)",
    "CREATE MACRO main.next_audit() AS nextval('main.audit_seq')",
    "CREATE TABLE main.mac_t (id INT)",
    "CREATE TRIGGER mac_ins AFTER INSERT ON main.mac_t REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
    "INSERT INTO audit_log SELECT 'mac ' || label_of(fresh.id) || ' ' || r.label, next_audit() "
    "FROM fresh JOIN lookup_rows() r ON r.id = fresh.id",
    # A sequence named by something other than a plain string.
    "CREATE TABLE main.seq_t (id INT)",
    "CREATE TRIGGER seq_cast AFTER INSERT ON main.seq_t FOR EACH STATEMENT "
    "INSERT INTO audit_log VALUES ('cast', nextval('audit_seq'::VARCHAR))",
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
    "GRANT SELECT ON main.lookup TO writers", "GRANT INSERT ON main.lk_r TO writers",
    "GRANT SELECT, UPDATE, DELETE ON main.lk_u TO writers", "GRANT INSERT ON main.mac_t TO writers",
    "GRANT INSERT ON main.seq_t TO writers",
]
# mia's own tables and triggers (she owns them: her triggers run with her rights) ...
LEGACY_MIA = [
    "CREATE TABLE app.mine (id INT)", "CREATE TABLE app.mine_rows (id INT)",
    "CREATE TRIGGER mine_ins AFTER INSERT ON app.mine FOR EACH STATEMENT INSERT INTO msink SELECT note FROM main.other_secret",
    "CREATE TRIGGER mine_row AFTER INSERT ON app.mine_rows FOR EACH ROW INSERT INTO msink VALUES ('row ' || NEW.id)",
    # One that reads a table of hers by a bare name (made in app, USE app).
    "USE app", "CREATE TABLE mnotes (note VARCHAR)", "INSERT INTO mnotes VALUES ('mia note')",
    "CREATE TABLE mine_notes (id INT)",
    "CREATE TRIGGER mine_notes_ins AFTER INSERT ON mine_notes FOR EACH STATEMENT INSERT INTO main.msink SELECT note FROM mnotes",
    "USE main",
]
# ... and who may write them.
LEGACY_MIA_ACCESS = ["GRANT INSERT ON app.mine TO writers", "GRANT INSERT ON app.mine_rows TO writers",
                     "GRANT INSERT ON app.mine_notes TO writers"]

# The fixture's access-control store is a DuckDB one. With another store (PostgreSQL) the
# database is the fixture's and the grants are made again; who made which trigger is on record
# in the fixture's store alone, so there mia's triggers run with the writer's rights, like one
# with no owner on record, and the writers get what her triggers' bodies need.
OWNERS_ON_RECORD = STORE_KIND == "duckdb"
WITHOUT_OWNERS = ["GRANT SELECT ON main.other_secret TO writers", "GRANT INSERT ON main.msink TO writers",
                  "GRANT SELECT ON app.mnotes TO writers"]

TEMPORARY = "in this session that is a temporary object of the session's own"
ANOTHER = "in this session that is another object than it is for the trigger's own table"
# What each refusal says to do about it.
DROP_TEMPORARY = "Drop or rename the temporary object to write this table"
RESTORE = "The trigger's owner can run CREATE OR REPLACE TRIGGER once, with the same text, which stores the names in its action with their schema"
USE_LEGACY = f"Run USE {LEGACY} first"
OUTER_CTE = "which in this statement is a CTE (WITH) from outside the trigger's body"


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


def refused(client, sql: str, trigger: str, name: str, why: str, remedy: str) -> ChargewireError:
    error = expect_blocked(client=client, sql=sql, why=why)
    assert f"trigger {trigger} on table" in error.detail and f" names {name}" in error.detail, error.detail
    assert remedy in error.detail, error.detail
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
        refused(client=wes, sql=sql, trigger=trigger, name="audit_log", why=TEMPORARY, remedy=DROP_TEMPORARY)
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
    refused(client=wes, sql="INSERT INTO main.inbox VALUES (6)", trigger="inbox_ins", name="payments", why=TEMPORARY,
            remedy=DROP_TEMPORARY)
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
        refused(client=wes, sql=insert, trigger="pay_audit", name=name, why=TEMPORARY, remedy=DROP_TEMPORARY)
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
            refused(client=wes, sql=sql.replace("main.", f"{LEGACY}.main."), trigger=trigger, name="audit_log", why=ANOTHER,
                    remedy=RESTORE)
        # And the table a body writes on the way (the chain's payments).
        expect_allowed(client=wes, sql="CREATE TABLE ws.payments (id INT)")
        refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.inbox VALUES (5)", trigger="inbox_ins", name="payments", why=ANOTHER,
                remedy=RESTORE)
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
    refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.payments VALUES (1)", trigger="pay_audit", name="audit_log", why=ANOTHER,
            remedy=USE_LEGACY)
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
    refused(client=wes, sql="INSERT INTO app.mine VALUES (1)", trigger="mine_ins", name="msink", why=TEMPORARY, remedy=DROP_TEMPORARY)
    refused(client=wes, sql="INSERT INTO app.mine_rows VALUES (1)", trigger="mine_row", name="msink", why=TEMPORARY,
            remedy=DROP_TEMPORARY)
    assert count(client=wes, table="temp.main.msink") == 0 and count(client=admin, table="main.msink") == 0
    assert count(client=admin, table="app.mine") == 0
    expect_allowed(client=wes, sql="DROP TABLE temp.main.msink")
    # And through its search path.
    expect_allowed(client=wes, sql="CREATE TABLE ws.msink (note VARCHAR)")
    expect_allowed(client=wes, sql="GRANT INSERT ON ws.msink TO makers")
    expect_allowed(client=wes, sql="USE ws")
    refused(client=wes, sql="INSERT INTO app.mine VALUES (1)", trigger="mine_ins", name="msink", why=ANOTHER, remedy=RESTORE)
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
                        why="both the table's own schema and its catalog's main schema have one",
                        remedy="The trigger's owner can name it with its schema (schema.name) with CREATE OR REPLACE TRIGGER")
        assert "for any session" in error.detail
    # In neither (the creator's search path found it in a third schema).
    expect_blocked(client=wes, sql="INSERT INTO main.elsewhere VALUES (1)", code="sql_error", why="elsewhere_log does not exist")
    expect_allowed(client=wes, sql="USE other")
    refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.elsewhere VALUES (1)", trigger="elsewhere_audit", name="elsewhere_log",
            why=ANOTHER, remedy=RESTORE)
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


def test_replacing_a_trigger_from_before_stores_its_names_with_their_schema(legacy):
    admin, wes, mia = legacy.admin(), legacy.user(name="wes"), legacy.user(name="mia")

    def stored(trigger: str) -> str:
        return values(client=admin, sql=f"SELECT sql FROM duckdb_triggers() WHERE trigger_name = '{trigger}'")[0]

    assert stored(trigger="pay_audit") == (
        "CREATE TRIGGER pay_audit AFTER INSERT ON payments FOR EACH STATEMENT "
        "INSERT INTO audit_log SELECT ('payment ' || rate), nextval('audit_seq') FROM rates;")
    assert "INSERT INTO msink SELECT note FROM main.other_secret" in stored(trigger="mine_ins")
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER pay_audit AFTER INSERT ON main.payments FOR EACH STATEMENT "
                                     "INSERT INTO audit_log SELECT 'payment ' || rate, nextval('audit_seq') FROM rates")
    # With their schema, not their catalog: the database file's name is no part of a trigger.
    assert stored(trigger="pay_audit") == (
        "CREATE TRIGGER pay_audit AFTER INSERT ON payments FOR EACH STATEMENT "
        "INSERT INTO main.audit_log SELECT ('payment ' || rate), nextval('main.audit_seq') FROM main.rates;")
    owner = mia if OWNERS_ON_RECORD else admin
    expect_allowed(client=owner, sql="CREATE OR REPLACE TRIGGER mine_ins AFTER INSERT ON app.mine FOR EACH STATEMENT "
                                     "INSERT INTO msink SELECT note FROM main.other_secret")
    assert "INSERT INTO main.msink SELECT note FROM main.other_secret" in stored(trigger="mine_ins")
    # The writer's own tables of those names ahead in its search path stand in for nothing now,
    # and the rows land where they belong.
    for sql in ["CREATE TABLE ws.audit_log (what VARCHAR, n BIGINT)", "CREATE TABLE ws.msink (note VARCHAR)",
                "CREATE TABLE ws.rates (rate INT)", "USE ws"]:
        expect_allowed(client=wes, sql=sql)
    expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.main.payments VALUES (1)")
    expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.app.mine VALUES (1)")
    assert values(client=admin, sql="SELECT what || ' ' || n FROM main.audit_log") == ["payment 7 100"]
    assert values(client=admin, sql="SELECT note FROM main.msink") == ["other-secret"]
    # A temporary object of such a name would (the temporary catalog's schema is main, so
    # main.audit_log finds it first): the write is refused, and the remedy is the writer's.
    for sql, name, drop_it in [
        ("CREATE TEMP TABLE audit_log (what VARCHAR, n BIGINT)", "main.audit_log", "DROP TABLE temp.main.audit_log"),
        ("CREATE TEMP VIEW rates AS SELECT 999 AS rate", "main.rates", "DROP VIEW temp.main.rates"),
        ("CREATE TEMP SEQUENCE audit_seq START 9000", "main.audit_seq", "DROP SEQUENCE temp.main.audit_seq"),
    ]:
        expect_allowed(client=wes, sql=sql)
        refused(client=wes, sql=f"INSERT INTO {LEGACY}.main.payments VALUES (2)", trigger="pay_audit", name=name,
                why=TEMPORARY, remedy=DROP_TEMPORARY)
        expect_allowed(client=wes, sql=drop_it)
    expect_allowed(client=wes, sql="CREATE TEMP TABLE msink (note VARCHAR)")
    refused(client=wes, sql=f"INSERT INTO {LEGACY}.app.mine VALUES (2)", trigger="mine_ins", name="main.msink",
            why=TEMPORARY, remedy=DROP_TEMPORARY)
    assert values(client=admin, sql="SELECT what || ' ' || n FROM main.audit_log") == ["payment 7 100"]
    assert values(client=admin, sql="SELECT note FROM main.msink") == ["other-secret"]
    for table in ["temp.main.msink", "ws.audit_log", "ws.msink"]:
        assert count(client=wes, table=table) == 0, table
    if OWNERS_ON_RECORD:
        # mia's trigger is still hers (its owner record is of the definition as stored): wes,
        # who may not write her table, fires it.
        expect_blocked(client=wes, sql=f"INSERT INTO {LEGACY}.main.msink VALUES ('x')", why="no INSERT privilege")


# ---- a writer standing in for what a trigger names: a CTE, a temporary object, USE ----

# Writes that fire a trigger from before whose body READS a table or view by a bare name: the
# trigger, the name, the write, the trail it leaves, and the name the refusal gives when a
# temporary object of that name is in the way (a macro's own name for it, where the body calls
# one: label_of says main.lookup, and comes first).
READS = [
    ("statement", "pay_audit", "rates", "INSERT INTO main.payments VALUES (1)", ["payment 7"], "rates"),
    ("row", "lk_row", "lookup", "INSERT INTO main.lk_r VALUES (1)", ["lk one"], "lookup"),
    ("update", "lk_upd", "lookup", "UPDATE main.lk_u SET v = v + 1 WHERE id = 2", ["lk upd two"], "lookup"),
    ("before delete", "lk_del", "lookup", "DELETE FROM main.lk_u WHERE id = 1", ["lk del"], "lookup"),
    ("chain", "pay_audit", "rates", "INSERT INTO main.inbox VALUES (5)", ["payment 7"], "rates"),
    ("macro", "mac_ins", "lookup", "INSERT INTO main.mac_t VALUES (2)", ["mac two two"], "main.lookup"),
]
# A CTE (or temporary table) that answers for every name above.
FORGED = "SELECT 1 AS id, 'FORGED' AS label, 999 AS rate"


def twins(admin, wes) -> None:
    """Each write of READS, nothing in the way: it runs, and its trail is the real one."""
    for kind, _, _, sql, rows, _ in READS:
        expect_allowed(client=wes, sql=sql)
        assert trail(admin=admin) == rows, kind


def test_a_writers_cte_does_not_stand_in_for_what_a_trigger_reads(legacy):
    # The review's probe (finding 01): DuckDB binds a trigger's body inside the statement that
    # fires it, and looks a bare name up as a CTE before the catalog. The writer's WITH stood
    # in for the table an audit trigger read (its rows dropped or forged), on every route.
    admin, wes = legacy.admin(), legacy.user(name="wes")
    for kind, trigger, name, sql, _, _ in READS:
        error = expect_blocked(client=wes, sql=f"WITH {name} AS ({FORGED}) {sql}", why=OUTER_CTE)
        assert f"trigger {trigger} on table" in error.detail and f" reads {name}," in error.detail, (kind, error.detail)
        assert "Give that CTE another name" in error.detail
        assert trail(admin=admin) == [], kind
    assert count(client=admin, table="main.payments") + count(client=admin, table="main.lk_r") == 0
    assert values(client=admin, sql="SELECT v FROM main.lk_u ORDER BY id") == ["0", "0"]
    # A CTE of another name, or of a name the body writes or takes numbers from (never bound
    # to a CTE), stands in for nothing: the write runs and is audited.
    expect_allowed(client=wes, sql="WITH lookups AS (SELECT 1 AS x), audit_log AS (SELECT 1 AS x), "
                                   "audit_seq AS (SELECT 1 AS x) INSERT INTO main.payments VALUES (1)")
    assert trail(admin=admin) == ["payment 7"]
    twins(admin=admin, wes=wes)
    # A user's trigger, which runs with its owner's rights: the writer's WITH would have it
    # write what the writer chose into its owner's table.
    error = expect_blocked(client=wes, sql="WITH mnotes AS (SELECT 'forged by wes' AS note) INSERT INTO app.mine_notes VALUES (1)",
                           why=OUTER_CTE)
    assert "trigger mine_notes_ins on table" in error.detail
    assert count(client=admin, table="main.msink") == 0
    # Twin (its bare name is app's: a session finds it under USE app, as on 2.13.0).
    expect_allowed(client=wes, sql="USE app")
    expect_allowed(client=wes, sql="INSERT INTO app.mine_notes VALUES (1)")
    assert values(client=admin, sql="SELECT note FROM main.msink") == ["mia note"]


def test_a_cte_of_an_enclosing_triggers_body_does_not_stand_in_either(legacy):
    # A body that writes a table with triggers binds them inside itself: its own WITH is in
    # scope there. mia's trigger (made now, on her table) names a CTE like the view the audit
    # trigger on payments reads.
    admin, wes, mia = legacy.admin(), legacy.user(name="wes"), legacy.user(name="mia")
    for sql in ["GRANT INSERT ON main.payments TO makers", "GRANT INSERT ON main.audit_log TO makers",
                "GRANT SELECT ON main.rates TO makers", "GRANT USAGE ON SEQUENCE main.audit_seq TO makers"]:
        expect_allowed(client=admin, sql=sql)
    for sql in ["CREATE TABLE app.relay (id INT)", "GRANT INSERT ON app.relay TO writers",
                "CREATE TRIGGER relay_ins AFTER INSERT ON app.relay REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
                "WITH rates AS (SELECT 555 AS rate) INSERT INTO main.payments SELECT id FROM fresh"]:
        expect_allowed(client=mia, sql=sql)
    for client in [wes, mia]:
        error = expect_blocked(client=client, sql="INSERT INTO app.relay VALUES (1)", why=OUTER_CTE)
        assert "trigger pay_audit on table" in error.detail and " reads rates," in error.detail
    assert trail(admin=admin) == [] and count(client=admin, table="main.payments") == 0
    # Twin: the same body without the WITH.
    expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER relay_ins AFTER INSERT ON app.relay REFERENCING NEW TABLE AS fresh "
                                   "FOR EACH STATEMENT INSERT INTO main.payments SELECT id FROM fresh")
    expect_allowed(client=wes, sql="INSERT INTO app.relay VALUES (1)")
    assert trail(admin=admin) == ["payment 7"]


def test_a_writers_temporary_objects_do_not_stand_in_for_what_a_trigger_reads(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    for kind, trigger, name, sql, _, refused_name in READS:
        expect_allowed(client=wes, sql=f"CREATE TEMP TABLE {name} AS {FORGED}")
        error = refused(client=wes, sql=sql, trigger=trigger, name=refused_name, why=TEMPORARY, remedy=DROP_TEMPORARY)
        if kind == "macro":
            assert "calls macro label_of, which names main.lookup" in error.detail, error.detail
        assert trail(admin=admin) == [], kind
        expect_allowed(client=wes, sql=f"DROP TABLE temp.main.{name}")
    twins(admin=admin, wes=wes)


def test_a_writers_search_path_does_not_stand_in_for_what_a_trigger_reads(legacy):
    admin, wes = legacy.admin(), legacy.user(name="wes")
    for name in ["rates", "lookup"]:
        expect_allowed(client=wes, sql=f"CREATE TABLE ws.{name} AS {FORGED}")
    expect_allowed(client=wes, sql="USE ws")
    for kind, trigger, name, sql, _, _ in READS:
        error = refused(client=wes, sql=sql, trigger=trigger, name=name, why=ANOTHER,
                        remedy=RESTORE if kind != "macro" else "An administrator can name it with its schema in the macro")
        if kind == "macro":
            assert "calls macro lookup_rows, which names lookup" in error.detail, error.detail
        assert trail(admin=admin) == [], kind
    # Twin: the same session, back in main.
    expect_allowed(client=wes, sql="USE main")
    twins(admin=admin, wes=wes)


def test_an_administrators_session_does_not_decide_what_a_users_trigger_reads(legacy):
    # Finding 02: an administrator's own statements are not checked, but a user's trigger
    # they fire runs with its owner's rights: what the administrator's session has of the
    # names it reads (a temporary table, a WITH) would be copied into the owner's tables.
    admin = legacy.admin()
    expect_allowed(client=admin, sql="CREATE TEMP TABLE mnotes AS SELECT 'ADMIN TEMP ROW' AS note")
    expect_allowed(client=admin, sql="USE app")  # where the bare name is app.mnotes, as on 2.13.0
    insert = f"INSERT INTO {LEGACY}.app.mine_notes VALUES (1)"
    if OWNERS_ON_RECORD:
        refused(client=admin, sql=insert, trigger="mine_notes_ins", name="mnotes", why=TEMPORARY, remedy=DROP_TEMPORARY)
        expect_allowed(client=admin, sql="DROP TABLE temp.main.mnotes")
        expect_blocked(client=admin, sql="WITH mnotes AS (SELECT 'ADMIN CTE ROW' AS note) " + insert, why=OUTER_CTE)
        assert count(client=admin, table="main.msink") == 0
        expect_allowed(client=admin, sql=insert)
        assert values(client=admin, sql="SELECT note FROM main.msink") == ["mia note"]
    else:
        # Without its owner on record (this store), the trigger runs with the writer's rights,
        # as an administrator's does: the administrator's statement is theirs.
        expect_allowed(client=admin, sql=insert)
        assert values(client=admin, sql="SELECT note FROM main.msink") == ["ADMIN TEMP ROW"]
    # An administrator's own trigger fired by an administrator: not held to it (their session).
    expect_allowed(client=admin, sql="CREATE TEMP TABLE lookup AS SELECT 1 AS id, 'ADMIN' AS label")
    expect_allowed(client=admin, sql=f"INSERT INTO {LEGACY}.main.lk_r VALUES (1)")
    assert trail(admin=admin) == ["lk ADMIN"]


def test_macros_a_body_calls_are_held_to_the_same_names(legacy):
    # Finding 04: a body calling a macro that reads a table was refused for every writer,
    # whatever the macro named. Now the macro's own names are held against the session like
    # the body's (above), and what they are for the trigger's table is what may be reached.
    admin, wes = legacy.admin(), legacy.user(name="wes")
    for use in ["USE main", "USE ws", "USE other"]:
        expect_allowed(client=wes, sql=use)
        expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.main.mac_t VALUES (1)")
        assert trail(admin=admin) == ["mac one one"], use
    assert values(client=admin, sql="SELECT currval('main.audit_seq')") == ["102"]
    # A sequence named by something that is not a plain string can't be checked: refused for
    # every writer but the administrator, saying how to name it ...
    error = expect_blocked(client=wes, sql=f"INSERT INTO {LEGACY}.main.seq_t VALUES (1)",
                           why="takes a sequence's numbers by a name that is not a plain string")
    assert "trigger seq_cast on table" in error.detail
    assert "Its owner can name the sequence as a plain string with CREATE OR REPLACE TRIGGER" in error.detail
    # ... and no new trigger is made so.
    for sql in ["CREATE OR REPLACE TRIGGER seq_cast AFTER INSERT ON main.seq_t FOR EACH STATEMENT "
                "INSERT INTO audit_log VALUES ('cast', nextval('audit_seq'::VARCHAR))",
                "CREATE OR REPLACE TRIGGER seq_cast AFTER INSERT ON main.seq_t FOR EACH STATEMENT "
                "INSERT INTO audit_log VALUES ('cast', nextval('main.' || 'audit_seq'))"]:
        expect_blocked(client=admin, sql=sql, why="name the sequence as one (nextval('schema.name'))")
    # Twin: named as a plain string, it fires for the writer.
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER seq_cast AFTER INSERT ON main.seq_t FOR EACH STATEMENT "
                                     "INSERT INTO audit_log VALUES ('cast', nextval('audit_seq'))")
    expect_allowed(client=wes, sql=f"INSERT INTO {LEGACY}.main.seq_t VALUES (1)")
    assert trail(admin=admin) == ["cast"]


def test_a_name_a_trigger_from_before_reads_bare_cant_be_made_two(legacy):
    # Finding 06: a bare name in both the trigger table's schema and main is refused for
    # every writer, so a user who could create the second would stop every write of that
    # table. Creating it (or renaming a table to it) is the administrator's.
    admin, wes = legacy.admin(), legacy.user(name="wes")
    expect_allowed(client=admin, sql="GRANT CREATE TABLE, CREATE VIEW ON SCHEMA main TO writers")
    why = "that names it without its schema find one in both its table's schema and main"
    # orders_audit (on app.orders) names order_log bare: app.order_log is the one.
    for sql in ["CREATE TABLE main.order_log (what VARCHAR)", "CREATE VIEW main.order_log AS SELECT 'x' AS what",
                "CREATE TABLE main.mnotes (note VARCHAR)"]:
        expect_blocked(client=wes, sql=sql, why=why)
    expect_allowed(client=wes, sql="CREATE TABLE main.order_log_2 (what VARCHAR)")
    expect_blocked(client=wes, sql="ALTER TABLE main.order_log_2 RENAME TO order_log", why=why)
    # Twins: another name, another schema, and the administrator.
    expect_allowed(client=wes, sql="ALTER TABLE main.order_log_2 RENAME TO order_log_3")
    expect_allowed(client=wes, sql="CREATE TABLE ws.order_log (what VARCHAR)")
    expect_allowed(client=wes, sql="USE app")
    expect_allowed(client=wes, sql="INSERT INTO app.orders VALUES (1)")
    assert values(client=admin, sql="SELECT what FROM app.order_log") == ["order"]
    expect_allowed(client=admin, sql="CREATE TABLE main.order_log (what VARCHAR)")
    refused(client=wes, sql="INSERT INTO app.orders VALUES (2)", trigger="orders_audit", name="order_log",
            why="both the table's own schema and its catalog's main schema have one",
            remedy="The trigger's owner can name it with its schema (schema.name) with CREATE OR REPLACE TRIGGER")


# ---- new triggers: stored with their names given their schema ----

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


def test_create_trigger_stores_the_names_in_its_action_with_their_schema(admin, mia, wes):
    # mia works in app (USE app): her names are bare. What is stored names every table, view
    # and sequence with its schema; a CTE's name and a transition table's stay as they are.
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
            "INSERT INTO app.log WITH picked AS (SELECT note FROM fresh WHERE (id = ANY(SELECT id FROM app.state))) "
            "SELECT nextval('app.log_ids'), (\"label\" || note) FROM picked , app.labels;")
        assert "UPDATE app.state SET seen = (seen + 1)" in stored["src_upd"]
        assert "DELETE FROM app.state WHERE " in stored["src_del"]
        # The writer's session has its own objects of every one of those names, ahead in its
        # search path and temporary (in main, the temporary catalog's only schema, so none named
        # app.log): none of them stands in, and the triggers run with mia's
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
    # A table reached through a macro is looked up in the writer's session too: the macro's
    # own names are held against the session like the body's.
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
        assert "trigger nm_t_ins on table" in error.detail and "none of the names in its body or in the macros it calls" in error.detail
        assert DROP_TEMPORARY in error.detail
        expect_allowed(client=wes, sql="DROP SEQUENCE temp.main.nm_ids")
        expect_allowed(client=wes, sql="INSERT INTO main.nm_t VALUES (3)")
        assert values(client=admin, sql="SELECT id || ' ' || what FROM main.nm_log ORDER BY id") == ["10 default", "11 default"]
        # The macro's table is the one its name is for the trigger's table: it fires.
        expect_allowed(client=wes, sql="INSERT INTO main.nm_m VALUES (1)")
        assert values(client=admin, sql="SELECT what FROM main.nm_log WHERE id = 12") == ["macro 4"]
        # A temporary table of the name the macro reads would stand in for it: refused.
        expect_allowed(client=wes, sql="CREATE TEMP TABLE nm_src (v INT)")
        expect_allowed(client=wes, sql="INSERT INTO temp.main.nm_src VALUES (999)")
        error = expect_blocked(client=wes, sql="INSERT INTO main.nm_m VALUES (2)", why="calls macro nm_rows, which names nm_src")
        assert "trigger nm_m_ins on table" in error.detail and TEMPORARY in error.detail and DROP_TEMPORARY in error.detail
        expect_allowed(client=wes, sql="DROP TABLE temp.main.nm_src")
        expect_allowed(client=wes, sql="INSERT INTO main.nm_m VALUES (3)")
        assert values(client=admin, sql="SELECT what FROM main.nm_log WHERE id > 12 ORDER BY id") == ["macro 4"]
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS main.nm_t", "DROP TABLE IF EXISTS main.nm_m",
                                       "DROP MACRO TABLE IF EXISTS main.nm_rows", "DROP TABLE IF EXISTS main.nm_log",
                                       "DROP TABLE IF EXISTS main.nm_src", "DROP SEQUENCE IF EXISTS main.nm_ids"])


def test_a_cte_of_the_body_leaves_a_table_of_its_name_elsewhere_in_it(admin, mia, wes):
    # Finding 03: a name was left as written wherever any CTE of the body had it. Only a
    # reference in that CTE's scope is the CTE's; one outside it is the table, given its schema.
    for sql in ["USE app", "CREATE TABLE c03 (id INT)", "CREATE TABLE c03_lookup (id INT, label VARCHAR)",
                "INSERT INTO c03_lookup VALUES (1, 'one'), (2, 'two')", "CREATE TABLE c03_log (id INT, what VARCHAR)",
                "CREATE TRIGGER c03_ins AFTER INSERT ON c03 REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                "INSERT INTO c03_log SELECT id, 'c03' FROM n "
                "WHERE id IN (WITH c03_lookup AS (SELECT 1 AS id) SELECT id FROM c03_lookup) "
                "AND id IN (SELECT id FROM c03_lookup WHERE label = 'one')",
                # A CTE that reads the table of its own name: inside its own definition the name is not itself.
                "CREATE TABLE c03b (id INT)",
                "CREATE TRIGGER c03b_ins AFTER INSERT ON c03b FOR EACH STATEMENT "
                "WITH c03_lookup AS (SELECT id FROM c03_lookup WHERE label = 'two') INSERT INTO c03_log SELECT id, 'c03b' FROM c03_lookup",
                "GRANT INSERT ON c03 TO writers", "GRANT INSERT ON c03b TO writers", "USE main"]:
        expect_allowed(client=mia, sql=sql)
    try:
        rows = admin.run(sql="SELECT trigger_name, sql FROM duckdb_triggers() WHERE table_name IN ('c03', 'c03b')").to_pylist()
        stored = {row["trigger_name"]: row["sql"] for row in rows}
        assert "SELECT id FROM c03_lookup)) AND (id = ANY(SELECT id FROM app.c03_lookup WHERE" in stored["c03_ins"], stored
        assert "WITH c03_lookup AS (SELECT id FROM app.c03_lookup WHERE" in stored["c03b_ins"], stored
        assert "SELECT id, 'c03b' FROM c03_lookup;" in stored["c03b_ins"], stored
        # The writer's own CTE, temporary table and search path: none stands in for the table.
        for sql in ["CREATE TEMP TABLE c03_lookup (id INT, label VARCHAR)", "CREATE TABLE ws.c03_lookup (id INT, label VARCHAR)",
                    "USE ws"]:
            expect_allowed(client=wes, sql=sql)
        expect_allowed(client=wes, sql="WITH c03_lookup AS (SELECT 1 AS id, 'one' AS label) INSERT INTO tn.app.c03 VALUES (1), (2)")
        expect_allowed(client=wes, sql="WITH c03_lookup AS (SELECT 1 AS id, 'two' AS label) INSERT INTO tn.app.c03b VALUES (9)")
        assert sorted(values(client=mia, sql="SELECT id || ' ' || what FROM app.c03_log")) == ["1 c03", "2 c03b"]
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS ws.c03_lookup", "DROP TABLE IF EXISTS app.c03",
                                       "DROP TABLE IF EXISTS app.c03b", "DROP TABLE IF EXISTS app.c03_lookup",
                                       "DROP TABLE IF EXISTS app.c03_log"])


def test_new_triggers_are_the_writers_to_shadow_only_where_a_temporary_object_would(admin, mia, wes):
    # A trigger made now names its objects with their schema: a writer's WITH can't stand in
    # for a name with a schema, nor its search path within the catalog. A temporary object can
    # for one in main (the temporary catalog's schema): refused, and the remedy is the writer's.
    drop(client=admin, statements=[
        "CREATE TABLE main.nt_log (what VARCHAR)", "CREATE TABLE main.nt_rates (rate INT)", "INSERT INTO main.nt_rates VALUES (7)",
        "CREATE TABLE app.nt_lookup (id INT, label VARCHAR)", "INSERT INTO app.nt_lookup VALUES (1, 'one')",
        "CREATE TABLE main.nt_s (id INT)", "CREATE TABLE main.nt_r (id INT)",
        "CREATE TRIGGER nt_s_ins AFTER INSERT ON main.nt_s FOR EACH STATEMENT INSERT INTO nt_log SELECT 's ' || rate FROM nt_rates",
        "USE app",
        "CREATE TRIGGER nt_r_ins AFTER INSERT ON main.nt_r FOR EACH ROW "
        "INSERT INTO main.nt_log SELECT 'r ' || label FROM nt_lookup WHERE id = NEW.id",
        "USE main",
        "GRANT INSERT ON main.nt_s TO writers", "GRANT INSERT ON main.nt_r TO writers", "GRANT INSERT ON main.nt_log TO writers",
        "GRANT SELECT ON main.nt_rates TO writers", "GRANT SELECT ON app.nt_lookup TO writers",
    ])
    try:
        stored = values(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name IN ('nt_s_ins', 'nt_r_ins') ORDER BY 1")
        assert any("FROM app.nt_lookup" in sql for sql in stored) and any("FROM main.nt_rates" in sql for sql in stored), stored
        expect_allowed(client=wes, sql="WITH nt_rates AS (SELECT 999 AS rate), nt_log AS (SELECT 1 AS x) INSERT INTO main.nt_s VALUES (1)")
        expect_allowed(client=wes, sql="WITH nt_lookup AS (SELECT 1 AS id, 'FORGED' AS label) INSERT INTO main.nt_r VALUES (1)")
        for sql in ["CREATE TABLE ws.nt_rates (rate INT)", "CREATE TABLE ws.nt_lookup (id INT, label VARCHAR)",
                    "CREATE TABLE ws.nt_log (what VARCHAR)", "USE ws"]:
            expect_allowed(client=wes, sql=sql)
        expect_allowed(client=wes, sql="INSERT INTO tn.main.nt_s VALUES (1)")
        expect_allowed(client=wes, sql="INSERT INTO tn.main.nt_r VALUES (1)")
        assert sorted(values(client=admin, sql="SELECT what FROM main.nt_log")) == ["r one", "r one", "s 7", "s 7"]
        # A temporary table named like app's table stands in for nothing (app.nt_lookup) ...
        expect_allowed(client=wes, sql="CREATE TEMP TABLE nt_lookup AS SELECT 1 AS id, 'FORGED' AS label")
        expect_allowed(client=wes, sql="INSERT INTO tn.main.nt_r VALUES (1)")
        # ... one named like main's would (main.nt_rates): refused.
        expect_allowed(client=wes, sql="CREATE TEMP TABLE nt_rates AS SELECT 999 AS rate")
        error = expect_blocked(client=wes, sql="INSERT INTO tn.main.nt_s VALUES (2)", why="names main.nt_rates")
        assert TEMPORARY in error.detail and DROP_TEMPORARY in error.detail
        expect_allowed(client=wes, sql="DROP TABLE temp.main.nt_rates")
        expect_allowed(client=wes, sql="INSERT INTO tn.main.nt_s VALUES (3)")
        assert sorted(values(client=admin, sql="SELECT what FROM main.nt_log")) == ["r one", "r one", "r one", "s 7", "s 7", "s 7"]
        for table in ["ws.nt_log"]:
            assert count(client=wes, table=table) == 0
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS ws.nt_rates", "DROP TABLE IF EXISTS ws.nt_lookup",
                                       "DROP TABLE IF EXISTS ws.nt_log", "DROP TABLE IF EXISTS main.nt_s",
                                       "DROP TABLE IF EXISTS main.nt_r", "DROP TABLE IF EXISTS main.nt_log",
                                       "DROP TABLE IF EXISTS main.nt_rates", "DROP TABLE IF EXISTS app.nt_lookup"])


def test_a_macro_a_new_trigger_calls_is_held_to_its_names(admin, wes):
    # The macro's own text is the administrator's to write (and to name bare): a table macro is
    # bound where it is called, in the writer's session, so its names are held like the body's.
    drop(client=admin, statements=[
        "CREATE TABLE main.tm_lookup (id INT, label VARCHAR)", "INSERT INTO main.tm_lookup VALUES (1, 'one')",
        "CREATE MACRO main.tm_rows() AS TABLE SELECT id, label FROM tm_lookup",
        "CREATE TABLE main.tm_log (what VARCHAR)", "CREATE TABLE main.tm_t (id INT)",
        "CREATE TRIGGER tm_ins AFTER INSERT ON main.tm_t REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
        "INSERT INTO tm_log SELECT 'tm ' || r.label FROM fresh JOIN tm_rows() r USING (id)",
        "GRANT INSERT ON main.tm_t TO writers", "GRANT INSERT ON main.tm_log TO writers", "GRANT SELECT ON main.tm_lookup TO writers",
    ])
    try:
        insert = "INSERT INTO tn.main.tm_t VALUES (1)"
        expect_allowed(client=wes, sql=insert)
        error = expect_blocked(client=wes, sql="WITH tm_lookup AS (SELECT 1 AS id, 'FORGED' AS label) " + insert, why=OUTER_CTE)
        assert "trigger tm_ins on table" in error.detail and " reads tm_lookup," in error.detail
        expect_allowed(client=wes, sql="CREATE TEMP TABLE tm_lookup AS SELECT 1 AS id, 'FORGED' AS label")
        error = expect_blocked(client=wes, sql=insert, why="calls macro tm_rows, which names tm_lookup")
        assert TEMPORARY in error.detail
        expect_allowed(client=wes, sql="DROP TABLE temp.main.tm_lookup")
        expect_allowed(client=wes, sql="CREATE TABLE ws.tm_lookup AS SELECT 1 AS id, 'FORGED' AS label")
        expect_allowed(client=wes, sql="USE ws")
        error = expect_blocked(client=wes, sql=insert, why="calls macro tm_rows, which names tm_lookup")
        assert ANOTHER in error.detail and "An administrator can name it with its schema in the macro" in error.detail
        expect_allowed(client=wes, sql="USE main")
        expect_allowed(client=wes, sql=insert)
        assert values(client=admin, sql="SELECT what FROM main.tm_log") == ["tm one", "tm one"]
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS ws.tm_lookup", "DROP TABLE IF EXISTS main.tm_t",
                                       "DROP MACRO TABLE IF EXISTS main.tm_rows", "DROP TABLE IF EXISTS main.tm_log",
                                       "DROP TABLE IF EXISTS main.tm_lookup"])


def test_what_could_never_be_checked_is_not_stored(admin, mia):
    # A trigger that names a sequence, or a table through query_table(), by something other
    # than a plain string could never be held against a writer's session: it would fire for
    # the administrator alone. CREATE TRIGGER refuses it, for the administrator too.
    drop(client=admin, statements=["CREATE TABLE main.wc_t (id INT)", "CREATE TABLE main.wc_log (what VARCHAR)",
                                   "CREATE SEQUENCE main.wc_ids", "CREATE TABLE main.wc_src (what VARCHAR)",
                                   "INSERT INTO main.wc_src VALUES ('src')"])
    try:
        for body, why in [
            ("INSERT INTO wc_log VALUES (nextval('wc_' || 'ids')::VARCHAR)", "name the sequence as one (nextval('schema.name'))"),
            ("INSERT INTO wc_log VALUES (currval('wc_ids'::VARCHAR)::VARCHAR)", "name the sequence as one (nextval('schema.name'))"),
            ("INSERT INTO wc_log SELECT what FROM query_table('wc_' || 'src')", "name the table itself in the action"),
            ("INSERT INTO wc_log SELECT what FROM query('SELECT what FROM wc_src')", "name the table itself in the action"),
        ]:
            expect_blocked(client=admin, sql=f"CREATE TRIGGER wc_ins AFTER INSERT ON main.wc_t FOR EACH STATEMENT {body}", why=why)
        assert values(client=admin, sql="SELECT count(*) FROM duckdb_triggers() WHERE trigger_name = 'wc_ins'") == ["0"]
        # Twin: query_table of one table's name, a plain string, is stored with its schema.
        expect_allowed(client=admin, sql="CREATE TRIGGER wc_ins AFTER INSERT ON main.wc_t FOR EACH STATEMENT "
                                         "INSERT INTO wc_log SELECT what FROM query_table('wc_src')")
        assert "query_table('main.wc_src')" in values(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name = 'wc_ins'")[0]
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS main.wc_t", "DROP TABLE IF EXISTS main.wc_log",
                                       "DROP TABLE IF EXISTS main.wc_src", "DROP SEQUENCE IF EXISTS main.wc_ids"])


def test_an_administrators_if_not_exists_leaves_a_users_trigger_hers(admin, mia, wes):
    # Finding 07: an administrator's CREATE TRIGGER IF NOT EXISTS over a user's trigger kept
    # DuckDB's trigger but recorded it as the administrator's, so it ran with the writer's rights.
    for sql in ["CREATE TABLE app.ine (id INT)", "CREATE TABLE app.ine_sink (what VARCHAR)",
                "CREATE TRIGGER ine_ins AFTER INSERT ON app.ine FOR EACH STATEMENT INSERT INTO app.ine_sink VALUES ('by mia')",
                "GRANT INSERT ON app.ine TO writers"]:
        expect_allowed(client=mia, sql=sql)
    try:
        expect_blocked(client=wes, sql="INSERT INTO app.ine_sink VALUES ('wes')", why="no INSERT privilege")
        expect_allowed(client=wes, sql="INSERT INTO app.ine VALUES (1)")
        expect_allowed(client=admin, sql="CREATE TRIGGER IF NOT EXISTS ine_ins AFTER INSERT ON app.ine FOR EACH STATEMENT "
                                         "INSERT INTO app.ine_sink VALUES ('by admin')")
        assert "'by mia'" in values(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name = 'ine_ins'")[0]
        # Still mia's, run with her rights: wes, who may not write her sink, fires it.
        expect_allowed(client=wes, sql="INSERT INTO app.ine VALUES (2)")
        assert values(client=admin, sql="SELECT what FROM app.ine_sink") == ["by mia", "by mia"]
        # Twin: CREATE OR REPLACE by the administrator does make it theirs (writer's rights).
        expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER ine_ins AFTER INSERT ON app.ine FOR EACH STATEMENT "
                                         "INSERT INTO app.ine_sink VALUES ('by admin')")
        expect_blocked(client=wes, sql="INSERT INTO app.ine VALUES (3)", why="no INSERT privilege on table tn.app.ine_sink")
    finally:
        drop(client=admin, statements=["DROP TABLE IF EXISTS app.ine", "DROP TABLE IF EXISTS app.ine_sink"])


def test_a_renamed_database_or_attach_alias_keeps_its_triggers(launcher, store, tmp_path):
    # Finding 05 (Philip: schema-qualified names only): the database file's name, and an
    # ATTACH alias, are no part of a stored trigger, so renaming either breaks no write.
    first = tmp_path / "sales.duckdb"
    branch = tmp_path / "branch.duckdb"
    store_uri = store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(first)])
    admin = server.admin()
    for sql in ["CREATE TABLE main.pay (id INT)", "CREATE TABLE main.pay_log (what VARCHAR)",
                "CREATE TRIGGER pay_audit AFTER INSERT ON main.pay REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                "INSERT INTO pay_log SELECT 'pay ' || id FROM n",
                f"ATTACH '{branch}' AS branch", "USE branch",
                "CREATE TABLE main.pay (id INT)", "CREATE TABLE main.pay_log (what VARCHAR)",
                "CREATE TRIGGER pay_audit AFTER INSERT ON main.pay REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
                "INSERT INTO pay_log SELECT 'branch pay ' || id FROM n",
                "USE sales", "DETACH branch", "CHECKPOINT"]:
        expect_allowed(client=admin, sql=sql)
    assert values(client=admin, sql="SELECT sql FROM duckdb_triggers()") == [
        "CREATE TRIGGER pay_audit AFTER INSERT ON pay REFERENCING NEW TABLE AS n FOR EACH STATEMENT "
        "INSERT INTO main.pay_log SELECT ('pay ' || id) FROM n;"]
    server.running.stop()
    renamed = tmp_path / "sales_2026.duckdb"
    first.rename(renamed)
    server = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(renamed)])
    admin = server.admin()
    expect_allowed(client=admin, sql="INSERT INTO main.pay VALUES (1)")
    assert values(client=admin, sql="SELECT what FROM main.pay_log") == ["pay 1"]
    for sql in [f"ATTACH '{branch}' AS branch_eu", "USE branch_eu", "INSERT INTO main.pay VALUES (2)"]:
        expect_allowed(client=admin, sql=sql)
    assert values(client=admin, sql="SELECT what FROM branch_eu.main.pay_log") == ["branch pay 2"]


def test_the_init_sqls_triggers_are_stored_with_their_names_given_their_schema(launcher, store, tmp_path):
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
        "CREATE TRIGGER jobs_ins AFTER INSERT ON jobs FOR EACH STATEMENT INSERT INTO ops.job_log (VALUES ('job'));"]
    # A writer whose search path never finds job_log fires it, a temporary table or not.
    expect_allowed(client=wes, sql="CREATE TEMP TABLE job_log (what VARCHAR)")
    expect_allowed(client=wes, sql="INSERT INTO ops.jobs VALUES (1)")
    assert count(client=admin, table="ops.job_log") == 1 and count(client=wes, table="temp.main.job_log") == 0
