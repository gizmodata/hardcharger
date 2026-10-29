"""Where new sessions start: default_catalog and default_schema, and an init SQL that ends
in USE (GizmoSQL 1.x started every session where its init SQL's USE left off).

- default_catalog / default_schema (flag, HARDCHARGER_* variable or YAML key) move every
  new session there; a client's own catalog/schema session options still win, and USE
  still moves a session anywhere. default_schema alone is that schema in the database's
  catalog.
- With neither option set, an init SQL whose connection ends somewhere other than where
  a fresh connection starts (USE x, USE x.y) sets where sessions start, and says so in the
  log. Either option turns that off.
- Both are checked after the init SQL: a catalog that isn't attached, a schema that
  doesn't exist, or one of the server's own catalogs stops the server (exit 1).
- Under access control the move is checked like the user's own USE: a user who may not
  see the default catalog still gets a session, where they would have started without it.
  A tenant's default catalog wins over the server's.

The options are passed as environment variables, so a server without them (an older
build) starts and the assertions fail on what the sessions do.
"""

import json
import subprocess

import pytest
from conftest import BINARY, mint_server_token
from rbac import expect_allowed, expect_blocked, start_rbac_server

LAKE = ("ATTACH ':memory:' AS lake; CREATE SCHEMA lake.sales; CREATE SCHEMA lake.ops; "
        "CREATE TABLE lake.sales.orders AS SELECT 42 AS n; CREATE SCHEMA memory.extra;")


def where(session) -> tuple[str, str]:
    """The session's current catalog and schema, as SQL sees them."""
    row = session.rows(sql="SELECT current_database() AS c, current_schema() AS s")[0]
    return row["c"], row["s"]


def document(session) -> tuple[str, str]:
    """The session's catalog and schema, as GET /chargewire/v1/sessions/{id} reports them."""
    options = session.options()
    return options["catalog"], options["schema"]


def start(launcher, init_sql: str = LAKE, env: dict[str, str] | None = None, args: list[str] | None = None):
    return launcher.start(args=["--init-sql-commands", init_sql, *(args or [])], env=env)


def refused(launcher, message: str, init_sql: str = LAKE, env: dict[str, str] | None = None,
            args: list[str] | None = None) -> str:
    """Starts a server that must refuse to start; returns its log."""
    running = launcher.start(args=["--init-sql-commands", init_sql, *(args or [])], env=env, expect_up=False)
    assert running.process.wait(timeout=60) == 1, running.log()
    log = running.log()
    assert message in log, log
    return log


# ---- default_catalog / default_schema ----

def test_new_sessions_start_in_the_default_catalog_and_schema(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake",
                                           "HARDCHARGER_DEFAULT_SCHEMA": "sales"})
    client = server.client()
    created = client.open_session()
    assert created.status_code == 201, created.text
    # The create response's session document already says so.
    assert (created.json()["options"]["catalog"], created.json()["options"]["schema"]) == ("lake", "sales")
    session = client.new_session()
    assert where(session=session) == ("lake", "sales")
    assert document(session=session) == ("lake", "sales")
    # Unqualified names resolve there.
    assert session.scalar(sql="SELECT n FROM orders") == 42
    # The administrator sees the effective defaults.
    info = session.rows(sql="SELECT default_catalog, default_schema FROM hardcharger_server_info()")[0]
    assert (info["default_catalog"], info["default_schema"]) == ("lake", "sales"), info
    assert "sessions start in lake.sales (default_catalog, default_schema)" in server.log()


def test_the_default_catalog_alone_starts_in_its_default_schema(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake"})
    assert where(session=server.admin()) == ("lake", "main")


def test_the_default_schema_alone_is_in_the_databases_catalog(launcher):
    # Even when the init SQL ends elsewhere: setting either option turns adoption off.
    server = start(launcher=launcher, init_sql=LAKE + " USE lake.sales;",
                   env={"HARDCHARGER_DEFAULT_SCHEMA": "extra"})
    session = server.admin()
    assert where(session=session) == ("memory", "extra")
    assert document(session=session) == ("memory", "extra")
    assert "init SQL ended in USE" not in server.log()


def test_the_options_work_from_flags_and_yaml(launcher, tmp_path):
    server = start(launcher=launcher, args=["--default-catalog", "lake", "--default-schema", "ops"])
    assert where(session=server.admin()) == ("lake", "ops")
    config = tmp_path / "hardcharger.yaml"
    config.write_text(data="default_catalog: lake\ndefault_schema: sales\n")
    server = start(launcher=launcher, args=["--config", str(config)])
    assert where(session=server.admin()) == ("lake", "sales")


def test_a_clients_own_session_options_win(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake",
                                           "HARDCHARGER_DEFAULT_SCHEMA": "sales"})
    client = server.client()
    elsewhere = client.new_session(options={"catalog": "memory"})
    assert where(session=elsewhere) == ("memory", "main")
    assert document(session=elsewhere) == ("memory", "main")
    both = client.new_session(options={"catalog": "memory", "schema": "extra"})
    assert where(session=both) == ("memory", "extra")
    # A schema alone is in the catalog the session starts in: the default one.
    schema_only = client.new_session(options={"schema": "ops"})
    assert where(session=schema_only) == ("lake", "ops")
    assert document(session=schema_only) == ("lake", "ops")


