"""Sessions a dead client leaves behind (issue #26).

A replication loader's Kubernetes Job was killed at its deadline mid-MERGE; the server
ran the MERGE to its end 9 hours later and kept the session, and the transaction it had
opened, until an administrator killed it, and every later run failed on the uncommitted
staging table. Two rules end that:

- A running statement no longer keeps an idle session alive by itself (Chargewire
  §5.7): a request waiting on it, or a read of its result, does. A client whose
  connection dropped mid-wait and never comes back loses its session after
  session_idle_timeout; the statement is cancelled with the session and its transaction
  rolled back. A client that comes back to poll within the window keeps it.
- idle_in_transaction_session_timeout (startup, or SET [GLOBAL]
  hardcharger.idle_in_transaction_session_timeout), as PostgreSQL's: a session that holds a
  transaction open with nothing running and no request for that long is ended, its
  transaction rolled back, and its next request gets session_ended with reason
  idle_in_transaction.

Each eviction is watched from an administrator's hardcharger_active_connections(), for up
to the timeout plus the SDK's 5 s sweep (scaled for sanitizer builds): a request on the
session itself, GET of its document included, is activity and would keep it. Each "lives
on" check waits that whole window first.
"""

import http.client
import json
import threading
import time
import uuid

import pytest

from conftest import TIMEOUT_SCALE, problem

SWEEP = 5  # seconds between the SDK's idle sweeps
TIMEOUT = 2  # the timeouts the servers here run with, in seconds
WINDOW = (TIMEOUT + SWEEP + 1) * TIMEOUT_SCALE  # an eviction due at TIMEOUT has happened by then

# A write that runs for minutes unless it is cancelled, and is cancelled promptly.
SLOW_COUNT = "SELECT count(*) FROM range(100000000000) a(i) WHERE i % 7 = 3"


def session_state(session) -> tuple[int, dict]:
    response = session.http.get(url=session.url, headers=session.headers())
    return response.status_code, response.json()


def alive(session) -> bool:
    return session_state(session)[0] == 200


def wait_until(predicate, what: str, timeout: float = WINDOW):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"not within {timeout:.0f}s: {what}")


def listed(admin, session) -> bool:
    """Whether the server still has `session`, asked without touching it."""
    return admin.scalar(sql=f"SELECT count(*) FROM hardcharger_active_connections() WHERE session_id = '{session.id}'") == 1


def wait_gone(admin, session, what: str) -> None:
    wait_until(lambda: not listed(admin, session), what=what)


def statements_like(admin, marker: str, state: str | None = None) -> int:
    """Other sessions' statements carrying `marker` (the asking statement carries it too)."""
    where = f"session_id <> HARDCHARGER_CURRENT_SESSION() AND statement LIKE '%{marker}%'"
    if state:
        where += f" AND state = '{state}'"
    return admin.scalar(sql=f"SELECT count(*) FROM hardcharger_active_connections() WHERE {where}")


def submit_and_drop_connection(server, session, sql: str, wait: int = 1, read_answer: bool = False) -> dict | None:
    """Submits `sql` on `session` over a connection of its own and closes it: without
    reading the answer (the client died mid-request; the request is complete, so the
    server runs it), or right after the answer (`read_answer`: the client's connection
    dropped once the wait ended). Returns the statement document when read."""
    host, port = server.base_url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host=host, port=int(port), timeout=30)
    connection.request(method="POST", url=f"{session.url}/statements", body=json.dumps({"sql": sql}),
                       headers={"Authorization": session.headers()["Authorization"],
                                "Content-Type": "application/json", "Prefer": f"wait={wait}"})
    doc = None
    if read_answer:
        response = connection.getresponse()
        doc = json.loads(response.read())
        assert response.status in (200, 201), doc
    connection.close()
    return doc


def wait_for_running(admin, marker: str) -> None:
    """Until the statement carrying `marker` runs on the engine (the raw request may still
    be in the server's hands when the socket closes)."""
    wait_until(lambda: statements_like(admin=admin, marker=marker, state="running") == 1,
               what=f"{marker} running", timeout=30 * TIMEOUT_SCALE)


