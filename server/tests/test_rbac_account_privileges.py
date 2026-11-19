"""The account privileges: CREATE USER, ALTER USER, DROP USER, CREATE ROLE and DROP ROLE.

GRANT CREATE USER, ALTER USER, ... TO helpdesk lets helpdesk's members manage accounts without
reading data or becoming SYSTEM ADMIN: system-scope users and roles that are no more than the
session itself. Only an unrestricted administrator grants them (never WITH ADMIN OPTION, never
to PUBLIC or a tenant's role), a role holds one only through memberships an administrator
granted, a session limited by its sign-in never uses one, and each statement asks the store
itself, so a revoke on any replica counts at the very next statement. Each test gets its own
server and store.
"""

import os
import secrets
import time

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked, mint, start_rbac_server

ACCOUNT_PRIVILEGES = ("CREATE USER", "ALTER USER", "DROP USER", "CREATE ROLE", "DROP ROLE")
ALL_FIVE = ", ".join(ACCOUNT_PRIVILEGES)
# As refusals list them (sorted).
SCALE = float(os.environ.get("HARDCHARGER_TEST_TIMEOUT_SCALE", "1"))
ONLY_SYSTEM_ADMINS = "only the administrator, or a user acting as System Admin (SET ROLE SYSTEM ADMIN)"


def not_held(statement: str) -> str:
    """What a session without the statement's account privilege is told."""
    return (f"Hardcharger blocked {statement}: only administrators manage users and roles on this server (and, for "
            f"system-scope users and roles, a role granted the {statement} privilege)")


def beyond(statement: str, target: str, why: str, verb: str = "changes") -> str:
    return f"Hardcharger blocked {statement}: user {target} {why}; only an administrator {verb} it"


def password(name: str) -> str:
    """A password no other test uses, to look for in the log afterwards."""
    return f"{name}-pw-{secrets.token_urlsafe(nbytes=12)}"


class World:
    def __init__(self, server):
        self.server = server
        self.admin = server.admin()
        self.sessions = {}
        self.passwords = {"amy": password(name="amy"), "carl": password(name="carl")}

    def as_user(self, user: str):
        if user not in self.sessions:
            self.sessions[user] = self.server.user(name=user)
        return self.sessions[user]

    def setup(self, *statements: str) -> None:
        for sql in statements:
            expect_allowed(client=self.admin, sql=sql)

    def signs_in(self, user: str, secret: str) -> bool:
        try:
            self.server.password_token(user=user, password=secret)
            return True
        except ChargewireError as error:
            assert error.code == "invalid_credentials", error
            return False


def start_world(launcher, store_uri: str, env: dict | None = None) -> World:
    world = World(server=start_rbac_server(launcher=launcher, store_uri=store_uri, env=env))
    world.setup(
        "CREATE TABLE ledger (id INT, region VARCHAR)",
        "INSERT INTO ledger VALUES (1, 'eu'), (2, 'us')",
        # helpdesk manages accounts; analyst reads the ledger; clerks holds nothing.
        "CREATE ROLE helpdesk", f"GRANT {ALL_FIVE} TO helpdesk",
        "CREATE ROLE analyst", "GRANT SELECT ON ledger TO analyst",
        "CREATE ROLE clerks",
        "CREATE ROLE eu_team", "CREATE ROW POLICY eu_rows ON ledger TO eu_team USING (region = 'eu')",
        "CREATE ROW POLICY everyone_else ON ledger TO analyst USING (true)",
        # amy is the help desk (and a clerk); bob an analyst; carl holds nothing but PUBLIC;
        # dana a clerk; sam holds SYSTEM ADMIN.
        f"CREATE USER amy WITH PASSWORD '{world.passwords['amy']}'", "GRANT helpdesk TO amy", "GRANT clerks TO amy",
        "CREATE USER bob", "GRANT analyst TO bob",
        f"CREATE USER carl WITH PASSWORD '{world.passwords['carl']}'",
        "CREATE USER dana", "GRANT clerks TO dana",
        "CREATE USER sam", "GRANT SYSTEM ADMIN TO sam",
    )
    return world


@pytest.fixture
def world(launcher, store):
    return start_world(launcher=launcher, store_uri=store.new_database())


