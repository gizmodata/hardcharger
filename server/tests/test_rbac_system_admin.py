"""SYSTEM ADMIN: a named user acts as the administrator, in a session that asks to.

GRANT SYSTEM ADMIN TO alice (or TO ROLE dba) lets the user say SET ROLE SYSTEM ADMIN in a
session, which then does what the administrator (the server's own admin user) does until
RESET ROLE. Holding it changes nothing by itself; only the administrator, or a user acting
as System Admin, grants and revokes it; the session is asked of the grants on every
statement; and what a session is limited to (a scoped token, a read-only connection) is
never widened by it. Each test gets its own server and store, so none leaves anything
behind for another.
"""

import subprocess
import time

import pytest

from conftest import CLIENT_BINARY, clean_environment
from rbac import ChargewireError, Client, expect_allowed, expect_blocked, mint, start_rbac_server

ELEVATE = "SET ROLE SYSTEM ADMIN"
# What every refusal of GRANT / REVOKE SYSTEM ADMIN, and of a change to what carries it, says.
ONLY = "only the administrator, or a user acting as System Admin (SET ROLE SYSTEM ADMIN)"
# What a session that asked for SYSTEM ADMIN is told once its user no longer holds it.
LOST = "no longer holds SYSTEM ADMIN, which SET ROLE made active. Run RESET ROLE to go on as "
NOT_HELD = "does not hold SYSTEM ADMIN"


class World:
    def __init__(self, server):
        self.server = server
        self.admin = server.admin()
        self.sessions: dict[str, Client] = {}

    def as_user(self, user: str) -> Client:
        if user not in self.sessions:
            self.sessions[user] = self.server.user(name=user)
        return self.sessions[user]

    def elevated(self, user: str) -> Client:
        """A new session of `user`, acting as System Admin."""
        client = self.server.user(name=user)
        expect_allowed(client=client, sql=ELEVATE)
        return client


@pytest.fixture
def world(launcher, store):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    world = World(server=server)
    for sql in [
        # ledger: readable by clerks, with a row policy and a mask; vault: by nobody.
        "CREATE TABLE ledger (id INT, region VARCHAR, card VARCHAR)",
        "INSERT INTO ledger VALUES (1, 'eu', '4111-1'), (2, 'us', '4111-2'), (3, 'us', '4111-3')",
        "CREATE TABLE vault AS SELECT 42 AS secret",
        "CREATE ROLE clerks", "GRANT SELECT, INSERT ON ledger TO clerks",
        "CREATE ROW POLICY eu_only ON ledger USING (region = 'eu')",
        "CREATE MASKING POLICY hide_card AS (v VARCHAR) RETURNS VARCHAR -> '****'",
        "ALTER TABLE ledger MODIFY COLUMN card SET MASKING POLICY hide_card",
        # alice holds SYSTEM ADMIN herself; carol through the role dba; bob does not.
        "CREATE USER alice", "CREATE USER bob", "CREATE USER carol", "CREATE ROLE dba",
        "GRANT clerks TO alice", "GRANT clerks TO bob", "GRANT clerks TO carol", "GRANT dba TO carol",
        "GRANT SYSTEM ADMIN TO alice", "GRANT SYSTEM ADMIN TO ROLE dba",
    ]:
        expect_allowed(client=world.admin, sql=sql)
    return world


def error_of(client, sql: str) -> str:
    """'' when the statement ran, else why not."""
    try:
        client.run(sql=sql)
        return ""
    except ChargewireError as error:
        return error.detail


def refused(client, sql: str, why: str, code: str = "sql_error", sqlstate: str | None = None) -> None:
    """The statement fails, not as a permission refusal: with `code`, `sqlstate` and `why`."""
    with pytest.raises(ChargewireError) as raised:
        client.run(sql=sql)
    error = raised.value
    assert (error.code, error.sqlstate) == (code, sqlstate), f"{sql}: {error} ({error.sqlstate})"
    assert why in error.detail, f"{sql}: expected '{why}' in: {error.detail}"


def rows(client, sql: str) -> list[dict]:
    return expect_allowed(client=client, sql=sql).to_pylist()


def one(client, sql: str):
    """The single value the statement returns."""
    table = expect_allowed(client=client, sql=sql)
    assert table.num_rows == 1 and table.num_columns == 1, table
    return table.column(0).to_pylist()[0]


def is_system_admin(client) -> bool:
    return one(client=client, sql="SELECT HARDCHARGER_IS_SYSTEM_ADMIN()")


def ledger(client) -> list[tuple]:
    return [(r["id"], r["card"]) for r in rows(client=client, sql="SELECT id, card FROM ledger ORDER BY id")]


EVERY_ROW = [(1, "4111-1"), (2, "4111-2"), (3, "4111-3")]
A_CLERKS_ROWS = [(1, "****")]


def assert_ordinary(client, user: str) -> None:
    """The session is the user it signed in as: its grants, the row policy, the mask and
    the administrator's statements all apply to it."""
    assert not is_system_admin(client=client)
    assert ledger(client=client) == A_CLERKS_ROWS
    expect_blocked(client=client, sql="SELECT * FROM vault", why=f"user {user} has no SELECT privilege on table")
    expect_blocked(client=client, sql="CREATE USER made_by_a_user",
                   why="only administrators manage users and roles on this server")
    expect_blocked(client=client, sql="CREATE ROLE made_by_a_user",
                   why="only administrators manage users and roles on this server")
    expect_blocked(client=client, sql="GRANT SELECT ON vault TO clerks", why=f"user {user} may not grant SELECT")
    expect_blocked(client=client, sql="CREATE ROW POLICY p ON vault USING (true)",
                   why="only administrators manage row and masking policies")
    expect_blocked(client=client, sql="CREATE TENANT made_by_a_user", why="only administrators manage tenants")
    expect_blocked(client=client, sql="ATTACH ':memory:' AS made_by_a_user",
                   why="needs the MANAGE CATALOGS system privilege")
    expect_blocked(client=client, sql="CREATE TABLE made_by_a_user (id INT)", why="has no CREATE TABLE privilege")
    with pytest.raises(ChargewireError) as kill:
        client.run(sql="KILL SESSION 's_nobody'")
    assert (kill.value.code, kill.value.detail) == ("permission_denied", "Only admin users can execute KILL SESSION")
    with pytest.raises(ChargewireError) as settings:
        client.run(sql="SELECT * FROM hardcharger_server_settings()")
    assert "Only unrestricted administrators can monitor SQL or view server configuration" in settings.value.detail
    with pytest.raises(ChargewireError) as global_setting:
        client.run(sql="SET GLOBAL hardcharger.query_timeout = 0")
    assert global_setting.value.detail == "Only admin users can SET GLOBAL hardcharger.query_timeout"


# What only the administrator runs: each must run for a session acting as System Admin.
ADMINISTRATORS_STATEMENTS = [
    "SELECT * FROM vault",
    "CREATE USER made_{n} WITH PASSWORD 'made-{n}-password'", "ALTER USER made_{n} DISABLE", "ALTER USER made_{n} ENABLE",
    "CREATE ROLE role_{n}", "GRANT role_{n} TO made_{n} WITH ADMIN OPTION", "GRANT SELECT ON vault TO role_{n}",
    "GRANT READ FILESYSTEM, MANAGE CONFIG TO role_{n} WITH ADMIN OPTION",
    "CREATE ROW POLICY policy_{n} ON vault USING (secret > 0)", "DROP ROW POLICY policy_{n} ON vault",
    "CREATE MASKING POLICY mask_{n} AS (v INT) RETURNS INT -> 0", "DROP MASKING POLICY mask_{n}",
    "CREATE TENANT tenant_{n}", "CREATE USER tenant_{n}/user", "DROP USER tenant_{n}/user", "DROP TENANT tenant_{n}",
    "CREATE GROUP MAPPING 'group-{n}' TO ROLE role_{n}", "DROP GROUP MAPPING 'group-{n}'",
    "CREATE SCOPED TOKEN token_{n} FOR USER made_{n} ALLOW SELECT", "DROP SCOPED TOKEN token_{n} FOR USER made_{n}",
    "SHOW GRANTS FOR USER bob", "SELECT * FROM hardcharger_effective_privileges('bob')",
    "CREATE TABLE table_{n} AS SELECT 1 AS id", "ALTER TABLE table_{n} OWNER TO bob", "DROP TABLE table_{n}",
    "ATTACH ':memory:' AS attached_{n}", "DETACH attached_{n}",
    "CREATE SECRET secret_{n} (TYPE s3, KEY_ID 'made-{n}-key', SECRET 'made-{n}-secret')", "DROP SECRET secret_{n}",
    "SELECT count(*) FROM duckdb_secrets()", "SELECT count(*) FROM read_csv('{csv}')", "CHECKPOINT",
    "SET GLOBAL hardcharger.query_timeout = 0", "SELECT * FROM hardcharger_server_settings()",
    "SELECT * FROM hardcharger_monitor_info()", "SET hardcharger.bypass_queue = true",
    "REVOKE role_{n} FROM made_{n}", "DROP ROLE role_{n}", "DROP USER made_{n}",
]


