"""The limits on failed password sign-ins (login guard), through the server, on both stores.

A (user, client address) pair locks after login_lockout_threshold failures for
login_lockout_seconds; per user (any address) and per address (any user) only a growing delay,
never a lock, and a user's recent sign-in address skips the delays. A refusal is 429 rate_limited
with Retry-After, before the password is checked, and the same for a user that doesn't exist. A
success resets its pair. Records go to the access-control store, so a restart, another replica and
hardcharger-server --verify-credentials (GizmoData Cloud's auto-resume verifier) see the same
counts. An administrator lists and clears them in SQL. Also the password policy (length,
optional character classes, optional expiry) and hardcharger_users()' sign-in columns.

The clients' addresses come from X-Forwarded-For, which the servers here believe from
127.0.0.1 (trusted_proxies), as a server behind a proxy would. The rules' arithmetic with the
clock in hand (expiry of a 15-minute lock, the delays' growth and cap, the known-address window)
is in unit/login_guard_test.cc; nothing here waits more than a few seconds.
"""

import concurrent.futures
import json
import subprocess
import time

import httpx
import pytest

from conftest import BINARY, TIMEOUT_SCALE, clean_environment
from rbac import SECRET_KEY, ChargewireError, expect_allowed, start_rbac_server

pytestmark = pytest.mark.rbac_store

# The documented defaults, over the launcher's lenient ones (conftest.LENIENT_SIGN_IN).
DEFAULTS = {"HARDCHARGER_LOGIN_LOCKOUT_THRESHOLD": "5", "HARDCHARGER_LOGIN_BACKOFF_MAX_SECONDS": "30",
            "HARDCHARGER_PASSWORD_MIN_LENGTH": "12"}
PROXIED = ["--trusted-proxies", "127.0.0.1"]
JOE_PASSWORD = "joe-long-password-1"
LOCKED_DETAIL = "too many failed sign-in attempts"


def start(launcher, store_uri: str, env: dict[str, str] | None = None, args: list[str] | None = None):
    return start_rbac_server(launcher=launcher, store_uri=store_uri, env={**DEFAULTS, **(env or {})},
                             args=[*PROXIED, *(args or [])])


def sign_in(server, user: str, password: str, address: str | None = None) -> httpx.Response:
    headers = {"X-Forwarded-For": address} if address else {}
    return httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token", headers=headers, timeout=120,
                      json={"grant_type": "password", "username": user, "password": password})


