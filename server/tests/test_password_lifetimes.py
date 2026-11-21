"""How long a password lasts, and guessing the current password in a user's own change, on both stores.

- A password older than password_max_age_days still signs in, but held like a temporary one: its
  session runs nothing but ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>', so its
  user renews it without an administrator (--verify-credentials says password_must_change).
- A temporary password (a help desk's reset, an administrator's EXPIRE) stops working after
  password_temporary_max_age_hours (72 by default; 0: no limit): its sign-in is refused, saying an
  administrator must set a new one.
- A wrong current password in ALTER USER ... REPLACE is a failed sign-in of that user from the
  session's client address in the login guard, and a locked pair refuses the change the same way a
  sign-in is refused.
- With login_lockout_threshold and login_address_backoff_after both 0 (GizmoData Cloud's gateway
  clusters until their proxy secret is set), only the per-user delay is left.

Dates are moved back in the stopped server's store: with psycopg on PostgreSQL, and on DuckDB
through a second, plain server that attaches the store file (Python's duckdb can't open a DuckDB
2.0 file).
"""

import json
import pathlib
import subprocess
import time

import psycopg
import pytest

from conftest import BINARY, TIMEOUT_SCALE, clean_environment
from rbac import SECRET_KEY, ChargewireError, RbacServer, expect_allowed, expect_blocked, start_rbac_server
from test_login_guard import DEFAULTS, LOCKED_DETAIL, PROXIED, fail, limited, refused, sign_in

pytestmark = pytest.mark.rbac_store

JOE_PASSWORD = "joe-long-password-1"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def start(launcher, store_uri: str, env: dict[str, str] | None = None) -> RbacServer:
    return start_rbac_server(launcher=launcher, store_uri=store_uri, env={**DEFAULTS, **(env or {})}, args=PROXIED)


def move_back(launcher, store_uri: str, table: str, column: str, by_ms: int) -> None:
    """Moves `column` of every row of the store's `table` back by `by_ms` (the server is stopped)."""
    sql = f"UPDATE {{schema}}.{table} SET {column} = {column} - {by_ms} WHERE {column} IS NOT NULL"
    if store_uri.startswith("duckdb:"):
        plain = RbacServer(running=launcher.start(), store_uri="")
        file = pathlib.Path(store_uri.removeprefix("duckdb:"))
        admin = plain.admin()
        admin.run(sql=f"ATTACH '{file}' AS aged_store")
        changed = admin.run(sql=sql.format(schema="aged_store.hardcharger_rbac")).to_pylist()
        admin.run(sql="DETACH aged_store")
        admin.close()
        plain.running.stop()
        assert changed and changed[0].get("Count", changed[0].get("rows_affected")) == 1, changed
        return
    with psycopg.connect(conninfo=store_uri, autocommit=True) as connection:
        assert connection.execute(query=sql.format(schema="hardcharger_rbac")).rowcount == 1


def verify(store_uri: str, admin_password: str, credential: dict, env: dict[str, str] | None = None) -> tuple[int, dict]:
    run = subprocess.run(args=[str(BINARY), "--verify-credentials"],
                         env={**clean_environment(), "HARDCHARGER_ADMIN_USERNAME": "admin",
                              "HARDCHARGER_ADMIN_PASSWORD": admin_password, "HARDCHARGER_RBAC_STORE_URI": store_uri,
                              "HARDCHARGER_SECRET_KEY": SECRET_KEY, **DEFAULTS, **(env or {})},
                         input=json.dumps(credential), capture_output=True, text=True, timeout=120 * TIMEOUT_SCALE)
    lines = run.stdout.splitlines()
    assert len(lines) == 1, f"{run.stdout!r}\n{run.stderr}"
    return run.returncode, json.loads(lines[0])


def joe(password: str) -> dict:
    return {"grant_type": "password", "username": "joe", "password": password}


def held_message(user: str, why: str) -> str:
    return (f"Hardcharger blocked this statement: the password of user {user} {why} and must be changed before "
            "anything else runs: ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'")


def must_change(server: RbacServer, user: str) -> bool:
    rows = server.admin().run(sql=f"SELECT password_must_change FROM hardcharger_users() WHERE name = '{user}'")
    return rows.to_pylist()[0]["password_must_change"]


# ---- password_max_age_days: an old password signs in held ----