def test_a_holder_is_an_ordinary_user_until_it_asks_and_again_after(world, tmp_path):
    csv = tmp_path / "rows.csv"
    csv.write_text("a\n1\n")
    alice = world.as_user(user="alice")
    assert_ordinary(client=alice, user="alice")

    assert rows(client=alice, sql=ELEVATE) == [
        {"result": "Active role: SYSTEM ADMIN (this session acts as the administrator until RESET ROLE)"}]
    assert is_system_admin(client=alice)
    assert one(client=alice, sql="SELECT HARDCHARGER_IS_FULL_ADMIN()") is True
    # Grants, the row policy and the mask no longer apply, as they don't to the administrator.
    assert ledger(client=alice) == EVERY_ROW == ledger(client=world.admin)
    # Everything the administrator runs, the session runs; and the administrator still does.
    for who, client in (("alice", alice), ("admin", world.admin)):
        for sql in ADMINISTRATORS_STATEMENTS:
            expect_allowed(client=client, sql=sql.format(n=who, csv=csv))
    # Another session of the same user, at the same time, is the ordinary user.
    assert_ordinary(client=world.server.user(name="alice"), user="alice")

    expect_allowed(client=alice, sql="SET GLOBAL hardcharger.query_timeout = 0")
    assert rows(client=alice, sql="RESET ROLE") == [{"result": "All of alice's roles are active"}]
    assert_ordinary(client=alice, user="alice")
    # What the session set as System Admin stays set, and stops counting: only an
    # administrator's statements skip the statement queue.
    with pytest.raises(ChargewireError) as bypass:
        alice.run(sql="SET hardcharger.bypass_queue = true")
    assert bypass.value.detail == "Only admin users may set hardcharger.bypass_queue = true"


@pytest.mark.parametrize("back", ["RESET ROLE", "SET ROLE ALL", "SET ROLE NONE", "SET ROLE clerks"])
def test_every_way_out_of_set_role_drops_it(world, back):
    alice = world.elevated(user="alice")
    assert ledger(client=alice) == EVERY_ROW
    expect_allowed(client=alice, sql=back)
    assert_ordinary(client=alice, user="alice")
    # And back in, in the same session.
    expect_allowed(client=alice, sql=ELEVATE)
    assert ledger(client=alice) == EVERY_ROW


def test_what_it_creates_is_the_administrators_not_the_users(world):
    alice = world.elevated(user="alice")
    for sql in ["CREATE TABLE made_as_admin AS SELECT 1 AS id", "CREATE VIEW seen_as_admin AS SELECT secret FROM vault",
                "GRANT SELECT ON seen_as_admin TO clerks", "CREATE ROLE made_role", "GRANT made_role TO bob"]:
        expect_allowed(client=alice, sql=sql)
    owned = rows(client=world.admin, sql="SELECT grantee, object_name FROM hardcharger_grants() "
                                         "WHERE privilege = 'OWNERSHIP'")
    assert owned == [], owned  # the system's, as what the administrator creates
    # Its grants are the administrator's: they stand without her, and name no user.
    assert rows(client=world.admin, sql="SELECT grantor FROM hardcharger_role_members() WHERE role = 'made_role'") == \
        [{"grantor": "administrator"}]
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    assert one(client=world.as_user(user="bob"), sql="SELECT secret FROM seen_as_admin") == 42
    assert "made_role" in {r["name"] for r in rows(client=world.as_user(user="bob"), sql="SELECT name FROM hardcharger_roles()")}
    # Back as herself, she holds nothing on what she made as System Admin.
    expect_allowed(client=alice, sql="RESET ROLE")
    expect_blocked(client=alice, sql="SELECT * FROM made_as_admin", why="user alice has no SELECT privilege on table")
    expect_blocked(client=alice, sql="DROP TABLE made_as_admin", why="user alice may not drop table")


# ---- Who may ask ----

def test_a_user_who_does_not_hold_it_cannot_ask(world):
    bob = world.as_user(user="bob")
    expect_blocked(client=bob, sql=ELEVATE, why="Hardcharger blocked SET ROLE SYSTEM ADMIN: user bob " + NOT_HELD)
    assert_ordinary(client=bob, user="bob")  # a refused SET ROLE changes nothing
    # It is no role: SET ROLE with a quoted name asks for a role called that, which nobody holds.
    expect_blocked(client=world.as_user(user="alice"), sql='SET ROLE "SYSTEM ADMIN"',
                   why="user alice does not hold role SYSTEM ADMIN")
    # The administrator has nothing to ask for.
    expect_blocked(client=world.admin, sql=ELEVATE, why="the administrator holds every right already")
    # Positive twins: the user granted it, and the member of the role granted it.
    assert is_system_admin(client=world.elevated(user="alice"))
    assert is_system_admin(client=world.elevated(user="carol"))


def test_it_is_per_session_and_never_carried_to_another(world):
    alice = world.elevated(user="alice")
    token = alice.token
    # A new session with the same token, as a client makes after losing one, starts as the user.
    again = world.server.client(token=token)
    assert_ordinary(client=again, user="alice")
    alice.close()
    replacement = world.server.client(token=token)
    assert_ordinary(client=replacement, user="alice")
    # A client that replays what it had said is asked like the first time: allowed while
    # the user holds it...
    expect_allowed(client=replacement, sql=ELEVATE)
    assert is_system_admin(client=replacement)
    replacement.close()
    # ...and refused once it does not.
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    replayed = world.server.client(token=token)
    expect_blocked(client=replayed, sql=ELEVATE, why="user alice " + NOT_HELD)
    assert_ordinary(client=replayed, user="alice")


def test_a_prepared_or_batched_set_role_is_asked_when_it_runs(world):
    alice = world.as_user(user="alice")
    prepared = alice.prepare(sql=ELEVATE)
    assert not is_system_admin(client=alice)  # preparing it elevates nothing
    alice.execute_prepared(prepared_id=prepared)
    assert is_system_admin(client=alice)
    expect_allowed(client=alice, sql="RESET ROLE")
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    with pytest.raises(ChargewireError) as raised:
        alice.execute_prepared(prepared_id=prepared)
    assert raised.value.code == "permission_denied" and "user alice " + NOT_HELD in raised.value.detail
    assert not is_system_admin(client=alice)
    # Not in a batch with what it would open.
    carol = world.as_user(user="carol")
    refused(client=carol, sql=ELEVATE + "; SELECT * FROM vault", sqlstate="42601",
            why="unexpected 'SELECT' after the end of the statement. Access-control statements run one at a time")
    # (After another statement, the batch is DuckDB's to read, and it has no such statement.)
    refused(client=carol, sql="SELECT 1; " + ELEVATE, sqlstate="42601", why='syntax error at or near "SYSTEM"')
    assert not is_system_admin(client=carol)


# ---- Granting ----

def test_only_the_administrator_or_a_system_admin_grants_and_revokes(world):
    alice, bob = world.as_user(user="alice"), world.as_user(user="bob")
    grant = "Hardcharger blocked GRANT SYSTEM ADMIN: " + ONLY + " grants and revokes SYSTEM ADMIN"
    revoke = "Hardcharger blocked REVOKE SYSTEM ADMIN: " + ONLY + " grants and revokes SYSTEM ADMIN"
    # Not a user without it, not a holder who has not asked, for themselves or anyone.
    for client in (bob, alice):
        for target in ("bob", "alice", "ROLE clerks"):
            expect_blocked(client=client, sql=f"GRANT SYSTEM ADMIN TO {target}", why=grant)
            expect_blocked(client=client, sql=f"REVOKE SYSTEM ADMIN FROM {target}", why=revoke)
    expect_blocked(client=bob, sql=ELEVATE, why=NOT_HELD)
    assert is_system_admin(client=world.elevated(user="alice")), "a refused REVOKE took it away"
    # Acting as System Admin, alice grants it, and takes it away.
    expect_allowed(client=alice, sql=ELEVATE)
    assert rows(client=alice, sql="GRANT SYSTEM ADMIN TO bob") == [{"result": "Granted SYSTEM ADMIN to bob"}]
    assert rows(client=alice, sql="GRANT SYSTEM ADMIN TO bob") == [{"result": "bob already holds SYSTEM ADMIN"}]
    expect_allowed(client=bob, sql=ELEVATE)
    # SHOW GRANTS names who granted it.
    assert rows(client=world.admin, sql="SHOW GRANTS FOR USER bob")[0] == {
        "privilege": "SYSTEM ADMIN", "object_type": "SERVER", "object": "*", "columns": "", "granted_to": "bob",
        "grantee_type": "USER", "grant_option": "NO", "granted_by": "alice"}
    # It stands without its grantor: what a System Admin does is the administrator's doing.
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    assert is_system_admin(client=bob)
    assert rows(client=bob, sql="REVOKE SYSTEM ADMIN FROM bob, alice") == [
        {"result": "Revoked SYSTEM ADMIN from bob; alice does not hold SYSTEM ADMIN"}]
    expect_blocked(client=bob, sql="SELECT 1", why="user bob " + LOST + "bob")


