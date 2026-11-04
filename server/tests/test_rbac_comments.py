"""COMMENT ON under access control: what may change an object may describe it.

A table, a column of one, or an index on one: ALTER TABLE (the owner holds it). A view or a
column of one: ALTER VIEW. A sequence or a macro (they have no owner): DROP SEQUENCE / DROP
MACRO on its schema. Types stay the administrator's. Every refusal is asserted by its
reason, next to the twin that succeeds."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_comments.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA app",
        "CREATE TABLE app.orders (id INT, item VARCHAR)", "CREATE INDEX orders_id ON app.orders (id)",
        "CREATE VIEW app.recent AS SELECT id FROM app.orders",
        "CREATE SEQUENCE app.ids", "CREATE MACRO app.twice(x) AS x * 2", "CREATE MACRO app.rows3() AS TABLE SELECT 1 AS one",
        "CREATE TYPE mood AS ENUM ('ok')",
        "CREATE ROLE readers", "CREATE ROLE alterers", "CREATE ROLE managers", "CREATE ROLE owners",
        "GRANT SELECT ON app.orders TO readers", "GRANT SELECT ON app.recent TO readers",
        "GRANT ALTER TABLE, ALTER VIEW ON SCHEMA app TO alterers",
        "GRANT DROP SEQUENCE, DROP MACRO ON SCHEMA app TO managers",
        "CREATE TABLE app.owned (v INT)",
        "CREATE USER rita", "CREATE USER alan", "CREATE USER mona", "CREATE USER otto",
        "GRANT readers TO rita", "GRANT alterers TO alan", "GRANT managers TO mona", "GRANT owners TO otto",
    ]:
        expect_allowed(client=admin, sql=sql)
    expect_allowed(client=admin, sql="ALTER TABLE app.owned OWNER TO ROLE owners")
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


def table_comment(admin, name):
    rows = admin.run(sql=f"SELECT comment FROM duckdb_tables() WHERE table_name = '{name}'").to_pylist()
    return rows[0]["comment"]


@pytest.mark.parametrize("sql, why", [
    ("COMMENT ON TABLE app.orders IS 'x'", "may not comment on table rbac_comments.app.orders"),
    ("COMMENT ON COLUMN app.orders.item IS 'x'", "may not comment on a column of rbac_comments.app.orders"),
    ("COMMENT ON INDEX app.orders_id IS 'x'", "may not comment on index rbac_comments.app.orders_id"),
    ("COMMENT ON VIEW app.recent IS 'x'", "may not comment on view rbac_comments.app.recent"),
    ("COMMENT ON COLUMN app.recent.id IS 'x'", "may not comment on a column of rbac_comments.app.recent"),
])
def test_tables_views_columns_and_indexes_need_alter(server, admin, sql, why):
    rita, alan = server.user(name="rita"), server.user(name="alan")
    error = expect_blocked(client=rita, sql=sql, why=why)
    assert ("ALTER VIEW" if "recent" in sql else "ALTER TABLE") in error.detail
    # Twin: a role granted ALTER TABLE / ALTER VIEW on the schema.
    expect_allowed(client=alan, sql=sql)


def test_the_comment_is_written(server, admin):
    alan = server.user(name="alan")
    expect_allowed(client=alan, sql="COMMENT ON TABLE app.orders IS 'what was ordered'")
    assert table_comment(admin=admin, name="orders") == "what was ordered"


def test_a_tables_owner_may_comment_on_it(server, admin):
    otto = server.user(name="otto")
    expect_allowed(client=otto, sql="COMMENT ON TABLE app.owned IS 'mine'")
    expect_allowed(client=otto, sql="COMMENT ON COLUMN app.owned.v IS 'the value'")
    assert table_comment(admin=admin, name="owned") == "mine"
    # Not on a table it doesn't own.
    expect_blocked(client=otto, sql="COMMENT ON TABLE app.orders IS 'x'", why="may not comment on table")


@pytest.mark.parametrize("sql, why", [
    ("COMMENT ON SEQUENCE app.ids IS 'x'", "may not comment on sequence rbac_comments.app.ids (it needs DROP SEQUENCE"),
    ("COMMENT ON MACRO app.twice IS 'x'", "may not comment on macro rbac_comments.app.twice (it needs DROP MACRO"),
    ("COMMENT ON MACRO TABLE app.rows3 IS 'x'", "may not comment on macro rbac_comments.app.rows3 (it needs DROP MACRO"),
])
def test_sequences_and_macros_need_their_schema_privilege(server, admin, sql, why):
    alan, mona = server.user(name="alan"), server.user(name="mona")
    expect_blocked(client=alan, sql=sql, why=why)
    expect_allowed(client=mona, sql=sql)


def test_types_stay_the_administrators(server, admin):
    mona = server.user(name="mona")
    # A type: DROP TYPE on its schema (mona holds it on app, not main).
    expect_blocked(client=mona, sql="COMMENT ON TYPE mood IS 'x'", why="may not comment on type")
    expect_allowed(client=admin, sql="COMMENT ON TYPE mood IS 'x'")
    expect_allowed(client=admin, sql="GRANT DROP TYPE ON SCHEMA main TO managers")
    try:
        expect_allowed(client=mona, sql="COMMENT ON TYPE mood IS 'monas'")
    finally:
        expect_allowed(client=admin, sql="REVOKE DROP TYPE ON SCHEMA main FROM managers")