def rows(client, sql: str) -> list[dict]:
    return expect_allowed(client=client, sql=sql).to_pylist()


def test_the_help_desk_manages_an_account_from_start_to_end(world):
    amy = world.as_user(user="amy")
    first, second = password(name="eve"), password(name="eve")
    expect_allowed(client=amy, sql=f"CREATE USER eve WITH PASSWORD '{first}'")
    assert world.signs_in(user="eve", secret=first)
    expect_allowed(client=amy, sql=f"ALTER USER eve WITH PASSWORD '{second}'")
    assert not world.signs_in(user="eve", secret=first)
    assert world.signs_in(user="eve", secret=second)
    expect_allowed(client=amy, sql="ALTER USER eve DISABLE")
    assert not world.signs_in(user="eve", secret=second)
    expect_allowed(client=amy, sql="ALTER USER eve ENABLE")
    assert world.signs_in(user="eve", secret=second)
    expect_allowed(client=amy, sql="ALTER USER eve WITH PASSWORD NULL")
    assert not world.signs_in(user="eve", secret=second)
    expect_allowed(client=amy, sql="DROP USER eve")
    expect_allowed(client=amy, sql="CREATE ROLE night_shift")
    expect_allowed(client=amy, sql="DROP ROLE night_shift")
    users = {r["name"] for r in rows(client=world.admin, sql="SELECT name FROM hardcharger_users()")}
    assert "eve" not in users and "amy" in users
    # A password reset of a user no more powerful than the help desk: carl's.
    reset = password(name="carl")
    expect_allowed(client=amy, sql=f"ALTER USER carl WITH PASSWORD '{reset}'")
    assert world.signs_in(user="carl", secret=reset)
    # The help desk reads nothing it could not read before.
    expect_blocked(client=amy, sql="SELECT * FROM ledger", why="user amy has no SELECT privilege on table")


@pytest.mark.parametrize("privilege", ACCOUNT_PRIVILEGES)
def test_each_account_privilege_opens_its_own_statement_only(world, privilege):
    slug = privilege.lower().replace(" ", "_")
    world.setup(f"CREATE ROLE only_{slug}", f"GRANT {privilege} TO only_{slug}",
                f"CREATE USER holder_{slug}", f"GRANT only_{slug} TO holder_{slug}",
                "CREATE USER plain", "CREATE USER doomed", "CREATE ROLE doomed_role")
    statements = {"CREATE USER": "CREATE USER made_by_holder", "ALTER USER": "ALTER USER plain DISABLE",
                  "DROP USER": "DROP USER doomed", "CREATE ROLE": "CREATE ROLE made_by_holder_role",
                  "DROP ROLE": "DROP ROLE doomed_role"}
    holder = world.as_user(user=f"holder_{slug}")
    for other, sql in statements.items():
        if other != privilege:
            expect_blocked(client=holder, sql=sql, why=not_held(statement=other))
    expect_allowed(client=holder, sql=statements[privilege])
    # Positive and negative twin: a user without it is refused the same statement.
    expect_blocked(client=world.as_user(user="carl"), sql=statements[privilege].replace("doomed", "plain"),
                   why=not_held(statement=privilege))