def test_no_privilege_option_or_delegation_gives_it(world):
    # bob gets everything that can be granted and passed on.
    for sql in ["GRANT ALL PRIVILEGES ON CATALOG memory TO clerks WITH GRANT OPTION",
                "GRANT MANAGE CATALOGS, MANAGE EXTENSIONS, MANAGE SECRETS, MANAGE CONFIG, READ FILESYSTEM, "
                "WRITE FILESYSTEM, EXPORT DATABASE, IMPORT DATABASE, CHECKPOINT, READ OBJECT STORAGE, "
                "WRITE OBJECT STORAGE, READ HTTP, WRITE HTTP TO clerks WITH ADMIN OPTION",
                "GRANT clerks TO bob WITH ADMIN OPTION", "ALTER TABLE vault OWNER TO bob"]:
        expect_allowed(client=world.admin, sql=sql)
    bob = world.as_user(user="bob")
    assert one(client=bob, sql="SELECT secret FROM vault") == 42  # he does hold them
    expect_blocked(client=bob, sql=ELEVATE, why="user bob " + NOT_HELD)
    expect_blocked(client=bob, sql="GRANT SYSTEM ADMIN TO bob", why=ONLY)
    expect_blocked(client=bob, sql="GRANT SYSTEM ADMIN TO ROLE clerks", why=ONLY)
    held = rows(client=world.admin, sql="SELECT grantee FROM hardcharger_grants() WHERE privilege = 'SYSTEM ADMIN' "
                                        "ORDER BY grantee")
    assert held == [{"grantee": "alice"}, {"grantee": "dba"}], held
    # A tenant admin manages its tenant's users and roles, and nothing of this.
    for sql in ["CREATE TENANT acme", "CREATE USER acme/tina", "CREATE USER acme/tom", "CREATE ROLE acme/staff",
                "GRANT acme/tenant_admin TO acme/tina"]:
        expect_allowed(client=world.admin, sql=sql)
    tina = world.as_user(user="acme/tina")
    expect_allowed(client=tina, sql="CREATE ROLE acme/made_by_tina")  # she does manage her tenant
    for sql in ["GRANT SYSTEM ADMIN TO acme/tina", "GRANT SYSTEM ADMIN TO ROLE acme/made_by_tina",
                "GRANT SYSTEM ADMIN TO bob"]:
        expect_blocked(client=tina, sql=sql, why=ONLY)
    expect_blocked(client=tina, sql=ELEVATE, why="user acme/tina belongs to a tenant, and a tenant's user never acts "
                                                 "as System Admin")
    # The administrator itself cannot put it inside a tenant, or in everyone's hands.
    refused(client=world.admin, sql="GRANT SYSTEM ADMIN TO acme/tom",
            why="SYSTEM ADMIN reaches the whole server; a tenant's user (acme/tom) cannot hold it")
    refused(client=world.admin, sql="GRANT SYSTEM ADMIN TO ROLE acme/staff",
            why="SYSTEM ADMIN reaches the whole server; a tenant's role (acme/staff) cannot hold it")
    refused(client=world.admin, sql="GRANT SYSTEM ADMIN TO PUBLIC",
            why="SYSTEM ADMIN cannot be granted to the built-in role PUBLIC: every user holds it")
    expect_blocked(client=world.as_user(user="acme/tom"), sql=ELEVATE, why="belongs to a tenant")


@pytest.mark.parametrize(("sql", "why"), [
    ("GRANT SYSTEM ADMIN TO bob WITH ADMIN OPTION", "SYSTEM ADMIN has no ADMIN OPTION or GRANT OPTION"),
    ("GRANT SYSTEM ADMIN TO bob WITH GRANT OPTION", "SYSTEM ADMIN has no ADMIN OPTION or GRANT OPTION"),
    ("REVOKE ADMIN OPTION FOR SYSTEM ADMIN FROM alice", "SYSTEM ADMIN has no ADMIN OPTION or GRANT OPTION to revoke"),
    ("REVOKE GRANT OPTION FOR SYSTEM ADMIN FROM alice", "SYSTEM ADMIN has no ADMIN OPTION or GRANT OPTION to revoke"),
    ("GRANT SYSTEM ADMIN ON CATALOG memory TO bob", "SYSTEM ADMIN applies to the whole server: no ON clause"),
    ("GRANT SYSTEM ADMIN, CHECKPOINT TO clerks", "SYSTEM ADMIN is granted and revoked on its own"),
    ("GRANT CHECKPOINT, SYSTEM ADMIN TO clerks", "SYSTEM ADMIN is granted and revoked on its own"),
    ("GRANT clerks, SYSTEM ADMIN TO bob", "SYSTEM ADMIN is granted and revoked on its own"),
    ("GRANT SELECT, SYSTEM ADMIN ON vault TO clerks", "SYSTEM ADMIN is granted and revoked on its own"),
    ("REVOKE SYSTEM ADMIN FROM alice CASCADE", "unexpected 'CASCADE' after the end of the statement"),
    ("SET ROLE SYSTEM ADMIN NOW", "unexpected 'NOW' after the end of the statement"),
    ("GRANT SYSTEM ADMIN TO", "expected a user or role name"),
])
def test_grammar_refuses(world, sql, why):
    refused(client=world.admin, sql=sql, why=why, sqlstate="42601")
    expect_blocked(client=world.as_user(user="bob"), sql=ELEVATE, why=NOT_HELD)  # nothing was granted


def test_grammar_and_names(world):
    admin = world.admin
    for sql, said in [("GRANT SYSTEM ADMIN TO USER bob", "Granted SYSTEM ADMIN to bob"),
                      ("grant system admin to role clerks", "Granted SYSTEM ADMIN to clerks"),
                      ("/* twice */ GRANT SYSTEM   ADMIN TO bob, ROLE clerks",
                       "bob already holds SYSTEM ADMIN; clerks already holds SYSTEM ADMIN"),
                      ("REVOKE SYSTEM ADMIN FROM bob, clerks;",
                       "Revoked SYSTEM ADMIN from bob; Revoked SYSTEM ADMIN from clerks"),
                      ("REVOKE SYSTEM ADMIN FROM bob", "bob does not hold SYSTEM ADMIN")]:
        assert rows(client=admin, sql=sql) == [{"result": said}], sql
    refused(client=admin, sql="GRANT SYSTEM ADMIN TO nobody", why="user or role nobody does not exist")
    refused(client=admin, sql="GRANT SYSTEM ADMIN TO USER dba", why="user dba does not exist")
    # The name is the capability's: no user or role takes it, in any case.
    for sql in ['CREATE ROLE "SYSTEM ADMIN"', 'CREATE USER "system admin"']:
        refused(client=admin, sql=sql,
                why="SYSTEM ADMIN is the name of the System Admin capability: no user or role can take it")
    # A role or user called sysadmin, or system, or admin, is an ordinary one.
    for sql in ["CREATE ROLE sysadmin", "CREATE ROLE system", "CREATE USER admins", "GRANT sysadmin, system TO bob"]:
        expect_allowed(client=admin, sql=sql)
    bob = world.as_user(user="bob")
    for role in ("sysadmin", "system"):
        expect_allowed(client=bob, sql=f"SET ROLE {role}")
        assert not is_system_admin(client=bob)
        assert one(client=bob, sql="SELECT HARDCHARGER_ACTIVE_ROLE()") == role
        expect_blocked(client=bob, sql="SELECT * FROM vault", why=f"This session acts with role {role} only (SET ROLE)")
    expect_blocked(client=bob, sql=ELEVATE, why=NOT_HELD)


# ---- Through a role ----

def test_a_role_carries_it_to_the_members_the_administrator_gave_it(world):
    admin = world.admin
    # dba holds it; ops holds dba; dave holds ops: each by the administrator's hand.
    for sql in ["CREATE ROLE ops", "GRANT dba TO ops", "CREATE USER dave", "GRANT ops TO dave"]:
        expect_allowed(client=admin, sql=sql)
    dave = world.elevated(user="dave")
    assert ledger(client=dave) == EVERY_ROW
    # A System Admin's memberships count as the administrator's.
    carol = world.elevated(user="carol")
    expect_allowed(client=carol, sql="CREATE USER erin")
    expect_allowed(client=carol, sql="GRANT dba TO erin")
    assert is_system_admin(client=world.elevated(user="erin"))
    # A role granted it later carries it to the members the administrator already gave it.
    for sql in ["CREATE ROLE late", "CREATE USER fay", "GRANT late TO fay", "GRANT SYSTEM ADMIN TO ROLE late"]:
        expect_allowed(client=admin, sql=sql)
    assert is_system_admin(client=world.elevated(user="fay"))