@pytest.mark.parametrize("age_days, held, read_only", [(91, True, False), (91, True, True), (89, False, False)])
def test_a_password_past_its_max_age_signs_in_held_until_its_user_changes_it(launcher, store, age_days, held, read_only):
    uri = store.new_database()
    env = {"HARDCHARGER_PASSWORD_MAX_AGE_DAYS": "90"}
    server = start(launcher=launcher, store_uri=uri, env=env)
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")
    admin_password = server.running.password
    server.running.stop()
    move_back(launcher=launcher, store_uri=uri, table="users", column="password_changed_at_ms",
              by_ms=age_days * DAY_MS)
    code, answer = verify(store_uri=uri, admin_password=admin_password, credential=joe(password=JOE_PASSWORD), env=env)
    assert code == 0 and answer["verified"] is True, answer
    assert answer.get("password_must_change", False) is held, answer
    server = start(launcher=launcher, store_uri=uri, env=env)
    session = server.client(token=server.password_token(user="joe", password=JOE_PASSWORD),
                            options={"read_only": True} if read_only else None)
    assert must_change(server=server, user="joe") is held
    if not held:
        assert session.run(sql="SELECT 1 AS one").to_pylist() == [{"one": 1}]
        return
    expect_blocked(client=session, sql="SELECT 1", why=held_message(user="joe", why="has expired (password_max_age_days)"))
    expect_allowed(client=session, sql=f"ALTER USER CURRENT_USER WITH PASSWORD 'joe-renewed-password' "
                                       f"REPLACE '{JOE_PASSWORD}'")
    # The new password is dated today: not held.
    renewed = server.user(name="joe", password="joe-renewed-password")
    assert renewed.run(sql="SELECT 1 AS one").to_pylist() == [{"one": 1}]
    assert must_change(server=server, user="joe") is False
    with pytest.raises(ChargewireError) as old:
        server.password_token(user="joe", password=JOE_PASSWORD)
    assert old.value.code == "invalid_credentials", old.value


# ---- password_temporary_max_age_hours: a temporary password stops working ----

@pytest.mark.parametrize("age_hours, limit, works", [(73, None, False), (71, None, True), (73, "0", True),
                                                     (25, "24", False)])
def test_a_temporary_password_stops_working_after_its_limit(launcher, store, age_hours, limit, works):
    uri = store.new_database()
    env = {} if limit is None else {"HARDCHARGER_PASSWORD_TEMPORARY_MAX_AGE_HOURS": limit}
    server = start(launcher=launcher, store_uri=uri, env=env)
    admin = server.admin()
    expect_allowed(client=admin, sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}' EXPIRE")
    admin_password = server.running.password
    server.running.stop()
    move_back(launcher=launcher, store_uri=uri, table="users", column="password_reset_at_ms", by_ms=age_hours * HOUR_MS)
    # (A DuckDB store serves one process at a time: the verifier first.)
    code, answer = verify(store_uri=uri, admin_password=admin_password, credential=joe(password=JOE_PASSWORD), env=env)
    server = start(launcher=launcher, store_uri=uri, env=env)
    response = sign_in(server=server, user="joe", password=JOE_PASSWORD)
    if works:
        assert response.status_code == 200, response.text
        session = server.client(token=response.json()["access_token"])
        expect_blocked(client=session, sql="SELECT 1", why=held_message(user="joe", why="was set by someone else"))
        assert (code, answer["verified"], answer.get("password_must_change")) == (0, True, True), answer
        return
    hours = limit or "72"
    refused(response=response)
    assert response.json()["detail"] == (f"the temporary password of user joe has expired (it was set more than {hours} "
                                         "hours ago): an administrator must set a new one"), response.text
    assert code == 1 and answer["verified"] is False and "has expired" in answer["message"], answer
    # Said only to whoever gave the password: a wrong one is the ordinary refusal.
    wrong = sign_in(server=server, user="joe", password="not-joes-password")
    refused(response=wrong)
    assert "expired" not in wrong.json()["detail"], wrong.text
    # It counted as no failure: the right-but-expired attempts left joe's pair clean.
    assert server.admin().run(sql="SELECT failed_sign_ins FROM hardcharger_users() WHERE name = 'joe'").to_pylist() \
        == [{"failed_sign_ins": 1}]
    # An administrator sets a new one, which works.
    expect_allowed(client=server.admin(), sql="ALTER USER joe WITH PASSWORD 'joe-fresh-password'")
    assert sign_in(server=server, user="joe", password="joe-fresh-password").status_code == 200


# ---- A wrong current password in ALTER USER ... REPLACE is a failed sign-in ----

def joe_session(server: RbacServer, address: str):
    response = sign_in(server=server, user="joe", password=JOE_PASSWORD, address=address)
    assert response.status_code == 200, response.text
    session = server.client(token=response.json()["access_token"])
    session.http.headers["X-Forwarded-For"] = address  # every statement comes from there too
    return session


def replace(new: str, current: str) -> str:
    return f"ALTER USER CURRENT_USER WITH PASSWORD '{new}' REPLACE '{current}'"


WRONG_CURRENT = "Hardcharger blocked ALTER USER: the current password given with REPLACE is not user joe's password"