def test_users_beyond_the_session_are_out_of_reach(world):
    amy = world.as_user(user="amy")
    # SYSTEM ADMIN, and the server administrator itself.
    for sql, verb in ((f"ALTER USER sam WITH PASSWORD '{password(name='sam')}'", "changes"),
                      ("ALTER USER sam DISABLE", "changes"), ("DROP USER sam", "drops")):
        statement = " ".join(sql.split()[:2])
        expect_blocked(client=amy, sql=sql,
                       why=f"Hardcharger blocked {statement}: user sam holds SYSTEM ADMIN; {ONLY_SYSTEM_ADMINS} {verb} it")
    for sql in (f"ALTER USER admin WITH PASSWORD '{password(name='admin')}'", "ALTER USER admin DISABLE",
                "DROP USER admin"):
        statement = " ".join(sql.split()[:2])
        expect_blocked(client=amy, sql=sql,
                       why=f"Hardcharger blocked {statement}: admin is the server administrator, which is set in the "
                           "server's configuration and never managed with SQL")
    # A role amy does not hold: bob is an analyst.
    for sql, verb in ((f"ALTER USER bob WITH PASSWORD '{password(name='bob')}'", "changes"),
                      ("ALTER USER bob DISABLE", "changes"), ("DROP USER bob", "drops")):
        statement = " ".join(sql.split()[:2])
        expect_blocked(client=amy, sql=sql, why=beyond(statement=statement, target="bob", verb=verb,
                                                       why="holds role analyst, which user amy does not hold in this session"))
    # Enabling a privileged account is refused the same way.
    world.setup("ALTER USER bob DISABLE")
    expect_blocked(client=amy, sql="ALTER USER bob ENABLE", why=beyond(
        statement="ALTER USER", target="bob", why="holds role analyst, which user amy does not hold in this session"))
    # Twin: dana holds only clerks, which amy holds too.
    expect_allowed(client=amy, sql="ALTER USER dana DISABLE")
    expect_allowed(client=amy, sql="ALTER USER dana ENABLE")

    # An admin option amy could not give.
    world.setup("CREATE USER opal", "GRANT clerks TO opal WITH ADMIN OPTION")
    expect_blocked(client=amy, sql="ALTER USER opal DISABLE", why=beyond(
        statement="ALTER USER", target="opal", why="holds role clerks WITH ADMIN OPTION, which user amy does not"))
    world.setup("GRANT clerks TO amy WITH ADMIN OPTION")
    expect_allowed(client=amy, sql="ALTER USER opal DISABLE")

    # A session attribute (row and masking policies read them) amy does not have.
    world.setup("CREATE USER rhea", "ALTER USER rhea SET ATTRIBUTE region = 'eu'")
    expect_blocked(client=amy, sql="ALTER USER rhea DISABLE", why=beyond(
        statement="ALTER USER", target="rhea",
        why="has the session attribute region, which row and masking policies read, and user amy does not have it "
            "with the same value"))
    world.setup("ALTER USER amy SET ATTRIBUTE region = 'us'")
    expect_blocked(client=amy, sql="ALTER USER rhea DISABLE", why="has the session attribute region")
    world.setup("ALTER USER amy SET ATTRIBUTE region = 'eu'")
    expect_allowed(client=amy, sql="ALTER USER rhea DISABLE")

    # A user that owns something: signing in as it acts as the owner.
    world.setup("CREATE ROLE builders", "GRANT CREATE TABLE ON SCHEMA main TO builders", "CREATE USER owen",
                "GRANT builders TO owen", "GRANT builders TO amy")
    expect_allowed(client=world.as_user(user="owen"), sql="CREATE TABLE owens_table (id INT)")
    expect_blocked(client=amy, sql=f"ALTER USER owen WITH PASSWORD '{password(name='owen')}'",
                   why=", and signing in as it acts as their owner; only an administrator changes it")
    expect_blocked(client=amy, sql="DROP USER owen", why="user owen owns", code="sql_error")  # as before
    world.setup("ALTER TABLE owens_table OWNER TO amy")
    expect_allowed(client=amy, sql=f"ALTER USER owen WITH PASSWORD '{password(name='owen')}'")

    # Session attributes are policy, not account management: never through ALTER USER the privilege.
    for sql in ("ALTER USER carl SET ATTRIBUTE region = 'eu'", "ALTER USER carl UNSET ATTRIBUTE region",
                "ALTER USER amy SET ATTRIBUTE region = '*'"):
        expect_blocked(client=amy, sql=sql,
                       why="Hardcharger blocked ALTER USER: session attributes decide what row and masking policies "
                           "show, and only an administrator sets them (the ALTER USER privilege sets passwords and "
                           "enables and disables users)")
    expect_allowed(client=world.admin, sql="ALTER USER carl SET ATTRIBUTE region = 'eu'")  # the administrator's twin