def test_a_membership_someone_else_granted_never_carries_it(world):
    admin = world.admin
    # eve administers the role team (WITH ADMIN OPTION) while it is an ordinary role, and
    # gives it to mallory. Then the administrator makes team carry SYSTEM ADMIN.
    for sql in ["CREATE ROLE team", "CREATE USER eve", "CREATE USER mallory", "GRANT team TO eve WITH ADMIN OPTION"]:
        expect_allowed(client=admin, sql=sql)
    eve = world.as_user(user="eve")
    expect_allowed(client=eve, sql="GRANT team TO mallory")
    expect_allowed(client=admin, sql="GRANT dba TO team")
    mallory = world.as_user(user="mallory")
    assert "team" in {r["name"] for r in rows(client=mallory, sql="SELECT name FROM hardcharger_roles()")}
    expect_blocked(client=mallory, sql=ELEVATE, why="user mallory " + NOT_HELD)
    assert rows(client=mallory, sql="SELECT * FROM hardcharger_effective_privileges() "
                                    "WHERE privilege = 'SYSTEM ADMIN'") == []
    # eve, whom the administrator gave the role, holds it (and says through which role).
    assert rows(client=eve, sql="SELECT privilege, via FROM hardcharger_effective_privileges() "
                                "WHERE privilege = 'SYSTEM ADMIN'") == [{"privilege": "SYSTEM ADMIN", "via": "dba"}]
    # Her admin option no longer gives or takes the role, now that it carries SYSTEM ADMIN...
    held = "holds SYSTEM ADMIN; " + ONLY + " grants or revokes it"
    expect_blocked(client=eve, sql="GRANT team TO bob", why="Hardcharger blocked GRANT: role team " + held)
    expect_blocked(client=eve, sql="REVOKE team FROM mallory", why="Hardcharger blocked REVOKE: role team " + held)
    expect_blocked(client=world.as_user(user="bob"), sql=ELEVATE, why=NOT_HELD)
    # ...nor does one on the role granted it.
    expect_allowed(client=admin, sql="CREATE USER frank")
    expect_allowed(client=admin, sql="GRANT dba TO frank WITH ADMIN OPTION")
    frank = world.as_user(user="frank")
    expect_blocked(client=frank, sql="GRANT dba TO bob", why="Hardcharger blocked GRANT: role dba " + held)
    expect_blocked(client=frank, sql="REVOKE dba FROM carol", why="Hardcharger blocked REVOKE: role dba " + held)
    expect_blocked(client=frank, sql="GRANT dba TO clerks", why="role dba " + held)
    assert is_system_admin(client=world.elevated(user="carol")), "a refused REVOKE took it away"
    # Acting as System Admin, they may (positive twin): frank gives bob the role.
    expect_allowed(client=frank, sql=ELEVATE)
    expect_allowed(client=frank, sql="GRANT dba TO bob")
    assert is_system_admin(client=world.elevated(user="bob"))
    # And the administrator's own grant of the same membership makes mallory's count.
    assert rows(client=world.admin, sql="SELECT grantor FROM hardcharger_role_members() WHERE member = 'mallory'") == \
        [{"grantor": "eve"}]
    assert rows(client=admin, sql="GRANT team TO mallory") == [{"result": "Granted team to mallory"}]
    assert rows(client=world.admin, sql="SELECT grantor FROM hardcharger_role_members() WHERE member = 'mallory'") == \
        [{"grantor": "administrator"}]
    expect_allowed(client=mallory, sql=ELEVATE)
    assert rows(client=admin, sql="GRANT team TO mallory") == [{"result": "mallory already holds team"}]


def test_an_identity_provider_group_mapped_to_the_role_carries_it(world):
    issuer = "https://idp.example.test"
    admin = world.admin
    for sql in ["CREATE USER gina", f"CREATE GROUP MAPPING 'platform-admins' ISSUER '{issuer}' TO ROLE dba"]:
        expect_allowed(client=admin, sql=sql)

    def gina(groups: list[str] | None, iss: str = issuer):
        return world.server.user(name="gina", **({"idp": {"iss": iss, "groups": groups}} if groups else {}))

    member = gina(groups=["platform-admins"])
    assert not is_system_admin(client=member)  # the group gives the right to ask, no more
    assert rows(client=member, sql="SELECT privilege, via FROM hardcharger_effective_privileges() "
                                   "WHERE privilege = 'SYSTEM ADMIN'") == [{"privilege": "SYSTEM ADMIN", "via": "dba"}]
    expect_allowed(client=member, sql=ELEVATE)
    assert ledger(client=member) == EVERY_ROW
    # Not without the group, with another group, or with the group from another issuer.
    for other in (gina(groups=None), gina(groups=["platform-users"]),
                  gina(groups=["platform-admins"], iss="https://other.example")):
        expect_blocked(client=other, sql=ELEVATE, why="user gina " + NOT_HELD)
    # Only the administrator maps a group to a role.
    expect_blocked(client=world.as_user(user="bob"), sql="CREATE GROUP MAPPING 'everyone' TO ROLE dba",
                   why="only administrators manage group mappings")
    # Unmapping the group ends it for the session that asked through it.
    expect_allowed(client=admin, sql=f"DROP GROUP MAPPING 'platform-admins' ISSUER '{issuer}'")
    expect_blocked(client=member, sql="SELECT * FROM vault", why="user gina " + LOST + "gina")


# ---- What a session is limited to is never widened ----

def test_a_scoped_token_never_acts_as_system_admin(world):
    alice = world.as_user(user="alice")
    minted = rows(client=alice, sql="CREATE SCOPED TOKEN reports ALLOW SELECT VALID FOR '1 hour'")[0]["token"]
    by_admin = rows(client=world.admin, sql="CREATE SCOPED TOKEN nightly FOR USER alice ALLOW SELECT, INSERT")[0]["token"]
    for name, token in (("reports", minted), ("nightly", by_admin)):
        scoped = world.server.client(token=token)
        expect_blocked(client=scoped, sql=ELEVATE,
                       why=f"Hardcharger blocked SET ROLE SYSTEM ADMIN: this session signed in with scoped token "
                           f"{name}, and a scoped token never acts as System Admin")
        assert not is_system_admin(client=scoped)
        assert ledger(client=scoped) == A_CLERKS_ROWS  # still the token's user, inside its scope
        expect_blocked(client=scoped, sql="SELECT * FROM vault", why="no SELECT privilege")
        expect_blocked(client=scoped, sql="GRANT SYSTEM ADMIN TO bob", why="a scoped token cannot change access control")
    # A token minted by a session acting as System Admin is a scoped token like any other.
    elevated = world.elevated(user="alice")
    for_carol = rows(client=elevated, sql="CREATE SCOPED TOKEN c FOR USER carol ALLOW SELECT")[0]["token"]
    expect_blocked(client=world.server.client(token=for_carol), sql=ELEVATE, why="a scoped token never acts as System Admin")
    # Positive twin: the same user's own sign-in.
    assert is_system_admin(client=world.elevated(user="alice"))


def test_a_token_with_catalog_access_rules_never_acts_as_system_admin(world):
    limited = world.server.user(name="alice", catalog_access=[{"catalog": "*", "access": "write"}])
    expect_blocked(client=limited, sql=ELEVATE,
                   why="Hardcharger blocked SET ROLE SYSTEM ADMIN: this session's sign-in token carries catalog_access "
                       "rules, and a token limited to some catalogs never acts as System Admin")
    assert_ordinary(client=limited, user="alice")
    # Rules that name nothing are rules too.
    for rules in ([], [{"catalog": "memory", "access": "read"}]):
        expect_blocked(client=world.server.user(name="alice", catalog_access=rules), sql=ELEVATE,
                       why="carries catalog_access rules")
    # An administrator's token with rules is held to them, and runs no access-control statement.
    restricted_admin = world.server.client(token=mint(user="admin", role="admin",
                                                      catalog_access=[{"catalog": "memory", "access": "write"}]))
    for sql in (ELEVATE, "GRANT SYSTEM ADMIN TO bob"):
        expect_blocked(client=restricted_admin, sql=sql,
                       why="an administrator token with catalog_access rules cannot run access-control statements")
    expect_blocked(client=world.as_user(user="bob"), sql=ELEVATE, why=NOT_HELD)
    assert is_system_admin(client=world.elevated(user="alice"))  # the twin: no rules


