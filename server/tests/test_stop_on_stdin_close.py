"""stop_on_stdin_close: a server started with its standard input on a pipe stops, the way the
first stop signal stops it (draining with graceful_shutdown), when that pipe closes. The
kernel closes it when the parent process dies, so a parent that holds the write end (the
desktop app, gizmodata/hardcharger#28) can crash, be killed or quit without stopping the
server, and the server still goes. Off by default: a server whose stdin is already at its
end (started from a script with stdin on /dev/null) would otherwise stop at once."""

import subprocess
import threading
import time

import pytest

from conftest import TIMEOUT_SCALE

GRACE = ["--graceful-shutdown=true", "--shutdown-grace-period-seconds=2"]


def exits_cleanly(running, timeout: float) -> None:
    try:
        code = running.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"the server did not exit within {timeout} s; its log ends:\n{running.log()[-4000:]}")
    assert code == 0, f"the server exited {code}; its log ends:\n{running.log()[-4000:]}"


def test_closing_stdin_stops_the_server_like_a_stop_signal(launcher):
    server = launcher.start(args=["--stop-on-stdin-close=true", *GRACE], stdin=subprocess.PIPE)
    assert server.client().new_session().scalar(sql="SELECT 42") == 42
    server.process.stdin.close()
    exits_cleanly(running=server, timeout=10 * TIMEOUT_SCALE)
    log = server.log()
    assert "standard input closed; stopping (stop_on_stdin_close)" in log
    assert "draining" in log


def test_closing_stdin_drains_running_work_within_the_grace_period(launcher):
    server = launcher.start(args=["--stop-on-stdin-close=true", *GRACE], stdin=subprocess.PIPE)
    client = server.client()
    session = client.new_session()
    # A statement that outlives the grace period: the stop cancels it after 2 s, so the
    # server is gone long before the statement would have finished on its own.
    def run_long():
        try:
            session.submit(sql="SELECT count(*) FROM range(10000000000) a, range(100000) b", timeout=60)
        except Exception:  # noqa: BLE001  the stop cancels it, or the connection drops
            pass

    thread = threading.Thread(target=run_long, daemon=True)
    thread.start()
    watcher = client.new_session()
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while time.monotonic() < deadline:
        if watcher.scalar(sql="SELECT count(*) FROM hardcharger_sessions() WHERE running AND NOT is_current") > 0:
            break
        time.sleep(0.05)
    else:
        pytest.fail(reason="the long statement never showed as running")
    started = time.monotonic()
    server.process.stdin.close()
    exits_cleanly(running=server, timeout=15 * TIMEOUT_SCALE)
    assert time.monotonic() - started < 12 * TIMEOUT_SCALE
    assert "grace period over; stopping" in server.log()


def test_without_the_option_a_closed_stdin_changes_nothing(launcher):
    server = launcher.start(args=GRACE, stdin=subprocess.PIPE)
    server.process.stdin.close()
    time.sleep(2 * TIMEOUT_SCALE)
    assert server.process.poll() is None, f"the server exited without the option:\n{server.log()[-2000:]}"
    assert server.client().new_session().scalar(sql="SELECT 1") == 1
    assert "standard input closed" not in server.log()


def test_a_stdin_already_at_its_end_stops_the_server_at_once(launcher):
    # Documented edge: with the option on, stdin on /dev/null means "the parent is gone".
    server = launcher.start(args=["--stop-on-stdin-close=true", *GRACE], stdin=subprocess.DEVNULL,
                            expect_up=False)
    exits_cleanly(running=server, timeout=15 * TIMEOUT_SCALE)
    assert "standard input closed; stopping (stop_on_stdin_close)" in server.log()


def test_the_option_is_in_the_options_schema():
    # The desktop app reads this schema to validate what it passes the server.
    from conftest import BINARY
    import json
    output = subprocess.run(args=[str(BINARY), "--print-options-json"], capture_output=True, text=True, check=True).stdout
    names = {option["name"] for option in json.loads(output)["options"]}
    assert "stop_on_stdin_close" in names