def test_set_role_narrows_the_reach(world):
    amy = world.as_user(user="amy")
    expect_allowed(client=amy, sql="SET ROLE helpdesk")
    expect_blocked(client=amy, sql="ALTER USER dana DISABLE", why=beyond(
        statement="ALTER USER", target="dana", why="holds role clerks, which user amy does not hold in this session"))
    expect_allowed(client=amy, sql="ALTER USER carl DISABLE")  # PUBLIC only: in reach under any role
    # Acting with clerks alone, the help desk's privileges are not the session's.
    expect_allowed(client=amy, sql="SET ROLE clerks")
    expect_blocked(client=amy, sql="ALTER USER dana DISABLE", why=not_held(statement="ALTER USER"))
    expect_allowed(client=amy, sql="RESET ROLE")
    expect_allowed(client=amy, sql="ALTER USER dana DISABLE")


def test_roles_are_dropped_only_when_nothing_beyond_the_session_goes_with_them(world):
    amy = world.as_user(user="amy")
    world.setup("CREATE ROLE lead", "GRANT analyst TO ROLE lead", "CREATE ROLE empty_one",
                "CREATE ROLE clerk_lead", "GRANT clerks TO ROLE clerk_lead")

    def dropping(role: str, why: str) -> str:
        return f"Hardcharger blocked DROP ROLE: role {role} {why}; only an administrator drops it"

    expect_blocked(client=amy, sql="DROP ROLE analyst", why=dropping(
        role="analyst", why="holds privileges of its own, and user amy does not hold the role in this session"))
    expect_blocked(client=amy, sql="DROP ROLE lead", why=dropping(
        role="lead", why="inherits role analyst, which user amy does not hold in this session"))
    expect_blocked(client=amy, sql="DROP ROLE eu_team",
                   why=dropping(role="eu_team", why="is named by row policy eu_rows on memory.main.ledger"))
    world.setup("CREATE MASKING POLICY hide AS (v VARCHAR) RETURNS VARCHAR -> '***'", "CREATE ROLE unmasked",
                "ALTER TABLE ledger MODIFY COLUMN region SET MASKING POLICY hide EXEMPT ROLE unmasked")
    expect_blocked(client=amy, sql="DROP ROLE unmasked", why=dropping(
        role="unmasked", why="is exempt from the masking policy on memory.main.ledger.region"))
    world.setup("CREATE ROLE dbas", "GRANT SYSTEM ADMIN TO ROLE dbas")
    expect_blocked(client=amy, sql="DROP ROLE dbas",
                   why=f"Hardcharger blocked DROP ROLE: role dbas holds SYSTEM ADMIN; {ONLY_SYSTEM_ADMINS} drops it")
    expect_blocked(client=amy, sql="DROP ROLE PUBLIC", why="the built-in role PUBLIC cannot be dropped", code="sql_error")
    # Twins: a role that holds nothing, and one that inherits only what amy holds.
    expect_allowed(client=amy, sql="DROP ROLE empty_one")
    expect_allowed(client=amy, sql="DROP ROLE clerk_lead")
    expect_allowed(client=world.admin, sql="DROP ROLE lead")


