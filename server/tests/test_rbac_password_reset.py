"""Temporary passwords and changing one's own password.

A password someone else sets for a user is temporary when a delegate sets it (a holder of the
ALTER USER or CREATE USER privilege, a tenant admin), and when an administrator says so (`WITH
PASSWORD '...' EXPIRE`, or `ALTER USER x PASSWORD EXPIRE`). A sign-in with a temporary password
succeeds, but its session runs nothing until the user changes it with `ALTER USER CURRENT_USER
WITH PASSWORD '<new>' REPLACE '<current>'`, which every user may run for itself and which needs
the current password, so a session (or a stolen access token) alone never changes it. A help
desk that resets a password and signs in with it can therefore only change it, which the audit
records and the real user notices at its next sign-in.
"""

import pytest

from rbac import ChargewireError, expect_allowed, expect_blocked
from test_rbac_account_privileges import password, rows, start_world
from test_rbac_audit_chain import KEY
from test_rbac_system_admin_audit import about, audit_records


def must_change(user: str) -> str:
    return (f"the password of user {user} was set by someone else and must be changed before anything else runs: "
            "ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'")


def change(new: str, current: str, who: str = "CURRENT_USER") -> str:
    return f"ALTER USER {who} WITH PASSWORD '{new}' REPLACE '{current}'"


def assert_restricted(client, user: str) -> None:
    """The session runs nothing but the change of its password."""
    for sql in ("SELECT 1", "SELECT * FROM hardcharger_users()", "SHOW GRANTS", "SET ROLE helpdesk",
                "CREATE TABLE t (i INT)", f"ALTER USER {user} DISABLE"):
        expect_blocked(client=client, sql=sql, why="Hardcharger blocked this statement: " + must_change(user=user))
    for what, call in (("preparing a statement", lambda: client.prepare(sql="SELECT 1")),
                       ("a metadata request", lambda: client.metadata(kind="catalogs"))):
        with pytest.raises(ChargewireError) as refused:
            call()
        assert (refused.value.code, refused.value.detail) == ("permission_denied",
                                                              f"Hardcharger blocked {what}: " + must_change(user=user))


def signs_in(world, user: str, secret: str) -> bool:
    try:
        world.server.password_token(user=user, password=secret)
        return True
    except ChargewireError as error:
        assert error.code == "invalid_credentials", error
        return False


