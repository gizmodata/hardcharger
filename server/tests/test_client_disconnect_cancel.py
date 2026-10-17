"""Port of GizmoSQL 1.x tests/integration/test_client_disconnect_cancel.cpp (3 cases).

A client that goes away must not leave its query running: the session runs one
statement at a time, so a follow-up SELECT 1 on the same session finishing promptly
proves the query was interrupted. 1.x's two ways to go away become Warp SQL's:

- CancelledDoGetInterruptsRunningQuery: the client closes the connection of a streamed,
  unretained result (§6.5); here both mid-stream and while the first batch is still
  being computed (1.x's long aggregate).
- ClientDeadlineInterruptsRunningQuery: Warp-Statement-Timeout: 1000 ends it with
  cancel_reason "timeout" (§6.9).
- ConsumedQueryCompletesNormally: the control.
All 3 apply.
"""

import threading
import time

import httpx
import pytest

from warp_client import ARROW_STREAM, read_table

LONG_AGGREGATE = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b"
LONG_STREAM = "SELECT a.range * 2 AS v FROM range(100000000000) a"
SESSION_FREE_WITHIN = 30


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_PRINT_QUERIES": "true"})


def session_frees_within(session, seconds: float) -> float:
    started = time.monotonic()
    assert session.scalar(sql="SELECT 1") == 1
    elapsed = time.monotonic() - started
    assert elapsed < seconds, f"SELECT 1 took {elapsed:.1f}s: the abandoned query kept running"
    return elapsed


def test_closing_a_stream_mid_way_interrupts_the_query(server):
    session = server.admin()
    with httpx.Client(timeout=60) as client:
        with client.stream("POST", f"{server.base_url}{session.url}/statements",
                           headers=session.headers(**{"Accept": ARROW_STREAM}), json={"sql": LONG_STREAM}) as stream:
            assert stream.status_code == 200
            next(stream.iter_bytes())
            time.sleep(1)
    session_frees_within(session=session, seconds=SESSION_FREE_WITHIN)


def test_closing_the_connection_before_the_first_batch_interrupts_the_query(server):
    session = server.admin()

    def go_away():
        try:
            httpx.post(url=f"{server.base_url}{session.url}/statements",
                       headers=session.headers(**{"Accept": ARROW_STREAM}), json={"sql": LONG_AGGREGATE}, timeout=1)
        except httpx.TimeoutException:
            pass  # the client gives up after 1 s and closes its connection

    worker = threading.Thread(target=go_away)
    worker.start()
    worker.join()
    session_frees_within(session=session, seconds=SESSION_FREE_WITHIN)


def test_statement_timeout_interrupts_running_query(server):
    session = server.admin()
    response = session.submit(sql=LONG_AGGREGATE, headers={"Warp-Statement-Timeout": "1000",
                                                           "Prefer": "respond-async"})
    doc = session.wait_done(statement_id=response.json()["statement_id"], timeout=30)
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    session_frees_within(session=session, seconds=SESSION_FREE_WITHIN)


def test_consumed_query_completes_normally(server):
    session = server.admin()
    table = session.query(sql="SELECT count(*) AS n FROM range(1000000)")
    assert table.column("n").to_pylist() == [1000000]