def test_a_read_only_session_never_acts_as_system_admin(world):
    token = mint(user="alice")
    # The connection asked to be read-only.
    reader = world.server.client(token=token, options={"read_only": True})
    expect_blocked(client=reader, sql=ELEVATE,
                   why="Hardcharger blocked SET ROLE SYSTEM ADMIN: this is a read-only session (its connection asked "
                       "for read_only), and a read-only session never acts as System Admin")
    assert ledger(client=reader) == A_CLERKS_ROWS
    with pytest.raises(ChargewireError) as write:
        reader.run(sql="INSERT INTO ledger VALUES (9, 'eu', 'x')")
    assert (write.value.code, write.value.sqlstate) == ("read_only", "25006")
    # The sign-in is readonly (its token's role).
    readonly = world.server.user(name="alice", role="readonly")
    expect_blocked(client=readonly, sql=ELEVATE,
                   why="Hardcharger blocked SET ROLE SYSTEM ADMIN: this session signed in readonly (its sign-in's "
                       "role), and a readonly session never acts as System Admin")
    assert ledger(client=readonly) == A_CLERKS_ROWS
    with pytest.raises(ChargewireError) as write:
        readonly.run(sql="INSERT INTO ledger VALUES (9, 'eu', 'x')")
    assert "has a readonly session and cannot run statements that modify state" in write.value.detail
    with pytest.raises(ChargewireError) as grant:
        readonly.run(sql="GRANT SYSTEM ADMIN TO bob")
    assert grant.value.detail == "User 'alice' has a readonly session and cannot run GRANT SYSTEM ADMIN."
    # A session acting as System Admin that turns read-only stops acting as one, and stays
    # read-only whatever its user holds.
    writer = world.server.client(token=token)
    expect_allowed(client=writer, sql=ELEVATE)
    assert ledger(client=writer) == EVERY_ROW
    response = writer.http.patch(url=writer.url, headers=writer.headers(), json={"options": {"read_only": True}})
    assert response.status_code == 200, response.text
    assert not is_system_admin(client=writer)
    assert ledger(client=writer) == A_CLERKS_ROWS
    expect_blocked(client=writer, sql="SELECT * FROM vault", why="user alice has no SELECT privilege on table")
    expect_blocked(client=writer, sql=ELEVATE, why="this is a read-only session")
    with pytest.raises(ChargewireError) as write:
        writer.run(sql="CREATE USER made_read_only")
    assert (write.value.code, write.value.sqlstate) == ("read_only", "25006")
    assert rows(client=world.admin, sql="SELECT id FROM ledger WHERE id = 9") == []
    # Positive twin: an ordinary session with the same token.
    twin = world.server.client(token=token)
    expect_allowed(client=twin, sql=ELEVATE)
    expect_allowed(client=twin, sql="INSERT INTO ledger VALUES (9, 'eu', 'x')")


# ---- The administrator stays what it is ----

def test_the_administrator_is_out_of_a_system_admins_reach(world):
    alice = world.elevated(user="alice")
    name = world.server.running.username  # "admin": the server's configuration, no user of the store
    for sql, why in [
        (f"ALTER USER {name} WITH PASSWORD 'taken-over-1'", f"user {name} does not exist"),
        (f"ALTER USER {name} PASSWORD NULL", f"user {name} does not exist"),
        (f"ALTER USER {name} DISABLE", f"user {name} does not exist"),
        (f"ALTER USER {name} SET ATTRIBUTE provisioned_role = 'readonly'", f"user {name} does not exist"),
        (f"DROP USER {name}", f"user {name} does not exist"),
        (f"REVOKE SYSTEM ADMIN FROM {name}", f"user or role {name} does not exist"),
        (f"GRANT SYSTEM ADMIN TO {name}", f"user or role {name} does not exist"),
        (f"CREATE USER {name} WITH PASSWORD 'taken-over-2'", f"{name} is the server administrator's name"),
    ]:
        refused(client=alice, sql=sql, why=why)
    # Its credentials are the server's configuration, which no SQL reads, a System Admin's
    # (as the administrator's own) included.
    expect_blocked(client=alice, sql="SELECT * FROM read_text('/proc/self/environ')",
                   why="Hardcharger blocked access to '/proc/self/environ'")
    assert error_of(client=alice, sql="SELECT getenv('HARDCHARGER_ADMIN_PASSWORD')") != ""
    assert rows(client=alice, sql="SELECT name FROM hardcharger_server_settings() WHERE name ILIKE '%password%' "
                                  "OR name ILIKE '%secret_key%'") == []
    # Its password still signs it in, with everything it could do before.
    admin = world.server.admin()
    assert ledger(client=admin) == EVERY_ROW
    # It takes SYSTEM ADMIN from every holder, a session acting as one included.
    assert rows(client=admin, sql="REVOKE SYSTEM ADMIN FROM alice, ROLE dba") == [
        {"result": "Revoked SYSTEM ADMIN from alice; Revoked SYSTEM ADMIN from dba"}]
    expect_blocked(client=alice, sql="GRANT SYSTEM ADMIN TO alice", why="user alice " + LOST + "alice")
    assert rows(client=admin, sql="SELECT * FROM hardcharger_grants() WHERE privilege = 'SYSTEM ADMIN'") == []
    for user in ("alice", "carol"):
        expect_blocked(client=world.server.user(name=user), sql=ELEVATE, why=NOT_HELD)
    expect_allowed(client=admin, sql="GRANT SYSTEM ADMIN TO alice")  # and gives it back


# ---- Taking it over ----

def test_nothing_a_non_administrator_runs_reaches_a_holder(world):
    admin = world.admin
    # bob holds every right that can be granted and passed on, and administers clerks.
    for sql in ["GRANT ALL PRIVILEGES ON CATALOG memory TO clerks WITH GRANT OPTION",
                "GRANT MANAGE CONFIG, MANAGE SECRETS, MANAGE CATALOGS TO clerks WITH ADMIN OPTION",
                "REVOKE clerks FROM bob", "GRANT clerks TO bob WITH ADMIN OPTION",
                "CREATE USER dan WITH PASSWORD 'dan-first-password'", "GRANT SYSTEM ADMIN TO dan",
                "ALTER USER dan DISABLE"]:
        expect_allowed(client=admin, sql=sql)
    bob = world.as_user(user="bob")
    users_and_roles = "only administrators manage users and roles on this server"
    for sql, why in [
        # A holder's credentials, its being enabled, its attributes, its existence.
        ("ALTER USER alice WITH PASSWORD 'bobs-choice-1'", users_and_roles),
        ("ALTER USER alice PASSWORD NULL", users_and_roles),
        ("ALTER USER dan ENABLE", users_and_roles),
        ("ALTER USER dan WITH PASSWORD 'bobs-choice-2'", users_and_roles),
        ("ALTER USER alice DISABLE", users_and_roles),
        ("ALTER USER alice SET ATTRIBUTE provisioned_role = 'user'", users_and_roles),
        ("ALTER USER alice UNSET ATTRIBUTE provisioned_role", users_and_roles),
        ("DROP USER alice", users_and_roles),
        ("CREATE USER alice2", users_and_roles),
        # The role that holds it: its existence, its members, what holds it.
        ("DROP ROLE dba", users_and_roles),
        ("CREATE ROLE dba2", users_and_roles),
        # (He holds no admin option on it; one who does is refused too, in
        # test_a_membership_someone_else_granted_never_carries_it.)
        ("GRANT dba TO bob", "user bob may not grant or revoke role dba"),
        ("GRANT dba TO clerks", "user bob may not grant or revoke role dba"),
        ("REVOKE dba FROM carol", "user bob may not grant or revoke role dba"),
        ("GRANT clerks TO dba", ""),  # allowed: giving a role to dba gives dba's holders more, and no one SYSTEM ADMIN
        # Identity-provider groups, tokens for someone else, tenants, policies.
        ("CREATE GROUP MAPPING 'bobs-group' TO ROLE dba", "only administrators manage group mappings"),
        ("DROP GROUP MAPPING 'bobs-group'", "only administrators manage group mappings"),
        ("CREATE TENANT bobs", "only administrators manage tenants"),
        # Acting as someone else.
        ("SET ROLE dba", "user bob does not hold role dba"),
        ("SET ROLE alice", "user bob does not hold role alice"),
        ("SHOW GRANTS FOR USER alice", "Hardcharger blocked SHOW GRANTS for another user"),
    ]:
        if why:
            expect_blocked(client=bob, sql=sql, why=why)
        else:
            expect_allowed(client=bob, sql=sql)
    # A token for someone else is the administrator's to mint (and would be a scoped token,
    # which never acts as System Admin).
    for sql in ["CREATE SCOPED TOKEN t FOR USER alice ALLOW SELECT", "DROP SCOPED TOKEN t FOR USER alice"]:
        refused(client=bob, sql=sql, why="users mint scoped tokens only for yourself; the administrator mints them "
                                         "for others")
    # DuckDB has no statement that makes a session another user.
    for sql in ["SET SESSION AUTHORIZATION alice", "SET hardcharger.user = 'alice'", "EXECUTE AS alice"]:
        assert error_of(client=bob, sql=sql) != "", sql
    assert one(client=bob, sql="SELECT HARDCHARGER_USER()") == "bob"
    expect_blocked(client=bob, sql=ELEVATE, why="user bob " + NOT_HELD)
    holders = rows(client=admin, sql="SELECT grantee FROM hardcharger_grants() WHERE privilege = 'SYSTEM ADMIN' "
                                     "ORDER BY grantee")
    assert holders == [{"grantee": "alice"}, {"grantee": "dan"}, {"grantee": "dba"}]
    # dan is still disabled, with the password the administrator gave him.
    with pytest.raises(ChargewireError) as sign_in:
        world.server.password_token(user="dan", password="dan-first-password")
    assert sign_in.value.code == "invalid_credentials"
    with pytest.raises(ChargewireError):
        world.server.password_token(user="dan", password="bobs-choice-2")
    # The administrator's own (positive twins): enabled, dan signs in and may ask.
    expect_allowed(client=admin, sql="ALTER USER dan ENABLE")
    dan = world.server.user(name="dan", password="dan-first-password")
    expect_allowed(client=dan, sql=ELEVATE)
    # A tenant admin reaches its tenant's users, never a holder (none is a tenant's).
    for sql in ["CREATE TENANT acme", "CREATE USER acme/tina", "GRANT acme/tenant_admin TO acme/tina"]:
        expect_allowed(client=admin, sql=sql)
    tina = world.as_user(user="acme/tina")
    expect_allowed(client=tina, sql="CREATE USER acme/made WITH PASSWORD 'tinas-choice-1'")
    for sql in ["ALTER USER alice WITH PASSWORD 'tinas-choice-2'", "ALTER USER dan DISABLE", "DROP USER alice",
                "DROP ROLE dba"]:
        expect_blocked(client=tina, sql=sql, why=users_and_roles)
    expect_blocked(client=tina, sql="GRANT dba TO acme/made", why="user acme/tina may not grant or revoke role dba")


