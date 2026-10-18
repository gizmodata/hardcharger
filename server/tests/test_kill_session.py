"""Port of GizmoSQL 1.x tests/integration/test_kill_session.cpp (4 cases).

KILL SESSION '<uuid>' (also "uuid" and bare, any case, trailing ;) ends another session:
what it runs stops, and its next request fails with 401
session_ended ("session has been killed ... re-authenticate", Warp SQL §5.6). An admin
can't kill its own session; an unknown id is "not found"; non-admins may not kill.

Strengthened: 1.x's NonAdminCannotKillSession only had an admin kill itself; here it is
a real non-admin (role user) and asserts "Only admin users can execute KILL SESSION",
and the "own session" case is its own test. The syntax case asserts "not found" only
(1.x also accepted "Only admin"). Added: killing a running statement interrupts it, and
every request naming a killed session, GET of the session (a liveness probe) included,
gets session_ended with the structured reason (Warp SQL §5.6), as does a session access
control ended.
"""

import threading
import time

import pytest

from rbac import expect_allowed, start_rbac_server
from warp_client import mint_server_token

UNKNOWN = "12345678-1234-1234-1234-123456789012"


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start()


def test_non_admin_cannot_kill_session(server):
    victim_id = server.admin().scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    user = server.client(token=mint_server_token(secret_key=server.secret_key, subject="u", role="user")).new_session()
    problem = user.error(sql=f"KILL SESSION '{victim_id}'")
    assert problem["code"] == "permission_denied" and "Only admin users can execute KILL SESSION" in problem["detail"], problem


def test_admin_cannot_kill_own_session(server):
    session = server.admin()
    own = session.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    assert len(own) == 36 and own.count("-") == 4
    assert "Cannot kill your own session" in session.error(sql=f"KILL SESSION '{own}'")["detail"]


def test_kill_nonexistent_session(server):
    problem = server.admin().error(sql="KILL SESSION '00000000-0000-0000-0000-000000000000'")
    assert "not found" in problem["detail"], problem


@pytest.mark.parametrize("sql", [f"KILL SESSION '{UNKNOWN}'", f'KILL SESSION "{UNKNOWN}"', f"KILL SESSION {UNKNOWN}",
                                 f"kill session '{UNKNOWN}'", f"KILL SESSION '{UNKNOWN}';"])
def test_kill_session_syntax(server, sql):
    problem = server.admin().error(sql=sql)
    assert "Session '" + UNKNOWN + "' not found" in problem["detail"], problem


def test_killed_session_must_reauthenticate(server):
    victim = server.client().new_session()
    victim_id = victim.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    server.admin().execute(sql=f"KILL SESSION '{victim_id.upper()}'")
    problem = victim.error(sql="SELECT 1")
    assert problem["status"] == 401 and problem["code"] == "session_ended", problem
    assert "session has been killed" in problem["detail"] and "re-authenticate" in problem["detail"], problem
    assert problem["reason"] == "killed", problem  # Warp SQL §5.6
    # Killing it again: it is gone.
    assert "not found" in server.admin().error(sql=f"KILL SESSION '{victim_id}'")["detail"]


def test_kill_interrupts_a_running_statement(server):
    victim = server.client().new_session()
    victim_id = victim.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    outcome = {}

    def run():
        outcome["problem"] = victim.error(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b")

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1)
    started = time.monotonic()
    server.admin().execute(sql=f"KILL SESSION '{victim_id}'")
    worker.join(timeout=30)
    assert not worker.is_alive(), "the killed statement kept running"
    assert time.monotonic() - started < 15
    assert outcome["problem"]["code"] in ("session_ended", "statement_cancelled"), outcome


def assert_ended(response, reason: str) -> dict:
    """`response` is Warp SQL §5.6's session_ended, with `reason` as its own field."""
    assert response.status_code == 401, response.text
    problem = response.json()
    assert problem["code"] == "session_ended" and problem["reason"] == reason, problem
    assert "Bearer" in response.headers.get("www-authenticate", ""), response.headers
    return problem


def test_every_request_naming_a_killed_session_says_why(server):
    victim = server.client().new_session()
    victim_id = victim.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    prepared = victim.prepare(sql="SELECT 1")["prepared_id"]
    running = victim.submit(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b",
                            headers={"Prefer": "respond-async"}).json()["statement_id"]
    time.sleep(0.5)
    server.admin().execute(sql=f"KILL SESSION '{victim_id}'")
    # The statement it was running failed with the same error, reason included.
    doc = victim.wait_done(statement_id=running)
    assert doc["state"] == "failed" and doc["error"]["code"] == "session_ended", doc
    assert doc["error"]["reason"] == "killed", doc
    # GET of the session answered 200 with its options before; it is the liveness probe.
    problem = assert_ended(victim.http.get(url=victim.url, headers=victim.headers()), reason="killed")
    assert "killed by an administrator" in problem["detail"], problem  # the text stays
    for method, path, body in [("POST", "/statements", {"sql": "SELECT 1"}),
                               ("GET", f"/statements/{running}/result", None),
                               ("POST", "/prepared", {"sql": "SELECT 1"}),
                               ("POST", f"/prepared/{prepared}/execute", {}),
                               ("GET", "/metadata/tables", None),
                               ("PATCH", "", {"options": {"schema": "main"}})]:
        assert_ended(victim.http.request(method=method, url=victim.url + path, headers=victim.headers(), json=body),
                     reason="killed")
    # Closing it still works (nothing is left to free).
    assert victim.http.delete(url=victim.url, headers=victim.headers()).status_code == 204
    # The twin: another session of the same principal is alive and answers GET.
    other = server.client().new_session()
    assert other.http.get(url=other.url, headers=other.headers()).status_code == 200


def test_a_session_access_control_ended_says_why_on_get_too(module_launcher, store):
    rbac = start_rbac_server(launcher=module_launcher, store_uri=store.new_database())
    admin = rbac.admin()
    for sql in ["CREATE USER pat WITH PASSWORD 'pw'", "CREATE USER sam WITH PASSWORD 'pw'"]:
        expect_allowed(client=admin, sql=sql)
    pat = rbac.user(name="pat")  # a server-signed token, which a password change leaves standing
    sam = rbac.user(name="sam")
    expect_allowed(client=admin, sql="ALTER USER pat WITH PASSWORD 'pw2'")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = pat.http.get(url=pat.url, headers=pat.headers())
        if response.status_code != 200:
            break
        time.sleep(0.2)
    assert_ended(response, reason="credentials_changed")
    assert_ended(pat.http.post(url=pat.url + "/statements", headers=pat.headers(), json={"sql": "SELECT 1"}),
                 reason="credentials_changed")
    # The twin: a user whose password didn't change.
    assert sam.http.get(url=sam.url, headers=sam.headers()).status_code == 200