def test_use_still_moves_a_session_anywhere(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake",
                                           "HARDCHARGER_DEFAULT_SCHEMA": "sales"})
    session = server.admin()
    session.execute(sql="USE memory.extra")
    assert where(session=session) == ("memory", "extra")
    assert document(session=session) == ("memory", "extra")
    # PATCH of the session's options too.
    assert session.set_options(options={"catalog": "lake", "schema": "ops"}).status_code == 200
    assert where(session=session) == ("lake", "ops")
    # Another session still starts at the default.
    assert where(session=server.admin()) == ("lake", "sales")


# ---- The init SQL's final USE ----

def test_an_init_sql_that_ends_in_use_sets_where_sessions_start(launcher):
    server = start(launcher=launcher, init_sql=LAKE + " USE lake.sales;")
    session = server.admin()
    assert where(session=session) == ("lake", "sales")
    assert document(session=session) == ("lake", "sales")
    assert session.scalar(sql="SELECT n FROM orders") == 42
    assert ("init SQL ended in USE lake.sales: sessions start there (set default_catalog to choose explicitly)"
            in server.log())
    info = session.rows(sql="SELECT default_catalog, default_schema FROM hardcharger_server_info()")[0]
    assert (info["default_catalog"], info["default_schema"]) == ("lake", "sales"), info


def test_an_init_sql_file_that_ends_in_use_of_a_catalog(launcher, tmp_path):
    init = tmp_path / "init.sql"
    init.write_text(data=LAKE + "\nUSE lake;\n")
    server = launcher.start(args=["--init-sql-commands-file", str(init)])
    assert where(session=server.admin()) == ("lake", "main")
    assert "init SQL ended in USE lake.main: sessions start there" in server.log()


def test_a_use_the_init_sql_undid_is_not_adopted(launcher):
    server = start(launcher=launcher, init_sql=LAKE + " USE lake.sales; USE memory.main;")
    assert where(session=server.admin()) == ("memory", "main")
    assert "init SQL ended in USE" not in server.log()


def test_default_catalog_wins_over_the_init_sqls_use(launcher):
    server = start(launcher=launcher, init_sql=LAKE + " USE lake.sales;",
                   env={"HARDCHARGER_DEFAULT_CATALOG": "memory"})
    assert where(session=server.admin()) == ("memory", "main")
    assert "init SQL ended in USE" not in server.log()


# ---- Checked at startup ----

def test_a_default_catalog_that_is_not_attached_stops_the_server(launcher):
    refused(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lakehouse"},
            message="default_catalog 'lakehouse' is not attached: the attached catalogs are lake, memory "
                    "(attach it in the init SQL)")


def test_a_default_schema_that_does_not_exist_stops_the_server(launcher):
    refused(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake", "HARDCHARGER_DEFAULT_SCHEMA": "nope"},
            message="default_schema 'nope' does not exist in catalog 'lake': its schemas are main, ops, sales")
    refused(launcher=launcher, env={"HARDCHARGER_DEFAULT_SCHEMA": "nope"},
            message="default_schema 'nope' does not exist in catalog 'memory'")


@pytest.mark.parametrize(argnames="catalog", argvalues=["_hardcharger_system", "_hardcharger_instr", "system"])
def test_the_servers_own_catalogs_cannot_be_the_default(launcher, catalog):
    refused(launcher=launcher, args=["--enable-instrumentation"], env={"HARDCHARGER_DEFAULT_CATALOG": catalog},
            message=f"default_catalog '{catalog}' is one of the server's own catalogs")


def test_an_init_sql_that_ends_in_a_servers_own_catalog_stops_the_server(launcher):
    refused(launcher=launcher, init_sql="USE _hardcharger_system;",
            message="init SQL ended in USE _hardcharger_system.main: default_catalog '_hardcharger_system' is one "
                    "of the server's own catalogs")


def test_the_options_are_in_the_reference():
    out = subprocess.run(args=[str(BINARY), "--print-options-json"], capture_output=True, text=True, check=True,
                         timeout=30)
    options = {o["name"]: o for o in json.loads(out.stdout)["options"]}
    for name in ("default_catalog", "default_schema"):
        option = options[name]
        assert (option["type"], option["group"], option["default"]) == ("string", "Database", ""), option
        assert option["env"] == "HARDCHARGER_" + name.upper(), option
    assert "init SQL that ends in USE" in options["default_catalog"]["description"]