def test_escalation_attempts_are_refused(world):
    amy = world.as_user(user="amy")
    # Granting herself SYSTEM ADMIN, a role she lacks, or an account privilege.
    expect_blocked(client=amy, sql="GRANT SYSTEM ADMIN TO amy", why="grants and revokes SYSTEM ADMIN")
    expect_blocked(client=amy, sql="GRANT analyst TO amy",
                   why="user amy may not grant or revoke role analyst (only an administrator, or a holder of it "
                       "WITH ADMIN OPTION)")
    expect_blocked(client=amy, sql="GRANT CREATE USER TO clerks",
                   why="Hardcharger blocked GRANT: only an administrator grants CREATE USER (an account privilege is "
                       "never delegated)")
    expect_blocked(client=amy, sql="REVOKE CREATE USER FROM helpdesk",
                   why="Hardcharger blocked REVOKE: only an administrator revokes CREATE USER")
    # Creating a role, then trying to put something into it or hand it out.
    expect_allowed(client=amy, sql="CREATE ROLE amys_role")
    for sql, why in (("GRANT amys_role TO amy", "may not grant or revoke role amys_role"),
                     ("GRANT amys_role TO carl", "may not grant or revoke role amys_role"),
                     ("GRANT helpdesk TO ROLE amys_role", "may not grant or revoke role helpdesk"),
                     ("GRANT analyst TO ROLE amys_role", "may not grant or revoke role analyst"),
                     ("GRANT SELECT ON ledger TO amys_role", "user amy may not grant SELECT on"),
                     ("GRANT READ FILESYSTEM TO amys_role", "user amy may not grant READ FILESYSTEM"),
                     ("GRANT SYSTEM ADMIN TO ROLE amys_role", "grants and revokes SYSTEM ADMIN"),
                     ("GRANT DROP USER TO amys_role", "only an administrator grants DROP USER")):
        expect_blocked(client=amy, sql=sql, why=why)
    assert [r for r in rows(client=world.admin, sql="SHOW GRANTS FOR ROLE amys_role")] == []
    # Resetting an administrator's password, dropping the bootstrap administrator.
    expect_blocked(client=amy, sql=f"ALTER USER sam WITH PASSWORD '{password(name='sam')}'", why="holds SYSTEM ADMIN")
    expect_blocked(client=amy, sql="DROP USER admin", why="admin is the server administrator")
    expect_blocked(client=amy, sql="CREATE USER admin", why="admin is the server administrator's name", code="sql_error")
    expect_blocked(client=amy, sql='CREATE USER "SYSTEM ADMIN"', why="is the name of the System Admin capability",
                   code="sql_error")
    # An account privilege granted with an option to pass on, or to everyone, is refused even
    # to the administrator.
    expect_blocked(client=world.admin, sql="GRANT CREATE USER TO helpdesk WITH ADMIN OPTION",
                   why="Hardcharger blocked GRANT: CREATE USER has no ADMIN OPTION; only an administrator grants and "
                       "revokes an account privilege")
    expect_blocked(client=world.admin, sql="GRANT ALTER USER TO PUBLIC",
                   why="Hardcharger blocked GRANT: ALTER USER cannot be granted to the built-in role PUBLIC, which "
                       "every user holds")
    expect_allowed(client=world.admin, sql="GRANT ALTER USER TO clerks")  # the twin: an ordinary role


def test_membership_in_a_help_desk_role_is_the_administrators_to_give(world):
    # carl and opal were made members of desk2 by an admin-option holder before desk2 got
    # CREATE USER: those memberships carry no account privilege, in any order of events.
    world.setup("CREATE ROLE desk2", "GRANT desk2 TO dana WITH ADMIN OPTION", "CREATE USER opal")
    dana = world.as_user(user="dana")
    expect_allowed(client=dana, sql="GRANT desk2 TO carl, opal")
    world.setup("GRANT CREATE USER TO desk2")
    carl = world.as_user(user="carl")
    expect_blocked(client=carl, sql="CREATE USER by_carl", why=not_held(statement="CREATE USER"))

    def listed(client, sql: str) -> list[str]:
        return [r["privilege"] for r in rows(client=client, sql=sql) if r["privilege"] == "CREATE USER"]

    assert listed(client=carl, sql="SHOW GRANTS") == []
    assert listed(client=world.admin, sql="SHOW GRANTS FOR carl") == []
    assert listed(client=world.admin, sql="SHOW GRANTS FOR ROLE desk2") == ["CREATE USER"]
    expect_allowed(client=dana, sql="CREATE USER by_dana")  # dana's membership is the administrator's
    # Its admin option no longer gives membership in it; taking back what it gave stays allowed.
    expect_blocked(client=dana, sql="GRANT desk2 TO bob",
                   why="Hardcharger blocked GRANT: role desk2 holds the account privilege CREATE USER; only an "
                       "administrator grants membership in it")
    expect_allowed(client=dana, sql="REVOKE desk2 FROM opal")
    # The administrator's own grant of carl's membership takes it over: now it carries it.
    world.setup("GRANT desk2 TO carl")
    expect_allowed(client=carl, sql="CREATE USER by_carl")
    assert listed(client=carl, sql="SHOW GRANTS") == ["CREATE USER"]
    assert listed(client=world.admin, sql="SHOW GRANTS FOR carl") == ["CREATE USER"]
    effective = rows(client=world.admin, sql="SELECT user_name, privilege, via FROM hardcharger_effective_privileges('carl') "
                                             "WHERE privilege = 'CREATE USER'")
    assert effective == [{"user_name": "carl", "privilege": "CREATE USER", "via": "desk2"}]


