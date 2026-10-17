"""Port of GizmoSQL 1.x tests/integration/test_reject_unknown_sessions.cpp (6 cases; 3 apply).

Warp SQL never recreates a session implicitly: a closed, idle-ended or unknown session
id answers 404 session_not_found (§5.6). There is no --reject-unknown-sessions option:
it is always so (1.x's option to turn it on, and its refusal of turning it off, are
not applicable: Hardcharger takes only its own names).

Not applicable: ClosedSessionIsRecreatedByDefault and OtherInstancesSessionIsCreatedByDefault,
1.x's default of silently starting a fresh session for an unknown one; that is the one
deliberate behaviour change of Warp SQL (clients reopen a session; drivers retry).
OtherInstancesSessionIsRefused: 1.x answered "belongs to GizmoSQL instance X" because its
tokens named a session; Warp tokens don't, so another replica's session id is simply
unknown here (404), and the token itself is checked as in test_cross_instance_tokens.
"""

import time

import pytest

from conftest import problem


@pytest.fixture(scope="module")
def server(module_launcher):
    # As in 1.x's fixture, cross-instance tokens are on, so the only question left for
    # another replica's session is the session itself.
    return module_launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": "2",
                                      "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"})


def gone(session) -> None:
    problem(response=session.submit(sql="SELECT 1"), status=404, code="session_not_found")


def test_new_session_starts_on_first_use(server):
    assert server.admin().scalar(sql="SELECT 1") == 1


def test_closed_session_is_refused(server):
    session = server.admin()
    session.close()
    gone(session=session)
    gone(session=session)  # and is still not back


def test_idle_evicted_session_is_refused(server):
    session = server.admin()
    time.sleep(2 + 5 + 1)
    gone(session=session)


def test_other_instances_session_is_refused(server, module_launcher):
    other = module_launcher.start(env={"HARDCHARGER_SECRET_KEY": server.secret_key,
                                       "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true"}, password=server.password)
    foreign = other.admin()
    from warp_client import Session
    here = Session(client=server.client(token=foreign.client.token), session_id=foreign.id)
    gone(session=here)