# ---- Access control ----

def test_catalog_access_rules_a_user_who_cannot_see_the_default_starts_where_they_would(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake", "HARDCHARGER_DEFAULT_SCHEMA": "sales",
                                           "HARDCHARGER_LOG_LEVEL": "debug", "HARDCHARGER_SESSION_LOG_LEVEL": "debug"})
    reader = server.as_role(role="user", subject="reader", catalog_access=[{"catalog": "*", "access": "read"}])
    assert where(session=reader) == ("lake", "sales")
    assert reader.scalar(sql="SELECT n FROM orders") == 42
    outsider = server.as_role(role="user", subject="outsider",
                              catalog_access=[{"catalog": "memory", "access": "read"}])
    # A working session, in the database's catalog, not an error.
    assert where(session=outsider) == ("memory", "main")
    assert document(session=outsider) == ("memory", "main")
    assert outsider.scalar(sql="SELECT 1") == 1
    # And the default's name isn't theirs to read in hardcharger_server_info().
    info = outsider.rows(sql="SELECT default_catalog FROM hardcharger_server_info()")[0]
    assert info["default_catalog"] is None, info
    assert "session starts in its usual catalog: its user may not use the default catalog" in server.log()


def test_rbac_the_default_applies_where_the_user_may_see_it(launcher, store):
    """Under RBAC a user outside any tenant sees every catalog's name (grants decide what they
    may do in it), so their sessions start in the default, as their own USE could; a tenant's
    user sees only the tenant's catalogs, and starts in the tenant's default catalog, or
    where they would without the server's default."""
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_DEFAULT_CATALOG": "lake", "HARDCHARGER_DEFAULT_SCHEMA": "sales",
                                    "HARDCHARGER_LOG_LEVEL": "debug", "HARDCHARGER_SESSION_LOG_LEVEL": "debug"},
                               args=["--init-sql-commands", LAKE])
    admin = server.admin()
    assert (admin.run(sql="SELECT current_database() AS c, current_schema() AS s").to_pylist()
            == [{"c": "lake", "s": "sales"}])
    for sql in ["CREATE ROLE lake_readers", "GRANT SELECT ON lake.sales.orders TO lake_readers",
                "CREATE USER bob", "GRANT lake_readers TO bob", "CREATE USER alice",
                "ATTACH ':memory:' AS acme_db", "ATTACH ':memory:' AS beta_db",
                "CREATE TENANT acme", "ALTER CATALOG acme_db SET TENANT acme",
                "ALTER TENANT acme SET DEFAULT CATALOG acme_db", "CREATE USER acme/joe",
                "CREATE TENANT beta", "ALTER CATALOG beta_db SET TENANT beta", "CREATE USER beta/bea"]:
        expect_allowed(client=admin, sql=sql)
    bob = server.user(name="bob")
    assert (bob.run(sql="SELECT current_database() AS c, current_schema() AS s, n FROM orders").to_pylist()
            == [{"c": "lake", "s": "sales", "n": 42}])
    # alice has no grants in lake: she starts there, may not read what she has no grant on,
    # and can go elsewhere.
    alice = server.user(name="alice")
    assert (alice.run(sql="SELECT current_database() AS c, current_schema() AS s").to_pylist()
            == [{"c": "lake", "s": "sales"}])
    expect_blocked(client=alice, sql="SELECT n FROM orders", why="SELECT")
    expect_allowed(client=alice, sql="USE memory.main")
    assert alice.run(sql="SELECT current_database() AS c").to_pylist() == [{"c": "memory"}]
    # A tenant's user starts in the tenant's default catalog, not the server's.
    joe = server.user(name="acme/joe")
    assert joe.run(sql="SELECT current_database() AS c").to_pylist() == [{"c": "acme_db"}]
    # One whose tenant has no default catalog may not see lake: a working session, where
    # it would have started without the server's default.
    bea = server.user(name="beta/bea")
    assert (bea.run(sql="SELECT current_database() AS c, current_schema() AS s").to_pylist()
            == [{"c": "memory", "s": "main"}])
    session = bea.http.get(url=bea.url, headers=bea.headers()).json()
    assert (session["options"]["catalog"], session["options"]["schema"]) == ("memory", "main"), session
    expect_blocked(client=bea, sql="USE lake.sales")
    assert "session starts in its usual catalog: its user may not use the default catalog" in server.log()


def test_a_readonly_session_starts_in_the_default_too(launcher):
    server = start(launcher=launcher, env={"HARDCHARGER_DEFAULT_CATALOG": "lake", "HARDCHARGER_DEFAULT_SCHEMA": "sales"})
    token = mint_server_token(secret_key=server.secret_key, subject="viewer", role="readonly")
    session = server.client(token=token).new_session()
    assert where(session=session) == ("lake", "sales")
    assert session.scalar(sql="SELECT n FROM orders") == 42