def test_reach_compares_how_each_role_is_held(world):
    """A membership a role's admin-option holder granted carries no policy exemption or account
    privilege; one the administrator granted does. So a help desk that holds a role only the first
    way never resets, enables, expires, drops (or drops a role carrying) a user who holds it the
    second way: signing in as that user would hand over what the help desk does not carry."""
    amy = world.as_user(user="amy")
    world.setup("CREATE USER dave", "CREATE ROLE readers_all", "GRANT SELECT ON ledger TO readers_all",
                "GRANT readers_all TO dave WITH ADMIN OPTION")
    dave = world.as_user(user="dave")
    expect_allowed(client=dave, sql="GRANT readers_all TO amy")  # delegated: carries no exemption
    carol_password = password(name="carol")
    world.setup("GRANT EXEMPT ROW POLICIES TO readers_all", f"CREATE USER carol WITH PASSWORD '{carol_password}'",
                "GRANT readers_all TO carol",
                # An account privilege, carried by ops: amy's membership is dave's, made before it (amy has
                # DROP USER through helpdesk too: what counts is how she holds ops).
                "CREATE ROLE ops", "GRANT ops TO dave WITH ADMIN OPTION", f"CREATE USER erin WITH PASSWORD '{password(name='erin')}'")
    expect_allowed(client=dave, sql="GRANT ops TO amy")
    world.setup("GRANT DROP USER TO ops", "GRANT ops TO erin",
                "CREATE ROLE readers_lead", "GRANT readers_all TO ROLE readers_lead")
    carol = world.server.client(token=world.server.password_token(user="carol", password=carol_password))
    # What each holds: carol is exempt from the ledger's row policies, amy is not.
    assert len(rows(client=carol, sql="SELECT * FROM ledger")) == 2
    assert len(rows(client=amy, sql="SELECT * FROM ledger")) == 0
    def carried(client) -> list[tuple[str, str]]:
        return sorted((r["privilege"], r["granted_to"]) for r in rows(client=client, sql="SHOW GRANTS")
                      if r["granted_to"] in ("readers_all", "ops") and r["privilege"] != "SELECT")

    assert carried(client=amy) == []

    def held(target: str, role: str, verb: str = "changes") -> str:
        return (f"user {target} holds role {role} through a membership an administrator granted, and user amy holds it "
                f"in this session only through one a role's admin-option holder granted (which carries no account "
                f"privilege or policy exemption); only an administrator {verb} it")

    world.setup("CREATE USER parked", "GRANT readers_all TO parked", "ALTER USER parked DISABLE")
    for target, role in (("carol", "readers_all"), ("erin", "ops")):
        for sql in (f"ALTER USER {target} WITH PASSWORD '{password(name=target)}'", f"ALTER USER {target} DISABLE",
                    f"ALTER USER {target} PASSWORD EXPIRE", f"ALTER USER {target} WITH PASSWORD NULL"):
            expect_blocked(client=amy, sql=sql, why=held(target=target, role=role))
        expect_blocked(client=amy, sql=f"DROP USER {target}", why=held(target=target, role=role, verb="drops"))
    # A disabled user is weighed by its memberships all the same.
    expect_blocked(client=amy, sql="ALTER USER parked ENABLE", why=held(target="parked", role="readers_all"))
    assert world.signs_in(user="carol", secret=carol_password)
    # Dropping the role, or a role that inherits it by the administrator's hand, takes away what amy lacks.
    expect_blocked(client=amy, sql="DROP ROLE readers_all",
                   why="Hardcharger blocked DROP ROLE: role readers_all is, or inherits, role readers_all through a "
                       "membership an administrator granted, and user amy holds it in this session only through one")
    expect_blocked(client=amy, sql="DROP ROLE readers_lead",
                   why="Hardcharger blocked DROP ROLE: role readers_lead inherits role readers_all through a membership "
                       "an administrator granted")

    # The twin: the administrator's own grant of amy's memberships makes them carry it, and the
    # same statements are in reach.
    world.setup("GRANT readers_all TO amy", "GRANT ops TO amy")
    assert len(rows(client=amy, sql="SELECT * FROM ledger")) == 2
    assert carried(client=amy) == [("DROP USER", "ops"), ("EXEMPT ROW POLICIES", "readers_all")]
    reset = password(name="carol")
    expect_allowed(client=amy, sql=f"ALTER USER carol WITH PASSWORD '{reset}'")
    assert world.signs_in(user="carol", secret=reset)
    expect_allowed(client=amy, sql="ALTER USER parked ENABLE")
    expect_allowed(client=amy, sql="ALTER USER erin PASSWORD EXPIRE")
    expect_allowed(client=amy, sql="DROP USER erin")
    expect_allowed(client=amy, sql="DROP ROLE readers_lead")


