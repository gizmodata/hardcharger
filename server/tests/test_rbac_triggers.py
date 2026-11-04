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
        "CREATE ROLE inserters",
        "CREATE USER mia", "CREATE USER joe", "CREATE USER cal", "CREATE USER dan", "CREATE USER sid", "CREATE USER ike",
        "GRANT trigger_makers, app_builders, writers, eu_staff TO mia",
        "GRANT writers TO joe", "GRANT app_builders, macro_makers TO cal", "GRANT trigger_droppers TO dan",
        "GRANT secret_readers TO sid", "GRANT inserters TO ike",
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