def test_a_name_used_again_does_not_inherit_it(world):
    admin = world.admin
    # The user and the role that held it are dropped; new ones take their names.
    for sql in ["DROP USER alice", "CREATE USER alice", "REVOKE dba FROM carol", "DROP ROLE dba", "CREATE ROLE dba",
                "GRANT dba TO carol"]:
        expect_allowed(client=admin, sql=sql)
    for user in ("alice", "carol"):
        expect_blocked(client=world.server.user(name=user), sql=ELEVATE, why=f"user {user} " + NOT_HELD)
    assert rows(client=admin, sql="SELECT * FROM hardcharger_grants() WHERE privilege = 'SYSTEM ADMIN'") == []
    # Positive twin: granted again, to the new ones.
    expect_allowed(client=admin, sql="GRANT SYSTEM ADMIN TO alice")
    assert is_system_admin(client=world.elevated(user="alice"))


# ---- Seeing it ----

def test_who_holds_it_and_which_sessions_act_as_one(world):
    admin = world.admin
    alice, bob, carol = world.as_user(user="alice"), world.as_user(user="bob"), world.as_user(user="carol")
    grants = "SELECT grantee, grantee_type, privilege, object_type, grantor, with_grant_option FROM hardcharger_grants() " \
             "WHERE privilege = 'SYSTEM ADMIN' ORDER BY grantee"
    to_alice = {"grantee": "alice", "grantee_type": "USER", "privilege": "SYSTEM ADMIN", "object_type": "SYSTEM",
                "grantor": "administrator", "with_grant_option": False}
    to_dba = to_alice | {"grantee": "dba", "grantee_type": "ROLE"}
    # The administrator sees every grant; a user its own and its roles'; another user none.
    assert rows(client=admin, sql=grants) == [to_alice, to_dba]
    assert rows(client=alice, sql=grants) == [to_alice]
    assert rows(client=carol, sql=grants) == [to_dba]
    assert rows(client=bob, sql=grants) == []
    # SHOW GRANTS: the caller's own, a user's (to the administrator), a role's.
    show = {"privilege": "SYSTEM ADMIN", "object_type": "SERVER", "object": "*", "columns": "", "granted_to": "alice",
            "grantee_type": "USER", "grant_option": "NO", "granted_by": "administrator"}
    through_dba = show | {"granted_to": "dba", "grantee_type": "ROLE"}

    def system_admin_rows(client, sql: str) -> list[dict]:
        return [r for r in rows(client=client, sql=sql) if r["privilege"] == "SYSTEM ADMIN"]

    assert system_admin_rows(client=alice, sql="SHOW GRANTS") == [show]
    assert system_admin_rows(client=carol, sql="SHOW GRANTS") == [through_dba]
    assert system_admin_rows(client=bob, sql="SHOW GRANTS") == []
    assert system_admin_rows(client=admin, sql="SHOW GRANTS FOR USER alice") == [show]
    assert system_admin_rows(client=admin, sql="SHOW GRANTS FOR USER carol") == [through_dba]
    assert system_admin_rows(client=admin, sql="SHOW GRANTS FOR ROLE dba") == [through_dba]
    assert system_admin_rows(client=admin, sql="SHOW GRANTS FOR ROLE clerks") == []
    # Effective privileges say through what: the user itself, or the role.
    effective = "SELECT user_name, privilege, object_type, via FROM hardcharger_effective_privileges({}) " \
                "WHERE privilege IN ('SYSTEM ADMIN', 'ALL')"
    assert rows(client=alice, sql=effective.format("")) == [
        {"user_name": "alice", "privilege": "SYSTEM ADMIN", "object_type": "SYSTEM", "via": "alice"}]
    assert rows(client=admin, sql=effective.format("'carol'")) == [
        {"user_name": "carol", "privilege": "SYSTEM ADMIN", "object_type": "SYSTEM", "via": "dba"}]
    assert rows(client=bob, sql=effective.format("")) == []

    # A session asks what it is acting as.
    state = "SELECT HARDCHARGER_IS_SYSTEM_ADMIN() AS system_admin, HARDCHARGER_ACTIVE_ROLE() AS active_role, " \
            "HARDCHARGER_IS_FULL_ADMIN() AS full_admin, HARDCHARGER_ROLE() AS role, HARDCHARGER_USER() AS user_name"
    as_herself = {"system_admin": False, "active_role": None, "full_admin": False, "role": "user", "user_name": "alice"}
    assert rows(client=alice, sql=state) == [as_herself]
    expect_allowed(client=alice, sql="SET ROLE clerks")
    assert rows(client=alice, sql=state) == [as_herself | {"active_role": "clerks"}]
    expect_allowed(client=alice, sql=ELEVATE)
    assert rows(client=alice, sql=state) == [as_herself | {"system_admin": True, "active_role": "SYSTEM ADMIN",
                                                           "full_admin": True}]
    assert rows(client=alice, sql="SHOW GRANTS") == [
        {"privilege": "ALL", "object_type": "SERVER", "object": "*", "columns": "", "granted_to": "alice",
         "grantee_type": "SYSTEM ADMIN", "grant_option": "YES", "granted_by": ""}]
    assert rows(client=alice, sql=effective.format("")) == [
        {"user_name": "alice", "privilege": "ALL", "object_type": "SERVER", "via": "SYSTEM ADMIN"}]
    # The administrator is not one: it is the administrator.
    assert rows(client=admin, sql=state) == [{"system_admin": False, "active_role": None, "full_admin": True,
                                              "role": "admin", "user_name": "admin"}]

    # The session views show which sessions act as System Admin, to an administrator.
    for view in ("hardcharger_sessions()", "hardcharger_active_connections()"):
        listing = f"SELECT session_id, username, role, system_admin FROM {view}"
        seen = {r["session_id"]: r for r in rows(client=admin, sql=listing)}
        assert seen[alice.session_id] == {"session_id": alice.session_id, "username": "alice", "role": "user",
                                          "system_admin": True}, view
        assert [r["system_admin"] for r in seen.values()].count(True) == 1, seen
        # A session acting as System Admin sees every session, as the administrator does...
        assert {r["session_id"] for r in rows(client=alice, sql=listing)} == set(seen), view
        # ...and to an ordinary session it is an administrator's: not listed, the same user's included.
        other = world.server.user(name="alice")
        assert {r["session_id"] for r in rows(client=other, sql=listing)} == {other.session_id}, view
        other.close()
    # The moment it stops, the views say so.
    expect_allowed(client=alice, sql="RESET ROLE")
    assert rows(client=admin, sql="SELECT count(*) AS n FROM hardcharger_sessions() WHERE system_admin") == [{"n": 0}]
    # KILL SESSION and hardcharger_cancel() are an administrator's, and a System Admin's.
    expect_allowed(client=alice, sql=ELEVATE)
    expect_allowed(client=alice, sql=f"KILL SESSION '{bob.session_id}'")
    with pytest.raises(ChargewireError) as killed:
        bob.run(sql="SELECT 1")
    assert killed.value.code == "session_ended"


# ---- Asked of the grants on every statement ----

def lost(client, user: str, sql: str = "SELECT secret FROM vault") -> None:
    expect_blocked(client=client, sql=sql, why=f"Hardcharger blocked this statement: user {user} " + LOST + user)


