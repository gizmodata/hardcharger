"""Port of GizmoSQL 1.x tests/integration/test_session_options_probe.cpp (3 cases).

Session options (Chargewire §5.2: GET /sessions/{id}) report the current catalog. Once a
session is closed, a probe with the same token fails with 404 session_not_found and
does not bring the session back (1.x: GetSessionOptions on an evicted session); a new
session probes fine. All 3 apply.
"""

from conftest import problem


def test_get_session_options_succeeds_for_live_session(launcher):
    session = launcher.start().admin()
    options = session.options()
    assert options["catalog"] == "memory", options


def test_get_session_options_fails_for_closed_session(launcher):
    session = launcher.start().admin()
    assert session.close().status_code == 204
    for _ in range(2):  # probing again must not have recreated it
        problem(response=session.http.get(url=session.url, headers=session.headers()), status=404,
                code="session_not_found")


def test_fresh_session_probes_healthy_after_closing(launcher):
    server = launcher.start()
    client = server.client()
    stale = client.new_session()
    stale.close()
    problem(response=stale.http.get(url=stale.url, headers=stale.headers()), status=404, code="session_not_found")
    fresh = client.new_session()
    assert fresh.options()["catalog"] == "memory"
