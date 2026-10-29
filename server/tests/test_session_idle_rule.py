"""The idle-session rule, exactly (README, session_idle_timeout): the Chargewire SDK sweeps
every 5 s, and a session is evicted at the first sweep after session_idle_timeout seconds
without a request, unless a statement of it is running, an ingest of it is in progress, a
read of one of its results is under way, or it holds a retained result that hasn't expired
(Chargewire §5.7). An evicted session answers 404 session_not_found.

Each session here is probed once, at the moment the test is about: every request is
activity that restarts its idle clock."""

import time

import httpx
import pytest

from conftest import problem

SWEEP = 5  # seconds between the SDK's idle sweeps
IDLE = 2  # session_idle_timeout of the servers here
SLACK = 1.5
RETENTION = 10  # result_retention_seconds of the retained-result test
ARROW_STREAM = "application/vnd.apache.arrow.stream"


def status_of(session) -> int:
    return session.http.get(url=session.url, headers=session.headers()).status_code


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_SESSION_IDLE_TIMEOUT": str(IDLE),
                                      "HARDCHARGER_RESULT_RETENTION_SECONDS": str(RETENTION)})


def test_a_session_without_requests_is_gone_within_one_sweep_after_its_timeout(server):
    early, late = server.admin(), server.admin()
    started = time.monotonic()
    time.sleep(IDLE - 1)
    assert status_of(session=early) == 200, "evicted before its idle timeout"
    time.sleep(started + IDLE + SWEEP + SLACK - time.monotonic())
    problem(response=late.http.get(url=late.url, headers=late.headers()), status=404, code="session_not_found")


def test_a_running_statement_keeps_the_session(server):
    session = server.admin()
    response = session.submit(sql="SELECT sleep_ms(12000) AS slept",
                              headers={"Prefer": "respond-async", "Chargewire-Retain-Result": "false"})
    assert response.status_code in (200, 201, 202), response.text
    statement_id = response.json()["statement_id"]
    time.sleep(IDLE + SWEEP + SLACK)  # no request at all meanwhile
    assert session.status(statement_id=statement_id)["state"] == "running"
    assert status_of(session=session) == 200


def test_a_retained_result_keeps_the_session_until_it_expires(server):
    kept, gone = server.admin(), server.admin()
    docs = [s.execute(sql="SELECT 42 AS answer") for s in (kept, gone)]  # JSON mode: retained
    assert all(doc["result"]["retained"] and doc["result"]["expires_at"] for doc in docs), docs
    started = time.monotonic()
    time.sleep(IDLE + SWEEP + SLACK)
    # Long idle, but its result is kept: the session stays, and the result reads again.
    result = kept.result(statement_id=docs[0]["statement_id"])
    assert result.status_code == 200, result.text
    assert kept.table_of(response=result).to_pylist() == [{"answer": 42}]
    # Untouched since, the other session goes at the first sweep after its result expires.
    time.sleep(started + RETENTION + SWEEP + SLACK - time.monotonic())
    problem(response=gone.http.get(url=gone.url, headers=gone.headers()), status=404, code="session_not_found")


def test_a_slow_read_keeps_the_session_to_the_end_of_its_stream(server):
    # The statement finishes while up to 32 MB of its unretained result are still to be
    # sent; reading that takes longer than the idle timeout plus a sweep, and the stream
    # must still reach its end (2.0.0-rc.1 evicted the session and cut it).
    session = server.admin()
    headers = session.headers(**{"Accept": ARROW_STREAM, "Chargewire-Retain-Result": "false"})
    received, failure = 0, None
    tail = b""
    checked_live = False
    started = time.monotonic()
    with session.http.stream(method="POST", url=f"{session.url}/statements", headers=headers,
                             json={"sql": "SELECT range AS n FROM range(10000000)"}, timeout=120) as response:
        assert response.status_code == 200, response.read()
        try:
            for chunk in response.iter_raw(chunk_size=256 * 1024):
                received += len(chunk)
                tail = (tail + chunk)[-8:]
                if not checked_live and time.monotonic() - started > IDLE + SWEEP + SLACK:
                    # Probe while most of the body is still to come. Once the server
                    # has sent it all, the session may expire while the client drains
                    # bytes already buffered by TCP, even though iter_raw still runs.
                    assert received < 40_000_000, received
                    assert status_of(session=session) == 200
                    checked_live = True
                time.sleep(0.08)  # ~3 MB/s
        except httpx.HTTPError as error:
            failure = repr(error)
    assert failure is None, f"the stream was cut after {received} bytes: {failure}"
    assert received > 80_000_000, received
    assert checked_live, "the read must outlast the idle timeout plus a sweep"
    assert tail == b"\xff\xff\xff\xff\x00\x00\x00\x00", "missing Arrow end-of-stream marker"


def test_a_session_whose_reads_are_done_is_idle_again(server):
    session = server.admin()
    assert session.scalar(sql="SELECT 1") == 1  # streamed, unretained, read to its end
    time.sleep(IDLE + SWEEP + SLACK)
    problem(response=session.http.get(url=session.url, headers=session.headers()), status=404,
            code="session_not_found")
