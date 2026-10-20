"""One info line per cancelled statement (GizmoSQL 1.x logged "SQL Statement was
successfully canceled"): message "statement cancelled" with session_id, statement_id (the
statement's q_... id), user, reason (Warp SQL's cancel_reason) and duration in seconds, at
the default log level whatever the statement log level, for each way a statement is
stopped: the client's cancel, a statement timeout (Warp-Statement-Timeout and
query_timeout_seconds), KILL SESSION, the session's deletion and the server's shutdown. A
statement that ends normally, or fails, logs none.
"""

import threading
import time

import pytest

from conftest import STOP_SIGNAL

# (count(*) over range() alone is answered from its cardinality, at once.)
LONG = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b"


@pytest.fixture(scope="module")
def server(module_launcher):
    # print_queries off: the line must not depend on the statement log.
    return module_launcher.start(args=["--log-format", "json", "--print-queries", "false"])


def cancelled_lines(server, statement_id: str | None = None, session_id: str | None = None,
                    timeout: float = 15, expect: bool = True) -> list[dict]:
    """The "statement cancelled" lines for `statement_id` (or `session_id`), waiting up to
    `timeout` for one when `expect`."""
    deadline = time.monotonic() + timeout
    while True:
        lines = [r for r in server.log_records() if r.get("message") == "statement cancelled"
                 and (statement_id is None or r.get("statement_id") == statement_id)
                 and (session_id is None or r.get("session_id") == session_id)]
        if lines or not expect or time.monotonic() > deadline:
            return lines
        time.sleep(0.1)


def start_long(session, headers: dict | None = None) -> str:
    response = session.submit(sql=LONG, headers={"Prefer": "respond-async", **(headers or {})})
    assert response.status_code in (200, 201, 202), response.text
    statement_id = response.json()["statement_id"]
    wait_running(session=session, statement_id=statement_id)
    return statement_id


def wait_running(session, statement_id: str) -> None:
    deadline = time.monotonic() + 10
    while session.status(statement_id=statement_id)["state"] != "running":
        assert time.monotonic() < deadline, "the statement never started"
        time.sleep(0.05)
    time.sleep(0.5)  # in the engine, not just admitted


def assert_one_line(lines: list[dict], session_id: str, statement_id: str, user: str, reason: str,
                    at_least: float = 0.0) -> dict:
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["level"].lower() == "info", line
    assert line["session_id"] == session_id and line["statement_id"] == statement_id, line
    assert line["user"] == user and line["reason"] == reason, line
    assert float(line["duration"]) >= at_least, line
    return line


def test_client_cancel_logs_one_line(server):
    session = server.admin()
    statement_id = start_long(session=session)
    response = session.http.post(url=f"{session.url}/statements/{statement_id}/cancel", headers=session.headers())
    assert response.status_code == 202, response.text
    doc = session.wait_done(statement_id=statement_id, timeout=30)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "client", doc
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="client", at_least=0.5)


def test_statement_timeout_logs_one_line(server):
    session = server.admin()
    response = session.submit(sql=LONG, headers={"Prefer": "respond-async", "Warp-Statement-Timeout": "1000"})
    statement_id = response.json()["statement_id"]
    doc = session.wait_done(statement_id=statement_id, timeout=30)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    line = assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                           statement_id=statement_id, user=server.username, reason="timeout", at_least=0.9)
    assert float(line["duration"]) < 20, line


def test_server_query_timeout_logs_one_line(launcher):
    server = launcher.start(args=["--log-format", "json"], env={"HARDCHARGER_QUERY_TIMEOUT_SECONDS": "1"})
    session = server.admin()
    statement_id = session.submit(sql=LONG, headers={"Prefer": "respond-async"}).json()["statement_id"]
    doc = session.wait_done(statement_id=statement_id, timeout=30)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="timeout", at_least=0.9)


def test_kill_session_logs_one_line(server):
    victim = server.as_role(role="user", subject="victim")
    statement_id = start_long(session=victim)
    server.admin().execute(sql=f"KILL SESSION '{victim.id}'")
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=victim.id,
                    statement_id=statement_id, user="victim", reason="killed")
    time.sleep(0.5)
    assert len(cancelled_lines(server=server, statement_id=statement_id, expect=False)) == 1


def test_deleting_the_session_logs_one_line(server):
    session = server.as_role(role="user", subject="leaver")
    statement_id = start_long(session=session)
    response = session.close()
    assert response.status_code in (200, 204), response.text
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user="leaver", reason="session_closed")


def test_server_shutdown_logs_one_line(launcher):
    # Without graceful_shutdown the first signal stops the server, cancelling what runs.
    server = launcher.start(args=["--log-format", "json"], env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "false"})
    session = server.admin()
    statement_id = start_long(session=session)
    server.process.send_signal(STOP_SIGNAL)
    assert server.process.wait(timeout=30) == 0, server.log()
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id, timeout=1),
                    session_id=session.id, statement_id=statement_id, user=server.username, reason="server_shutdown")


def test_cancel_through_hardcharger_cancel_logs_one_line(server):
    victim = server.as_role(role="user", subject="cancelled_user")
    statement_id = start_long(session=victim)
    assert server.admin().scalar(sql=f"SELECT hardcharger_cancel('{victim.id}')") is True
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=victim.id,
                    statement_id=statement_id, user="cancelled_user", reason="cancelled")


def test_statements_that_end_normally_or_fail_log_none(server):
    session = server.admin()
    assert session.scalar(sql="SELECT count(*) FROM range(1000000)") == 1000000
    assert "does not exist" in session.error(sql="SELECT * FROM no_such_table")["detail"]
    # Its twin, cancelled the same way the first test does, is logged: the absence here is
    # the statements', not the log's.
    twin = start_long(session=session)
    session.http.post(url=f"{session.url}/statements/{twin}/cancel", headers=session.headers())
    assert len(cancelled_lines(server=server, statement_id=twin)) == 1
    assert cancelled_lines(server=server, session_id=session.id, expect=False) == cancelled_lines(
        server=server, statement_id=twin, expect=False)


def test_one_line_per_statement_of_a_session(server):
    """Several cancels of one statement (the client's and then the session's end) still
    give one line."""
    session = server.admin()
    statement_id = start_long(session=session)
    url = f"{session.url}/statements/{statement_id}/cancel"
    threads = [threading.Thread(target=lambda: session.http.post(url=url, headers=session.headers()))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    session.wait_done(statement_id=statement_id, timeout=30)
    session.close()
    time.sleep(0.5)
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="client")