def prepared_error(client, prepared_id: str) -> ChargewireError:
    with pytest.raises(ChargewireError) as raised:
        client.execute_prepared(prepared_id=prepared_id)
    return raised.value


# Each way a user stops holding SYSTEM ADMIN while a session of theirs acts as one: the
# user, the statement the administrator runs, and whether the session itself ends.
ENDINGS = {
    "revoked from the user": ("alice", "REVOKE SYSTEM ADMIN FROM alice", False),
    "revoked from the role": ("carol", "REVOKE SYSTEM ADMIN FROM ROLE dba", False),
    "the role is dropped": ("carol", "DROP ROLE dba", False),
    "the user is taken out of the role": ("carol", "REVOKE dba FROM carol", False),
    "the user is disabled": ("alice", "ALTER USER alice DISABLE", True),
    "the user is dropped": ("alice", "DROP USER alice", True),
}


@pytest.mark.parametrize("how", list(ENDINGS))
def test_it_ends_for_an_open_session_at_its_next_statement(world, how):
    user, statement, session_ends = ENDINGS[how]
    session = world.elevated(user=user)
    # A transaction is open, with a write made as System Admin, and a statement was
    # prepared as one.
    expect_allowed(client=session, sql="BEGIN")
    expect_allowed(client=session, sql="INSERT INTO vault VALUES (7)")
    read_vault = session.prepare(sql="SELECT max(secret) AS m FROM vault")
    make_user = session.prepare(sql="CREATE USER made_from_a_prepared_statement")
    assert session.execute_prepared(prepared_id=read_vault).column("m").to_pylist() == [42]

    expect_allowed(client=world.admin, sql=statement)

    if session_ends:
        # A disabled or dropped user's sessions end, whatever they were acting as.
        for call in (lambda: session.run(sql="SELECT secret FROM vault"),
                     lambda: session.execute_prepared(prepared_id=read_vault),
                     lambda: session.run(sql="COMMIT"), lambda: session.run(sql="RESET ROLE")):
            with pytest.raises(ChargewireError) as ended:
                call()
            # (The session is ended, and its token, which every request shows first, refused.)
            assert ended.value.code in ("session_ended", "invalid_token"), ended.value
            assert ended.value.reason == "principal_dropped", ended.value
        with pytest.raises(ChargewireError) as sign_in:
            world.server.user(name=user)
        assert sign_in.value.code in ("permission_denied", "invalid_token"), sign_in.value
    else:
        # The very next statement, the prepared ones and the transaction's end included.
        lost(client=session, user=user)
        for prepared in (read_vault, make_user):
            error = prepared_error(client=session, prepared_id=prepared)
            assert (error.code, error.sqlstate) == ("permission_denied", "42501"), error
            assert f"user {user} " + LOST + user in error.detail, error
        for sql in ("CREATE USER made_after_the_end", "GRANT SYSTEM ADMIN TO " + user, "COMMIT", "ROLLBACK",
                    "INSERT INTO vault VALUES (8)", "ATTACH ':memory:' AS after_the_end"):
            lost(client=session, user=user, sql=sql)
        # RESET ROLE makes it the user again: every grant, the row policy and the mask apply,
        # to statements prepared as System Admin too.
        expect_allowed(client=session, sql="RESET ROLE")
        error = prepared_error(client=session, prepared_id=read_vault)
        assert error.code == "permission_denied" and f"user {user} has no SELECT privilege on table" in error.detail
        error = prepared_error(client=session, prepared_id=make_user)
        assert "only administrators manage users and roles on this server" in error.detail, error
        expect_allowed(client=session, sql="ROLLBACK")
        assert_ordinary(client=session, user=user)
        expect_blocked(client=session, sql=ELEVATE, why=f"user {user} " + NOT_HELD)
    # Nothing ran after the end: the transaction's write is not there, no user was made.
    assert rows(client=world.admin, sql="SELECT secret FROM vault ORDER BY secret") == [{"secret": 42}]
    made = {r["name"] for r in rows(client=world.admin, sql="SELECT name FROM hardcharger_users()")}
    assert not {"made_after_the_end", "made_from_a_prepared_statement"} & made, made
    # Another holder, untouched by the change, goes on (the twin).
    other = "carol" if user == "alice" else "alice"
    assert ledger(client=world.elevated(user=other)) == EVERY_ROW


def test_a_disabled_holder_enabled_again_asks_again(world):
    alice = world.elevated(user="alice")
    expect_allowed(client=world.admin, sql="ALTER USER alice DISABLE")
    with pytest.raises(ChargewireError) as ended:
        alice.run(sql="SELECT 1")
    assert ended.value.reason == "principal_dropped", ended.value
    expect_allowed(client=world.admin, sql="ALTER USER alice ENABLE")
    with pytest.raises(ChargewireError) as still_ended:
        alice.run(sql="SELECT * FROM vault")  # the session that ended stays ended
    assert still_ended.value.code == "session_ended", still_ended.value
    # The grant is still hers; no session of hers acts as System Admin until it asks.
    again = world.server.user(name="alice")
    assert_ordinary(client=again, user="alice")
    expect_allowed(client=again, sql=ELEVATE)
    assert ledger(client=again) == EVERY_ROW


def test_a_statement_still_running_is_stopped(world):
    alice = world.elevated(user="alice")
    # Minutes of work for a session that reads a table it holds nothing on.
    statement = alice.submit(sql="SELECT count(*) FROM range(100000000000) r, vault v WHERE r.range % 7 = v.secret")
    time.sleep(0.5)
    assert alice.wait(statement_id=statement, timeout=0.1)["state"] in ("running", "queued")
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    doc = alice.wait(statement_id=statement, timeout=30)
    assert doc["state"] in ("failed", "cancelled"), doc
    lost(client=alice, user="alice")


def test_what_it_did_before_the_end_stands_and_is_the_transactions_to_finish(world):
    # A revoke reaches the next statement; it does not undo the ones before it. The open
    # transaction's writes were made by a System Admin, and the user finishes it either way.
    alice = world.elevated(user="alice")
    expect_allowed(client=alice, sql="BEGIN")
    expect_allowed(client=alice, sql="INSERT INTO vault VALUES (7)")
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    lost(client=alice, user="alice", sql="INSERT INTO vault VALUES (8)")
    expect_allowed(client=alice, sql="RESET ROLE")
    expect_blocked(client=alice, sql="INSERT INTO vault VALUES (9)", why="user alice has no INSERT privilege on table")
    expect_allowed(client=alice, sql="ROLLBACK")
    assert rows(client=world.admin, sql="SELECT secret FROM vault ORDER BY secret") == [{"secret": 42}]


def test_prepared_statements_follow_what_the_session_acts_as(world):
    alice = world.as_user(user="alice")
    # Prepared as the user: the row policy and the mask are part of what was planned.
    as_user = alice.prepare(sql="SELECT id, card FROM ledger ORDER BY id")
    expect_allowed(client=alice, sql="PREPARE sql_as_user AS SELECT id, card FROM ledger ORDER BY id")

    def run(prepared_id: str) -> list[tuple]:
        return [(r["id"], r["card"]) for r in alice.execute_prepared(prepared_id=prepared_id).to_pylist()]

    def execute(name: str) -> list[tuple]:
        return [tuple(r.values()) for r in rows(client=alice, sql=f"EXECUTE {name}")]

    assert run(prepared_id=as_user) == A_CLERKS_ROWS == execute(name="sql_as_user")
    expect_allowed(client=alice, sql=ELEVATE)
    # As System Admin they run as the administrator's would: planned again, without either.
    assert run(prepared_id=as_user) == EVERY_ROW == execute(name="sql_as_user")
    as_admin = alice.prepare(sql="SELECT id, card FROM ledger ORDER BY id")
    vault = alice.prepare(sql="SELECT secret FROM vault")
    expect_allowed(client=alice, sql="PREPARE sql_as_admin AS SELECT id, card FROM ledger ORDER BY id")
    expect_allowed(client=alice, sql="PREPARE sql_vault AS SELECT secret FROM vault")
    assert run(prepared_id=as_admin) == EVERY_ROW == execute(name="sql_as_admin")
    assert execute(name="sql_vault") == [(42,)]
    # And back as the user, the ones prepared as System Admin are the user's again.
    expect_allowed(client=alice, sql="RESET ROLE")
    for prepared in (as_user, as_admin):
        assert run(prepared_id=prepared) == A_CLERKS_ROWS
    for name in ("sql_as_user", "sql_as_admin"):
        assert execute(name=name) == A_CLERKS_ROWS
    error = prepared_error(client=alice, prepared_id=vault)
    assert error.code == "permission_denied" and "user alice has no SELECT privilege on table" in error.detail
    expect_blocked(client=alice, sql="EXECUTE sql_vault", why="user alice has no SELECT privilege on table")


