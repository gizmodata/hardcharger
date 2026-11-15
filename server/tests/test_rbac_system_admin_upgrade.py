"""SYSTEM ADMIN and the access-control store: a store a 2.13.0 server made is upgraded in
place when a server opens it; the administrator is the way back in when the store's SYSTEM
ADMIN grants are missing or damaged; and every replica on the store asks it.

The 2.13.0 stores are the ones a 2.13.0 server wrote (fixtures/rbac_store_2_13_0, with
the script that made them): a DuckDB file and a PostgreSQL schema, holding the same users,
roles and grants.
"""

import gzip
import pathlib
import time

import psycopg
import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, start_rbac_server

ELEVATE = "SET ROLE SYSTEM ADMIN"
NOT_HELD = "does not hold SYSTEM ADMIN"
LOST = "no longer holds SYSTEM ADMIN"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rbac_store_2_13_0"
ANN_PASSWORD = "ann-2-13-password"  # as fixtures/rbac_store_2_13_0/generate.py set it


def duckdb_store_of_2_13_0(directory: pathlib.Path) -> str:
    """A copy of the 2.13.0 DuckDB store, in a directory of its own: its store URI."""
    directory.mkdir(mode=0o700)
    file = directory / "rbac.duckdb"
    file.write_bytes(gzip.decompress(data=(FIXTURES / "rbac.duckdb.gz").read_bytes()))
    file.chmod(mode=0o600)
    return f"duckdb:{file}"


def postgres_store_of_2_13_0(pg_store) -> str:
    uri = pg_store.new_database()
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        connection.execute(query=(FIXTURES / "postgres.sql").read_text())
    return uri


def in_store(uri: str, query: str) -> list[tuple]:
    with psycopg.connect(conninfo=uri, autocommit=True) as connection:
        cursor = connection.execute(query=query)
        return cursor.fetchall() if cursor.description else []


def rows(client, sql: str) -> list[dict]:
    return expect_allowed(client=client, sql=sql).to_pylist()


def holders(admin) -> list[str]:
    return [r["grantee"] for r in rows(client=admin, sql="SELECT grantee FROM hardcharger_grants() "
                                                         "WHERE privilege = 'SYSTEM ADMIN' ORDER BY grantee")]


