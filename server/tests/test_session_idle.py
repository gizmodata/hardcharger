"""Port of GizmoSQL 1.x tests/integration/test_session_idle.cpp (5 cases).

The Chargewire SDK checks for idle sessions every five seconds. These tests exercise
session_idle_timeout through a live server. A status request waiting on a statement
counts as activity until the response ends. An evicted session returns 404 session_not_found.
"""

import time

import pytest

from conftest import problem

SWEEP = 5  # seconds between the SDK's idle sweeps


def alive(session) -> bool:
    return session.http.get(url=session.url, headers=session.headers()).status_code == 200


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": "2"})


def test_timeout_zero_never_evicts(launcher):
    server = launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": "0"})
    session = server.admin()
    time.sleep(SWEEP + 2)
    assert alive(session=session)


def test_evicts_when_past_timeout_without_in_flight_sql(server):
    session = server.admin()
    time.sleep(2 + SWEEP + 1)
    problem(response=session.http.get(url=session.url, headers=session.headers()), status=404, code="session_not_found")


def test_does_not_evict_before_timeout(launcher):
    server = launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": "30"})
    session = server.admin()
    time.sleep(SWEEP + 1)
    assert alive(session=session)


def test_sql_activity_refreshes_clock(launcher):
    server = launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": "4"})
    session = server.admin()
    deadline = time.monotonic() + 2 * SWEEP + 2
    while time.monotonic() < deadline:
        assert session.scalar(sql="SELECT 1") == 1
        time.sleep(1)
    assert alive(session=session)


def test_in_flight_sql_not_idle(server):
    session = server.admin()
    # execute() long-polls status with Prefer: wait=5, longer than the 2 s idle timeout.
    doc = session.execute(sql="SELECT sleep_ms(9000)")
    assert doc["state"] == "succeeded", doc
    assert alive(session=session), "a session running SQL past its idle timeout must not be ended"