@pytest.fixture(scope="module")
def idle_server(module_launcher):
    """session_idle_timeout 2 s, with instrumentation, so a dead client's statement shows
    its cancel reason and its session its end reason."""
    return module_launcher.start(args=["--enable-instrumentation"], env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": str(TIMEOUT)})


@pytest.fixture(scope="module")
def tx_server(module_launcher):
    """idle_in_transaction_session_timeout 2 s (the environment form of the option)."""
    return module_launcher.start(env={"HARDCHARGER_IDLE_IN_TRANSACTION_SESSION_TIMEOUT": str(TIMEOUT)})


# ---- A dead client's running write ----


def test_a_dead_clients_running_write_is_cancelled_and_rolled_back_with_its_session(idle_server):
    """The issue's case: an explicit transaction, a long write, the client gone. After the
    idle timeout the session is gone, the write cancelled (cancel_reason session_closed in
    instrumentation), and the transaction rolled back: the table has none of its rows."""
    admin = idle_server.admin()
    table = f"dead_write_{uuid.uuid4().hex[:8]}"
    admin.execute(sql=f"CREATE TABLE {table} (v BIGINT)")
    victim = idle_server.admin()
    victim.execute(sql="BEGIN")
    victim.execute(sql=f"INSERT INTO {table} VALUES (1)")
    assert session_state(victim)[1]["transaction"] == "open"
    # Identify the write by its unique table: SQL comments are deliberately masked.
    marker = table
    submit_and_drop_connection(server=idle_server, session=victim,
                               sql=f"INSERT INTO {table} {SLOW_COUNT}")
    wait_for_running(admin=admin, marker=marker)
    wait_gone(admin=admin, session=victim, what="the dead client's session evicted")
    # It held a transaction open, so it says so (issue #29), rather than vanishing.
    doc = problem(response=victim.http.get(url=victim.url, headers=victim.headers()), status=401, code="session_ended")
    assert doc["reason"] == "idle_in_transaction", doc
    assert admin.scalar(sql=f"SELECT count(*) FROM {table}") == 0, "the transaction must be rolled back"
    assert statements_like(admin=admin, marker=marker) == 0, "the write stopped with its session"
    wait_until(lambda: admin.rows(sql="SELECT status, cancel_reason FROM _hardcharger_instr.execution_details "
                                      f"WHERE sql_text LIKE '%{marker}%' AND status = 'cancelled'")
               == [{"status": "cancelled", "cancel_reason": "session_closed"}],
               what="instrumentation records the cancel", timeout=15 * TIMEOUT_SCALE)
    wait_until(lambda: admin.rows(sql=f"SELECT stop_reason FROM _hardcharger_instr.sessions WHERE session_id = '{victim.id}'")
               == [{"stop_reason": "idle_in_transaction"}], what="instrumentation records why the session ended",
               timeout=15 * TIMEOUT_SCALE)


def test_a_dead_clients_autocommit_write_is_cancelled_too(idle_server):
    """Without an explicit transaction the write's own transaction rolls back: the table
    the CTAS was making never exists. The session held no transaction the client opened, so
    it is simply gone (session_not_found)."""
    admin = idle_server.admin()
    table = f"dead_ctas_{uuid.uuid4().hex[:8]}"
    victim = idle_server.admin()
    submit_and_drop_connection(server=idle_server, session=victim, sql=f"/* {table} */ CREATE TABLE {table} AS {SLOW_COUNT}")
    wait_for_running(admin=admin, marker=table)
    wait_gone(admin=admin, session=victim, what="the dead client's session evicted")
    problem(response=victim.http.get(url=victim.url, headers=victim.headers()), status=404, code="session_not_found")
    assert statements_like(admin=admin, marker=table) == 0, "the CTAS stopped with its session"
    assert admin.scalar(sql=f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{table}'") == 0, "no table"


def test_an_idle_session_that_held_a_transaction_ends_with_a_reason(idle_server):
    """Issue #29: a session evicted as idle (session_idle_timeout; the idle-in-transaction
    timeout is off here) while it held a transaction open from SQL BEGIN ends with
    session_ended, reason idle_in_transaction, so its client learns the transaction was
    rolled back. Before, it vanished (session_not_found), and a client that replaces lost
    sessions ran its next statement alone. A session of the same age in no transaction still
    gets session_not_found."""
    admin = idle_server.admin()
    table = f"idle_evicted_{uuid.uuid4().hex[:8]}"
    admin.execute(sql=f"CREATE TABLE {table} (v BIGINT)")
    in_tx = idle_server.admin()
    in_tx.execute(sql="BEGIN")
    in_tx.execute(sql=f"INSERT INTO {table} VALUES (1)")
    assert session_state(in_tx)[1]["transaction"] == "open"
    assert session_state(in_tx)[1]["idle_in_transaction_timeout_s"] == 0, "only the idle rule applies here"
    no_tx = idle_server.admin()  # the same age, in no transaction (and no retained result to keep it)
    wait_gone(admin=admin, session=in_tx, what="the session in a transaction evicted")
    wait_gone(admin=admin, session=no_tx, what="the session in no transaction evicted")
    doc = problem(response=in_tx.submit(sql=f"INSERT INTO {table} VALUES (2)"), status=401, code="session_ended")
    assert doc["reason"] == "idle_in_transaction", doc
    assert "session_idle_timeout" in doc["detail"] and "rolled back" in doc["detail"], doc
    problem(response=no_tx.http.get(url=no_tx.url, headers=no_tx.headers()), status=404, code="session_not_found")
    assert admin.scalar(sql=f"SELECT count(*) FROM {table}") == 0, "rolled back, and the refused insert never ran"
    wait_until(lambda: admin.rows(sql="SELECT session_id, stop_reason FROM _hardcharger_instr.sessions "
                                      f"WHERE session_id IN ('{in_tx.id}', '{no_tx.id}') ORDER BY session_id = '{no_tx.id}'")
               == [{"session_id": in_tx.id, "stop_reason": "idle_in_transaction"},
                   {"session_id": no_tx.id, "stop_reason": "idle"}],
               what="instrumentation records each session's end", timeout=15 * TIMEOUT_SCALE)


def test_a_client_that_comes_back_to_poll_keeps_its_running_write(idle_server):
    """The positive twin: the connection drops mid-wait, but the client polls the
    statement within the window, so the session, the statement and its result stay."""
    table = f"polled_{uuid.uuid4().hex[:8]}"
    client = idle_server.admin()
    client.execute(sql="BEGIN")
    doc = submit_and_drop_connection(server=idle_server, session=client, read_answer=True,
                                     sql=f"CREATE TABLE {table} AS SELECT sleep_ms({int(2 * WINDOW * 1000)}) AS slept")
    assert doc["state"] == "running", doc
    statement_id = doc["statement_id"]
    doc = client.status(statement_id=statement_id, wait=1)  # back, over a new connection
    deadline = time.monotonic() + 4 * WINDOW
    while doc["state"] not in ("succeeded", "failed", "cancelled"):
        assert time.monotonic() < deadline, doc
        time.sleep(0.5)
        doc = client.status(statement_id=statement_id, wait=1)  # a request every ~1.5 s, within 2 s
    assert doc["state"] == "succeeded", doc
    assert alive(client), "a client that polls within the window keeps its session"
    client.execute(sql="COMMIT")
    assert idle_server.admin().scalar(sql=f"SELECT count(*) FROM {table}") == 1  # a fresh session: the old admin's idled out


def test_a_request_waiting_on_a_statement_keeps_the_session(idle_server):
    """A client holding one long-poll after another, each longer than the idle timeout,
    never lets the session look idle (§5.7: a request in flight is a request)."""
    client = idle_server.admin()
    done = {}
    worker = threading.Thread(target=lambda: done.setdefault("doc", client.execute(sql=f"SELECT sleep_ms({int(1000 * WINDOW)})")))
    worker.start()
    worker.join(timeout=4 * WINDOW)
    assert done["doc"]["state"] == "succeeded", done
    assert alive(client)


# ---- idle_in_transaction_session_timeout ----


def test_the_session_document_shows_the_timeout(tx_server):
    status, doc = session_state(tx_server.admin())
    assert status == 200 and doc["idle_in_transaction_timeout_s"] == TIMEOUT, doc


def test_a_session_idle_in_a_transaction_is_ended_and_rolled_back(tx_server):
    admin = tx_server.admin()
    table = f"idle_tx_{uuid.uuid4().hex[:8]}"
    idle, control = tx_server.admin(), tx_server.admin()
    idle.execute(sql="BEGIN")
    idle.execute(sql=f"CREATE TABLE {table} AS SELECT 1 AS v")
    assert session_state(idle)[1]["transaction"] == "open"
    control.execute(sql="SELECT 1")  # the same age, in no transaction
    wait_gone(admin=admin, session=idle, what="the idle transaction's session ended")
    doc = problem(response=idle.http.get(url=idle.url, headers=idle.headers()), status=401, code="session_ended")
    assert doc["reason"] == "idle_in_transaction", doc
    assert "idle_in_transaction_timeout" in doc["detail"] and "rolled back" in doc["detail"], doc
    problem(response=idle.submit(sql="SELECT 1"), status=401, code="session_ended")
    assert admin.scalar(sql=f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{table}'") == 0, "rolled back"
    assert alive(control), "a session in no transaction is not idle in one"


def test_an_aborted_transaction_counts_as_an_open_one(tx_server):
    aborted = tx_server.admin()
    aborted.execute(sql="BEGIN")
    aborted.error(sql="SELECT * FROM no_such_table_here")
    assert session_state(aborted)[1]["transaction"] == "aborted"
    wait_gone(admin=tx_server.admin(), session=aborted, what="the aborted transaction's session ended")
    assert problem(response=aborted.http.get(url=aborted.url, headers=aborted.headers()), status=401,
                   code="session_ended")["reason"] == "idle_in_transaction"


def test_autocommit_off_is_a_transaction_from_the_start(tx_server):
    session = tx_server.admin()
    assert session.set_options(options={"autocommit": False}).status_code == 200
    assert session_state(session)[1]["transaction"] == "open"
    wait_gone(admin=tx_server.admin(), session=session, what="the autocommit-off session ended")
    assert problem(response=session.http.get(url=session.url, headers=session.headers()), status=401,
                   code="session_ended")["reason"] == "idle_in_transaction"


def test_requests_and_running_statements_keep_a_transaction_alive(tx_server):
    """Activity within the window keeps the session: requests (SELECT 1 every half second),
    then one statement that runs longer than the timeout and the sweep together."""
    session = tx_server.admin()
    session.execute(sql="BEGIN")
    deadline = time.monotonic() + WINDOW
    while time.monotonic() < deadline:
        assert session.scalar(sql="SELECT 1") == 1
        time.sleep(0.5)
    assert alive(session) and session_state(session)[1]["transaction"] == "open"
    assert session.execute(sql=f"SELECT sleep_ms({int(1000 * WINDOW)})")["state"] == "succeeded"
    assert alive(session) and session_state(session)[1]["transaction"] == "open"
    session.execute(sql="COMMIT")
    time.sleep(WINDOW)
    assert alive(session), "after the commit the session is in no transaction, and lives on"


def test_set_session_applies_to_that_session_only(launcher):
    server = launcher.start()  # the option off
    chosen, other = server.admin(), server.admin()
    assert session_state(chosen)[1]["idle_in_transaction_timeout_s"] == 0
    chosen.execute(sql=f"SET hardcharger.idle_in_transaction_session_timeout = {TIMEOUT}")
    assert session_state(chosen)[1]["idle_in_transaction_timeout_s"] == TIMEOUT
    assert chosen.scalar(sql="SELECT session_value FROM hardcharger_settings() "
                             "WHERE name = 'hardcharger.idle_in_transaction_session_timeout'") == str(TIMEOUT)
    assert other.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.idle_in_transaction_session_timeout'") == "0"
    chosen.execute(sql="BEGIN")
    other.execute(sql="BEGIN")
    wait_gone(admin=server.admin(), session=chosen, what="the session that set the timeout ended")
    assert problem(response=chosen.http.get(url=chosen.url, headers=chosen.headers()), status=401,
                   code="session_ended")["reason"] == "idle_in_transaction"
    assert alive(other) and session_state(other)[1]["transaction"] == "open", "no timeout for the other session"
    doc = server.admin().error(sql="SET hardcharger.idle_in_transaction_session_timeout = -1")
    assert "idle_in_transaction_session_timeout must be at least 0, not -1" in doc["detail"], doc


def test_set_global_applies_to_sessions_without_their_own_and_needs_an_admin(launcher):
    server = launcher.start(args=["--idle-in-transaction-session-timeout", "0"])  # the flag form
    admin = server.admin()
    user = server.as_role(role="user")
    doc = user.error(sql=f"SET GLOBAL hardcharger.idle_in_transaction_session_timeout = {TIMEOUT}")
    assert doc["code"] == "permission_denied", doc
    assert doc["detail"] == "Only admin users can SET GLOBAL hardcharger.idle_in_transaction_session_timeout", doc
    user.execute(sql="BEGIN")
    own = server.admin()
    own.execute(sql="SET hardcharger.idle_in_transaction_session_timeout = 0")  # its own value: never
    own.execute(sql="BEGIN")
    admin.execute(sql=f"SET GLOBAL hardcharger.idle_in_transaction_session_timeout = {TIMEOUT}")
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.idle_in_transaction_session_timeout'") == str(TIMEOUT)
    assert session_state(user)[1]["idle_in_transaction_timeout_s"] == TIMEOUT, "an open session follows the global"
    assert session_state(own)[1]["idle_in_transaction_timeout_s"] == 0, "a session's own value wins"
    wait_gone(admin=admin, session=user, what="the user's idle transaction ended under the global")
    assert problem(response=user.http.get(url=user.url, headers=user.headers()), status=401,
                   code="session_ended")["reason"] == "idle_in_transaction"
    assert alive(own) and session_state(own)[1]["transaction"] == "open"
    admin.execute(sql="RESET GLOBAL hardcharger.idle_in_transaction_session_timeout")
    assert admin.scalar(sql="SELECT value FROM hardcharger_settings() "
                            "WHERE name = 'hardcharger.idle_in_transaction_session_timeout'") == "0"
    late = server.admin()
    late.execute(sql="BEGIN")
    time.sleep(WINDOW)
    assert alive(late), "back at the startup value, never"