def test_role_inheritance_carries_the_privileges(world):
    world.setup("CREATE ROLE support", "GRANT helpdesk TO ROLE support", "CREATE USER sue", "GRANT support TO sue")
    sue = world.as_user(user="sue")
    expect_allowed(client=sue, sql="CREATE USER made_by_sue")
    expect_allowed(client=sue, sql="SET ROLE support")
    expect_allowed(client=sue, sql="DROP USER made_by_sue")
    expect_allowed(client=sue, sql="RESET ROLE")
    world.setup("REVOKE helpdesk FROM ROLE support")
    expect_blocked(client=sue, sql="CREATE USER made_by_sue", why=not_held(statement="CREATE USER"))
    grants = rows(client=world.admin, sql="SELECT privilege FROM hardcharger_grants() WHERE grantee = 'helpdesk' "
                                          "ORDER BY privilege")
    assert sorted(g["privilege"] for g in grants) == sorted(ACCOUNT_PRIVILEGES)


def test_tenants_stay_with_their_own_admins(world):
    world.setup("CREATE TENANT acme", "CREATE USER acme/tina", "GRANT acme/tenant_admin TO acme/tina",
                "CREATE USER acme/tom")
    amy = world.as_user(user="amy")
    for sql, statement, name in (("CREATE USER acme/ted", "CREATE USER", "acme/ted"),
                                 ("ALTER USER acme/tom DISABLE", "ALTER USER", "acme/tom"),
                                 ("DROP USER acme/tom", "DROP USER", "acme/tom"),
                                 ("CREATE ROLE acme/crew", "CREATE ROLE", "acme/crew"),
                                 ("DROP ROLE acme/PUBLIC", "DROP ROLE", "acme/PUBLIC")):
        expect_blocked(client=amy, sql=sql,
                       why=f"Hardcharger blocked {statement}: {name} belongs to tenant acme, and the {statement} "
                           "privilege reaches system-scope users and roles only (the tenant's own tenant_admin role "
                           "manages the tenant's)")
    # The tenant's own admin manages its users, as before, and nobody else's.
    tina = world.as_user(user="acme/tina")
    expect_allowed(client=tina, sql="CREATE USER acme/ted")
    expect_allowed(client=tina, sql="ALTER USER acme/tom DISABLE")
    expect_blocked(client=tina, sql="CREATE USER outsider", why=not_held(statement="CREATE USER"))
    expect_blocked(client=tina, sql="ALTER USER carl DISABLE", why=not_held(statement="ALTER USER"))
    # A tenant's role never holds an account privilege.
    world.setup("CREATE ROLE acme/crew")
    expect_blocked(client=world.admin, sql="GRANT CREATE USER TO acme/crew", code="sql_error",
                   why="system privileges reach the whole server; a tenant's role (acme/crew) cannot hold them")
    # Twin: amy, in the system scope.
    expect_allowed(client=amy, sql="CREATE USER ted")