def refused(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "invalid_credentials", response.text


def limited(response: httpx.Response) -> int:
    """Asserts a guard refusal and returns its Retry-After."""
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "rate_limited", body
    assert LOCKED_DETAIL in body["detail"], body
    retry_after = int(response.headers["Retry-After"])
    assert retry_after >= 1, response.headers
    return retry_after


def fail(server, user: str, address: str, times: int = 1) -> None:
    for _ in range(times):
        refused(sign_in(server=server, user=user, password="wrong-password", address=address))


def admin_password(server) -> str:
    return server.running.password


def create_joe(server) -> None:
    expect_allowed(client=server.admin(), sql=f"CREATE USER joe WITH PASSWORD '{JOE_PASSWORD}'")


# ---- The pair lock ----

def test_five_failures_lock_the_pair_and_then_even_the_right_password_waits(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    fail(server=server, user="admin", address="203.0.113.9", times=5)
    retry_after = limited(sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9"))
    assert 880 <= retry_after <= 900, retry_after
    # Another wrong password from there is refused the same way, uncounted, and unchecked.
    limited(sign_in(server=server, user="admin", password="wrong-password", address="203.0.113.9"))
    log = server.log()
    assert "status=locked" in log and "client_address=203.0.113.9" in log, log
    assert "sign-in locked for a user and client address" in log, log  # the audit record


def test_the_locked_user_still_signs_in_from_another_address(launcher, store):
    # The negative twin: a lock never keeps anyone out from everywhere, the administrator included.
    server = start(launcher=launcher, store_uri=store.new_database())
    assert sign_in(server=server, user="admin", password=admin_password(server), address="192.0.2.10").status_code == 200
    fail(server=server, user="admin", address="203.0.113.9", times=5)
    limited(sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9"))
    # An address it signed in from before: at once.
    assert sign_in(server=server, user="admin", password=admin_password(server), address="192.0.2.10").status_code == 200
    # A new address: after the user's short delay (1 s after its 5th failure), never a lock.
    response = sign_in(server=server, user="admin", password=admin_password(server), address="198.51.100.20")
    if response.status_code == 429:
        time.sleep(limited(response=response))
        response = sign_in(server=server, user="admin", password=admin_password(server), address="198.51.100.20")
    assert response.status_code == 200, response.text


def test_a_store_user_locks_the_same_way_and_a_success_resets_its_pair(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    create_joe(server=server)
    fail(server=server, user="joe", address="203.0.113.9", times=4)
    assert sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9").status_code == 200
    # The count starts again: four more failures are not five.
    fail(server=server, user="joe", address="203.0.113.9", times=4)
    assert sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9").status_code == 200
    fail(server=server, user="joe", address="203.0.113.9", times=5)
    limited(sign_in(server=server, user="joe", password=JOE_PASSWORD, address="203.0.113.9"))


def test_an_unknown_user_gets_exactly_the_answers_a_known_one_gets(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    create_joe(server=server)

    def answers(user: str, address: str) -> list[tuple]:
        out = []
        for _ in range(7):
            response = sign_in(server=server, user=user, password="wrong-password", address=address)
            body = response.json()
            retry_after = response.headers.get("Retry-After")
            # Retry-After counts down: equal to within the second the attempts take.
            out.append((response.status_code, body["code"], body["detail"],
                        None if retry_after is None else int(retry_after) // 10))
        return out

    known = answers(user="joe", address="203.0.113.1")
    unknown = answers(user="nobody-at-all", address="203.0.113.2")
    administrator = answers(user="admin", address="203.0.113.3")
    assert known == unknown == administrator, (known, unknown, administrator)
    assert [status for status, *_ in known] == [401] * 5 + [429] * 2, known


def test_many_attempts_at_once_are_not_all_checked(launcher, store):
    # Twenty wrong passwords in parallel for one pair: no more than the threshold are checked
    # (401); the rest are refused unchecked (429), so a burst buys no extra guesses.
    server = start(launcher=launcher, store_uri=store.new_database(), args=["--enable-metrics", "true"])
    admin = server.admin()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(lambda _: sign_in(server=server, user="ann", password="wrong-password",
                                                   address="203.0.113.9").status_code, range(20)))
    assert statuses.count(401) <= 5 and statuses.count(401) + statuses.count(429) == 20, statuses
    failures = admin.run(sql="SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_login_failures_total'")
    assert failures.to_pylist()[0]["value"] == statuses.count(401), (failures, statuses)


# ---- Delays across addresses and users ----

def test_one_address_trying_many_users_is_slowed(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_LOGIN_ADDRESS_BACKOFF_AFTER": "3", "HARDCHARGER_LOGIN_BACKOFF_BASE_SECONDS": "2"})
    for name in ("u1", "u2"):
        fail(server=server, user=name, address="203.0.113.9")
    fail(server=server, user="u3", address="203.0.113.9")
    retry_after = limited(sign_in(server=server, user="u4", password="wrong-password", address="203.0.113.9"))
    assert retry_after <= 2, retry_after
    # The twin: another address is not slowed.
    refused(sign_in(server=server, user="u4", password="wrong-password", address="203.0.113.10"))
    # ... and once the delay has passed, this one is checked again.
    time.sleep(retry_after)
    refused(sign_in(server=server, user="u4", password="wrong-password", address="203.0.113.9"))


def test_many_addresses_guessing_one_user_are_slowed_but_never_lock_it(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(), env={"HARDCHARGER_LOGIN_LOCKOUT_THRESHOLD": "0"})
    for i in range(5):
        fail(server=server, user="admin", address=f"198.51.100.{i + 1}")
    retry_after = limited(sign_in(server=server, user="admin", password=admin_password(server), address="198.51.100.99"))
    assert retry_after <= 1, retry_after
    time.sleep(retry_after)
    assert sign_in(server=server, user="admin", password=admin_password(server), address="198.51.100.99").status_code == 200


# ---- Behind a proxy ----

def test_a_forwarding_header_from_an_untrusted_peer_is_ignored_and_said_so(launcher, store):
    # Without trusted_proxies every client is 127.0.0.1 here, whatever the header claims: the
    # header can't be used to dodge a lock, and the log says how to fix the setup.
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(), env=DEFAULTS)
    for i in range(5):
        fail(server=server, user="admin", address=f"192.0.2.{i + 1}")
    limited(sign_in(server=server, user="admin", password=admin_password(server), address="192.0.2.77"))
    log = server.log()
    assert "not in trusted_proxies" in log, log
    assert "client_address=127.0.0.1" in log, log


# ---- Seeing and clearing ----

def test_an_administrator_lists_and_clears_locks_and_a_user_may_not(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(), args=["--log-format", "json"])
    create_joe(server=server)
    admin = server.admin()
    joe = server.user(name="joe", password=JOE_PASSWORD)
    fail(server=server, user="admin", address="203.0.113.9", times=5)
    rows = admin.run(sql="SELECT scope, user_name, client_address, failures, locked, retry_after_seconds "
                         "FROM hardcharger_login_locks() WHERE scope = 'pair'").to_pylist()
    assert rows == [{"scope": "pair", "user_name": "admin", "client_address": "203.0.113.9", "failures": 5,
                     "locked": True, "retry_after_seconds": rows[0]["retry_after_seconds"]}], rows
    assert 880 <= rows[0]["retry_after_seconds"] <= 900, rows
    # Users don't see them (the plan check refuses the table function before it runs), and
    # can't clear them (the function itself refuses).
    with pytest.raises(ChargewireError) as raised:
        joe.run(sql="SELECT * FROM hardcharger_login_locks()")
    assert raised.value.code == "permission_denied", raised.value
    assert "blocked the table function hardcharger_login_locks" in raised.value.detail, raised.value
    with pytest.raises(ChargewireError) as raised:
        joe.run(sql="SELECT hardcharger_clear_login_lock('admin', NULL)")
    assert "only an administrator without catalog_access rules can see or clear sign-in locks" in \
        raised.value.detail, raised.value
    limited(sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9"))
    cleared = admin.run(sql="SELECT hardcharger_clear_login_lock('ADMIN', '203.0.113.9') AS n").to_pylist()[0]["n"]
    assert cleared >= 1, cleared
    assert sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9").status_code == 200
    assert admin.run(sql="SELECT count(*) AS n FROM hardcharger_login_locks() WHERE locked").to_pylist() == [{"n": 0}]
    records = server.running.log_records()
    locked = [r for r in records if r.get("status") == "locked" and r.get("audit_seq")]
    cleared_records = [r for r in records if r.get("status") == "cleared" and r.get("audit_seq")]
    assert locked and locked[0]["user"] == "admin" and locked[0]["client_address"] == "203.0.113.9", locked
    assert cleared_records and cleared_records[0]["by"] == "admin", cleared_records


def test_clearing_an_address_or_everything(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    admin = server.admin()
    fail(server=server, user="ann", address="203.0.113.9", times=5)
    fail(server=server, user="ben", address="203.0.113.9", times=5)
    fail(server=server, user="cy", address="203.0.113.10", times=5)
    assert admin.run(sql="SELECT hardcharger_clear_login_lock(NULL, '203.0.113.9') AS n").to_pylist()[0]["n"] >= 2
    pairs = admin.run(sql="SELECT user_name FROM hardcharger_login_locks() WHERE locked ORDER BY 1").to_pylist()
    assert pairs == [{"user_name": "cy"}], pairs
    assert admin.run(sql="SELECT hardcharger_clear_login_lock(NULL, NULL) AS n").to_pylist()[0]["n"] >= 1
    assert admin.run(sql="SELECT count(*) AS n FROM hardcharger_login_locks()").to_pylist() == [{"n": 0}]


def test_hardcharger_users_shows_last_sign_in_and_failures(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    create_joe(server=server)
    admin = server.admin()
    expect_allowed(client=admin, sql="CREATE USER kim WITH PASSWORD 'kim-long-password-2'")

    def users(client) -> dict:
        return {r["name"]: r for r in client.run(
            sql="SELECT name, last_sign_in_at, failed_sign_ins FROM hardcharger_users()").to_pylist()}

    assert users(client=admin)["joe"]["last_sign_in_at"] is None
    fail(server=server, user="joe", address="203.0.113.9", times=3)
    assert users(client=admin)["joe"]["failed_sign_ins"] == 3
    joe = server.user(name="joe", password=JOE_PASSWORD)
    seen = users(client=admin)["joe"]
    assert seen["last_sign_in_at"] is not None, seen
    # A user sees its own, not another's.
    own = users(client=joe)
    assert own["joe"]["last_sign_in_at"] is not None and own["joe"]["failed_sign_ins"] == 3, own
    if "kim" in own:
        assert own["kim"]["last_sign_in_at"] is None and own["kim"]["failed_sign_ins"] is None, own


def test_metrics_count_failures_throttles_and_locks(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(), args=["--enable-metrics", "true"])
    admin = server.admin()  # before the failures: its address is then one it signs in from
    fail(server=server, user="admin", address="203.0.113.9", times=5)
    limited(sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9"))
    rows = admin.run(sql="SELECT name, labels, value FROM hardcharger_metrics() "
                                  "WHERE name LIKE 'hardcharger_login%'").to_pylist()
    values = {(r["name"], json.dumps(r["labels"], sort_keys=True, default=str)): r["value"] for r in rows}
    by_name = {}
    for (name, labels), value in values.items():
        by_name.setdefault(name, []).append((labels, value))
    assert by_name["hardcharger_login_failures_total"][0][1] == 5, by_name
    assert by_name["hardcharger_login_locks_total"][0][1] == 1, by_name
    assert by_name["hardcharger_login_locks_active"][0][1] == 1, by_name
    throttled = {labels: value for labels, value in by_name["hardcharger_login_throttled_total"]}
    assert sum(throttled.values()) == 1 and any("locked" in labels for labels, v in throttled.items() if v == 1), throttled


# ---- Shared through the store ----

def test_a_lock_survives_a_restart(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri)
    fail(server=server, user="admin", address="203.0.113.9", times=5)
    server.running.stop()
    again = start(launcher=launcher, store_uri=uri)
    limited(sign_in(server=again, user="admin", password=again.running.password, address="203.0.113.9"))
    assert sign_in(server=again, user="admin", password=again.running.password, address="192.0.2.1").status_code in (200, 429)


def test_replicas_on_one_store_share_failures_and_locks(launcher, pg_store):
    uri = pg_store.new_database()
    first = start(launcher=launcher, store_uri=uri)
    second = start(launcher=launcher, store_uri=uri, args=[])
    second_password = second.running.password
    # Three failures on one replica and two on the other lock the pair on both.
    fail(server=first, user="ann", address="203.0.113.9", times=3)
    time.sleep(2.5 * TIMEOUT_SCALE)  # the records' sync (about a second)
    fail(server=second, user="ann", address="203.0.113.9", times=2)
    limited(sign_in(server=second, user="ann", password="anything", address="203.0.113.9"))
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while True:
        response = sign_in(server=first, user="ann", password="anything", address="203.0.113.9")
        if response.status_code == 429 or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    limited(response=response)
    # A clear on one replica reaches the other.
    second.admin().run(sql="SELECT hardcharger_clear_login_lock('ann', NULL)")
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while True:
        response = sign_in(server=first, user="ann", password="anything", address="203.0.113.9")
        if response.status_code == 401 or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    refused(response=response)
    assert second_password  # (each replica has its own administrator password here)


# ---- --verify-credentials (GizmoData Cloud's auto-resume verifier) ----

def verify(store_uri: str, credential: dict, admin_password: str) -> tuple[int, dict]:
    run = subprocess.run(args=[str(BINARY), "--verify-credentials"],
                         env={**clean_environment(), "HARDCHARGER_ADMIN_USERNAME": "admin",
                              "HARDCHARGER_ADMIN_PASSWORD": admin_password, "HARDCHARGER_RBAC_STORE_URI": store_uri,
                              "HARDCHARGER_SECRET_KEY": SECRET_KEY, **DEFAULTS},
                         input=json.dumps(credential), capture_output=True, text=True, timeout=120 * TIMEOUT_SCALE)
    lines = run.stdout.splitlines()
    assert len(lines) == 1, f"{run.stdout!r}\n{run.stderr}"
    for secret in (admin_password, credential.get("password", "")):
        assert secret not in run.stdout and secret not in run.stderr
    return run.returncode, json.loads(lines[0])


def joe_credential(password: str, address: str | None) -> dict:
    out = {"grant_type": "password", "username": "joe", "password": password}
    if address:
        out["client_address"] = address
    return out


def test_the_verifier_refuses_a_pair_the_server_locked_and_not_another(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri)
    create_joe(server=server)
    fail(server=server, user="joe", address="203.0.113.9", times=5)
    password = admin_password(server)
    time.sleep(2 * TIMEOUT_SCALE)  # the server's records reach the store
    server.running.stop()  # a stopped cluster
    code, answer = verify(store_uri=uri, credential=joe_credential(password=JOE_PASSWORD, address="203.0.113.9"),
                          admin_password=password)
    assert code == 1, answer
    assert answer["reason"] == "rate_limited" and 880 <= answer["retry_after_seconds"] <= 900, answer
    assert LOCKED_DETAIL in answer["message"], answer
    # The twin: the same user from another address (after its 1 s delay) is verified.
    time.sleep(1.1)
    code, answer = verify(store_uri=uri, credential=joe_credential(password=JOE_PASSWORD, address="192.0.2.1"),
                          admin_password=password)
    assert code == 0 and answer["verified"] is True and answer["user"] == "joe", answer


def test_the_verifier_counts_its_failures_and_the_server_honours_them(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri)
    create_joe(server=server)
    password = admin_password(server)
    server.running.stop()
    for _ in range(5):
        code, answer = verify(store_uri=uri, credential=joe_credential(password="wrong-password", address="203.0.113.9"),
                              admin_password=password)
        assert code == 1 and answer["reason"] == "invalid_credentials", answer
    code, answer = verify(store_uri=uri, credential=joe_credential(password=JOE_PASSWORD, address="203.0.113.9"),
                          admin_password=password)
    assert code == 1 and answer["reason"] == "rate_limited", answer
    # The cluster starts (the wake): the lock holds there too.
    again = start(launcher=launcher, store_uri=uri)
    limited(sign_in(server=again, user="joe", password=JOE_PASSWORD, address="203.0.113.9"))


def test_a_verified_password_resets_the_pair_for_the_verifier(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri)
    create_joe(server=server)
    password = admin_password(server)
    server.running.stop()

    def attempt(secret: str) -> tuple[int, dict]:
        return verify(store_uri=uri, credential=joe_credential(password=secret, address="203.0.113.9"),
                      admin_password=password)

    for _ in range(4):
        assert attempt(secret="wrong-password")[0] == 1
    assert attempt(secret=JOE_PASSWORD)[0] == 0
    for _ in range(4):
        assert attempt(secret="wrong-password")[1]["reason"] == "invalid_credentials"
    code, answer = attempt(secret=JOE_PASSWORD)
    assert code == 0, answer


def test_the_verifier_without_an_address_applies_the_user_limits(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri)
    create_joe(server=server)
    for i in range(5):
        fail(server=server, user="joe", address=f"198.51.100.{i + 1}")
    password = admin_password(server)
    time.sleep(2 * TIMEOUT_SCALE)
    server.running.stop()
    code, answer = verify(store_uri=uri, credential=joe_credential(password=JOE_PASSWORD, address=None),
                          admin_password=password)
    # Within a second of the fifth failure: the user's delay; after it, verified.
    if code == 1:
        assert answer["reason"] == "rate_limited", answer
        time.sleep(answer["retry_after_seconds"])
        code, answer = verify(store_uri=uri, credential=joe_credential(password=JOE_PASSWORD, address=None),
                              admin_password=password)
    assert code == 0, answer


# ---- The password policy ----

def test_a_new_password_needs_twelve_characters(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database())
    admin = server.admin()
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="CREATE USER amy WITH PASSWORD 'elevenchars'")
    assert "the password is too short: it needs at least 12 characters (password_min_length)" in raised.value.detail
    assert "elevenchars" not in raised.value.detail
    assert admin.run(sql="SELECT count(*) AS n FROM hardcharger_users() WHERE name = 'amy'").to_pylist() == [{"n": 0}]
    expect_allowed(client=admin, sql="CREATE USER amy WITH PASSWORD 'twelve chars'")
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="ALTER USER amy WITH PASSWORD 'short'")
    assert "at least 12 characters" in raised.value.detail
    # The old password still signs in; a long new one replaces it.
    assert sign_in(server=server, user="amy", password="twelve chars").status_code == 200
    expect_allowed(client=admin, sql="ALTER USER amy WITH PASSWORD 'another long one'")
    assert sign_in(server=server, user="amy", password="another long one").status_code == 200
    # A user without a password is no password at all: allowed.
    expect_allowed(client=admin, sql="CREATE USER svc")


def test_existing_passwords_keep_working_and_if_not_exists_sets_nothing(launcher, store):
    uri = store.new_database()
    server = start(launcher=launcher, store_uri=uri, env={"HARDCHARGER_PASSWORD_MIN_LENGTH": "4"})
    expect_allowed(client=server.admin(), sql="CREATE USER old WITH PASSWORD 'abcd1'")
    server.running.stop()
    # The policy rises: the password set before still signs in, and an init SQL that names it
    # again with IF NOT EXISTS (which changes nothing) still starts the server.
    again = start(launcher=launcher, store_uri=uri,
                  env={"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE USER IF NOT EXISTS old WITH PASSWORD 'abcd1';"})
    assert sign_in(server=again, user="old", password="abcd1").status_code == 200
    with pytest.raises(ChargewireError) as raised:
        again.admin().run(sql="ALTER USER old WITH PASSWORD 'abcd2'")
    assert "at least 12 characters" in raised.value.detail


def test_character_classes_when_asked_for(launcher, store):
    server = start(launcher=launcher, store_uri=store.new_database(),
                   env={"HARDCHARGER_PASSWORD_MIN_CHARACTER_CLASSES": "3"})
    admin = server.admin()
    with pytest.raises(ChargewireError) as raised:
        admin.run(sql="CREATE USER amy WITH PASSWORD 'alllowercaseletters'")
    assert "at least 3 kinds" in raised.value.detail
    expect_allowed(client=admin, sql="CREATE USER amy WITH PASSWORD 'Lowercase4567'")


# An expired password (password_max_age_days) signs in held until its user changes it, and a
# temporary password stops working after password_temporary_max_age_hours: test_password_lifetimes.py.


# ---- trusted_proxy_secret: a proxy trusted by a secret header, not its address ----

PROXY_SECRET = "proxy-" + "7f3c9a" * 8  # 54 characters
SECRET_HEADER = "X-Hardcharger-Proxy-Secret"
NO_USER_DELAY = {"HARDCHARGER_LOGIN_USER_BACKOFF_AFTER": "0"}


def proxied_sign_in(server, user: str, password: str, address: str, secret: str | None) -> httpx.Response:
    headers = {"X-Forwarded-For": address}
    if secret is not None:
        headers[SECRET_HEADER] = secret
    return httpx.post(url=f"{server.base_url}/chargewire/v1/auth/token", headers=headers, timeout=120,
                      json={"grant_type": "password", "username": user, "password": password})


def start_with_secret(launcher, store_uri: str, args: list[str] | None = None, **env: str):
    # Not through start(): no trusted_proxies unless a test asks for them.
    return start_rbac_server(launcher=launcher, store_uri=store_uri, args=args,
                             env={**DEFAULTS, **NO_USER_DELAY, "HARDCHARGER_TRUSTED_PROXY_SECRET": PROXY_SECRET,
                                  **env})


def test_with_the_right_secret_the_forwarded_address_is_the_client(launcher, store):
    server = start_with_secret(launcher=launcher, store_uri=store.new_database())
    for _ in range(5):
        refused(proxied_sign_in(server=server, user="admin", password="wrong-password", address="203.0.113.9",
                                secret=PROXY_SECRET))
    limited(proxied_sign_in(server=server, user="admin", password=admin_password(server), address="203.0.113.9",
                            secret=PROXY_SECRET))
    # Another client behind the same proxy: its own pair, not locked.
    response = proxied_sign_in(server=server, user="admin", password=admin_password(server), address="198.51.100.7",
                               secret=PROXY_SECRET)
    assert response.status_code == 200, response.text
    log = server.log()
    assert "client_address=203.0.113.9" in log, log
    assert PROXY_SECRET not in log


@pytest.mark.parametrize("secret", ["wrong-" + "0" * 40, None])
def test_without_the_right_secret_a_forged_forwarding_header_is_ignored(launcher, store, secret):
    # A tenant pod next to the proxy forging X-Forwarded-For: every attempt counts against its own
    # address (127.0.0.1 here), so changing the forged address dodges nothing.
    server = start_with_secret(launcher=launcher, store_uri=store.new_database())
    for i in range(5):
        refused(proxied_sign_in(server=server, user="admin", password="wrong-password", address=f"192.0.2.{i + 1}",
                                secret=secret))
    locked = proxied_sign_in(server=server, user="admin", password=admin_password(server), address="192.0.2.99",
                             secret=secret)
    limited(locked)
    log = server.log()
    assert "client_address=127.0.0.1" in log and "client_address=192.0.2." not in log, log
    if secret is not None:
        # Said once (rate-limited), never with the value.
        assert log.count("with a value that is not trusted_proxy_secret") == 1, log
        assert secret not in log and PROXY_SECRET not in log


def test_a_wrong_secret_answers_exactly_as_no_secret(launcher, store):
    server = start_with_secret(launcher=launcher, store_uri=store.new_database())

    def answer(secret: str | None) -> tuple:
        response = proxied_sign_in(server=server, user="nobody", password="wrong-password", address="192.0.2.1",
                                   secret=secret)
        body = response.json()
        body.pop("request_id")
        headers = {k.lower() for k in response.headers} - {"chargewire-request-id", "date"}
        return response.status_code, body, headers

    assert answer(secret="wrong-" + "1" * 40) == answer(secret=None)
    assert answer(secret=PROXY_SECRET)[0] == 401  # (counted under 192.0.2.1 instead)


def test_secret_and_cidr_trust_together(launcher, store):
    server = start_with_secret(launcher=launcher, store_uri=store.new_database(), args=PROXIED)
    # A CIDR-trusted peer (127.0.0.1) needs no secret, and a wrong one doesn't take its trust away.
    for secret in (None, "wrong-" + "2" * 40, PROXY_SECRET):
        for _ in range(5):
            refused(proxied_sign_in(server=server, user="ann", password="wrong-password", address="203.0.113.9",
                                    secret=secret))
        limited(proxied_sign_in(server=server, user="ann", password="anything", address="203.0.113.9", secret=secret))
        assert server.admin().run(sql="SELECT hardcharger_clear_login_lock('ann', NULL) AS n").to_pylist()[0]["n"] >= 1
    assert "client_address=127.0.0.1" not in "\n".join(
        line for line in server.log().splitlines() if "user=ann" in line), server.log()


def test_the_secret_is_never_shown(launcher, store):
    server = start_with_secret(launcher=launcher, store_uri=store.new_database(), args=["--log-level", "debug"])
    admin = server.admin()
    proxied_sign_in(server=server, user="admin", password="wrong-password", address="192.0.2.1", secret=PROXY_SECRET)
    proxied_sign_in(server=server, user="admin", password="wrong-password", address="192.0.2.1", secret="x" * 40)
    for sql in ("SELECT * FROM hardcharger_settings()", "SELECT * FROM hardcharger_server_settings()",
                "SELECT * FROM hardcharger_server_info()", "SELECT * FROM hardcharger_login_locks()"):
        assert PROXY_SECRET not in str(admin.run(sql=sql).to_pylist()), sql
    assert PROXY_SECRET not in server.log()
    # --help and --print-options-json describe the option, never a value.
    for flag in ("--help", "--print-options-json"):
        run = subprocess.run(args=[str(BINARY), flag], env={**clean_environment(),
                                                             "HARDCHARGER_TRUSTED_PROXY_SECRET": PROXY_SECRET},
                             capture_output=True, text=True, timeout=60)
        assert "trusted-proxy-secret" in run.stdout or "trusted_proxy_secret" in run.stdout, run.stdout[:500]
        assert PROXY_SECRET not in run.stdout + run.stderr


def test_a_short_secret_stops_the_server_without_quoting_it(launcher):
    short = "short-secret-1234"
    running = launcher.start(env={"HARDCHARGER_TRUSTED_PROXY_SECRET": short}, expect_up=False)
    running.process.wait(timeout=60 * TIMEOUT_SCALE)
    assert running.process.returncode != 0
    log = running.log()
    assert "trusted_proxy_secret must be at least 32 characters" in log, log
    assert short not in log


@pytest.mark.parametrize("length, warned", [("4", True), ("7", True), ("8", False), ("12", False)])
def test_a_minimum_length_below_eight_is_allowed_with_one_warning(launcher, length, warned):
    server = launcher.start(env={"HARDCHARGER_PASSWORD_MIN_LENGTH": length})
    log = server.log()
    lines = [line for line in log.splitlines() if "password_min_length is below 8" in line]
    if warned:
        assert len(lines) == 1, log
        assert "warning" in lines[0] and f"value={length}" in lines[0] and "recommended_minimum=12" in lines[0], lines
    else:
        assert lines == [], lines
