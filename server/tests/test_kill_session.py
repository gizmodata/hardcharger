"""Port of GizmoSQL 1.x tests/integration/test_kill_session.cpp (4 cases).

KILL SESSION '<uuid>' (also "uuid" and bare, any case, trailing ;) ends another session:
what it runs stops, and its next request fails with 401
session_ended ("session has been killed ... re-authenticate", Warp SQL §5.6). An admin
can't kill its own session; an unknown id is "not found"; non-admins may not kill.

Strengthened: 1.x's NonAdminCannotKillSession only had an admin kill itself; here it is
a real non-admin (role user) and asserts "Only admin users can execute KILL SESSION",
and the "own session" case is its own test. The syntax case asserts "not found" only
(1.x also accepted "Only admin"). Added: killing a running statement interrupts it.
"""

import threading
import time

import pytest

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
