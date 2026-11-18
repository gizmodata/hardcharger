"""Triggers under access control: CREATE TRIGGER and DROP TRIGGER, granted ON SCHEMA or ON
CATALOG, on a table the user owns.

DuckDB binds a trigger's body into the statement that fires it. A user's trigger runs with
its OWNER's rights (definer rights): the body is checked, and row and masking policies are
applied, as if the owner ran it, whoever writes the table, so an administrator's write lends
it nothing. An administrator's trigger (or one with no owner record) runs with the writer's
rights, as DuckDB runs it. Every refusal is asserted by its reason, next to the twin that
succeeds."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NEEDS_CREATE = "it needs CREATE TRIGGER on schema"
LOG_ORDERS = ("CREATE TRIGGER log_orders AFTER INSERT ON app.orders FOR EACH STATEMENT "
              "INSERT INTO app.order_log VALUES ('inserted')")
ONLY_OWNER = "only the table's owner may"
MAY_NOT_DROP = "may not drop trigger"
CANNOT_RUN = "cannot run, because its owner user mia may not do what its body does"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_triggers.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA app",
        "CREATE TABLE secrets (id INT, value VARCHAR)",
        "INSERT INTO secrets VALUES (1, 'top-secret-value')",
        "CREATE TABLE audit (what VARCHAR)",
        "CREATE TABLE shared (id INT)",
        "CREATE TABLE governed (id INT, region VARCHAR)",
        "INSERT INTO governed VALUES (1, 'EU'), (2, 'US')",
        "CREATE MACRO main.fmt(x) AS 'order ' || x",
        "CREATE ROLE writers", "CREATE ROLE trigger_makers", "CREATE ROLE trigger_droppers", "CREATE ROLE app_builders",
        "CREATE ROLE secret_readers", "CREATE ROLE macro_makers", "CREATE ROLE eu_staff",
        "GRANT INSERT, SELECT ON shared TO writers",
        "GRANT CREATE TRIGGER, DROP TRIGGER ON SCHEMA app TO trigger_makers",
        "GRANT DROP TRIGGER ON SCHEMA app TO trigger_droppers",
        "GRANT CREATE TABLE, SELECT, INSERT ON SCHEMA app TO app_builders",
        "GRANT SELECT ON secrets TO secret_readers",
        "GRANT CREATE MACRO, DROP MACRO ON SCHEMA app TO macro_makers",
        "GRANT SELECT ON governed TO eu_staff",
        "CREATE ROW POLICY eu_rows ON governed TO eu_staff USING (region = 'EU')",
        "CREATE ROLE inserters", "CREATE ROLE changers",
        "CREATE USER mia", "CREATE USER joe", "CREATE USER cal", "CREATE USER dan", "CREATE USER sid", "CREATE USER ike",
        "CREATE USER wes",
        "GRANT trigger_makers, app_builders, writers, eu_staff TO mia",
        "GRANT writers TO joe", "GRANT app_builders, macro_makers TO cal", "GRANT trigger_droppers TO dan",
        "GRANT secret_readers TO sid", "GRANT inserters TO ike", "GRANT changers TO wes",
    ]:
        expect_allowed(client=admin, sql=sql)
    mia = server.user(name="mia")
    # mia's tables (she owns them), with INSERT for the writers.
    for sql in [
        "CREATE TABLE app.orders (id INT, note VARCHAR)",
        "CREATE TABLE app.order_log (what VARCHAR)",
        "CREATE TABLE app.items (id INT)",
        "CREATE TABLE app.item_log (id INT)",
        "CREATE TABLE app.leak (value VARCHAR)",
        "CREATE TABLE app.eu_copy (id INT, region VARCHAR)",
        "GRANT INSERT, SELECT ON app.orders TO writers", "GRANT INSERT ON app.orders TO inserters",
        "GRANT INSERT, SELECT ON app.items TO writers",
        "GRANT SELECT ON app.order_log TO writers", "GRANT SELECT ON app.item_log TO writers",
        # mia's trigger on her table, writing a table only she may write.
        LOG_ORDERS,
    ]:
        expect_allowed(client=mia, sql=sql)
    server.admin_client = admin
    return server


# Every test leaves the server as it found it: pytest-xdist runs the tests in any order.


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.fixture
def mia(server):
    return server.user(name="mia")


@pytest.fixture
def joe(server):
    return server.user(name="joe")


@pytest.fixture
def cal(server):
    return server.user(name="cal")


@pytest.fixture
def wes(server):
    # Holds role changers alone: what each test grants it on the test's own tables.
    return server.user(name="wes")


def triggers_of(admin, table: str) -> set[str]:
    rows = admin.run(sql=f"SELECT trigger_name FROM duckdb_triggers() WHERE table_name = '{table}'").to_pylist()
    return {r["trigger_name"] for r in rows}


def values(client, sql: str) -> list:
    table = expect_allowed(client=client, sql=sql)
    return [str(v) for v in table.column(0).to_pylist()]


# ---- the privileges ----

def test_create_trigger_needs_ownership_of_the_table_and_the_privilege(admin, mia, joe, cal):
    body = "AFTER INSERT ON app.orders FOR EACH STATEMENT INSERT INTO app.order_log VALUES ('extra')"
    # joe holds no CREATE TRIGGER and owns nothing; cal may build in app but holds no
    # CREATE TRIGGER; mia owns the table and holds the privilege.
    expect_blocked(client=joe, sql=f"CREATE TRIGGER extra_log {body}", why=ONLY_OWNER)
    expect_allowed(client=cal, sql="CREATE TABLE IF NOT EXISTS app.cals (id INT)")
    expect_allowed(client=cal, sql="CREATE TABLE IF NOT EXISTS app.cals_log (id INT)")
    expect_blocked(client=cal, sql="CREATE TRIGGER log_cals AFTER INSERT ON app.cals FOR EACH STATEMENT "
                                   "INSERT INTO app.cals_log VALUES (0)", why=NEEDS_CREATE)
    expect_allowed(client=mia, sql=f"CREATE TRIGGER extra_log {body}")
    try:
        assert triggers_of(admin=admin, table="orders") == {"log_orders", "extra_log"}
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER extra_log ON app.orders")
    # The privilege alone is not enough: mia owns app.orders, not main.shared.
    expect_blocked(client=mia, sql="CREATE TRIGGER log_shared AFTER INSERT ON main.shared FOR EACH STATEMENT "
                                   "INSERT INTO app.order_log VALUES ('shared')", why=ONLY_OWNER)


def test_the_grant_is_on_a_schema_or_a_catalog_never_one_object(admin):
    for sql in ["GRANT CREATE TRIGGER ON app.orders TO writers", "GRANT DROP TRIGGER ON main.shared TO writers"]:
        with pytest.raises(ChargewireError) as raised:
            admin.run(sql=sql)
        assert "is granted ON SCHEMA or ON CATALOG, not on a single object" in raised.value.detail
    catalog = admin.run(sql="SELECT current_database() AS c").column("c").to_pylist()[0]
    expect_allowed(client=admin, sql=f"GRANT CREATE TRIGGER, DROP TRIGGER ON CATALOG {catalog} TO writers")
    grants = admin.run(sql="SHOW GRANTS FOR writers").to_pylist()
    assert {"CREATE TRIGGER", "DROP TRIGGER"} <= {g["privilege"] for g in grants if g["object_type"] == "CATALOG"}
    expect_allowed(client=admin, sql=f"REVOKE CREATE TRIGGER, DROP TRIGGER ON CATALOG {catalog} FROM writers")


# ---- whose rights a body runs with ----

def logged(client) -> int:
    return int(values(client=client, sql="SELECT count(*) FROM app.order_log WHERE what = 'inserted'")[0])


def test_a_users_trigger_runs_with_its_owners_rights(admin, mia, joe):
    # log_orders writes app.order_log, which joe may not write: the body runs as mia, so
    # joe's INSERT fires it all the same.
    expect_blocked(client=joe, sql="INSERT INTO app.order_log VALUES ('joes')", why="no INSERT privilege")
    before = logged(client=joe)
    expect_allowed(client=joe, sql="INSERT INTO app.orders VALUES (1, 'first')")
    assert logged(client=joe) == before + 1
    # The administrator's write fires it too (an administrator holds every right, so the
    # body's own rights don't show here; the next test is where they do).
    expect_allowed(client=admin, sql="INSERT INTO app.orders VALUES (2, 'second')")
    assert logged(client=joe) == before + 2


def test_a_users_trigger_gets_no_administrator_rights(admin, mia, joe, server):
    # The escalation path: mia's trigger copies main.secrets (which she may not read) into
    # her own table; an administrator's INSERT would run the body with administrator rights
    # if DuckDB had its way.
    expect_allowed(client=mia, sql="CREATE TRIGGER leak_secrets AFTER INSERT ON app.items FOR EACH STATEMENT "
                                   "INSERT INTO app.leak SELECT value FROM main.secrets")
    for client in [admin, joe]:
        error = expect_blocked(client=client, sql="INSERT INTO app.items VALUES (1)", why=CANNOT_RUN)
        assert "trigger leak_secrets on table" in error.detail
        assert "no SELECT privilege on table" in error.detail and "secrets" in error.detail
    assert values(client=mia, sql="SELECT count(*) FROM app.leak") == ["0"]
    assert values(client=mia, sql="SELECT count(*) FROM app.items") == ["0"]
    # Twin: once mia may read secrets, the same statements run, and the copy holds what mia
    # may read anyway.
    expect_allowed(client=admin, sql="GRANT secret_readers TO mia")
    try:
        expect_allowed(client=admin, sql="INSERT INTO app.items VALUES (1)")
        expect_allowed(client=joe, sql="INSERT INTO app.items VALUES (2)")
        assert values(client=mia, sql="SELECT value FROM app.leak") == ["top-secret-value", "top-secret-value"]
    finally:
        expect_allowed(client=admin, sql="REVOKE secret_readers FROM mia")
        expect_allowed(client=mia, sql="DROP TRIGGER leak_secrets ON app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.leak")
        expect_allowed(client=mia, sql="DELETE FROM app.items")


def test_an_administrators_trigger_runs_with_the_writers_rights(admin, joe):
    # As DuckDB runs it: the body is part of the writer's statement.
    expect_allowed(client=admin, sql="CREATE TRIGGER audit_shared AFTER INSERT ON main.shared FOR EACH STATEMENT "
                                     "INSERT INTO main.audit VALUES ('shared row')")
    try:
        error = expect_blocked(client=joe, sql="INSERT INTO main.shared VALUES (1)", why="no INSERT privilege on table")
        assert "audit" in error.detail
        assert values(client=admin, sql="SELECT count(*) FROM main.shared") == ["0"]
        expect_allowed(client=admin, sql="INSERT INTO main.shared VALUES (1)")
        # Twin: a writer who may write the audit table fires it.
        expect_allowed(client=admin, sql="GRANT INSERT ON main.audit TO writers")
        expect_allowed(client=joe, sql="INSERT INTO main.shared VALUES (2)")
        assert values(client=admin, sql="SELECT what FROM main.audit") == ["shared row", "shared row"]
    finally:
        expect_allowed(client=admin, sql="REVOKE INSERT ON main.audit FROM writers")
        expect_allowed(client=admin, sql="DROP TRIGGER audit_shared ON main.shared")


def test_a_row_trigger_runs_with_its_owners_rights(admin, mia, joe):
    # FOR EACH ROW triggers are bound differently (a LogicalTrigger node per trigger).
    expect_allowed(client=mia, sql="CREATE TRIGGER log_items AFTER INSERT ON app.items FOR EACH ROW "
                                   "INSERT INTO app.item_log VALUES (NEW.id * 10)")
    try:
        expect_blocked(client=joe, sql="INSERT INTO app.item_log VALUES (0)", why="no INSERT privilege")
        expect_allowed(client=joe, sql="INSERT INTO app.items VALUES (3), (4)")
        assert sorted(values(client=joe, sql="SELECT id FROM app.item_log")) == ["30", "40"]
        # And gets no administrator rights either.
        expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER log_items AFTER INSERT ON app.items FOR EACH ROW "
                                       "INSERT INTO app.leak SELECT value || NEW.id FROM main.secrets")
        error = expect_blocked(client=admin, sql="INSERT INTO app.items VALUES (5)", why=CANNOT_RUN)
        assert "trigger log_items on table" in error.detail
        assert values(client=mia, sql="SELECT count(*) FROM app.leak") == ["0"]
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER log_items ON app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.item_log")


def test_a_writers_own_returning_still_needs_select(server, mia):
    # The binder adds RETURNING * to the writer's INSERT for the triggers' transition tables,
    # which the writer never sees: INSERT alone is enough. A RETURNING the writer wrote
    # hands the rows back, so it needs SELECT as always.
    ike = server.user(name="ike")
    before = logged(client=mia)
    expect_allowed(client=ike, sql="INSERT INTO app.orders VALUES (12, 'twelfth')")
    assert logged(client=mia) == before + 1
    expect_blocked(client=ike, sql="INSERT INTO app.orders VALUES (13, 'thirteenth') RETURNING *", why="no SELECT privilege")
    expect_allowed(client=mia, sql="DELETE FROM app.orders WHERE id = 12")


def test_before_triggers_too(admin, mia, joe):
    expect_allowed(client=mia, sql="CREATE TRIGGER before_items BEFORE INSERT ON app.items FOR EACH STATEMENT "
                                   "INSERT INTO app.item_log VALUES (-1)")
    try:
        expect_allowed(client=joe, sql="INSERT INTO app.items VALUES (6)")
        assert values(client=joe, sql="SELECT id FROM app.item_log") == ["-1"]
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER before_items ON app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.item_log")


def test_update_of_triggers_are_matched_to_their_bodies(admin, mia, joe):
    # Two AFTER UPDATE statement triggers: the administrator's fires only for note (and
    # writes audit, which joe may not), mia's for every update (and writes her log, which
    # joe may not, so it runs only if its body is matched to mia's trigger, not the
    # administrator's). The administrator's comes first in creation order and in name order,
    # so a match that ignored UPDATE OF would take it for mia's.
    expect_allowed(client=admin, sql="CREATE TRIGGER aaa_note_audit AFTER UPDATE OF note ON app.orders FOR EACH STATEMENT "
                                     "INSERT INTO main.audit VALUES ('note changed')")
    expect_allowed(client=mia, sql="CREATE TRIGGER zzz_any_update AFTER UPDATE ON app.orders FOR EACH STATEMENT "
                                   "INSERT INTO app.order_log VALUES ('updated')")
    expect_allowed(client=admin, sql="GRANT UPDATE ON app.orders TO writers")
    try:
        expect_allowed(client=joe, sql="UPDATE app.orders SET id = id + 100")
        assert values(client=joe, sql="SELECT count(*) FROM app.order_log WHERE what = 'updated'") == ["1"]
        error = expect_blocked(client=joe, sql="UPDATE app.orders SET note = 'changed'", why="no INSERT privilege on table")
        assert "audit" in error.detail
        assert values(client=joe, sql="SELECT count(*) FROM app.order_log WHERE what = 'updated'") == ["1"]
        expect_allowed(client=mia, sql="DELETE FROM app.order_log WHERE what = 'updated'")
    finally:
        expect_allowed(client=admin, sql="REVOKE UPDATE ON app.orders FROM writers")
        expect_allowed(client=admin, sql="DROP TRIGGER aaa_note_audit ON app.orders")
        expect_allowed(client=mia, sql="DROP TRIGGER zzz_any_update ON app.orders")


def test_policies_apply_to_the_owner_inside_the_body(admin, mia, joe):
    # mia's rows of governed are the EU ones (eu_staff's policy); the administrator and joe
    # have no policy there. The body copies what mia may see, whoever fires it.
    expect_allowed(client=mia, sql="CREATE TRIGGER copy_governed AFTER INSERT ON app.items FOR EACH STATEMENT "
                                   "INSERT INTO app.eu_copy SELECT id, region FROM main.governed")
    try:
        expect_allowed(client=admin, sql="INSERT INTO app.items VALUES (7)")
        expect_allowed(client=joe, sql="INSERT INTO app.items VALUES (8)")
        assert values(client=mia, sql="SELECT region FROM app.eu_copy") == ["EU", "EU"]
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER copy_governed ON app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.items")
        expect_allowed(client=mia, sql="DELETE FROM app.eu_copy")


def test_a_stale_owner_record_of_an_earlier_trigger_does_not_apply(launcher, store, tmp_path):
    # mia's trigger gets an owner record; the init SQL of a later start (which runs as
    # Hardcharger itself and records no owner) replaces the trigger under the same name.
    # The record's definition hash no longer matches, so the new body runs with the
    # writer's rights, not mia's.
    database = tmp_path / "stale.duckdb"
    store_uri = store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(database)])
    admin = first.admin()
    for sql in ["CREATE SCHEMA app", "CREATE ROLE builders", "GRANT CREATE TABLE, CREATE TRIGGER ON SCHEMA app TO builders",
                "CREATE ROLE writers", "CREATE USER mia", "CREATE USER joe", "GRANT builders TO mia", "GRANT writers TO joe"]:
        expect_allowed(client=admin, sql=sql)
    mia = first.user(name="mia")
    for sql in ["CREATE TABLE app.t (id INT)", "CREATE TABLE app.t_log (id INT)", "GRANT INSERT ON app.t TO writers",
                "CREATE TRIGGER log_t AFTER INSERT ON app.t FOR EACH STATEMENT INSERT INTO app.t_log VALUES (1)"]:
        expect_allowed(client=mia, sql=sql)
    expect_allowed(client=first.user(name="joe"), sql="INSERT INTO app.t VALUES (1)")  # mia's rights
    first.running.stop()
    init = "CREATE OR REPLACE TRIGGER log_t AFTER INSERT ON app.t FOR EACH STATEMENT INSERT INTO app.t_log VALUES (2);"
    second = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(database)],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": init})
    joe = second.user(name="joe")
    error = expect_blocked(client=joe, sql="INSERT INTO app.t VALUES (2)", why="no INSERT privilege on table")
    assert "t_log" in error.detail
    # Twin: mia, who may write the log herself, still fires it.
    expect_allowed(client=second.user(name="mia"), sql="INSERT INTO app.t VALUES (3)")


def test_a_disabled_owner_stops_the_trigger(admin, mia, joe, server):
    expect_allowed(client=admin, sql="ALTER USER mia DISABLE")
    try:
        error = expect_blocked(client=joe, sql="INSERT INTO app.orders VALUES (9, 'ninth')",
                               why="cannot run, because its owner is disabled or was dropped")
        assert "trigger log_orders on table" in error.detail
        expect_blocked(client=admin, sql="INSERT INTO app.orders VALUES (9, 'ninth')",
                       why="cannot run, because its owner is disabled or was dropped")
    finally:
        expect_allowed(client=admin, sql="ALTER USER mia ENABLE")
    expect_allowed(client=joe, sql="INSERT INTO app.orders VALUES (9, 'ninth')")
    # Disabling mia ended her sessions: a new one for the cleanup.
    expect_allowed(client=server.user(name="mia"), sql="DELETE FROM app.orders WHERE id = 9")


def test_an_administrator_replacing_a_users_trigger_makes_it_run_with_the_writers_rights(admin, mia, joe):
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER log_orders AFTER INSERT ON app.orders FOR EACH STATEMENT "
                                     "INSERT INTO app.order_log VALUES ('by the administrator')")
    try:
        # joe may not write the log himself, and the body is now the writer's.
        error = expect_blocked(client=joe, sql="INSERT INTO app.orders VALUES (10, 'tenth')", why="no INSERT privilege")
        assert "order_log" in error.detail
    finally:
        expect_allowed(client=mia, sql=LOG_ORDERS.replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER"))
    expect_allowed(client=joe, sql="INSERT INTO app.orders VALUES (10, 'tenth')")
    expect_allowed(client=mia, sql="DELETE FROM app.orders WHERE id = 10")


# ---- what a body may call ----

def test_a_triggers_body_may_call_only_what_its_creator_may(admin, mia, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("word\nfile-secret-word\n")
    for sql, why in [
        ("CREATE TRIGGER q AFTER INSERT ON app.items FOR EACH STATEMENT "
         "INSERT INTO app.leak SELECT * FROM query('SELECT value FROM main.secrets')", "query()"),
        ("CREATE TRIGGER qt AFTER INSERT ON app.items FOR EACH STATEMENT "
         "INSERT INTO app.leak SELECT * FROM query_table('main.secrets')", "query_table()"),
        (f"CREATE TRIGGER f AFTER INSERT ON app.items FOR EACH STATEMENT "
         f"INSERT INTO app.leak SELECT * FROM read_csv('{secret}')", "READ FILESYSTEM"),
    ]:
        expect_blocked(client=mia, sql=sql, why=why)
    assert triggers_of(admin=admin, table="items") == set()


def test_a_triggers_body_pins_the_functions_it_calls(admin, mia, cal):
    expect_allowed(client=mia, sql="CREATE TRIGGER fmt_orders AFTER INSERT ON app.orders FOR EACH STATEMENT "
                                   "INSERT INTO app.order_log SELECT fmt(1)")
    try:
        # cal's macro app.fmt would be found before main.fmt where the body is bound.
        expect_blocked(client=cal, sql="CREATE MACRO app.fmt(x) AS 'mine'",
                       why="would change what a view, a trigger, a row or masking policy")
        # Twin: another name.
        expect_allowed(client=cal, sql="CREATE MACRO app.fmt_other(x) AS 'mine'")
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER fmt_orders ON app.orders")


# ---- dropping ----

def test_drop_and_replace_need_ownership_or_drop_trigger(admin, mia, joe, cal, server):
    drop = "DROP TRIGGER log_orders ON app.orders"
    expect_blocked(client=joe, sql=drop, why=MAY_NOT_DROP)
    expect_blocked(client=cal, sql=drop, why=MAY_NOT_DROP)
    expect_blocked(client=joe, sql="CREATE OR REPLACE TRIGGER log_orders AFTER INSERT ON app.orders FOR EACH STATEMENT "
                                   "INSERT INTO app.order_log VALUES ('joes')", why=ONLY_OWNER)
    assert triggers_of(admin=admin, table="orders") == {"log_orders"}
    # A role granted DROP TRIGGER on the schema may drop another's trigger; so may the owner.
    dan = server.user(name="dan")
    expect_allowed(client=dan, sql=drop)
    try:
        assert triggers_of(admin=admin, table="orders") == set()
        expect_allowed(client=mia, sql=LOG_ORDERS)
        expect_allowed(client=mia, sql=drop)
        # DROP TRIGGER IF EXISTS of nothing needs nothing.
        expect_allowed(client=joe, sql="DROP TRIGGER IF EXISTS log_orders ON app.orders")
    finally:
        expect_allowed(client=mia, sql=LOG_ORDERS.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS"))


def test_triggers_are_listed_with_their_table(admin, mia, joe, cal, server):
    expect_allowed(client=mia, sql="CREATE TRIGGER log_leak AFTER INSERT ON app.leak FOR EACH STATEMENT "
                                   "INSERT INTO app.order_log VALUES ('leak')")
    try:
        # joe may access app.orders, not app.leak.
        rows = expect_allowed(client=joe, sql="SELECT table_name, trigger_name FROM duckdb_triggers()").to_pylist()
        assert {(r["table_name"], r["trigger_name"]) for r in rows} == {("orders", "log_orders")}
        rows = expect_allowed(client=mia, sql="SELECT table_name, trigger_name FROM duckdb_triggers()").to_pylist()
        assert {(r["table_name"], r["trigger_name"]) for r in rows} == {("orders", "log_orders"), ("leak", "log_leak")}
    finally:
        expect_allowed(client=mia, sql="DROP TRIGGER log_leak ON app.leak")


def test_a_trigger_on_a_temporary_table_is_the_sessions_own(joe):
    expect_allowed(client=joe, sql="CREATE TEMP TABLE scratch (id INT)")
    expect_allowed(client=joe, sql="CREATE TEMP TABLE scratch_log (id INT)")
    expect_allowed(client=joe, sql="CREATE TRIGGER log_scratch AFTER INSERT ON scratch FOR EACH STATEMENT "
                                   "INSERT INTO scratch_log VALUES (1)")
    expect_allowed(client=joe, sql="INSERT INTO scratch VALUES (1)")
    assert values(client=joe, sql="SELECT id FROM scratch_log") == ["1"]
    expect_allowed(client=joe, sql="DROP TRIGGER log_scratch ON scratch")


def test_the_administrator_is_unaffected(admin, mia):
    expect_allowed(client=admin, sql="CREATE TRIGGER admin_orders AFTER INSERT ON app.orders FOR EACH STATEMENT "
                                     "INSERT INTO main.audit SELECT value FROM main.secrets")
    try:
        expect_allowed(client=admin, sql="INSERT INTO app.orders VALUES (11, 'eleventh')")
        assert values(client=admin, sql="SELECT what FROM main.audit WHERE what = 'top-secret-value'") == ["top-secret-value"]
    finally:
        expect_allowed(client=admin, sql="DROP TRIGGER admin_orders ON app.orders")
        expect_allowed(client=admin, sql="DELETE FROM main.audit WHERE what = 'top-secret-value'")
        expect_allowed(client=admin, sql="DELETE FROM app.orders WHERE id = 11")


# ---- chains: a trigger whose body fires another, and a writer's own WITH (issue #52) ----

def make(client, statements: list[str]) -> None:
    for sql in statements:
        expect_allowed(client=client, sql=sql)


def drop_tables(client, names: list[str]) -> None:
    for name in names:
        expect_allowed(client=client, sql=f"DROP TABLE IF EXISTS {name}")


def test_a_chain_of_triggers_reading_transition_tables(admin, mia, joe):
    # raw -> staged -> totals, each step a statement trigger reading the rows of the step
    # before (REFERENCING NEW TABLE): the body of the first trigger is the statement that
    # fires the second. joe may write only the first table; the bodies run as mia.
    make(client=mia, statements=[
        "CREATE TABLE app.raw (id INT)", "CREATE TABLE app.staged (id INT)", "CREATE TABLE app.totals (total INT)",
        "CREATE TRIGGER staged_to_totals AFTER INSERT ON app.staged REFERENCING NEW TABLE AS fresh "
        "FOR EACH STATEMENT INSERT INTO app.totals SELECT sum(id) FROM fresh",
        "CREATE TRIGGER raw_to_staged AFTER INSERT ON app.raw REFERENCING NEW TABLE AS fresh "
        "FOR EACH STATEMENT INSERT INTO app.staged SELECT id * 10 FROM fresh",
        "GRANT INSERT ON app.raw TO writers", "GRANT SELECT ON app.staged TO writers", "GRANT SELECT ON app.totals TO writers",
    ])
    try:
        expect_blocked(client=joe, sql="INSERT INTO app.staged VALUES (0)", why="no INSERT privilege")
        expect_allowed(client=joe, sql="INSERT INTO app.raw VALUES (1), (2)")
        assert sorted(values(client=joe, sql="SELECT id FROM app.staged")) == ["10", "20"]
        assert values(client=joe, sql="SELECT total FROM app.totals") == ["30"]
        expect_allowed(client=admin, sql="INSERT INTO app.raw VALUES (3)")
        assert sorted(values(client=joe, sql="SELECT total FROM app.totals")) == ["30", "30"]
        # The second trigger's body still gets only its owner's rights, however deep it sits.
        expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER staged_to_totals AFTER INSERT ON app.staged "
                                       "REFERENCING NEW TABLE AS fresh FOR EACH STATEMENT "
                                       "INSERT INTO app.leak SELECT value FROM main.secrets, fresh")
        for client in [admin, joe]:
            error = expect_blocked(client=client, sql="INSERT INTO app.raw VALUES (4)", why=CANNOT_RUN)
            assert "trigger staged_to_totals on table" in error.detail
            assert "no SELECT privilege on table" in error.detail and "secrets" in error.detail
        assert values(client=mia, sql="SELECT count(*) FROM app.leak") == ["0"]
        assert values(client=mia, sql="SELECT count(*) FROM app.raw") == ["3"]
    finally:
        drop_tables(client=mia, names=["app.raw", "app.staged", "app.totals"])


def test_a_statement_trigger_whose_body_fires_a_row_trigger(mia, joe):
    make(client=mia, statements=[
        "CREATE TABLE app.batches (id INT)", "CREATE TABLE app.lines (id INT)", "CREATE TABLE app.line_log (id INT)",
        "CREATE TRIGGER log_lines AFTER INSERT ON app.lines FOR EACH ROW INSERT INTO app.line_log VALUES (NEW.id + 1)",
        "CREATE TRIGGER batch_lines AFTER INSERT ON app.batches REFERENCING NEW TABLE AS fresh "
        "FOR EACH STATEMENT INSERT INTO app.lines SELECT id FROM fresh",
        "GRANT INSERT ON app.batches TO writers", "GRANT SELECT ON app.line_log TO writers",
    ])
    try:
        expect_allowed(client=joe, sql="INSERT INTO app.batches VALUES (1), (2)")
        assert sorted(values(client=joe, sql="SELECT id FROM app.line_log")) == ["2", "3"]
    finally:
        drop_tables(client=mia, names=["app.batches", "app.lines", "app.line_log"])


def test_a_row_trigger_whose_body_fires_a_statement_trigger(admin, mia, joe):
    # The row trigger's body writes a table with a statement trigger of its own: DuckDB binds
    # that trigger's chain where the body's INSERT would be.
    make(client=mia, statements=[
        "CREATE TABLE app.parts (id INT)", "CREATE TABLE app.part_copies (id INT)", "CREATE TABLE app.copy_log (what VARCHAR)",
        "CREATE TRIGGER log_copies AFTER INSERT ON app.part_copies FOR EACH STATEMENT INSERT INTO app.copy_log VALUES ('copied')",
        "CREATE TRIGGER copy_parts AFTER INSERT ON app.parts FOR EACH ROW INSERT INTO app.part_copies VALUES (NEW.id)",
        "GRANT INSERT ON app.parts TO writers", "GRANT SELECT ON app.part_copies TO writers",
        "GRANT SELECT ON app.copy_log TO writers",
    ])
    try:
        expect_blocked(client=joe, sql="INSERT INTO app.part_copies VALUES (0)", why="no INSERT privilege")
        expect_allowed(client=joe, sql="INSERT INTO app.parts VALUES (7), (8)")
        assert sorted(values(client=joe, sql="SELECT id FROM app.part_copies")) == ["7", "8"]
        assert values(client=joe, sql="SELECT DISTINCT what FROM app.copy_log") == ["copied"]
        # An administrator's trigger on the table the body writes runs with the rights of who
        # wrote that table: the row trigger's owner, mia, who may not write main.audit.
        expect_allowed(client=admin, sql="CREATE TRIGGER audit_copies AFTER INSERT ON app.part_copies FOR EACH STATEMENT "
                                         "INSERT INTO main.audit VALUES ('part copied')")
        error = expect_blocked(client=joe, sql="INSERT INTO app.parts VALUES (9)", why=CANNOT_RUN)
        assert "trigger copy_parts on table" in error.detail
        assert "no INSERT privilege on table" in error.detail and "audit" in error.detail
        assert sorted(values(client=joe, sql="SELECT id FROM app.part_copies")) == ["7", "8"]
        # Twin: once mia may write it, the cascade runs.
        expect_allowed(client=admin, sql="GRANT INSERT ON main.audit TO trigger_makers")
        try:
            expect_allowed(client=joe, sql="INSERT INTO app.parts VALUES (9)")
            assert values(client=admin, sql="SELECT what FROM main.audit WHERE what = 'part copied'") == ["part copied"]
        finally:
            expect_allowed(client=admin, sql="REVOKE INSERT ON main.audit FROM trigger_makers")
            expect_allowed(client=admin, sql="DELETE FROM main.audit WHERE what = 'part copied'")
    finally:
        drop_tables(client=mia, names=["app.parts", "app.part_copies", "app.copy_log"])


def test_the_writers_own_with_on_a_table_with_a_trigger(server, mia):
    # ike holds INSERT on app.orders and nothing else.
    ike = server.user(name="ike")
    before = logged(client=mia)
    try:
        expect_allowed(client=ike, sql="WITH fresh AS (SELECT 14 AS id, 'fourteenth' AS note) "
                                       "INSERT INTO app.orders SELECT id, note FROM fresh")
        assert logged(client=mia) == before + 1
        assert values(client=mia, sql="SELECT note FROM app.orders WHERE id = 14") == ["fourteenth"]
        # A RETURNING of the writer's own hands the rows back, WITH or not: SELECT, as always.
        expect_blocked(client=ike, sql="WITH fresh AS (SELECT 15 AS id, 'fifteenth' AS note) "
                                       "INSERT INTO app.orders SELECT id, note FROM fresh RETURNING *",
                       why="no SELECT privilege")
        assert values(client=mia, sql="SELECT count(*) FROM app.orders WHERE id = 15") == ["0"]
    finally:
        expect_allowed(client=mia, sql="DELETE FROM app.orders WHERE id IN (14, 15)")


def test_a_cte_named_like_a_triggers_own_gets_no_owner_rights(mia, joe):
    # DuckDB names the CTEs it binds triggers into __duckdb_trigger_base_<uuid> and
    # __duckdb_trigger_body_<n>_<uuid>. A writer who names CTEs that way must not have a
    # statement of their own taken for the body of mia's trigger (which would run it with
    # mia's rights): joe may write app.orders, and not app.order_log.
    before = values(client=mia, sql="SELECT count(*) FROM app.order_log")
    orders = values(client=mia, sql="SELECT count(*) FROM app.orders")
    for sql in [
        'WITH "__duckdb_trigger_base_x" AS (INSERT INTO app.orders VALUES (16, \'s\') RETURNING *), '
        '"__duckdb_trigger_body_1_x" AS (INSERT INTO app.order_log VALUES (\'forged\') RETURNING *) '
        'SELECT count(*) FROM "__duckdb_trigger_base_x"',
        'WITH "__duckdb_trigger_base_x" AS (INSERT INTO app.items VALUES (16) RETURNING *), '
        '"__duckdb_trigger_body_1_x" AS (INSERT INTO app.order_log VALUES (\'forged\') RETURNING *) '
        'SELECT count(*) FROM "__duckdb_trigger_base_x"',
        'WITH "__duckdb_trigger_body_1_x" AS (INSERT INTO app.order_log VALUES (\'forged\') RETURNING *) SELECT 1',
    ]:
        expect_blocked(client=joe, sql=sql)
    assert values(client=mia, sql="SELECT count(*) FROM app.order_log") == before
    assert values(client=mia, sql="SELECT count(*) FROM app.orders") == orders
    assert values(client=mia, sql="SELECT count(*) FROM app.items") == ["0"]


# ---- INSERT ... ON CONFLICT on a table with a trigger (issue #53) ----

def test_on_conflict_fires_the_tables_triggers(admin, mia, joe, server):
    # DuckDB runs INSERT ... ON CONFLICT as a merge and fires the table's INSERT triggers.
    make(client=mia, statements=[
        "CREATE TABLE app.keyed (id INT PRIMARY KEY, note VARCHAR)", "CREATE TABLE app.keyed_log (what VARCHAR)",
        "CREATE TRIGGER log_keyed AFTER INSERT ON app.keyed FOR EACH STATEMENT INSERT INTO app.keyed_log VALUES ('fired')",
        "GRANT INSERT, SELECT ON app.keyed TO writers", "GRANT INSERT ON app.keyed TO inserters",
        "GRANT SELECT ON app.keyed_log TO writers",
    ])

    def fired() -> int:
        return int(values(client=mia, sql="SELECT count(*) FROM app.keyed_log")[0])

    def notes() -> list:
        return values(client=mia, sql="SELECT note FROM app.keyed ORDER BY id")

    try:
        expect_blocked(client=joe, sql="INSERT INTO app.keyed_log VALUES ('joes')", why="no INSERT privilege")
        expect_allowed(client=joe, sql="INSERT INTO app.keyed VALUES (1, 'first') ON CONFLICT DO NOTHING")
        expect_allowed(client=joe, sql="INSERT INTO app.keyed VALUES (1, 'again') ON CONFLICT DO NOTHING")
        expect_allowed(client=joe, sql="INSERT OR IGNORE INTO app.keyed VALUES (1, 'ignored'), (2, 'second')")
        assert notes() == ["first", "second"] and fired() == 3
        expect_allowed(client=admin, sql="INSERT INTO app.keyed VALUES (2, 'admin') ON CONFLICT (id) DO NOTHING")
        assert notes() == ["first", "second"] and fired() == 4
        # DO UPDATE and OR REPLACE update: UPDATE as well as INSERT.
        for sql in ["INSERT INTO app.keyed VALUES (1, 'updated') ON CONFLICT (id) DO UPDATE SET note = excluded.note",
                    "INSERT OR REPLACE INTO app.keyed VALUES (1, 'replaced')"]:
            expect_blocked(client=joe, sql=sql, why="no UPDATE privilege")
        assert notes() == ["first", "second"] and fired() == 4
        expect_allowed(client=mia, sql="GRANT UPDATE ON app.keyed TO writers")
        expect_allowed(client=joe, sql="INSERT INTO app.keyed VALUES (1, 'updated') ON CONFLICT (id) DO UPDATE "
                                       "SET note = excluded.note")
        assert notes() == ["updated", "second"] and fired() == 5
        expect_allowed(client=joe, sql="INSERT OR REPLACE INTO app.keyed VALUES (1, 'replaced'), (3, 'third')")
        assert notes() == ["replaced", "second", "third"] and fired() == 6
        # The writer's own RETURNING hands rows back (joe may read them).
        assert values(client=joe, sql="INSERT INTO app.keyed VALUES (4, 'fourth') ON CONFLICT DO NOTHING "
                                      "RETURNING note") == ["fourth"]
        # ike holds INSERT alone. Telling a conflict reads the key, so it takes SELECT on the
        # key column (as Postgres asks for a conflict target), and nothing more: not on the
        # rows the binder returns to the triggers.
        ike = server.user(name="ike")
        expect_blocked(client=ike, sql="INSERT INTO app.keyed VALUES (5, 'fifth') ON CONFLICT DO NOTHING",
                       why="no SELECT privilege")
        expect_allowed(client=mia, sql="GRANT SELECT (id) ON app.keyed TO inserters")
        expect_allowed(client=ike, sql="INSERT INTO app.keyed VALUES (5, 'fifth') ON CONFLICT DO NOTHING")
        expect_blocked(client=ike, sql="INSERT INTO app.keyed VALUES (6, 'sixth') ON CONFLICT DO NOTHING RETURNING *",
                       why="no SELECT on column(s) note")
        assert notes() == ["replaced", "second", "third", "fourth", "fifth"]
        # The body still runs with its owner's rights, and no more.
        expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER log_keyed AFTER INSERT ON app.keyed FOR EACH STATEMENT "
                                       "INSERT INTO app.leak SELECT value FROM main.secrets")
        for client in [admin, joe]:
            error = expect_blocked(client=client, sql="INSERT INTO app.keyed VALUES (7, 'seventh') ON CONFLICT DO NOTHING",
                                   why=CANNOT_RUN)
            assert "trigger log_keyed on table" in error.detail
        assert values(client=mia, sql="SELECT count(*) FROM app.leak") == ["0"]
        assert values(client=mia, sql="SELECT count(*) FROM app.keyed WHERE id = 7") == ["0"]
    finally:
        drop_tables(client=mia, names=["app.keyed", "app.keyed_log"])


def test_merge_into_a_table_with_triggers_is_duckdbs_to_refuse(admin, mia):
    # DuckDB 2.0 has no MERGE INTO on a table with triggers; the error is its own, not a
    # refusal of access control's.
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="MERGE INTO app.orders USING (SELECT 99 AS id) s ON orders.id = s.id "
                      "WHEN NOT MATCHED THEN INSERT VALUES (s.id, 'merged')")
    assert "MERGE INTO is not supported on tables with triggers" in raised.value.detail
    assert raised.value.code != "permission_denied"


# ---- UPDATE and DELETE on a table with triggers: what the writer must be able to read ----
#
# DuckDB adds RETURNING * to a write that fires triggers, to hand them their rows; the scan
# that locates the rows then reads every column of the table. Those columns are carried to
# the triggers, not read by the writer: a writer needs SELECT on what its own statement
# reads (WHERE, a SET expression, a join, a subquery, its own RETURNING), as on a table
# without triggers. Somebody answers for the rows where a trigger's body reads them (a
# transition table, OLD and NEW): the administrator who made the trigger (it vouches: nothing
# is asked of the writer), a user's trigger's owner (who needs SELECT), or else the writer.

NO_SELECT = "no SELECT privilege on table"
NO_SELECT_ON = "no SELECT on column(s) "
# Why a writer without SELECT is refused where a trigger's body reads the rows:
OWNER_READS = "the trigger's body reads the rows the statement changes"  # a user's: its owner lacks SELECT too
NOBODY_MADE_IT = "nobody is on record as having made it under access control"  # no owner record, or a stale one


def count(client, table: str, where: str = "true") -> int:
    return int(values(client=client, sql=f"SELECT count(*) FROM {table} WHERE {where}")[0])


def columns_missing(error: ChargewireError) -> set[str]:
    """The columns a "no SELECT on column(s) a, b of c.s.t" refusal names."""
    assert NO_SELECT_ON in error.detail, error.detail
    return set(error.detail.split(sep=NO_SELECT_ON)[1].split(sep=" of ")[0].split(sep=", "))


def test_a_write_that_reads_nothing_needs_no_select_on_a_table_with_triggers(admin, wes):
    # Every kind of statement trigger whose body reads no row: BEFORE, AFTER, UPDATE OF, and
    # ones that declare transition tables without reading them.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_t (id INT, x INT)", "INSERT INTO main.dr_t VALUES (1, 1), (2, 2)",
        "CREATE TABLE main.dr_plain (id INT, x INT)", "INSERT INTO main.dr_plain VALUES (1, 1), (2, 2)",
        "CREATE TABLE main.dr_log (what VARCHAR)",
        "CREATE TRIGGER dr_before BEFORE UPDATE ON main.dr_t FOR EACH STATEMENT INSERT INTO main.dr_log VALUES ('before')",
        "CREATE TRIGGER dr_upd AFTER UPDATE ON main.dr_t REFERENCING OLD TABLE AS was NEW TABLE AS now "
        "FOR EACH STATEMENT INSERT INTO main.dr_log VALUES ('updated')",
        "CREATE TRIGGER dr_of AFTER UPDATE OF x ON main.dr_t FOR EACH STATEMENT INSERT INTO main.dr_log VALUES ('x updated')",
        "CREATE TRIGGER dr_del AFTER DELETE ON main.dr_t REFERENCING OLD TABLE AS gone "
        "FOR EACH STATEMENT INSERT INTO main.dr_log VALUES ('deleted')",
        "GRANT UPDATE, DELETE ON main.dr_t TO changers", "GRANT UPDATE, DELETE ON main.dr_plain TO changers",
        "GRANT INSERT ON main.dr_log TO changers",
    ])
    try:
        for table in ["main.dr_t", "main.dr_plain"]:
            expect_blocked(client=wes, sql=f"SELECT x FROM {table}", why=NO_SELECT)
        # Without a trigger an unfiltered write needs no SELECT (as in Postgres): as ever.
        assert values(client=wes, sql="UPDATE main.dr_plain SET x = 0") == ["2"]
        assert values(client=wes, sql="DELETE FROM main.dr_plain") == ["2"]
        # With triggers it was refused with "no SELECT privilege on table": the same now.
        assert values(client=wes, sql="UPDATE main.dr_t SET x = 0") == ["2"]
        assert values(client=admin, sql="SELECT x FROM main.dr_t") == ["0", "0"]
        assert [count(client=admin, table="main.dr_log", where=f"what = '{what}'")
                for what in ["before", "updated", "x updated"]] == [1, 1, 1]
        # UPDATE OF x does not fire for id; a SET to the column's own value reads nothing.
        assert values(client=wes, sql="UPDATE main.dr_t SET id = 7") == ["2"]
        assert values(client=wes, sql="UPDATE main.dr_t SET id = id") == ["2"]
        assert [count(client=admin, table="main.dr_log", where=f"what = '{what}'")
                for what in ["before", "updated", "x updated"]] == [3, 3, 1]
        assert values(client=wes, sql="DELETE FROM main.dr_t") == ["2"]
        assert count(client=admin, table="main.dr_t") == 0
        assert count(client=admin, table="main.dr_log", where="what = 'deleted'") == 1
        # The triggers run with the writer's rights (an administrator made them): twin.
        expect_allowed(client=admin, sql="REVOKE INSERT ON main.dr_log FROM changers")
        expect_blocked(client=wes, sql="DELETE FROM main.dr_t", why="no INSERT privilege on table")
        # And the write still needs its own privilege.
        expect_allowed(client=admin, sql="REVOKE DELETE ON main.dr_t FROM changers")
        expect_blocked(client=wes, sql="DELETE FROM main.dr_t", why="no DELETE privilege on table")
    finally:
        drop_tables(client=admin, names=["main.dr_t", "main.dr_plain", "main.dr_log"])


# Statements on the table {t} (id, x, y, z, w) that read it, and the columns they read.
READS = [
    ("UPDATE {t} SET z = 0 WHERE x = 1", {"x"}),
    ("UPDATE {t} SET z = id", {"id"}),
    ("UPDATE {t} SET x = x + 1", {"x"}),
    ("UPDATE {t} SET z = o.v FROM main.dr_other o WHERE {n}.id = o.id", {"id"}),
    ("UPDATE {t} SET z = 0 WHERE id IN (SELECT id FROM {t} WHERE y > 1000)", {"id", "y"}),
    # Correlated subqueries: DuckDB keeps the outer rows in CTEs of its own for these.
    ("UPDATE {t} SET z = (SELECT max(o.v) FROM main.dr_other o WHERE o.v > {n}.y)", {"y"}),
    ("UPDATE {t} SET z = 0 WHERE EXISTS (SELECT 1 FROM main.dr_other o WHERE o.id = {n}.id "
     "AND EXISTS (SELECT 1 FROM main.dr_other p WHERE p.v > {n}.y))", {"id", "y"}),
    ("DELETE FROM {t} WHERE x = -1", {"x"}),
    ("DELETE FROM {t} USING main.dr_other o WHERE {n}.id = o.id AND o.v < 0", {"id"}),
    ("DELETE FROM {t} WHERE id IN (SELECT id FROM {t} WHERE y > 1000)", {"id", "y"}),
    ("DELETE FROM {t} WHERE (SELECT count(*) FROM main.dr_other o WHERE o.v > {n}.y) > 100", {"y"}),
]


@pytest.mark.parametrize("triggers", [True, False], ids=["with-triggers", "without-triggers"])
def test_a_write_needs_select_on_the_columns_it_reads_and_no_others(admin, wes, triggers):
    # The same statements on a table with triggers and on one without: each needs SELECT on
    # exactly the columns it reads itself, trigger or not.
    name = "dr_trig" if triggers else "dr_bare"
    table = f"main.{name}"
    make(client=admin, statements=[
        f"CREATE TABLE {table} (id INT, x INT, y INT, z INT, w INT)",
        f"INSERT INTO {table} VALUES (1, 1, 10, 100, 0), (2, 2, 20, 200, 0)",
        "CREATE TABLE IF NOT EXISTS main.dr_other (id INT, v INT)", "CREATE TABLE IF NOT EXISTS main.dr_rlog (what VARCHAR)",
        f"GRANT UPDATE, DELETE ON {table} TO changers", "GRANT SELECT ON main.dr_other TO changers",
        "GRANT INSERT ON main.dr_rlog TO changers",
    ])
    if triggers:
        make(client=admin, statements=[
            f"CREATE TRIGGER {name}_upd AFTER UPDATE ON {table} FOR EACH STATEMENT INSERT INTO main.dr_rlog VALUES ('{name}')",
            f"CREATE TRIGGER {name}_del AFTER DELETE ON {table} FOR EACH STATEMENT INSERT INTO main.dr_rlog VALUES ('{name}')",
        ])
    try:
        for statement, reads in READS:
            sql = statement.format(t=table, n=name)
            before = count(client=admin, table="main.dr_rlog", where=f"what = '{name}'")
            # No SELECT at all on the table.
            expect_blocked(client=wes, sql=sql, why=NO_SELECT)
            # SELECT on a column the statement does not read opens nothing ...
            expect_allowed(client=admin, sql=f"GRANT SELECT (w) ON {table} TO changers")
            # (a refusal names what the first scan it stops at lacks: the statement may have two)
            missing = columns_missing(error=expect_blocked(client=wes, sql=sql, why=NO_SELECT_ON))
            assert missing and missing <= reads, sql
            # ... and each column it reads is needed.
            for column in sorted(reads):
                others = sorted(reads - {column})
                if others:
                    expect_allowed(client=admin, sql=f"GRANT SELECT ({', '.join(others)}) ON {table} TO changers")
                assert columns_missing(error=expect_blocked(client=wes, sql=sql, why=NO_SELECT_ON)) == {column}, sql
                if others:
                    expect_allowed(client=admin, sql=f"REVOKE SELECT ({', '.join(others)}) ON {table} FROM changers")
            assert count(client=admin, table="main.dr_rlog", where=f"what = '{name}'") == before
            # Twin: SELECT on just those columns, and it runs (and fires the trigger).
            expect_allowed(client=admin, sql=f"GRANT SELECT ({', '.join(sorted(reads))}) ON {table} TO changers")
            expect_allowed(client=wes, sql=sql)
            assert count(client=admin, table="main.dr_rlog", where=f"what = '{name}'") == before + (1 if triggers else 0)
            # Reading the table is still refused for the other columns.
            assert "z" in columns_missing(error=expect_blocked(client=wes, sql=f"SELECT * FROM {table}", why=NO_SELECT_ON))
            expect_allowed(client=admin, sql=f"REVOKE SELECT ({', '.join(sorted(reads | {'w'}))}) ON {table} FROM changers")
        # A RETURNING of the writer's own hands rows back: SELECT on every column of the
        # table, whatever it lists, with a trigger or without.
        returning = f"UPDATE {table} SET z = 0 RETURNING x"
        expect_blocked(client=wes, sql=returning, why=NO_SELECT)
        expect_allowed(client=admin, sql=f"GRANT SELECT (x) ON {table} TO changers")
        assert columns_missing(error=expect_blocked(client=wes, sql=returning, why=NO_SELECT_ON)) == {"id", "y", "z", "w"}
        expect_allowed(client=admin, sql=f"GRANT SELECT ON {table} TO changers")
        assert len(values(client=wes, sql=returning)) == 2
    finally:
        drop_tables(client=admin, names=[table])


def test_a_users_trigger_may_hand_on_the_rows_its_owner_may_read(admin, mia, wes):
    # mia owns the tables and the triggers; their bodies copy the rows a write changes
    # (transition tables, OLD) into a table of hers that she lets the writers read. wes
    # may not read the tables he writes: what he gets to see is what mia, who may, chose.
    make(client=mia, statements=[
        "CREATE TABLE app.dr_src (id INT, note VARCHAR)", "INSERT INTO app.dr_src VALUES (1, 'one'), (2, 'two')",
        "CREATE TABLE app.dr_rows (id INT, note VARCHAR)", "INSERT INTO app.dr_rows VALUES (3, 'three')",
        "CREATE TABLE app.dr_copy (id INT, note VARCHAR)",
        "CREATE TABLE app.dr_quiet (id INT, note VARCHAR)", "INSERT INTO app.dr_quiet VALUES (6, 'six')",
        "CREATE TRIGGER dr_quiet_upd AFTER UPDATE ON app.dr_quiet FOR EACH STATEMENT INSERT INTO app.dr_copy VALUES (0, 'quiet')",
        "CREATE TRIGGER dr_src_upd AFTER UPDATE ON app.dr_src REFERENCING OLD TABLE AS was NEW TABLE AS now "
        "FOR EACH STATEMENT INSERT INTO app.dr_copy SELECT now.id, was.note || '>' || now.note FROM was JOIN now USING (id)",
        "CREATE TRIGGER dr_src_del AFTER DELETE ON app.dr_src REFERENCING OLD TABLE AS gone "
        "FOR EACH STATEMENT INSERT INTO app.dr_copy SELECT id, 'gone ' || note FROM gone",
        "CREATE TRIGGER dr_rows_del AFTER DELETE ON app.dr_rows FOR EACH ROW "
        "INSERT INTO app.dr_copy VALUES (OLD.id, 'row ' || OLD.note)",
        "GRANT UPDATE, DELETE ON app.dr_src TO changers", "GRANT DELETE ON app.dr_rows TO changers",
        "GRANT UPDATE ON app.dr_quiet TO changers", "GRANT SELECT ON app.dr_copy TO changers",
    ])
    try:
        for table in ["app.dr_src", "app.dr_rows", "app.dr_quiet"]:
            expect_blocked(client=wes, sql=f"SELECT note FROM {table}", why=NO_SELECT)
        expect_blocked(client=wes, sql="INSERT INTO app.dr_copy VALUES (0, 'wes')", why="no INSERT privilege")
        # A trigger that reads no row.
        assert values(client=wes, sql="UPDATE app.dr_quiet SET note = 'new'") == ["1"]
        assert values(client=wes, sql="SELECT note FROM app.dr_copy") == ["quiet"]
        expect_allowed(client=mia, sql="DELETE FROM app.dr_copy")
        # Triggers that read the rows.
        assert values(client=wes, sql="UPDATE app.dr_src SET note = 'new'") == ["2"]
        assert sorted(values(client=wes, sql="SELECT note FROM app.dr_copy")) == ["one>new", "two>new"]
        assert values(client=wes, sql="DELETE FROM app.dr_src") == ["2"]
        assert values(client=wes, sql="DELETE FROM app.dr_rows") == ["1"]
        assert sorted(values(client=wes, sql="SELECT note FROM app.dr_copy WHERE note NOT LIKE '%>%'")) == [
            "gone new", "gone new", "row three"]
        # What the statement itself reads still takes the writer's own SELECT.
        expect_allowed(client=mia, sql="INSERT INTO app.dr_src VALUES (4, 'four')")
        expect_blocked(client=wes, sql="DELETE FROM app.dr_src WHERE note = 'four'", why=NO_SELECT)
        expect_blocked(client=wes, sql="UPDATE app.dr_src SET note = note || '!'", why=NO_SELECT)
        expect_allowed(client=mia, sql="GRANT SELECT (id) ON app.dr_src TO changers")
        assert columns_missing(error=expect_blocked(client=wes, sql="DELETE FROM app.dr_src WHERE note = 'four'",
                                                    why=NO_SELECT_ON)) == {"note"}
        assert values(client=wes, sql="DELETE FROM app.dr_src WHERE id = 4") == ["1"]
        # An administrator's write fires them as before.
        expect_allowed(client=mia, sql="INSERT INTO app.dr_rows VALUES (5, 'five')")
        expect_allowed(client=admin, sql="DELETE FROM app.dr_rows")
        assert count(client=mia, table="app.dr_copy", where="note = 'row five'") == 1
    finally:
        drop_tables(client=mia, names=["app.dr_src", "app.dr_rows", "app.dr_quiet", "app.dr_copy"])


def test_an_administrators_trigger_vouches_for_the_rows_its_body_reads(admin, wes):
    # An administrator who puts a row-copying trigger on a table chooses who can see those
    # rows: the writer is not asked for SELECT because the body reads the rows of the
    # statement that fires it (a transition table, OLD). Here the body puts them in a table
    # the writer can read, and that is the administrator's stated choice.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_adm (id INT, note VARCHAR)", "INSERT INTO main.dr_adm VALUES (1, 'pin-1234'), (2, 'pin-5678')",
        "CREATE TABLE main.dr_seen (note VARCHAR)", "CREATE TABLE main.dr_num (n INT)",
        "GRANT UPDATE, DELETE ON main.dr_adm TO changers", "GRANT INSERT, SELECT ON main.dr_seen TO changers",
        "GRANT INSERT, SELECT ON main.dr_num TO changers",
    ])
    readers = [
        ("DELETE FROM main.dr_adm",
         "AFTER DELETE ON main.dr_adm REFERENCING OLD TABLE AS gone FOR EACH STATEMENT "
         "INSERT INTO main.dr_seen SELECT note FROM gone"),
        ("DELETE FROM main.dr_adm",
         "AFTER DELETE ON main.dr_adm FOR EACH ROW INSERT INTO main.dr_seen VALUES (OLD.note)"),
        ("UPDATE main.dr_adm SET id = 7",
         "AFTER UPDATE ON main.dr_adm REFERENCING OLD TABLE AS was FOR EACH STATEMENT "
         "INSERT INTO main.dr_seen SELECT note FROM was"),
        ("UPDATE main.dr_adm SET id = 0",
         "AFTER UPDATE ON main.dr_adm REFERENCING NEW TABLE AS now FOR EACH STATEMENT "
         "INSERT INTO main.dr_seen SELECT note FROM now"),
    ]
    try:
        for sql, trigger in readers:
            expect_allowed(client=admin, sql=f"CREATE OR REPLACE TRIGGER dr_adm_reads {trigger}")
            expect_blocked(client=wes, sql="SELECT note FROM main.dr_adm", why=NO_SELECT)
            assert values(client=wes, sql=sql) == ["2"], trigger
            assert sorted(values(client=wes, sql="SELECT note FROM main.dr_seen")) == ["pin-1234", "pin-5678"]
            # Only those reads are vouched for. The statement's own still take the writer's SELECT ...
            expect_allowed(client=admin, sql="DELETE FROM main.dr_seen")
            expect_allowed(client=admin, sql="DROP TRIGGER dr_adm_reads ON main.dr_adm")
            expect_allowed(client=admin, sql="DELETE FROM main.dr_adm")
            expect_allowed(client=admin, sql="INSERT INTO main.dr_adm VALUES (1, 'pin-1234'), (2, 'pin-5678')")
            expect_allowed(client=admin, sql=f"CREATE OR REPLACE TRIGGER dr_adm_reads {trigger}")
            reading = sql + (" WHERE note = 'pin-1234'" if "WHERE" not in sql else "")
            expect_blocked(client=wes, sql=reading, why=NO_SELECT)
            if "FOR EACH ROW" not in trigger:  # (DuckDB has no RETURNING beside row triggers)
                expect_blocked(client=wes, sql=sql + " RETURNING note", why=NO_SELECT)
            # ... and what else the body does takes the writer's rights, as ever.
            expect_allowed(client=admin, sql="REVOKE INSERT ON main.dr_seen FROM changers")
            error = expect_blocked(client=wes, sql=sql, why="no INSERT privilege on table")
            assert "dr_seen" in error.detail
            expect_allowed(client=admin, sql="GRANT INSERT ON main.dr_seen TO changers")
            assert count(client=admin, table="main.dr_seen") == 0 and count(client=admin, table="main.dr_adm") == 2
        # A body that reads another table beside the rows: that read is the writer's.
        expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER dr_adm_reads AFTER DELETE ON main.dr_adm "
                                         "REFERENCING OLD TABLE AS gone FOR EACH STATEMENT "
                                         "INSERT INTO main.dr_seen SELECT s.value || gone.note FROM main.secrets s, gone")
        error = expect_blocked(client=wes, sql="DELETE FROM main.dr_adm", why=NO_SELECT)
        assert "secrets" in error.detail and "dr_adm" not in error.detail.split(sep="Access control")[0]
        # A body that tells of the rows by failing is the administrator's choice as well: the
        # error is DuckDB's, and carries a value.
        expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER dr_adm_reads AFTER DELETE ON main.dr_adm "
                                         "REFERENCING OLD TABLE AS gone FOR EACH STATEMENT "
                                         "INSERT INTO main.dr_num SELECT CAST(note AS INT) FROM gone")
        with pytest.raises(ChargewireError) as raised:
            wes.run(sql="DELETE FROM main.dr_adm")
        assert raised.value.code != "permission_denied" and "pin-" in raised.value.detail
        assert count(client=admin, table="main.dr_adm") == 2
    finally:
        drop_tables(client=admin, names=["main.dr_adm", "main.dr_seen", "main.dr_num"])


def test_a_users_trigger_reads_the_rows_with_the_writers_select_or_its_owners(admin, mia, wes, server):
    # The same body as an administrator's above, in a user's trigger: mia's trigger reads the
    # rows of a table that is no longer hers and that she may not read. The writer's SELECT
    # covers the rows (as it always did), or the owner's, or nobody's does.
    make(client=admin, statements=[
        "CREATE SCHEMA dr_ops", "CREATE ROLE dr_owners", "CREATE ROLE dr_readers",
        "GRANT CREATE TABLE, CREATE TRIGGER ON SCHEMA dr_ops TO trigger_makers",
    ])
    try:
        make(client=mia, statements=[
            "CREATE TABLE dr_ops.src (id INT, note VARCHAR)", "INSERT INTO dr_ops.src VALUES (1, 'one')",
            "CREATE TABLE dr_ops.copy (note VARCHAR)",
            "CREATE TRIGGER src_del AFTER DELETE ON dr_ops.src REFERENCING OLD TABLE AS gone "
            "FOR EACH STATEMENT INSERT INTO dr_ops.copy SELECT note FROM gone",
        ])
        make(client=admin, statements=[
            "ALTER TABLE dr_ops.src OWNER TO ROLE dr_owners", "GRANT DELETE ON dr_ops.src TO changers",
            "GRANT SELECT ON dr_ops.src TO dr_readers",
        ])
        expect_blocked(client=mia, sql="SELECT note FROM dr_ops.src", why=NO_SELECT)
        error = expect_blocked(client=wes, sql="DELETE FROM dr_ops.src", why="cannot run, because its owner user mia")
        assert "trigger src_del on table" in error.detail and NO_SELECT in error.detail and OWNER_READS in error.detail
        assert count(client=admin, table="dr_ops.src") == 1 and count(client=admin, table="dr_ops.copy") == 0
        # Twin: the writer may read the rows. A user granted SELECT ...
        expect_allowed(client=admin, sql="GRANT dr_readers TO wes")
        expect_allowed(client=server.user(name="wes"), sql="DELETE FROM dr_ops.src")
        assert values(client=admin, sql="SELECT note FROM dr_ops.copy") == ["one"]
        expect_allowed(client=admin, sql="REVOKE dr_readers FROM wes")
        # ... and an administrator, as before.
        expect_allowed(client=admin, sql="INSERT INTO dr_ops.src VALUES (0, 'zero')")
        expect_allowed(client=admin, sql="DELETE FROM dr_ops.src")
        assert sorted(values(client=admin, sql="SELECT note FROM dr_ops.copy")) == ["one", "zero"]
        # Twin: the trigger's owner may.
        expect_allowed(client=admin, sql="INSERT INTO dr_ops.src VALUES (2, 'two')")
        expect_blocked(client=server.user(name="wes"), sql="DELETE FROM dr_ops.src", why="cannot run, because its owner user mia")
        expect_allowed(client=admin, sql="GRANT dr_readers TO mia")
        expect_allowed(client=server.user(name="wes"), sql="DELETE FROM dr_ops.src")
        assert sorted(values(client=admin, sql="SELECT note FROM dr_ops.copy")) == ["one", "two", "zero"]
        expect_allowed(client=admin, sql="REVOKE dr_readers FROM mia")
    finally:
        for sql in ["DROP TABLE IF EXISTS dr_ops.src", "DROP TABLE IF EXISTS dr_ops.copy",
                    "REVOKE CREATE TABLE, CREATE TRIGGER ON SCHEMA dr_ops FROM trigger_makers",
                    "DROP SCHEMA IF EXISTS dr_ops", "DROP ROLE IF EXISTS dr_owners", "DROP ROLE IF EXISTS dr_readers"]:
            expect_allowed(client=admin, sql=sql)


COPIES_ROWS = ("AFTER DELETE ON {table} REFERENCING OLD TABLE AS gone FOR EACH STATEMENT "
               "INSERT INTO {sink} SELECT note FROM gone")


def test_only_a_trigger_an_administrator_made_vouches(admin, mia, wes, server):
    # Every way a user's trigger might come to count as an administrator's: replacing the
    # administrator's, dropping it and making it again under its name, word for word, renaming
    # the table, a temporary trigger. Each is refused or leaves a trigger of mia's, which her
    # rights answer for, not an administrator's word. mia owns the table while she makes her trigger (only its owner
    # may), and loses it afterwards, so that her rights can be told from a voucher.
    make(client=admin, statements=[
        "CREATE SCHEMA dr_tk", "CREATE ROLE dr_tk_owners",
        "GRANT CREATE TABLE, CREATE TRIGGER, DROP TRIGGER ON SCHEMA dr_tk TO trigger_makers",
    ])
    trigger = COPIES_ROWS.format(table="dr_tk.box", sink="dr_tk.out")
    wes_cannot = "cannot run, because its owner user mia"

    def refill() -> None:
        expect_allowed(client=admin, sql="INSERT INTO dr_tk.box VALUES (1, 'one')")

    try:
        make(client=mia, statements=[
            "CREATE TABLE dr_tk.box (id INT, note VARCHAR)", "CREATE TABLE dr_tk.out (note VARCHAR)",
            "GRANT DELETE ON dr_tk.box TO changers", "GRANT INSERT, SELECT ON dr_tk.out TO changers",
        ])
        wes_now = lambda: server.user(name="wes")  # noqa: E731 (a session after each change of owner)
        for takeover in [
            [f"CREATE OR REPLACE TRIGGER box_del {trigger}"],
            ["DROP TRIGGER box_del ON dr_tk.box", f"CREATE TRIGGER box_del {trigger}"],
        ]:
            # The administrator's trigger vouches: wes, who may not read box, deletes from it.
            expect_allowed(client=admin, sql="ALTER TABLE dr_tk.box OWNER TO mia")
            expect_allowed(client=admin, sql=f"CREATE OR REPLACE TRIGGER box_del {trigger}")
            expect_allowed(client=admin, sql="ALTER TABLE dr_tk.box OWNER TO ROLE dr_tk_owners")
            refill()
            expect_blocked(client=mia, sql="SELECT note FROM dr_tk.box", why=NO_SELECT)
            expect_blocked(client=wes_now(), sql="SELECT note FROM dr_tk.box", why=NO_SELECT)
            assert values(client=wes_now(), sql="DELETE FROM dr_tk.box") == ["1"]
            # mia may not touch a trigger on a table that is not hers ...
            expect_blocked(client=mia, sql=f"CREATE OR REPLACE TRIGGER box_del {trigger}", why=ONLY_OWNER)
            # ... and what she makes on her own table is hers: it answers with her rights.
            expect_allowed(client=admin, sql="ALTER TABLE dr_tk.box OWNER TO mia")
            make(client=mia, statements=takeover)
            # (A table can't be renamed away from its trigger's owner record: DuckDB renames
            # no table that has a trigger, for its owner or for an administrator.)
            for client in [mia, admin]:
                expect_blocked(client=client, sql="ALTER TABLE dr_tk.box RENAME TO box_renamed", code="execution_error",
                               why="Cannot alter entry \"box\" because there are entries that depend on it")
            expect_allowed(client=admin, sql="ALTER TABLE dr_tk.box OWNER TO ROLE dr_tk_owners")
            refill()
            error = expect_blocked(client=wes_now(), sql="DELETE FROM dr_tk.box", why=wes_cannot)
            assert OWNER_READS in error.detail and NO_SELECT in error.detail
            assert count(client=admin, table="dr_tk.box") == 1
            expect_allowed(client=admin, sql="DELETE FROM dr_tk.box")
        # A user's trigger an administrator replaces is the administrator's (it was mia's a
        # moment ago, above): it vouches again.
        expect_allowed(client=admin, sql=f"CREATE OR REPLACE TRIGGER box_del {trigger}")
        expect_allowed(client=admin, sql="INSERT INTO dr_tk.box VALUES (2, 'two')")
        assert values(client=wes_now(), sql="DELETE FROM dr_tk.box") == ["1"]
        assert "two" in values(client=wes_now(), sql="SELECT note FROM dr_tk.out")
        # DuckDB has no temporary triggers (one would have no owner on record at all).
        expect_blocked(client=wes_now(), sql=f"CREATE TEMP TRIGGER box_tmp {trigger}", code="sql_error",
                       why="Temporary triggers are not supported")
    finally:
        for sql in ["DROP TABLE IF EXISTS dr_tk.box", "DROP TABLE IF EXISTS dr_tk.out",
                    "REVOKE CREATE TABLE, CREATE TRIGGER, DROP TRIGGER ON SCHEMA dr_tk FROM trigger_makers",
                    "DROP SCHEMA IF EXISTS dr_tk", "DROP ROLE IF EXISTS dr_tk_owners"]:
            expect_allowed(client=admin, sql=sql)


def test_a_trigger_made_outside_access_control_does_not_vouch(launcher, store, tmp_path):
    # The init SQL's DuckDB statements run as the operator's configuration, outside access
    # control: nobody is on record as having made a trigger they create, and one they replace
    # is no longer the trigger its record is of. Such a trigger runs with the writer's rights
    # and vouches for nothing: reading the rows takes the writer's SELECT, as it always did.
    database = tmp_path / "unrecorded.duckdb"
    store_uri = store.new_database()
    first = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(database)])
    admin = first.admin()
    recorded = COPIES_ROWS.format(table="recorded", sink="seen")
    for sql in ["CREATE TABLE recorded (id INT, note VARCHAR)", "CREATE TABLE unrecorded (id INT, note VARCHAR)",
                "CREATE TABLE seen (note VARCHAR)", "INSERT INTO recorded VALUES (1, 'one')",
                "INSERT INTO unrecorded VALUES (2, 'two')", f"CREATE TRIGGER recorded_del {recorded}",
                "CREATE ROLE changers", "CREATE USER wes", "GRANT changers TO wes",
                "GRANT DELETE ON recorded TO changers", "GRANT DELETE ON unrecorded TO changers",
                "GRANT INSERT ON seen TO changers"]:
        expect_allowed(client=admin, sql=sql)
    # Twin: the administrator's own trigger vouches.
    assert values(client=first.user(name="wes"), sql="DELETE FROM recorded") == ["1"]
    expect_allowed(client=admin, sql="INSERT INTO recorded VALUES (1, 'one')")
    first.running.stop()
    init = ("CREATE OR REPLACE TRIGGER recorded_del AFTER DELETE ON recorded REFERENCING OLD TABLE AS gone "
            "FOR EACH STATEMENT INSERT INTO seen SELECT upper(note) FROM gone; "
            "CREATE OR REPLACE TRIGGER unrecorded_del " + COPIES_ROWS.format(table="unrecorded", sink="seen") + ";")
    second = start_rbac_server(launcher=launcher, store_uri=store_uri, args=["--database", str(database)],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": init})
    admin, wes = second.admin(), second.user(name="wes")
    for table in ["recorded", "unrecorded"]:
        error = expect_blocked(client=wes, sql=f"DELETE FROM {table}", why=NO_SELECT)
        assert NOBODY_MADE_IT in error.detail and f"trigger {table}_del on table" in error.detail
        assert count(client=admin, table=table) == 1
    assert count(client=admin, table="seen") == 1
    # Twin: with SELECT the writer fires them; and one an administrator's session makes vouches.
    expect_allowed(client=admin, sql="GRANT SELECT ON recorded TO changers")
    assert values(client=wes, sql="DELETE FROM recorded") == ["1"]
    expect_allowed(client=admin, sql="CREATE OR REPLACE TRIGGER unrecorded_del " +
                                     COPIES_ROWS.format(table="unrecorded", sink="seen"))
    assert values(client=wes, sql="DELETE FROM unrecorded") == ["1"]
    assert sorted(values(client=admin, sql="SELECT note FROM seen")) == ["ONE", "one", "two"]


def test_the_rows_a_trigger_hands_on_go_where_its_maker_wrote(admin, mia, wes):
    # Whoever answers for the rows answers for a body that does what its maker wrote. A
    # trigger is stored with the names in its action given their schema
    # (test_rbac_trigger_names.py has the triggers of before), so a writer's own table of the
    # sink's name ahead in its search path takes nothing; a temporary one would stand in for
    # main's (the temporary catalog's schema is main), so the write is refused.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_pin (id INT, note VARCHAR)", "CREATE TABLE main.dr_pin_sink (note VARCHAR)",
        "CREATE SCHEMA dr_ws", "GRANT CREATE TABLE ON SCHEMA dr_ws TO changers",
        "GRANT DELETE ON main.dr_pin TO changers", "GRANT INSERT ON main.dr_pin_sink TO changers",
        "GRANT INSERT ON main.dr_pin_sink TO trigger_makers",
    ])
    make(client=mia, statements=[
        "CREATE TABLE app.dr_pin_mine (id INT, note VARCHAR)", "GRANT DELETE ON app.dr_pin_mine TO changers",
    ])
    try:
        for sql in ["CREATE TEMP TABLE dr_pin_sink (note VARCHAR)", "CREATE TABLE dr_ws.dr_pin_sink (note VARCHAR)",
                    "GRANT INSERT ON dr_ws.dr_pin_sink TO trigger_makers", "USE dr_ws"]:
            expect_allowed(client=wes, sql=sql)
        catalog = admin.run(sql="SELECT current_database() AS c").column("c").to_pylist()[0]
        for maker, table in [(admin, "main.dr_pin"), (mia, "app.dr_pin_mine")]:
            expect_allowed(client=admin, sql=f"INSERT INTO {table} VALUES (1, 'one'), (2, 'two')")
            expect_allowed(client=maker, sql="CREATE OR REPLACE TRIGGER dr_pin_del " +
                                             COPIES_ROWS.format(table=table, sink="dr_pin_sink"))
            stored = values(client=admin, sql="SELECT sql FROM duckdb_triggers() WHERE trigger_name = 'dr_pin_del'")[0]
            assert "INSERT INTO main.dr_pin_sink " in stored, stored
            expect_blocked(client=wes, sql=f"SELECT note FROM {catalog}.{table}", why=NO_SELECT)
            error = expect_blocked(client=wes, sql=f"DELETE FROM {catalog}.{table}",
                                   why="names main.dr_pin_sink, and in this session that is a temporary object")
            assert "Drop or rename the temporary object to write this table" in error.detail
            assert count(client=admin, table=table) == 2
            # Twin: without the temporary table, the writer's own table ahead in its search
            # path takes nothing.
            expect_allowed(client=wes, sql="DROP TABLE temp.main.dr_pin_sink")
            assert values(client=wes, sql=f"DELETE FROM {catalog}.{table}") == ["2"]
            expect_allowed(client=wes, sql="CREATE TEMP TABLE dr_pin_sink (note VARCHAR)")
            assert sorted(values(client=admin, sql="SELECT note FROM main.dr_pin_sink")) == ["one", "two"]
            expect_allowed(client=admin, sql="DELETE FROM main.dr_pin_sink")
            assert count(client=wes, table="dr_ws.dr_pin_sink") == 0 and count(client=wes, table="temp.main.dr_pin_sink") == 0
    finally:
        drop_tables(client=mia, names=["app.dr_pin_mine"])
        drop_tables(client=admin, names=["main.dr_pin", "main.dr_pin_sink", "dr_ws.dr_pin_sink"])
        for sql in ["REVOKE CREATE TABLE ON SCHEMA dr_ws FROM changers", "DROP SCHEMA IF EXISTS dr_ws"]:
            expect_allowed(client=admin, sql=sql)


def test_a_users_body_that_fires_an_administrators_trigger(admin, mia, wes):
    # mia's trigger deletes from a table she may write and not read; that table has an
    # administrator's trigger that copies the rows it loses. The writer of the inner DELETE is
    # mia's body, with mia's rights: the administrator's trigger vouches for its own reads of
    # those rows, and for nothing else mia's body does.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_inner (id INT, note VARCHAR)", "INSERT INTO main.dr_inner VALUES (1, 'one'), (2, 'two')",
        "CREATE TABLE main.dr_kept (note VARCHAR)",
        "CREATE TRIGGER dr_inner_del " + COPIES_ROWS.format(table="main.dr_inner", sink="main.dr_kept"),
        "GRANT DELETE ON main.dr_inner TO trigger_makers", "GRANT INSERT ON main.dr_kept TO trigger_makers",
    ])
    make(client=mia, statements=[
        "CREATE TABLE app.dr_outer (id INT)", "GRANT INSERT ON app.dr_outer TO changers",
        "CREATE TRIGGER dr_outer_ins AFTER INSERT ON app.dr_outer FOR EACH STATEMENT DELETE FROM main.dr_inner",
    ])
    try:
        for client in [mia, wes]:
            expect_blocked(client=client, sql="SELECT note FROM main.dr_inner", why=NO_SELECT)
        expect_blocked(client=wes, sql="DELETE FROM main.dr_inner", why="no DELETE privilege on table")
        expect_allowed(client=wes, sql="INSERT INTO app.dr_outer VALUES (1)")
        assert sorted(values(client=admin, sql="SELECT note FROM main.dr_kept")) == ["one", "two"]
        # mia's own body reading the table it deletes from is mia's read: no SELECT, no run.
        expect_allowed(client=admin, sql="INSERT INTO main.dr_inner VALUES (3, 'three')")
        expect_allowed(client=mia, sql="CREATE OR REPLACE TRIGGER dr_outer_ins AFTER INSERT ON app.dr_outer "
                                       "FOR EACH STATEMENT DELETE FROM main.dr_inner WHERE note = 'three'")
        error = expect_blocked(client=wes, sql="INSERT INTO app.dr_outer VALUES (2)", why=CANNOT_RUN)
        assert "trigger dr_outer_ins on table" in error.detail and NO_SELECT in error.detail
        assert count(client=admin, table="main.dr_inner") == 1 and count(client=admin, table="app.dr_outer") == 1
        # Twin: once mia may read that column.
        expect_allowed(client=admin, sql="GRANT SELECT (note) ON main.dr_inner TO trigger_makers")
        expect_allowed(client=wes, sql="INSERT INTO app.dr_outer VALUES (2)")
        assert sorted(values(client=admin, sql="SELECT note FROM main.dr_kept")) == ["one", "three", "two"]
    finally:
        drop_tables(client=mia, names=["app.dr_outer"])
        drop_tables(client=admin, names=["main.dr_inner", "main.dr_kept"])


def test_keys_and_checked_columns_are_not_carried(admin, wes):
    # What the write itself can tell of a column: a constraint error names a key's value
    # (DuckDB runs an UPDATE that returns its rows as a delete and an insert, so a foreign
    # key that refers to the row stops it), and a CHECK over several columns tells of the
    # ones the statement did not set. Those take SELECT, as on a table without triggers.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_parent (id INT PRIMARY KEY, x INT, y INT)",
        "INSERT INTO main.dr_parent VALUES (41, 1, 1), (42, 2, 2)",
        "CREATE TABLE main.dr_child (pid INT REFERENCES main.dr_parent (id))", "INSERT INTO main.dr_child VALUES (41)",
        "CREATE TABLE main.dr_ck (id INT, x INT, y INT, CHECK (x + y < 1000))", "INSERT INTO main.dr_ck VALUES (1, 1, 10)",
        "CREATE TABLE main.dr_klog (what VARCHAR)",
        "CREATE TRIGGER dr_parent_upd AFTER UPDATE ON main.dr_parent FOR EACH STATEMENT INSERT INTO main.dr_klog VALUES ('u')",
        "CREATE TRIGGER dr_parent_del AFTER DELETE ON main.dr_parent FOR EACH STATEMENT INSERT INTO main.dr_klog VALUES ('d')",
        "CREATE TRIGGER dr_ck_upd AFTER UPDATE ON main.dr_ck FOR EACH STATEMENT INSERT INTO main.dr_klog VALUES ('c')",
        "GRANT UPDATE, DELETE ON main.dr_parent TO changers", "GRANT UPDATE ON main.dr_ck TO changers",
        "GRANT INSERT ON main.dr_klog TO changers",
    ])
    try:
        for sql in ["DELETE FROM main.dr_parent", "UPDATE main.dr_parent SET x = 0"]:
            error = expect_blocked(client=wes, sql=sql, why=NO_SELECT)
            assert "41" not in error.detail
            assert KEYS_NOTE in error.detail, error.detail
        # Twin: SELECT on the key (and not on x or y), and the write is DuckDB's to run or to
        # refuse, naming a key the writer may read.
        expect_allowed(client=admin, sql="GRANT SELECT (id) ON main.dr_parent TO changers")
        for sql in ["DELETE FROM main.dr_parent", "UPDATE main.dr_parent SET x = 0"]:
            with pytest.raises(ChargewireError) as raised:
                wes.run(sql=sql)
            assert raised.value.code != "permission_denied" and "41" in raised.value.detail, str(raised.value)
        expect_allowed(client=admin, sql="DELETE FROM main.dr_child")
        assert values(client=wes, sql="UPDATE main.dr_parent SET x = 0") == ["2"]
        assert values(client=wes, sql="DELETE FROM main.dr_parent") == ["2"]
        assert count(client=admin, table="main.dr_klog", where="what IN ('u', 'd')") == 2
        expect_blocked(client=wes, sql="SELECT x FROM main.dr_parent", why=NO_SELECT_ON)

        # x + y < 1000: setting x tells of y.
        error = expect_blocked(client=wes, sql="UPDATE main.dr_ck SET x = 995", why=NO_SELECT)
        assert KEYS_NOTE in error.detail, error.detail
        expect_allowed(client=admin, sql="GRANT SELECT (y) ON main.dr_ck TO changers")
        with pytest.raises(ChargewireError) as raised:
            wes.run(sql="UPDATE main.dr_ck SET x = 995")
        assert raised.value.code != "permission_denied" and "CHECK constraint failed" in raised.value.detail
        assert values(client=wes, sql="UPDATE main.dr_ck SET x = 5") == ["1"]
        assert columns_missing(error=expect_blocked(client=wes, sql="SELECT * FROM main.dr_ck", why=NO_SELECT_ON)) == {"id", "x"}
    finally:
        drop_tables(client=admin, names=["main.dr_child", "main.dr_parent", "main.dr_ck", "main.dr_klog"])


# Why a write to a table with triggers still takes SELECT, in the refusal's words.
KEYS_NOTE = ("the table has triggers: a write to it that reads no column needs no SELECT, except on the columns of its "
             "keys and indexes, and an UPDATE's on those of a CHECK over several columns, whose errors can tell their values")
GENERATED_NOTE = ("the table has triggers and generated columns: a write to it computes the generated columns from the "
                  "rows it changes, which takes SELECT on the table")


def test_a_write_the_statement_reads_for_says_nothing_of_keys(admin, wes):
    # The note is for the columns a key, an index or a CHECK makes a read: a column the
    # statement itself reads (its WHERE) is refused as ever, without it.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_kw (id INT PRIMARY KEY, x INT)", "INSERT INTO main.dr_kw VALUES (1, 1)",
        "CREATE TABLE main.dr_kwlog (what VARCHAR)",
        "CREATE TRIGGER dr_kw_del AFTER DELETE ON main.dr_kw FOR EACH STATEMENT INSERT INTO main.dr_kwlog VALUES ('d')",
        "GRANT DELETE ON main.dr_kw TO changers", "GRANT INSERT ON main.dr_kwlog TO changers",
        "GRANT SELECT (id) ON main.dr_kw TO changers",
    ])
    try:
        error = expect_blocked(client=wes, sql="DELETE FROM main.dr_kw WHERE x = 1", why=NO_SELECT_ON)
        assert KEYS_NOTE not in error.detail and columns_missing(error=error) == {"x"}
        assert values(client=wes, sql="DELETE FROM main.dr_kw WHERE id = 1") == ["1"]
    finally:
        drop_tables(client=admin, names=["main.dr_kw", "main.dr_kwlog"])


def test_a_table_with_generated_columns_and_triggers_takes_select(admin, wes):
    # DuckDB computes a generated column from the rows a write changes, in the statement: no
    # column of such a table's write is carried, so the writer needs SELECT, and is told why.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_gen (id INT, x INT, twice INT GENERATED ALWAYS AS (x * 2))",
        "INSERT INTO main.dr_gen (id, x) VALUES (1, 1), (2, 2)",
        "CREATE TABLE main.dr_glog (what VARCHAR)",
        "CREATE TRIGGER dr_gen_del AFTER DELETE ON main.dr_gen FOR EACH STATEMENT INSERT INTO main.dr_glog VALUES ('d')",
        "CREATE TRIGGER dr_gen_upd AFTER UPDATE ON main.dr_gen FOR EACH STATEMENT INSERT INTO main.dr_glog VALUES ('u')",
        "GRANT DELETE, UPDATE ON main.dr_gen TO changers", "GRANT INSERT ON main.dr_glog TO changers",
    ])
    try:
        for sql in ["DELETE FROM main.dr_gen WHERE true", "UPDATE main.dr_gen SET x = 3"]:
            error = expect_blocked(client=wes, sql=sql, why=NO_SELECT)
            assert GENERATED_NOTE in error.detail, error.detail
        assert count(client=admin, table="main.dr_glog") == 0
        # Twin: with SELECT on the table, both run.
        expect_allowed(client=admin, sql="GRANT SELECT ON main.dr_gen TO changers")
        assert values(client=wes, sql="UPDATE main.dr_gen SET x = 3") == ["2"]
        assert values(client=wes, sql="DELETE FROM main.dr_gen WHERE true") == ["2"]
        assert sorted(values(client=admin, sql="SELECT what FROM main.dr_glog")) == ["d", "u"]
    finally:
        drop_tables(client=admin, names=["main.dr_gen", "main.dr_glog"])


def test_on_conflict_do_update_needs_select_on_the_key_and_what_it_reads(admin, wes):
    # INSERT ... ON CONFLICT DO UPDATE is a merge in the plan. With a trigger on the table
    # the binder carries the columns the update leaves alone, too: the writer needs SELECT
    # on the key that tells a conflict and on what its own statement reads, as without one.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_keyed (id INT PRIMARY KEY, note VARCHAR, extra INT)",
        "INSERT INTO main.dr_keyed VALUES (1, 'first', 10)",
        "CREATE TABLE main.dr_ulog (what VARCHAR)",
        "CREATE TRIGGER dr_keyed_ins AFTER INSERT ON main.dr_keyed FOR EACH STATEMENT INSERT INTO main.dr_ulog VALUES ('i')",
        "GRANT INSERT, UPDATE ON main.dr_keyed TO changers", "GRANT INSERT ON main.dr_ulog TO changers",
    ])
    upsert = "INSERT INTO main.dr_keyed VALUES (1, 'again', 0) ON CONFLICT (id) DO UPDATE SET note = excluded.note"
    try:
        expect_blocked(client=wes, sql=upsert, why=NO_SELECT)
        expect_allowed(client=admin, sql="GRANT SELECT (id) ON main.dr_keyed TO changers")
        assert values(client=wes, sql=upsert) == ["1"]
        assert values(client=admin, sql="SELECT note || ' ' || extra FROM main.dr_keyed") == ["again 10"]
        assert count(client=admin, table="main.dr_ulog") == 1
        for sql in [upsert + " WHERE dr_keyed.extra > 0",
                    "INSERT INTO main.dr_keyed VALUES (1, 'again', 0) ON CONFLICT (id) DO UPDATE SET extra = dr_keyed.extra + 1"]:
            assert columns_missing(error=expect_blocked(client=wes, sql=sql, why=NO_SELECT_ON)) == {"extra"}
            expect_allowed(client=admin, sql="GRANT SELECT (extra) ON main.dr_keyed TO changers")
            expect_allowed(client=wes, sql=sql)
            expect_allowed(client=admin, sql="REVOKE SELECT (extra) ON main.dr_keyed FROM changers")
        assert values(client=admin, sql="SELECT note || ' ' || extra FROM main.dr_keyed") == ["again 11"]
        assert "note" in columns_missing(error=expect_blocked(client=wes, sql=upsert + " RETURNING *", why=NO_SELECT_ON))
    finally:
        drop_tables(client=admin, names=["main.dr_keyed", "main.dr_ulog"])


def test_a_write_in_a_triggers_body_is_checked_the_same_way(admin, wes):
    # The UPDATE or DELETE is the body of an administrator's trigger, so it runs with the
    # writer's rights; the table it writes has a trigger of its own.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_inbox (id INT)", "CREATE TABLE main.dr_state (id INT, x INT)",
        "INSERT INTO main.dr_state VALUES (1, 1), (2, 2)", "CREATE TABLE main.dr_slog (what VARCHAR)",
        "CREATE TRIGGER dr_state_upd AFTER UPDATE ON main.dr_state FOR EACH STATEMENT INSERT INTO main.dr_slog VALUES ('u')",
        "CREATE TRIGGER dr_state_del AFTER DELETE ON main.dr_state FOR EACH STATEMENT INSERT INTO main.dr_slog VALUES ('d')",
        "CREATE TRIGGER dr_inbox_ins AFTER INSERT ON main.dr_inbox FOR EACH STATEMENT UPDATE main.dr_state SET x = 0",
        "GRANT INSERT ON main.dr_inbox TO changers", "GRANT UPDATE, DELETE ON main.dr_state TO changers",
        "GRANT INSERT ON main.dr_slog TO changers",
    ])
    try:
        expect_blocked(client=wes, sql="SELECT x FROM main.dr_state", why=NO_SELECT)
        expect_allowed(client=wes, sql="INSERT INTO main.dr_inbox VALUES (1)")
        assert values(client=admin, sql="SELECT x FROM main.dr_state") == ["0", "0"]
        assert count(client=admin, table="main.dr_slog", where="what = 'u'") == 1
        # A body that reads the table it writes: the writer's SELECT on that column.
        for body in ["DELETE FROM main.dr_state WHERE x = 0",
                     "CREATE OR REPLACE TRIGGER dr_inbox_ins AFTER INSERT ON main.dr_inbox FOR EACH ROW "
                     "DELETE FROM main.dr_state WHERE x = NEW.id"]:
            trigger = body if body.startswith("CREATE") else (
                "CREATE OR REPLACE TRIGGER dr_inbox_ins AFTER INSERT ON main.dr_inbox FOR EACH STATEMENT " + body)
            expect_allowed(client=admin, sql=trigger)
            expect_blocked(client=wes, sql="INSERT INTO main.dr_inbox VALUES (0)", why=NO_SELECT)
            assert count(client=admin, table="main.dr_state") == 2
            expect_allowed(client=admin, sql="GRANT SELECT (x) ON main.dr_state TO changers")
            expect_allowed(client=wes, sql="INSERT INTO main.dr_inbox VALUES (0)")
            assert count(client=admin, table="main.dr_state") == 0
            expect_allowed(client=admin, sql="REVOKE SELECT (x) ON main.dr_state FROM changers")
            expect_allowed(client=admin, sql="INSERT INTO main.dr_state VALUES (1, 0), (2, 0)")
        assert count(client=admin, table="main.dr_slog", where="what = 'd'") == 2
    finally:
        drop_tables(client=admin, names=["main.dr_inbox", "main.dr_state", "main.dr_slog"])


def test_the_carried_rows_reach_no_statement_of_the_writers_own(admin, wes):
    # Every way a writer's statement could get at the rows the binder carries to the
    # triggers: a RETURNING of its own (in a WITH too), a statement with parameters (bound
    # and checked on another path), and a CTE named like the one the binder keeps the rows
    # in (__duckdb_trigger_base_<uuid>), which this check then treats as that CTE. Counting
    # its rows tells what the write itself tells (the number of rows changed); anything
    # that reads a value takes SELECT.
    make(client=admin, statements=[
        "CREATE TABLE main.dr_c (id INT, x INT)", "INSERT INTO main.dr_c VALUES (1, 10), (2, 20)",
        "CREATE TABLE main.dr_cplain (id INT, x INT)", "INSERT INTO main.dr_cplain VALUES (1, 10), (2, 20)",
        "CREATE TABLE main.dr_clog (what VARCHAR)", "CREATE TABLE main.dr_mine (x INT)",
        "CREATE TRIGGER dr_c_upd AFTER UPDATE ON main.dr_c FOR EACH STATEMENT INSERT INTO main.dr_clog VALUES ('u')",
        "CREATE TRIGGER dr_c_del AFTER DELETE ON main.dr_c FOR EACH STATEMENT INSERT INTO main.dr_clog VALUES ('d')",
        "GRANT UPDATE, DELETE ON main.dr_c TO changers", "GRANT UPDATE, DELETE ON main.dr_cplain TO changers",
        "GRANT INSERT ON main.dr_clog TO changers", "GRANT INSERT, SELECT ON main.dr_mine TO changers",
    ])
    forged = 'WITH "__duckdb_trigger_base_x" AS (UPDATE main.dr_cplain SET id = 0 RETURNING *) '
    try:
        for sql in [
            "UPDATE main.dr_c SET id = 0 RETURNING x",
            "DELETE FROM main.dr_c RETURNING *",
            "WITH gone AS (DELETE FROM main.dr_c RETURNING *) SELECT count(*) FROM gone",
            "WITH gone AS (DELETE FROM main.dr_c RETURNING id) SELECT max(id) FROM gone",
            "WITH changed AS (UPDATE main.dr_c SET id = 0 RETURNING *) INSERT INTO main.dr_mine SELECT x FROM changed",
            "CREATE TEMP TABLE dr_kept AS WITH gone AS (DELETE FROM main.dr_c RETURNING *) SELECT * FROM gone",
            "UPDATE main.dr_c SET id = 0 WHERE rowid IN (SELECT rowid FROM main.dr_c WHERE x > 10)",
            "UPDATE main.dr_c SET id = 0 WHERE dr_c IS NOT NULL",
            "EXPLAIN UPDATE main.dr_c SET id = 0 WHERE x > 10",
            "PREPARE dr_p AS DELETE FROM main.dr_c WHERE x > $1",
            forged + 'SELECT max(x) FROM "__duckdb_trigger_base_x"',
            forged + 'SELECT count(*) FROM "__duckdb_trigger_base_x" WHERE x > 10',
            forged + 'SELECT count(*) FROM "__duckdb_trigger_base_x" GROUP BY x',
            forged + 'SELECT count(*) FROM (SELECT DISTINCT * FROM "__duckdb_trigger_base_x")',
            forged + 'SELECT count(*) FROM "__duckdb_trigger_base_x" a, "__duckdb_trigger_base_x" b WHERE a.x < b.x',
            forged + 'SELECT 1 FROM "__duckdb_trigger_base_x" ORDER BY x',
            forged + 'SELECT * FROM "__duckdb_trigger_base_x"',
            forged + 'INSERT INTO main.dr_mine SELECT x FROM "__duckdb_trigger_base_x"',
        ]:
            expect_blocked(client=wes, sql=sql, why=NO_SELECT)
        # A body of the writer's own beside it is no trigger's.
        expect_blocked(client=wes, why="could not be matched to its body", sql=forged + (
            ', "__duckdb_trigger_body_1_x" AS (INSERT INTO main.dr_mine SELECT x FROM "__duckdb_trigger_base_x" '
            'RETURNING *) SELECT count(*) FROM "__duckdb_trigger_base_x"'))
        # With parameters, run and prepared. A statement is checked when it is prepared, on
        # the plan as DuckDB binds it (a correlated subquery not yet flattened), and again
        # when it runs: both must refuse.
        for sql, parameters in [
            ("UPDATE main.dr_c SET id = ? WHERE x > ?", [0, 10]),
            ("UPDATE main.dr_c SET id = ? WHERE EXISTS (SELECT 1 FROM main.dr_mine m WHERE m.x > dr_c.x)", [0]),
            ("DELETE FROM main.dr_c WHERE (SELECT count(*) FROM main.dr_mine m WHERE m.x > dr_c.x) > ?", [100]),
            ("UPDATE main.dr_c SET id = (SELECT max(m.x) + ?::INT FROM main.dr_mine m WHERE m.x <> dr_c.x)", [1]),
        ]:
            for call in [lambda: wes.run(sql=sql, parameters=parameters), lambda: wes.prepare(sql=sql)]:
                with pytest.raises(ChargewireError) as raised:
                    call()
                assert raised.value.code == "permission_denied" and NO_SELECT in raised.value.detail, sql
        assert count(client=admin, table="main.dr_mine") == 0 and count(client=admin, table="main.dr_clog") == 0
        assert values(client=admin, sql="SELECT id FROM main.dr_c ORDER BY id") == ["1", "2"]
        assert values(client=admin, sql="SELECT id FROM main.dr_cplain ORDER BY id") == ["1", "2"]
        # Twins: the same shapes that read no value.
        assert values(client=wes, sql=forged + 'SELECT count(*) FROM "__duckdb_trigger_base_x"') == ["2"]
        assert values(client=wes, sql="UPDATE main.dr_c SET id = 0 WHERE rowid >= 0") == ["2"]
        assert expect_allowed(client=wes, sql="UPDATE main.dr_c SET x = ?", parameters=[5]).column(0).to_pylist() == [2]
        prepared = wes.prepare(sql="UPDATE main.dr_c SET x = ?")
        assert wes.execute_prepared(prepared_id=prepared, parameters=[6]).column(0).to_pylist() == [2]
        expect_allowed(client=wes, sql="PREPARE dr_p AS DELETE FROM main.dr_c")
        assert values(client=wes, sql="EXECUTE dr_p") == ["2"]
        assert count(client=admin, table="main.dr_clog") == 4
    finally:
        drop_tables(client=admin, names=["main.dr_c", "main.dr_cplain", "main.dr_clog", "main.dr_mine"])