def test_wrong_current_passwords_lock_the_pair_like_failed_sign_ins(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_LOGIN_USER_BACKOFF_AFTER": "0"})
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")
    session = joe_session(server=server, address="203.0.113.9")
    for i in range(5):
        expect_blocked(client=session, sql=replace(new="joe-next-password", current=f"guess-{i}"), why=WRONG_CURRENT)
    assert server.admin().run(sql="SELECT failed_sign_ins FROM hardcharger_users() WHERE name = 'joe'").to_pylist() \
        == [{"failed_sign_ins": 5}]
    # The pair is locked: the change is refused unchecked, even with the right password ...
    with pytest.raises(ChargewireError) as locked:
        session.run(sql=replace(new="joe-next-password", current=JOE_PASSWORD))
    assert locked.value.code == "rate_limited", locked.value
    assert locked.value.detail.startswith("Hardcharger blocked ALTER USER: " + LOCKED_DETAIL), locked.value
    # ... and so is joe's sign-in from that address, as after five failed sign-ins.
    limited(response=sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9"))
    # The twin: from another address joe signs in and changes the password.
    elsewhere = joe_session(server=server, address="192.0.2.10")
    expect_allowed(client=elsewhere, sql=replace(new="joe-next-password", current=JOE_PASSWORD))
    log = server.log()
    assert "sign-in locked for a user and client address" in log, log


def test_four_wrong_current_passwords_then_the_right_one_changes_it(launcher, store):
    # The positive twin: below the threshold the change goes through, and a right current
    # password is no sign-in (last_sign_in_at stays the sign-in's).
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_LOGIN_USER_BACKOFF_AFTER": "0"})
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")
    session = joe_session(server=server, address="203.0.113.9")
    for i in range(4):
        expect_blocked(client=session, sql=replace(new="joe-next-password", current=f"guess-{i}"), why=WRONG_CURRENT)
    expect_allowed(client=session, sql=replace(new="joe-next-password", current=JOE_PASSWORD))
    assert sign_in(server=server, user="joe", password="joe-next-password", address="203.0.113.9").status_code == 200


def test_a_held_sessions_wrong_current_password_counts_too(launcher, store):
    # A temporary password's session runs only the change: guessing through it is limited too.
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_LOGIN_USER_BACKOFF_AFTER": "0"})
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}' EXPIRE")
    session = joe_session(server=server, address="203.0.113.9")
    for i in range(5):
        expect_blocked(client=session, sql=replace(new="joe-next-password", current=f"guess-{i}"), why=WRONG_CURRENT)
    with pytest.raises(ChargewireError) as locked:
        session.run(sql=replace(new="joe-next-password", current=JOE_PASSWORD))
    assert locked.value.code == "rate_limited", locked.value


# ---- GizmoData Cloud's interim settings: only the per-user delay is left ----

def test_no_lock_and_no_address_delay_leave_only_the_per_user_delay(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_LOGIN_LOCKOUT_THRESHOLD": "0", "HARDCHARGER_LOGIN_ADDRESS_BACKOFF_AFTER": "0",
                        # A first delay long enough to be seen on a busy machine.
                        "HARDCHARGER_LOGIN_BACKOFF_BASE_SECONDS": "5"})
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")
    # Everyone behind one address (a gateway): 25 users failing once each are never slowed (the
    # default per-address delay starts after 20).
    for i in range(25):
        fail(server=server, user=f"u{i}", address="203.0.113.9")
    # One user failing from that address: the per-user delay (after 5), never a lock.
    fail(server=server, user="joe", address="203.0.113.9", times=5)
    retry_after = limited(response=sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9"))
    assert retry_after <= 5, retry_after
    locks = server.admin().run(sql="SELECT scope, user_name, locked FROM hardcharger_login_locks() "
                                   "WHERE retry_after_seconds > 0 ORDER BY scope").to_pylist()
    assert locks == [{"scope": "user", "user_name": "joe", "locked": False}], locks
    # Other users at that address are not held up by joe's failures.
    expect_allowed(client=server.admin(), sql="CREATE USER amy WITH PASSWORD 'amy-long-password-1'")
    assert sign_in(server=server, user="amy", password="amy-long-password-1", address="203.0.113.9").status_code == 200
    # Once the delay has passed, joe's right password works from the same address: no lock.
    time.sleep(retry_after)
    assert sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9").status_code == 200


def test_the_default_settings_lock_that_pair_and_slow_that_address(launcher, store):
    # The negative twin of the one above: with the defaults, the same failures lock joe's pair
    # and slow the shared address.
    server = start(launcher=launcher, store_uri=store.new_database(), env={"HARDCHARGER_LOGIN_BACKOFF_BASE_SECONDS": "5"})
    for i in range(20):
        fail(server=server, user=f"u{i}", address="203.0.113.9")
    limited(response=sign_in(server=server, user="u99", password="wrong-password", address="203.0.113.9"))
    server2 = start(launcher=launcher, store_uri=store.new_database(), env={"HARDCHARGER_LOGIN_USER_BACKOFF_AFTER": "0"})
    expect_allowed(client=server2.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")
    fail(server=server2, user="joe", address="203.0.113.9", times=5)
    assert limited(response=sign_in(server=server2, user="joe", password=JOE_PASSWORD, address="203.0.113.9")) > 60