def wait_for(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail(reason=f"timed out waiting for {what}")


@pytest.mark.parametrize("kind", ["duckdb", "postgres"])
def test_a_2_13_0_store_is_upgraded_in_place(launcher, request, tmp_path, kind):
    uri = (duckdb_store_of_2_13_0(directory=tmp_path / "store") if kind == "duckdb"
           else postgres_store_of_2_13_0(pg_store=request.getfixturevalue("pg_store")))
    if kind == "postgres":
        assert in_store(uri=uri, query="SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                                       "'hardcharger_rbac' AND table_name = 'system_admins'") == [(0,)]
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    admin = server.admin()
    expect_allowed(client=admin, sql="CREATE TABLE ledger AS SELECT 1 AS id")
    expect_allowed(client=admin, sql="CREATE TABLE vault AS SELECT 42 AS secret")

    # What 2.13.0 stored is all there, and means what it meant.
    assert {r["name"] for r in rows(client=admin, sql="SELECT name FROM hardcharger_users()")} == \
        {"ann", "bob", "cleo", "dora", "acme/amy"}
    ann = server.user(name="ann", password=ANN_PASSWORD)
    assert rows(client=ann, sql="SELECT id FROM ledger") == [{"id": 1}]  # analysts: SELECT ON SCHEMA main
    members = {(r["role"], r["member"], r["grantor"])
               for r in rows(client=admin, sql="SELECT role, member, grantor FROM hardcharger_role_members()")}
    assert members == {("analysts", "ann", "administrator"), ("sysadmin", "bob", "administrator"),
                       ("SYSTEM ADMIN", "cleo", "administrator"), ("sysadmin", "dora", "bob"),
                       ("sysadmin", "ops", None)}, members

    # Nobody holds SYSTEM ADMIN: not the members of the role called sysadmin, nor of the
    # one called "SYSTEM ADMIN", which stay the ordinary roles they were.
    assert holders(admin=admin) == []
    sessions = {name: server.user(name=name) for name in ("bob", "cleo", "dora", "acme/amy")} | {"ann": ann}
    for name, session in sessions.items():
        expect_blocked(client=session, sql=ELEVATE,
                       why="belongs to a tenant" if name == "acme/amy" else f"user {name} " + NOT_HELD)
        if name != "ann":  # (ann reads the whole schema)
            expect_blocked(client=session, sql="SELECT * FROM vault",
                           why="cannot see" if name == "acme/amy" else "no SELECT privilege")
        expect_blocked(client=session, sql="CREATE USER made_before_the_grant",
                       why="only administrators manage users and roles")
    expect_allowed(client=sessions["bob"], sql="SET ROLE sysadmin")
    expect_allowed(client=sessions["cleo"], sql='SET ROLE "SYSTEM ADMIN"')
    for name in ("bob", "cleo"):
        assert rows(client=sessions[name], sql="SELECT HARDCHARGER_IS_SYSTEM_ADMIN() AS a") == [{"a": False}]
        expect_blocked(client=sessions[name], sql="CREATE USER made_by_a_role",
                       why="only administrators manage users and roles")
        expect_allowed(client=sessions[name], sql="RESET ROLE")
    # bob still administers the ordinary role sysadmin (WITH ADMIN OPTION).
    expect_allowed(client=sessions["bob"], sql="GRANT sysadmin TO cleo")
    expect_allowed(client=sessions["bob"], sql="REVOKE sysadmin FROM cleo")
    # A name the earlier version allowed is not given out again.
    with pytest.raises(ChargewireError) as taken:
        admin.run(sql='CREATE USER "system admin"')
    assert "SYSTEM ADMIN is the name of the System Admin capability" in taken.value.detail

    # The capability is there to grant: to a user...
    assert rows(client=admin, sql="GRANT SYSTEM ADMIN TO ann") == [{"result": "Granted SYSTEM ADMIN to ann"}]
    expect_allowed(client=ann, sql=ELEVATE)
    assert rows(client=ann, sql="SELECT secret FROM vault") == [{"secret": 42}]
    # ...and to the role called sysadmin, which then carries it to the member the
    # administrator gave it (bob) and to the mapped group, and not to the member bob gave
    # it under 2.13.0 (dora).
    expect_allowed(client=ann, sql="GRANT SYSTEM ADMIN TO ROLE sysadmin")
    expect_allowed(client=sessions["bob"], sql=ELEVATE)
    expect_blocked(client=sessions["dora"], sql=ELEVATE, why="user dora " + NOT_HELD)
    expect_allowed(client=server.user(name="cleo", idp={"iss": "https://idp.example.test", "groups": ["ops"]}),
                   sql=ELEVATE)
    expect_blocked(client=sessions["cleo"], sql=ELEVATE, why="user cleo " + NOT_HELD)
    # bob's admin option on it no longer gives the role away (as himself).
    expect_allowed(client=sessions["bob"], sql="RESET ROLE")
    expect_blocked(client=sessions["bob"], sql="GRANT sysadmin TO cleo", why="role sysadmin holds SYSTEM ADMIN")
    assert holders(admin=admin) == ["ann", "sysadmin"]

    if kind == "postgres":
        # In place: the table is new, the schema's version is the one 2.13.0 opens.
        assert in_store(uri=uri, query="SELECT principal_kind, granted_by <> '' FROM hardcharger_rbac.system_admins "
                                       "ORDER BY principal_kind") == [("role", True), ("user", False)]
        assert in_store(uri=uri, query="SELECT value FROM hardcharger_rbac.meta WHERE key = 'schema_version'") == [("1",)]
    # And it is the store's: there after a restart.
    server.running.stop()
    restarted = start_rbac_server(launcher=launcher, store_uri=uri)
    assert holders(admin=restarted.admin()) == ["ann", "sysadmin"]
    again = restarted.user(name="ann", password=ANN_PASSWORD)
    expect_blocked(client=again, sql="CREATE USER made_before_asking", why="only administrators manage users and roles")
    expect_allowed(client=again, sql=ELEVATE)
    expect_allowed(client=again, sql="CREATE USER made_after_the_upgrade")


@pytest.mark.parametrize("kind", ["duckdb", "postgres"])
def test_a_read_only_check_of_a_2_13_0_store_leaves_it_as_it_is(request, tmp_path, kind):
    """hardcharger-server --verify-credentials opens the store read-only: a store without the
    SYSTEM ADMIN table is read as it is (nobody holds it), and not upgraded."""
    from test_verify_credentials import fingerprint, password, verify
    uri = (duckdb_store_of_2_13_0(directory=tmp_path / "store") if kind == "duckdb"
           else postgres_store_of_2_13_0(pg_store=request.getfixturevalue("pg_store")))
    before = fingerprint(store_uri=uri)
    env = {"HARDCHARGER_RBAC_STORE_URI": uri, "HARDCHARGER_SECRET_KEY": "a-secret-key-for-the-read-only-check-0123"}
    code, answer, log = verify(credential=password(username="ann", password=ANN_PASSWORD), env=env,
                               secrets=(ANN_PASSWORD,))
    assert code == 0 and answer == {"verified": True, "user": "ann", "role": "user", "method": "password",
                                    "would_provision": False}, (answer, log)
    assert "SYSTEM ADMIN" not in log, log  # a store that predates it is no damage to report
    code, answer, log = verify(credential=password(username="ann", password="not-her-password"), env=env)
    assert code == 1 and answer["reason"] == "invalid_credentials", (answer, log)
    assert fingerprint(store_uri=uri) == before, "the read-only check changed the store"
    if kind == "postgres":
        assert in_store(uri=uri, query="SELECT count(*) FROM information_schema.tables WHERE table_schema = "
                                       "'hardcharger_rbac' AND table_name = 'system_admins'") == [(0,)]


def test_the_administrator_is_the_way_in_when_the_grants_are_missing_or_damaged(launcher, pg_store):
    uri = pg_store.new_database()
    server = start_rbac_server(launcher=launcher, store_uri=uri)
    admin = server.admin()
    for sql in ["CREATE USER alice", "CREATE USER bob", "CREATE ROLE dba", "GRANT dba TO bob",
                "GRANT SYSTEM ADMIN TO alice", "GRANT SYSTEM ADMIN TO ROLE dba", "CREATE TENANT acme",
                "CREATE USER acme/tom", "CREATE ROLE acme/staff", "GRANT acme/staff TO acme/tom"]:
        expect_allowed(client=admin, sql=sql)
    expect_allowed(client=server.user(name="alice"), sql=ELEVATE)
    server.running.stop()

    def restart():
        started = start_rbac_server(launcher=launcher, store_uri=uri)
        return started, started.admin()

    def administrator_works(admin, n: int) -> None:
        for sql in [f"CREATE USER probe_{n} WITH PASSWORD 'probe-{n}-password'", f"CREATE ROLE probe_role_{n}",
                    f"GRANT probe_role_{n} TO probe_{n}", f"CREATE TABLE probe_table_{n} AS SELECT {n} AS id",
                    f"GRANT SELECT ON probe_table_{n} TO probe_role_{n}", f"DROP USER probe_{n}",
                    f"DROP ROLE probe_role_{n}", "SHOW GRANTS FOR USER alice"]:
            expect_allowed(client=admin, sql=sql)

    # 1. Rows that name nobody, or somebody who may never hold it: each counts for nothing.
    tom, = in_store(uri=uri, query="SELECT user_id FROM hardcharger_rbac.users WHERE name = 'acme/tom'")[0]
    staff, tenant_admin = (in_store(uri=uri, query=f"SELECT role_id FROM hardcharger_rbac.roles WHERE name = '{n}'")[0][0]
                           for n in ("acme/staff", "acme/tenant_admin"))
    in_store(uri=uri, query="INSERT INTO hardcharger_rbac.system_admins VALUES "
                            "('user', '00000000-0000-4000-8000-000000000000', ''), ('robot', 'public', ''), "
                            f"('role', 'public', ''), ('user', '{tom}', ''), ('role', '{staff}', ''), "
                            f"('role', '{tenant_admin}', ''), ('user', '', ''), ('role', '\x1dsystem-admin', '')")
    server, admin = restart()
    assert holders(admin=admin) == ["alice", "dba"]  # the two real ones, and none of the rows above
    expect_allowed(client=admin, sql="CREATE USER carl")  # PUBLIC's only
    for name, why in (("carl", "user carl " + NOT_HELD), ("acme/tom", "belongs to a tenant")):
        expect_blocked(client=server.user(name=name), sql=ELEVATE, why=why)
    expect_allowed(client=server.user(name="bob"), sql=ELEVATE)  # the twin: a real grant still counts
    administrator_works(admin=admin, n=1)
    server.running.stop()

    # 2. The table in a shape this server does not read: the server starts, says so, and
    # nobody holds SYSTEM ADMIN; everything else, the administrator's included, goes on.
    in_store(uri=uri, query="ALTER TABLE hardcharger_rbac.system_admins RENAME TO system_admins_kept")
    in_store(uri=uri, query="CREATE TABLE hardcharger_rbac.system_admins (principal_kind INT, note VARCHAR)")
    in_store(uri=uri, query="INSERT INTO hardcharger_rbac.system_admins VALUES (1, 'alice')")
    server, admin = restart()
    assert "the access control store's SYSTEM ADMIN grants can't be read: nobody holds SYSTEM ADMIN" in server.log()
    assert holders(admin=admin) == []
    for name in ("alice", "bob"):
        session = server.user(name=name)
        expect_blocked(client=session, sql=ELEVATE, why=f"user {name} " + NOT_HELD)
        assert rows(client=session, sql="SELECT 1 AS one") == [{"one": 1}]
    administrator_works(admin=admin, n=2)
    with pytest.raises(ChargewireError) as grant:
        admin.run(sql="GRANT SYSTEM ADMIN TO alice")
    assert "SYSTEM ADMIN can't be granted" in grant.value.detail and "system_admins" in grant.value.detail
    assert rows(client=admin, sql="REVOKE SYSTEM ADMIN FROM alice") == [{"result": "alice does not hold SYSTEM ADMIN"}]
    server.running.stop()

    # 3. No table at all (as in a store an earlier version made): the next start creates
    # it, empty, and the administrator grants again.
    in_store(uri=uri, query="DROP TABLE hardcharger_rbac.system_admins")
    server, admin = restart()
    assert "SYSTEM ADMIN grants can't be read" not in server.log()
    assert holders(admin=admin) == []
    alice = server.user(name="alice")
    expect_blocked(client=alice, sql=ELEVATE, why="user alice " + NOT_HELD)
    administrator_works(admin=admin, n=3)
    expect_allowed(client=admin, sql="GRANT SYSTEM ADMIN TO alice")
    expect_allowed(client=alice, sql=ELEVATE)
    assert in_store(uri=uri, query="SELECT principal_kind FROM hardcharger_rbac.system_admins") == [("user",)]


def test_every_replica_asks_the_store(launcher, pg_store):
    uri = pg_store.new_database()
    first, second = (start_rbac_server(launcher=launcher, store_uri=uri,
                                       env={"HARDCHARGER_LOG_FORMAT": "json"}) for _ in range(2))
    admin = first.admin()
    for sql in ["CREATE USER alice", "CREATE USER carol", "CREATE ROLE dba", "GRANT dba TO carol"]:
        expect_allowed(client=admin, sql=sql)
    for server in (first, second):  # each replica has its own memory database
        expect_allowed(client=server.admin(), sql="CREATE TABLE vault AS SELECT 42 AS secret")
    wait_for(condition=lambda: {"alice", "carol"} <= {r["name"] for r in rows(client=second.admin(),
                                                                              sql="SELECT name FROM hardcharger_users()")},
             timeout=15, what="the users to reach the second replica")
    alice, carol = second.user(name="alice"), second.user(name="carol")
    for session in (alice, carol):
        expect_blocked(client=session, sql=ELEVATE, why=NOT_HELD)

    # Granted on one replica, asked for on the other at once: SET ROLE SYSTEM ADMIN reads
    # the store, not what this replica last polled.
    expect_allowed(client=admin, sql="GRANT SYSTEM ADMIN TO alice")
    expect_allowed(client=admin, sql="GRANT SYSTEM ADMIN TO ROLE dba")
    for session in (alice, carol):
        expect_allowed(client=session, sql=ELEVATE)
        assert rows(client=session, sql="SELECT secret FROM vault") == [{"secret": 42}]
        expect_allowed(client=session, sql="BEGIN")
        expect_allowed(client=session, sql="INSERT INTO vault VALUES (7)")
    read_vault = {session: session.prepare(sql="SELECT max(secret) AS m FROM vault") for session in (alice, carol)}

    # Taken away on the first replica: from the user, and by taking the user out of the role.
    expect_allowed(client=admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    expect_allowed(client=admin, sql="REVOKE dba FROM carol")
    for session, name in ((alice, "alice"), (carol, "carol")):
        # At once, whatever this replica has polled: nothing it changes in the store goes
        # through (the change's own transaction reads the store), and it cannot ask again.
        for sql in ("CREATE USER made_on_the_second_replica", f"GRANT SYSTEM ADMIN TO {name}", "GRANT dba TO carol"):
            expect_blocked(client=session, sql=sql, why=f"user {name} " + LOST)
        expect_blocked(client=second.user(name=name), sql=ELEVATE, why=f"user {name} " + NOT_HELD)

        # Within the poll interval, every statement of the open session.
        def refused(session=session, name=name) -> bool:
            try:
                session.run(sql="SELECT secret FROM vault")
                return False
            except ChargewireError as error:
                assert f"user {name} " + LOST in error.detail, error
                return True
        wait_for(condition=refused, timeout=15, what=f"the revoke to reach {name}'s session on the second replica")
        with pytest.raises(ChargewireError) as prepared:
            session.execute_prepared(prepared_id=read_vault[session])
        assert f"user {name} " + LOST in prepared.value.detail, prepared.value
        expect_blocked(client=session, sql="COMMIT", why=LOST)
        expect_allowed(client=session, sql="RESET ROLE")
        expect_blocked(client=session, sql="SELECT secret FROM vault", why=f"user {name} has no SELECT privilege")
        expect_allowed(client=session, sql="ROLLBACK")
    assert rows(client=second.admin(), sql="SELECT secret FROM vault") == [{"secret": 42}]
    assert "made_on_the_second_replica" not in {r["name"] for r in rows(client=admin, sql="SELECT name FROM hardcharger_users()")}
    # The second replica recorded each session's end as System Admin, with its user.
    ended = [r for r in second.running.log_records() if r.get("message") == "System Admin elevation ended"]
    assert sorted((r["user"], r["reason"], r["session_id"]) for r in ended) == \
        [("alice", "SYSTEM ADMIN no longer held", alice.session_id),
         ("carol", "SYSTEM ADMIN no longer held", carol.session_id)]
    assert rows(client=second.admin(), sql="SELECT count(*) AS n FROM hardcharger_sessions() WHERE system_admin") == [{"n": 0}]