@pytest.fixture
def world(launcher, store):
    return start_world(launcher=launcher, store_uri=store.new_database(),
                       env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": KEY})


def test_a_password_the_help_desk_sets_is_temporary(world):
    amy = world.as_user(user="amy")
    temporary, chosen = password(name="carl-temporary"), password(name="carl-chosen")
    result = rows(client=amy, sql=f"ALTER USER carl WITH PASSWORD '{temporary}'")
    assert result == [{"result": "Temporary password set for user carl: it must be changed at the next sign-in "
                                 "(ALTER USER CURRENT_USER WITH PASSWORD ... REPLACE ...)"}]
    assert rows(client=world.admin, sql="SELECT password_must_change FROM hardcharger_users() WHERE name = 'carl'") \
        == [{"password_must_change": True}]
    # carl signs in with it: the session runs only the change.
    carl = world.server.user(name="carl", password=temporary)
    assert_restricted(client=carl, user="carl")
    expect_blocked(client=carl, sql=change(new=chosen, current="not-the-password"),
                   why="Hardcharger blocked ALTER USER: the current password given with REPLACE is not user carl's "
                       "password")
    expect_allowed(client=carl, sql=change(new=chosen, current=temporary))
    # The change ends carl's sessions (the access token was issued for the old password).
    with pytest.raises(ChargewireError) as ended:
        carl.run(sql="SELECT 1")
    assert ended.value.code in ("invalid_token", "session_ended"), ended.value
    assert not signs_in(world=world, user="carl", secret=temporary)
    carl = world.server.user(name="carl", password=chosen)
    assert rows(client=carl, sql="SELECT 1 AS one") == [{"one": 1}]
    assert rows(client=carl, sql="SELECT name, password_must_change FROM hardcharger_users()") == \
        [{"name": "carl", "password_must_change": False}]
    # A user the help desk creates with a password starts the same way.
    first = password(name="eve-first")
    expect_allowed(client=amy, sql=f"CREATE USER eve WITH PASSWORD '{first}'")
    eve = world.server.user(name="eve", password=first)
    assert_restricted(client=eve, user="eve")
    expect_allowed(client=eve, sql=change(new=password(name="eve"), current=first, who="eve"))  # its own name works too


def test_the_help_desk_using_a_temporary_password_can_only_change_it(world):
    amy = world.as_user(user="amy")
    temporary, hijacked = password(name="carl-temporary"), password(name="hijacked")
    expect_allowed(client=amy, sql=f"ALTER USER carl WITH PASSWORD '{temporary}'")
    as_carl = world.server.user(name="carl", password=temporary)
    assert_restricted(client=as_carl, user="carl")
    expect_allowed(client=as_carl, sql=change(new=hijacked, current=temporary))
    # The real carl finds neither his old password nor the temporary one working.
    assert not signs_in(world=world, user="carl", secret=world.passwords["carl"])
    assert not signs_in(world=world, user="carl", secret=temporary)

    records = audit_records(server=world.server)
    carl_id = about(records, statement="CREATE USER", target="user carl")[0]["target_ids"]
    reset = about(records, statement="ALTER USER", user="amy")
    assert len(reset) == 1 and reset[0]["password"] == "temporary password set", reset
    assert reset[0]["authorized_by"] == "ALTER USER privilege, through role helpdesk"
    assert reset[0]["after"].endswith("; password must be changed"), reset
    refused = about(records, message="statement refused until the password is changed", user="carl")
    assert {r.get("sql") for r in refused} >= {"SELECT 1", "SHOW GRANTS"}, refused
    assert {r["request"] for r in refused} >= {"this statement", "preparing a statement", "a metadata request"}
    assert all(r["user_id"] == carl_id and r["session_id"] == as_carl.session_id and r["status"] == "rejected"
               for r in refused)
    changed = about(records, statement="ALTER USER", user="carl", status="success")
    assert len(changed) == 1, changed
    assert (changed[0]["password"], changed[0]["authorized_by"], changed[0]["target_ids"]) == \
        ("password changed by the user", "the user's own current password", carl_id)
    assert not changed[0]["after"].endswith("password must be changed")
    log = world.server.log()
    for secret in (temporary, hijacked, world.passwords["carl"], "pbkdf2-sha256$"):
        assert secret not in log


def test_every_user_changes_its_own_password_with_the_current_one(world):
    world.setup("CREATE USER nopw")
    current = world.passwords["carl"]
    carl = world.server.user(name="carl", password=current)  # holds nothing but PUBLIC
    expect_blocked(client=carl, sql=f"ALTER USER carl WITH PASSWORD '{password(name='x')}'",
                   why="Hardcharger blocked ALTER USER: to change your own password, give the current one: ALTER "
                       "USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'")
    expect_blocked(client=carl, sql=f"ALTER USER CURRENT_USER WITH PASSWORD '{password(name='x')}'",
                   why="to change your own password, give the current one")
    expect_blocked(client=carl, sql=change(new=password(name="x"), current="wrong"),
                   why="the current password given with REPLACE is not user carl's password")
    expect_blocked(client=carl, sql=change(new=password(name="x"), current=current, who="dana"),
                   why="REPLACE gives your own current password, so it changes only your own password, not user "
                       "dana's")
    with pytest.raises(ChargewireError) as syntax:
        carl.run(sql=f"ALTER USER CURRENT_USER WITH PASSWORD 'a' REPLACE '{current}' EXPIRE")
    assert "a password you set for yourself (REPLACE) is not temporary" in syntax.value.detail
    assert world.signs_in(user="carl", secret=current)  # nothing changed so far
    chosen = password(name="carl-chosen")
    assert rows(client=carl, sql=change(new=chosen, current=current)) == \
        [{"result": "Password changed for user carl: sign in again with the new one"}]
    assert not world.signs_in(user="carl", secret=current) and world.signs_in(user="carl", secret=chosen)
    # Not temporary: the next session runs anything carl may.
    assert rows(client=world.server.user(name="carl", password=chosen), sql="SELECT 1 AS one") == [{"one": 1}]
    # A user who signs in another way has no password to replace.
    expect_blocked(client=world.as_user(user="nopw"), sql=change(new=password(name="n"), current="anything"),
                   why="user nopw has no password to replace (it signs in another way); an administrator sets its "
                       "first one")
    # A SYSTEM ADMIN holder changes its own too, without acting as one.
    old, new = password(name="sam-old"), password(name="sam-new")
    world.setup(f"ALTER USER sam WITH PASSWORD '{old}'")
    sam = world.server.user(name="sam", password=old)
    expect_allowed(client=sam, sql=change(new=new, current=old))
    assert world.signs_in(user="sam", secret=new)
    # The administrator is no store user.
    expect_blocked(client=world.admin, sql=change(new="x", current="y"),
                   why="Hardcharger blocked ALTER USER CURRENT_USER: the administrator is not a user of the access "
                       "control store; its password is set in the server's configuration")


def test_an_administrator_decides_with_expire(world):
    plain, temporary = password(name="plain"), password(name="temporary")
    world.setup(f"ALTER USER carl WITH PASSWORD '{plain}'")
    assert rows(client=world.server.user(name="carl", password=plain), sql="SELECT 1 AS one") == [{"one": 1}]
    assert rows(client=world.admin, sql=f"ALTER USER carl WITH PASSWORD '{temporary}' EXPIRE")[0]["result"].startswith(
        "Temporary password set for user carl")
    assert_restricted(client=world.server.user(name="carl", password=temporary), user="carl")
    # A password set without EXPIRE afterwards is not temporary.
    world.setup(f"ALTER USER carl WITH PASSWORD '{plain}'")
    carl = world.server.user(name="carl", password=plain)
    assert rows(client=carl, sql="SELECT 1 AS one") == [{"one": 1}]
    # PASSWORD EXPIRE makes the current password temporary: carl's open session is held at its
    # next statement.
    assert rows(client=world.admin, sql="ALTER USER carl PASSWORD EXPIRE") == \
        [{"result": "Password of user carl expired: it must be changed at the next statement"}]
    assert_restricted(client=carl, user="carl")
    expect_allowed(client=carl, sql=change(new=password(name="carl-own"), current=plain))
    # CREATE USER ... EXPIRE, and without.
    first, other = password(name="newbie"), password(name="regular")
    world.setup(f"CREATE USER newbie WITH PASSWORD '{first}' EXPIRE", f"CREATE USER regular WITH PASSWORD '{other}'")
    assert_restricted(client=world.server.user(name="newbie", password=first), user="newbie")
    assert rows(client=world.server.user(name="regular", password=other), sql="SELECT 1 AS one") == [{"one": 1}]
    flags = {r["name"]: r["password_must_change"] for r in rows(
        client=world.admin, sql="SELECT name, password_must_change FROM hardcharger_users()")}
    assert flags["newbie"] is True and flags["regular"] is False and flags["carl"] is False
    # The help desk may expire a password in its reach, too; EXPIRE there changes nothing (always temporary).
    expect_allowed(client=world.as_user(user="amy"), sql="ALTER USER regular PASSWORD EXPIRE")
    assert_restricted(client=world.server.user(name="regular", password=other), user="regular")
    expect_blocked(client=world.as_user(user="amy"), sql="ALTER USER bob PASSWORD EXPIRE", why="holds role analyst")


def test_a_token_sign_in_is_not_held_by_a_temporary_password(world):
    # The temporary password is what is held, not the user: a session that signed in another way
    # (an identity provider's or the server's token) is unaffected.
    expect_allowed(client=world.as_user(user="amy"), sql=f"ALTER USER carl WITH PASSWORD '{password(name='t')}'")
    assert rows(client=world.server.user(name="carl"), sql="SELECT 1 AS one") == [{"one": 1}]


def test_a_tenant_admins_reset_is_temporary_too(world):
    world.setup("CREATE TENANT acme", "CREATE USER acme/tina", "GRANT acme/tenant_admin TO acme/tina",
                "CREATE USER acme/tom")
    temporary = password(name="tom")
    expect_allowed(client=world.as_user(user="acme/tina"), sql=f"ALTER USER acme/tom WITH PASSWORD '{temporary}'")
    assert_restricted(client=world.server.user(name="acme/tom", password=temporary), user="acme/tom")


def test_the_reset_marks_only_that_password(world, store):
    """A record of a temporary password names the password it was made for: once any other
    password is set (by a 2.13 replica too, which never reads the record), it holds nothing."""
    temporary = password(name="temporary")
    expect_allowed(client=world.as_user(user="amy"), sql=f"ALTER USER carl WITH PASSWORD '{temporary}'")
    if store.kind == "postgres":
        import psycopg
        with psycopg.connect(conninfo=world.server.store_uri, autocommit=True) as connection:
            stored = connection.execute(query="SELECT r.password_fingerprint, u.password_hash FROM "
                                              "hardcharger_rbac.password_resets r JOIN hardcharger_rbac.users u "
                                              "USING (user_id) WHERE u.name = 'carl'").fetchall()
        assert len(stored) == 1 and len(stored[0][0]) == 32 and stored[0][0] not in stored[0][1], stored
    plain = password(name="plain")
    world.setup(f"ALTER USER carl WITH PASSWORD '{plain}'")
    assert rows(client=world.server.user(name="carl", password=plain), sql="SELECT 1 AS one") == [{"one": 1}]
    # Dropping the user takes its record with it.
    expect_allowed(client=world.as_user(user="amy"), sql=f"ALTER USER carl WITH PASSWORD '{temporary}'")
    world.setup("DROP USER carl")
    if store.kind == "postgres":
        import psycopg
        with psycopg.connect(conninfo=world.server.store_uri, autocommit=True) as connection:
            assert connection.execute(query="SELECT count(*) FROM hardcharger_rbac.password_resets").fetchone() == (0,)