def test_bulk_ingest_and_metadata_follow_it_too(world):
    import pyarrow as pa
    alice = world.as_user(user="alice")
    batch = pa.table({"secret": [1]})
    with pytest.raises(ChargewireError) as raised:
        alice.ingest(table="vault", rows=batch, mode="append")
    assert raised.value.code == "permission_denied", raised.value
    assert "vault" not in alice.metadata(kind="tables").column("table_name").to_pylist()
    expect_allowed(client=alice, sql=ELEVATE)
    assert alice.ingest(table="vault", rows=batch, mode="append") == 1
    assert "vault" in alice.metadata(kind="tables").column("table_name").to_pylist()
    expect_allowed(client=world.admin, sql="REVOKE SYSTEM ADMIN FROM alice")
    with pytest.raises(ChargewireError) as raised:
        alice.ingest(table="vault", rows=batch, mode="append")
    assert raised.value.code == "permission_denied" and LOST in raised.value.detail, raised.value
    assert "vault" not in alice.metadata(kind="tables").column("table_name").to_pylist()
    assert rows(client=world.admin, sql="SELECT count(*) AS n FROM vault") == [{"n": 2}]


def test_the_init_sql_grants_it_at_every_start(launcher, store):
    uri = store.new_database()
    init = "CREATE USER IF NOT EXISTS alice; CREATE ROLE IF NOT EXISTS dba; GRANT SYSTEM ADMIN TO alice, ROLE dba;"
    for _ in range(2):  # the second start finds what the first one stored
        server = start_rbac_server(launcher=launcher, store_uri=uri, args=["--init-sql-commands", init])
        assert rows(client=server.admin(), sql="SELECT grantee FROM hardcharger_grants() "
                                               "WHERE privilege = 'SYSTEM ADMIN' ORDER BY grantee") == \
            [{"grantee": "alice"}, {"grantee": "dba"}]
        expect_allowed(client=server.user(name="alice"), sql=ELEVATE)
        server.running.stop()
    # The init SQL runs as the administrator, which has nothing to ask for.
    failed = launcher.start(env={"HARDCHARGER_RBAC_STORE_URI": uri}, args=["--init-sql-commands", ELEVATE],
                            expect_up=False)
    assert failed.process.wait(timeout=60) != 0
    assert "the administrator holds every right already" in failed.log()


def test_the_command_line_client_asks_and_drops_it_in_plain_sql(world):
    """Nothing in the client knows about it: the statements are the server's, in the one
    session the client holds."""
    if not CLIENT_BINARY.exists():
        pytest.fail(reason=f"{CLIENT_BINARY} is missing: build the server first")
    password = "alice-cli-password-1"
    expect_allowed(client=world.admin, sql=f"ALTER USER alice WITH PASSWORD '{password}'")
    script = "\n".join([
        "SELECT 'before', count(*), HARDCHARGER_IS_SYSTEM_ADMIN() FROM ledger;",
        "SELECT secret FROM vault;",
        ELEVATE + ";",
        "SELECT 'acting', count(*), HARDCHARGER_IS_SYSTEM_ADMIN() FROM ledger;",
        "SELECT 'vault', secret FROM vault;",
        "CREATE USER made_from_the_client;",
        "RESET ROLE;",
        "SELECT 'after', count(*), HARDCHARGER_IS_SYSTEM_ADMIN() FROM ledger;",
        "CREATE USER made_from_the_client_afterwards;",
    ]) + "\n"
    result = subprocess.run(
        args=[str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(world.server.running.port), "--tls=false",
              "--username", "alice", "-q", "--csv", "--no-header"],
        env={**clean_environment(), "HARDCHARGER_PASSWORD": password}, input=script, capture_output=True, text=True,
        timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.startswith(("before", "acting", "vault", "after"))]
    assert lines == ["before,1,false", "acting,3,true", "vault,42", "after,1,false"], (result.stdout, result.stderr)
    assert "user alice has no SELECT privilege on table memory.main.vault" in result.stderr, result.stderr
    assert "only administrators manage users and roles" in result.stderr, result.stderr
    made = {r["name"] for r in rows(client=world.admin, sql="SELECT name FROM hardcharger_users()")}
    assert "made_from_the_client" in made and "made_from_the_client_afterwards" not in made, made
    # A second run of the client is a new session: the user again.
    again = subprocess.run(
        args=[str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(world.server.running.port), "--tls=false",
              "--username", "alice", "-q", "--csv", "--no-header", "--command",
              "SELECT HARDCHARGER_IS_SYSTEM_ADMIN()"],
        env={**clean_environment(), "HARDCHARGER_PASSWORD": password}, capture_output=True, text=True, timeout=120)
    assert again.stdout.strip() == "false", (again.stdout, again.stderr)


def test_tenant_boundaries_apply_before_and_not_after(world):
    admin = world.admin
    for sql in ["CREATE TENANT acme", "ATTACH ':memory:' AS acme_db", "CREATE TABLE acme_db.main.orders AS SELECT 7 AS id",
                "ALTER CATALOG acme_db SET TENANT acme", "CREATE USER acme/tina", "GRANT acme/tenant_admin TO acme/tina"]:
        expect_allowed(client=admin, sql=sql)
    alice = world.as_user(user="alice")

    def outside_the_tenant() -> None:
        expect_blocked(client=alice, sql="SELECT * FROM acme_db.main.orders",
                       why="user alice has no SELECT privilege on table acme_db.main.orders")
        expect_blocked(client=alice, sql="CREATE USER acme/made_by_alice",
                       why="only administrators manage users and roles on this server")
        expect_blocked(client=alice, sql="GRANT acme/tenant_admin TO acme/tina",
                       why="user alice may not grant or revoke role acme/tenant_admin")
        expect_blocked(client=alice, sql="ALTER CATALOG acme_db UNSET TENANT", why="only administrators manage tenants")

    outside_the_tenant()
    expect_allowed(client=alice, sql=ELEVATE)
    assert one(client=alice, sql="SELECT id FROM acme_db.main.orders") == 7
    for sql in ["CREATE USER acme/made_by_alice", "CREATE ROLE acme/readers", "GRANT acme/readers TO acme/made_by_alice",
                "GRANT SELECT ON acme_db.main.orders TO acme/readers", "INSERT INTO acme_db.main.orders VALUES (8)"]:
        expect_allowed(client=alice, sql=sql)
    assert [r["tenant"] for r in rows(client=alice, sql="SHOW TENANTS")] == ["acme"]
    assert one(client=world.server.user(name="acme/made_by_alice"), sql="SELECT count(*) FROM acme_db.main.orders") == 2
    expect_allowed(client=alice, sql="RESET ROLE")
    outside_the_tenant()
    # The tenant's own admin stays inside it: it cannot ask, and sees nothing of the rest.
    tina = world.as_user(user="acme/tina")
    expect_blocked(client=tina, sql=ELEVATE, why="belongs to a tenant")
    expect_blocked(client=tina, sql="SELECT * FROM memory.main.vault", why="cannot see")


def test_a_statement_waiting_in_the_queue_is_asked_when_it_runs(launcher, store):
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               args=["--max-concurrent-statements", "1"])
    admin = server.admin()
    for sql in ["CREATE TABLE vault AS SELECT 42 AS secret", "CREATE USER alice", "CREATE USER bob",
                "GRANT SYSTEM ADMIN TO alice"]:
        expect_allowed(client=admin, sql=sql)
    bob = server.user(name="bob")

    def state_of(session) -> str:
        return one(client=admin, sql=f"SELECT state FROM hardcharger_active_connections() "
                                     f"WHERE session_id = '{session.session_id}'")

    def queued_read(revoke: bool) -> dict:
        """alice's read of vault, queued behind bob's statement while she acts as System
        Admin, and run once his is cancelled."""
        alice = server.user(name="alice")
        expect_allowed(client=alice, sql=ELEVATE)
        # (An administrator's statements skip the queue unless it says otherwise.)
        expect_allowed(client=alice, sql="SET hardcharger.bypass_queue = false")
        holder = bob.submit(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b")
        deadline = time.monotonic() + 30
        while state_of(session=bob) != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        waiting = alice.submit(sql="SELECT secret FROM vault")
        while state_of(session=alice) != "queued" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert state_of(session=alice) == "queued"
        if revoke:
            expect_allowed(client=admin, sql="REVOKE SYSTEM ADMIN FROM alice")
        assert state_of(session=alice) == "queued"
        expect_allowed(client=admin, sql=f"SELECT hardcharger_cancel('{bob.session_id}')")
        bob.wait(statement_id=holder)
        return alice.wait(statement_id=waiting)

    assert queued_read(revoke=False)["state"] == "succeeded"  # the twin: nothing was revoked
    doc = queued_read(revoke=True)
    assert doc["state"] == "failed", doc
    assert doc["error"]["code"] == "permission_denied" and "user alice " + LOST + "alice" in doc["error"]["detail"], doc