def test_limited_sign_ins_never_use_them(world):
    amy = world.as_user(user="amy")
    token = rows(client=amy, sql="CREATE SCOPED TOKEN desk ALLOW SELECT VALID FOR '1 hour'")[0]["token"]
    scoped = world.server.client(token=token)
    expect_blocked(client=scoped, sql="CREATE USER by_token",
                   why="Hardcharger blocked CREATE USER: a scoped token cannot change access control")
    expect_blocked(client=scoped, sql="ALTER USER carl DISABLE", why="a scoped token cannot change access control")
    for rules in ([{"catalog": "*", "access": "write"}], []):
        limited = world.server.user(name="amy", catalog_access=rules)
        for privilege, sql in (("CREATE USER", "CREATE USER by_rules"), ("ALTER USER", "ALTER USER carl DISABLE"),
                               ("DROP ROLE", "DROP ROLE clerks")):
            expect_blocked(client=limited, sql=sql,
                           why=f"Hardcharger blocked {privilege}: this session's sign-in token carries catalog_access "
                               f"rules, and such a session never uses the {privilege} privilege")
    reader = world.server.client(token=mint(user="amy"), options={"read_only": True})
    with pytest.raises(ChargewireError) as read_only:
        reader.run(sql="CREATE USER by_reader")
    assert (read_only.value.code, read_only.value.detail) == ("read_only", "CREATE USER is not allowed in a read-only session")
    readonly = world.server.user(name="amy", role="readonly")
    with pytest.raises(ChargewireError) as readonly_role:
        readonly.run(sql="CREATE USER by_readonly")
    assert readonly_role.value.detail == "User 'amy' has a readonly session and cannot run CREATE USER."
    # The twin: amy's ordinary sign-in.
    expect_allowed(client=amy, sql="CREATE USER by_amy")
    names = {r["name"] for r in rows(client=world.admin, sql="SELECT name FROM hardcharger_users()")}
    assert "by_amy" in names and not names & {"by_token", "by_rules", "by_reader", "by_readonly"}


def test_where_the_store_keeps_them(world, store):
    """In storage_privileges, which a 2.13 server skips (it refuses a store whose
    system_privileges table names a privilege it does not know)."""
    if store.kind != "postgres":
        pytest.skip(reason="reads the PostgreSQL store's tables; the DuckDB store keeps the same tables")
    import psycopg
    with psycopg.connect(conninfo=world.server.store_uri, autocommit=True) as connection:
        system = connection.execute(query="SELECT privilege FROM hardcharger_rbac.system_privileges").fetchall()
        storage = connection.execute(query="SELECT privilege, location, admin_option, granted_by FROM "
                                           "hardcharger_rbac.storage_privileges ORDER BY privilege").fetchall()
    assert not {p for (p,) in system} & set(ACCOUNT_PRIVILEGES)
    assert storage == [(p, "", False, "") for p in sorted(ACCOUNT_PRIVILEGES)]


def test_a_revoke_counts_at_the_next_statement_on_every_replica(launcher, pg_store):
    uri = pg_store.new_database()
    first = start_world(launcher=launcher, store_uri=uri)
    second = start_rbac_server(launcher=launcher, store_uri=uri)
    first.setup("CREATE ROLE desk3", "CREATE USER zoe", "GRANT desk3 TO zoe")
    deadline = time.monotonic() + 15 * SCALE
    while "zoe" not in {r["name"] for r in rows(client=second.admin(), sql="SELECT name FROM hardcharger_users()")}:
        assert time.monotonic() < deadline, "zoe never reached the second replica"
        time.sleep(0.1)
    zoe = second.user(name="zoe")
    expect_blocked(client=zoe, sql="CREATE USER made_on_second", why=not_held(statement="CREATE USER"))
    # A grant made on the first replica counts on the second at once.
    first.setup("GRANT CREATE USER, DROP USER TO desk3")
    expect_allowed(client=zoe, sql="CREATE USER made_on_second")
    # Taken away on the first replica: the very next statement on the second is refused,
    # without waiting for the second to poll the store.
    first.setup("REVOKE CREATE USER FROM desk3")
    expect_blocked(client=zoe, sql="CREATE USER made_after_revoke", why=not_held(statement="CREATE USER"))
    expect_allowed(client=zoe, sql="DROP USER made_on_second")  # the twin: DROP USER stays
    first.setup("REVOKE desk3 FROM zoe")
    expect_blocked(client=zoe, sql="CREATE USER made_after_revoke", why=not_held(statement="CREATE USER"))
    expect_blocked(client=zoe, sql="DROP USER carl", why=not_held(statement="DROP USER"))
    names = {r["name"] for r in rows(client=first.admin, sql="SELECT name FROM hardcharger_users()")}
    assert "made_after_revoke" not in names and "carl" in names
