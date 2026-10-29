"""One info line per cancelled statement (GizmoSQL 1.x logged "SQL Statement was
successfully canceled"): message "statement cancelled" with session_id, statement_id (the
statement's q_... id), user, reason (Chargewire's cancel_reason) and duration in seconds, at
the default log level whatever the statement log level, for each way a statement is
stopped: the client's cancel, a statement timeout (Chargewire-Statement-Timeout and
query_timeout_seconds), KILL SESSION, the session's deletion and the server's shutdown. A
statement that ends normally, or fails, logs none.

The duration counts from the statement's submission, the instant its timeout counts from,
so a statement its timeout stopped logs at least the timeout, however long the server took
to start it.
"""

import threading
import time

import pytest

from conftest import STOP_SIGNAL, TIMEOUT_SCALE

# (count(*) over range() alone is answered from its cardinality, at once.)
LONG = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b"


@pytest.fixture(scope="module")
def server(module_launcher):
    # print_queries off: the line must not depend on the statement log.
    return module_launcher.start(args=["--log-format", "json", "--print-queries", "false"])


# The logged duration has three decimals: it may read up to half a millisecond short.
ROUNDING = 0.0005


def cancelled_lines(server, statement_id: str | None = None, session_id: str | None = None,
                    timeout: float = 15 * TIMEOUT_SCALE, expect: bool = True) -> list[dict]:
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


def submit_long(session) -> str:
    response = session.submit(sql=LONG, headers={"Prefer": "respond-async"})
    assert response.status_code in (200, 201, 202), response.text
    return response.json()["statement_id"]


def start_long(server, session) -> str:
    statement_id = submit_long(session=session)
    wait_in_engine(server=server, session=session, statement_id=statement_id)
    return statement_id


def wait_in_engine(server, session, statement_id: str) -> None:
    """Waits until the engine runs `statement_id`: hardcharger_active_connections() shows
    it as the session's statement, out of the statement queue. The statement document's
    "running" comes earlier, while the server may still be checking the statement."""
    admin = server.admin()
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while not admin.rows(sql="SELECT 1 FROM hardcharger_active_connections() WHERE session_id = ? "
                             "AND statement_id = ? AND coalesce(statement, '') <> '' AND queued_since IS NULL",
                         parameters=[session.id, statement_id]):
        assert time.monotonic() < deadline, "the statement never reached the engine"
        time.sleep(0.05)


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
    """The statement was submitted before its answer came, and cancelled after the cancel
    went out: the duration covers at least the time between the two, whenever the engine
    took the statement up."""
    session = server.admin()
    statement_id = submit_long(session=session)
    answered = time.monotonic()
    wait_in_engine(server=server, session=session, statement_id=statement_id)
    # Held a while after the answer, so a duration counted from later than the submission
    # falls short of the bound.
    time.sleep(max(0.0, answered + 0.5 - time.monotonic()))
    cancelling = time.monotonic()
    response = session.http.post(url=f"{session.url}/statements/{statement_id}/cancel", headers=session.headers())
    assert response.status_code == 202, response.text
    doc = session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "client", doc
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="client",
                    at_least=cancelling - answered - ROUNDING)


# A timeout counts from the statement's submission, and so does the logged duration: a
# statement its timeout stopped logs at least the timeout, however long the server took to
# start it (before the duration counted from the submission, a TSan build logged 0.575 for
# a 1 s timeout).
TIMEOUT_SECONDS = 1


def test_statement_timeout_logs_one_line(server):
    session = server.admin()
    response = session.submit(sql=LONG, headers={"Prefer": "respond-async",
                                                 "Chargewire-Statement-Timeout": str(TIMEOUT_SECONDS * 1000)})
    statement_id = response.json()["statement_id"]
    doc = session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    line = assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                           statement_id=statement_id, user=server.username, reason="timeout",
                           at_least=TIMEOUT_SECONDS)
    assert float(line["duration"]) < 20, line


def test_server_query_timeout_logs_one_line(launcher):
    server = launcher.start(args=["--log-format", "json"],
                            env={"HARDCHARGER_QUERY_TIMEOUT_SECONDS": str(TIMEOUT_SECONDS)})
    session = server.admin()
    statement_id = submit_long(session=session)
    doc = session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="timeout", at_least=TIMEOUT_SECONDS)


