"""ANALYZE and VACUUM under access control: the MAINTAIN privilege.

MAINTAIN, as in PostgreSQL 17, is granted ON TABLE, ON SCHEMA or ON CATALOG; a table's owner
holds it. It lets a role run ANALYZE and VACUUM of the table, nothing else: no SELECT, no
writes. ANALYZE or VACUUM of every table at once stays the administrator's, since it would
touch tables the user can't see. Every refusal is asserted by its reason, next to the twin
that succeeds."""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

NO_MAINTAIN = "has no MAINTAIN privilege on table"
EVERY_TABLE = "ANALYZE or VACUUM of every table needs an administrator"


@pytest.fixture(scope="module")
def server(module_launcher, store, tmp_path_factory):
    database = tmp_path_factory.mktemp("db") / "rbac_maintain.duckdb"
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(database)])
    admin = server.admin()
    for sql in [
        "CREATE SCHEMA app", "CREATE SCHEMA other",
        "CREATE TABLE app.events (id INT, kind VARCHAR)", "INSERT INTO app.events VALUES (1, 'a'), (2, 'b')",
        "CREATE TABLE app.owned (v INT)", "CREATE TABLE other.t (v INT)",
        "CREATE ROLE readers", "CREATE ROLE maintainers", "CREATE ROLE schema_maintainers", "CREATE ROLE owners",
        "GRANT SELECT ON app.events TO readers",
        "GRANT MAINTAIN ON app.events TO maintainers",
        "GRANT MAINTAIN ON SCHEMA app TO schema_maintainers",
        "CREATE USER rae", "CREATE USER max", "CREATE USER sue", "CREATE USER oz",
        "GRANT readers TO rae", "GRANT maintainers TO max", "GRANT schema_maintainers TO sue", "GRANT owners TO oz",
    ]:
        expect_allowed(client=admin, sql=sql)
    expect_allowed(client=admin, sql="ALTER TABLE app.owned OWNER TO ROLE owners")
    server.admin_client = admin
    return server


@pytest.fixture
def admin(server):
    return server.admin_client


@pytest.mark.parametrize("sql", ["ANALYZE app.events", "ANALYZE app.events (id)", "VACUUM app.events",
                                 "VACUUM ANALYZE app.events"])
def test_analyze_and_vacuum_need_maintain(server, sql):
    rae, max_ = server.user(name="rae"), server.user(name="max")
    # SELECT on the table is not enough.
    expect_blocked(client=rae, sql=sql, why=NO_MAINTAIN + " rbac_maintain.app.events")
    # Twin: MAINTAIN on the table, with no other privilege on it.
    expect_allowed(client=max_, sql=sql)


def test_maintain_grants_nothing_else(server):
    max_ = server.user(name="max")
    expect_blocked(client=max_, sql="SELECT * FROM app.events", why="no SELECT privilege")
    expect_blocked(client=max_, sql="INSERT INTO app.events VALUES (3, 'c')", why="no INSERT privilege")


def test_maintain_on_a_schema_and_ownership(server):
    sue, oz = server.user(name="sue"), server.user(name="oz")
    expect_allowed(client=sue, sql="ANALYZE app.owned")
    expect_blocked(client=sue, sql="ANALYZE other.t", why=NO_MAINTAIN + " rbac_maintain.other.t")
    # A table's owner holds it.
    expect_allowed(client=oz, sql="VACUUM ANALYZE app.owned")
    expect_blocked(client=oz, sql="ANALYZE app.events", why=NO_MAINTAIN)


@pytest.mark.parametrize("sql", ["ANALYZE", "VACUUM", "VACUUM ANALYZE"])
def test_every_table_at_once_stays_the_administrators(server, admin, sql):
    for name in ["sue", "max"]:
        expect_blocked(client=server.user(name=name), sql=sql, why=EVERY_TABLE)
    expect_allowed(client=admin, sql=sql)


def test_the_grant(server, admin):
    grants = admin.run(sql="SHOW GRANTS FOR maintainers").to_pylist()
    assert ("MAINTAIN", "TABLE") in {(g["privilege"], g["object_type"]) for g in grants}
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="GRANT MAINTAIN ON SEQUENCE app.nothing TO maintainers")
    assert "on a sequence only USAGE is granted" in raised.value.detail
