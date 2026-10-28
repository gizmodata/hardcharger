"""GRANT ALL means every privilege that applies where it is granted: on a table its
SELECT, INSERT, UPDATE and DELETE; on a schema those and CREATE / DROP / ALTER TABLE and
VIEW; on a catalog all of those and CREATE / DROP SCHEMA. REVOKE ALL takes back the same."""

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture
def server(launcher, store):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    admin = server.admin()
    for sql in ["CREATE SCHEMA analytics", "CREATE TABLE analytics.orders (id INT)", "CREATE TABLE main.other (id INT)",
                "CREATE ROLE loader", "CREATE USER lou", "GRANT loader TO lou"]:
        expect_allowed(client=admin, sql=sql)
    return server


def kinds(admin, role: str) -> set[tuple[str, str]]:
    return {(row["privilege"], row["object_type"]) for row in admin.run(sql=f"SHOW GRANTS FOR {role}").to_pylist()}


SCHEMA_DDL = ["CREATE TABLE", "CREATE VIEW", "DROP TABLE", "DROP VIEW", "ALTER TABLE", "ALTER VIEW"]
DML = ["SELECT", "INSERT", "UPDATE", "DELETE"]


def test_all_on_a_schema_includes_its_ddl(server):
    admin = server.admin()
    expect_allowed(client=admin, sql="GRANT ALL PRIVILEGES ON SCHEMA analytics TO loader")
    assert kinds(admin=admin, role="loader") == {(p, "SCHEMA") for p in DML + SCHEMA_DDL}
    lou = server.user(name="lou")
    for sql in ["CREATE TABLE analytics.events (id INT)", "INSERT INTO analytics.orders VALUES (1)",
                "SELECT * FROM analytics.orders", "ALTER TABLE analytics.orders ADD COLUMN note VARCHAR",
                "CREATE VIEW analytics.v AS SELECT id FROM analytics.orders", "DROP VIEW analytics.v",
                "DROP TABLE analytics.events"]:
        expect_allowed(client=lou, sql=sql)
    # Only that schema, and no schema DDL (that is the catalog's).
    expect_blocked(client=lou, sql="CREATE TABLE main.mine (id INT)", why="no CREATE TABLE privilege")
    expect_blocked(client=lou, sql="CREATE SCHEMA lous", why="no CREATE SCHEMA privilege")
    expect_blocked(client=lou, sql="SELECT * FROM main.other", why="no SELECT privilege")


def test_all_on_a_catalog_includes_schema_ddl(server):
    admin = server.admin()
    catalog = admin.run(sql="SELECT current_database() AS c").column("c").to_pylist()[0]
    expect_allowed(client=admin, sql=f"GRANT ALL ON CATALOG {catalog} TO loader")
    assert kinds(admin=admin, role="loader") == {(p, "CATALOG") for p in DML + SCHEMA_DDL + ["CREATE SCHEMA", "DROP SCHEMA"]}
    lou = server.user(name="lou")
    for sql in ["CREATE SCHEMA lous", "CREATE TABLE lous.t (id INT)", "SELECT * FROM main.other", "DROP TABLE lous.t",
                "DROP SCHEMA lous"]:
        expect_allowed(client=lou, sql=sql)


def test_all_on_a_table_is_its_table_privileges(server):
    admin = server.admin()
    expect_allowed(client=admin, sql="GRANT ALL ON analytics.orders TO loader")
    assert kinds(admin=admin, role="loader") == {(p, "TABLE") for p in DML}
    lou = server.user(name="lou")
    expect_allowed(client=lou, sql="INSERT INTO analytics.orders VALUES (2)")
    expect_blocked(client=lou, sql="CREATE TABLE analytics.events (id INT)", why="no CREATE TABLE privilege")


def test_revoke_all_takes_back_everything_all_gave(server):
    admin = server.admin()
    expect_allowed(client=admin, sql="GRANT ALL ON SCHEMA analytics TO loader")
    expect_allowed(client=admin, sql="REVOKE ALL ON SCHEMA analytics FROM loader")
    assert kinds(admin=admin, role="loader") == set()
    expect_blocked(client=server.user(name="lou"), sql="CREATE TABLE analytics.events (id INT)",
                   why="no CREATE TABLE privilege")
