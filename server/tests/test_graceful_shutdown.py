"""Port of GizmoSQL 1.x tests/integration/test_graceful_shutdown.cpp (5 cases).

On SIGTERM a server with graceful_shutdown drains (Warp SQL §11.1): running statements
finish, new ones get 503 server_draining ("the server is shutting down"), the health
check reports not serving, and it stops once idle, when shutdown_grace_period_seconds
runs out (cancelling what still runs), or at a second signal. 1.x drove most cases
through an in-process call and one through raise(SIGTERM); here every case sends a real
SIGTERM to the server process. All 5 apply, plus graceful_shutdown=false (stop at once).
"""

import signal
import threading
import time

import httpx

from conftest import problem

LONG = "SELECT count(*) FROM range(10000000000) a, range(100000) b"


def run_in_background(session, sql: str) -> dict:
    outcome = {}

    def run():
        try:
            outcome["response"] = session.submit(sql=sql, timeout=120)
        except httpx.HTTPError as error:
            outcome["error"] = error

    outcome["thread"] = threading.Thread(target=run)
    outcome["thread"].start()
    return outcome


def wait_running(session, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.scalar(sql="SELECT count(*) FROM hardcharger_sessions() WHERE running AND NOT is_current") > 0:
            return
        time.sleep(0.05)
    raise AssertionError("the slow statement never started")


def test_drains_in_flight_and_rejects_new_work(launcher):
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "true", "HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "30"})
    slow = server.admin()
    probe = server.admin()
    outcome = run_in_background(session=slow, sql="SELECT sleep_ms(4000) AS slept")
    wait_running(session=probe)
    server.process.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    started = time.monotonic()
    body = problem(response=probe.submit(sql="SELECT 1"), status=503, code="server_draining")
    assert time.monotonic() - started < 3
    assert "shutting down" in body["detail"], body
    assert httpx.get(url=f"{server.base_url}/warp/v1/health").status_code == 503
    outcome["thread"].join(timeout=30)
    assert outcome["response"].status_code in (200, 201), outcome  # the running statement finished
    assert server.process.wait(timeout=20) == 0


def test_stops_promptly_when_idle(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"})
    assert server.admin().scalar(sql="SELECT 1") == 1
    started = time.monotonic()
    server.process.send_signal(signal.SIGTERM)
    assert server.process.wait(timeout=10) == 0
    assert time.monotonic() - started < 10


def test_real_signal_drives_drain(launcher):
    server = launcher.start()  # graceful by default
    slow = server.admin()
    probe = server.admin()
    outcome = run_in_background(session=slow, sql="SELECT sleep_ms(3000) AS slept")
    wait_running(session=probe)
    server.process.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    problem(response=probe.submit(sql="SELECT 1"), status=503, code="server_draining")
    outcome["thread"].join(timeout=30)
    assert outcome["response"].status_code in (200, 201), outcome
    assert server.process.wait(timeout=20) == 0


def test_grace_period_elapsed_forces_stop(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "2"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(signal.SIGTERM)
    assert server.process.wait(timeout=15) == 0
    assert time.monotonic() - started < 15
    outcome["thread"].join(timeout=30)
    failed = "error" in outcome or outcome["response"].json().get("state") != "succeeded"
    assert failed, outcome


def test_second_signal_forces_stop(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(signal.SIGTERM)
    time.sleep(1)
    assert server.process.poll() is None, "the first signal must only drain"
    server.process.send_signal(signal.SIGTERM)
    assert server.process.wait(timeout=15) == 0
    assert time.monotonic() - started < 15
    outcome["thread"].join(timeout=30)


def test_without_graceful_shutdown_the_first_signal_stops(launcher):
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "false"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(signal.SIGTERM)
    assert server.process.wait(timeout=15) == 0
    assert time.monotonic() - started < 10
    outcome["thread"].join(timeout=30)


def test_a_drain_ends_in_a_clean_exit_every_time(launcher):
    # The drain's last step stops the server on a thread of its own, while main() returns
    # from Wait() and destroys it: that thread must be finished (joined) before the server
    # is torn down. Under ThreadSanitizer the race made the process exit 66, not 0.
    for _ in range(5):
        server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "true"})
        assert server.admin().scalar(sql="SELECT 1") == 1
        server.process.send_signal(signal.SIGTERM)
        assert server.process.wait(timeout=60) == 0, server.log()[-2000:]