def test_timeout_while_queued_logs_one_line(launcher):
    """A statement its timeout stops while it waits in the statement queue logs a line too,
    counted from its submission like any other."""
    server = launcher.start(args=["--log-format", "json", "--max-concurrent-statements", "1"])
    holder = server.admin()
    holder.execute(sql="SET SESSION hardcharger.bypass_queue = false")
    held = start_long(server=server, session=holder)
    session = server.admin()
    session.execute(sql="SET SESSION hardcharger.bypass_queue = false")
    response = session.submit(sql=LONG, headers={"Prefer": "respond-async",
                                                 "Chargewire-Statement-Timeout": str(TIMEOUT_SECONDS * 1000)})
    statement_id = response.json()["statement_id"]
    doc = session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    # It never left the queue: the holder still has the one slot.
    assert holder.status(statement_id=held)["state"] == "running"
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="timeout", at_least=TIMEOUT_SECONDS)
    holder.http.post(url=f"{holder.url}/statements/{held}/cancel", headers=holder.headers())


def test_kill_session_logs_one_line(server):
    victim = server.as_role(role="user", subject="victim")
    statement_id = start_long(server=server, session=victim)
    server.admin().execute(sql=f"KILL SESSION '{victim.id}'")
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=victim.id,
                    statement_id=statement_id, user="victim", reason="killed")
    time.sleep(0.5 * TIMEOUT_SCALE)  # time for a second line, if there were one
    assert len(cancelled_lines(server=server, statement_id=statement_id, expect=False)) == 1


def test_deleting_the_session_logs_one_line(server):
    session = server.as_role(role="user", subject="leaver")
    statement_id = start_long(server=server, session=session)
    response = session.close()
    assert response.status_code in (200, 204), response.text
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user="leaver", reason="session_closed")


def test_server_shutdown_logs_one_line(launcher):
    # Without graceful_shutdown the first signal stops the server, cancelling what runs.
    server = launcher.start(args=["--log-format", "json"], env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "false"})
    session = server.admin()
    statement_id = start_long(server=server, session=session)
    server.process.send_signal(STOP_SIGNAL)
    assert server.process.wait(timeout=30 * TIMEOUT_SCALE) == 0, server.log()
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id, timeout=1),
                    session_id=session.id, statement_id=statement_id, user=server.username, reason="server_shutdown")


def test_cancel_through_hardcharger_cancel_logs_one_line(server):
    victim = server.as_role(role="user", subject="cancelled_user")
    statement_id = start_long(server=server, session=victim)
    assert server.admin().scalar(sql=f"SELECT hardcharger_cancel('{victim.id}')") is True
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=victim.id,
                    statement_id=statement_id, user="cancelled_user", reason="cancelled")


def test_statements_that_end_normally_or_fail_log_none(server):
    session = server.admin()
    assert session.scalar(sql="SELECT count(*) FROM range(1000000)") == 1000000
    assert "does not exist" in session.error(sql="SELECT * FROM no_such_table")["detail"]
    # Its twin, cancelled the same way the first test does, is logged: the absence here is
    # the statements', not the log's.
    twin = start_long(server=server, session=session)
    session.http.post(url=f"{session.url}/statements/{twin}/cancel", headers=session.headers())
    assert len(cancelled_lines(server=server, statement_id=twin)) == 1
    assert cancelled_lines(server=server, session_id=session.id, expect=False) == cancelled_lines(
        server=server, statement_id=twin, expect=False)


def test_one_line_per_statement_of_a_session(server):
    """Several cancels of one statement (the client's and then the session's end) still
    give one line."""
    session = server.admin()
    statement_id = start_long(server=server, session=session)
    url = f"{session.url}/statements/{statement_id}/cancel"
    threads = [threading.Thread(target=lambda: session.http.post(url=url, headers=session.headers()))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)
    session.close()
    time.sleep(0.5 * TIMEOUT_SCALE)  # time for a second line, if there were one
    assert_one_line(lines=cancelled_lines(server=server, statement_id=statement_id), session_id=session.id,
                    statement_id=statement_id, user=server.username, reason="client")
