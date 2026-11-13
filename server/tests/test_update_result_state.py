"""A statement without rows answers with its finished document (issue #42).

Through 2.12.0 a streamed submit (`Accept: application/vnd.apache.arrow.stream`) of a
statement without rows could answer

    {"state": "running", "result": {"kind": "update", "rows_affected": 1}}

a statement that says it is still running, with its final result attached. The Chargewire SDK
set such a statement's result, then read the session's options and transaction, then set its
state, and the submit was answered at the result. A client that reads `state` then polls a
statement that is over (the ADBC driver: three requests for an INSERT instead of two). The
documented `curl` example (docs: clients/http, "Statements without rows answer with a JSON
statement document") failed that way once in the docs' CI.

How often it showed depended on the server's CPUs: the request thread, woken by the result,
had to run before the engine thread reached the next line. Measured on the 2.12.0 image,
looping the documented request: 0 of 3000 answers with 18 CPUs, 7 of 5000 with four, 25 of
5000 with two and 1955 of 2000 with one. So this test runs the server on ONE CPU where the
operating system lets a test say so (Linux: the server inherits a one-CPU affinity mask,
and the test checks every thread of the server has it); 2.12.0 fails it there in the first
rounds. On macOS and Windows it is a plain loop of the same requests. The SDK's own tests
(chargewire-sdk-tests, update_outcome_test.cc) hold the engine thread in the window on every
platform, whatever the CPUs.
"""

import os
import pathlib
import sys

import httpx
import pytest

ARROW = "application/vnd.apache.arrow.stream"
ROUNDS = 100


def server_cpus(pid: int) -> set[int]:
    """The CPUs any thread of process `pid` may run on (Linux)."""
    cpus: set[int] = set()
    for task in pathlib.Path(f"/proc/{pid}/task").iterdir():
        cpus |= os.sched_getaffinity(int(task.name))
    return cpus


@pytest.fixture(scope="module")
def server(module_launcher):
    """A server held to one CPU where the platform has CPU affinity (Linux): the child
    process inherits the mask this thread has while it starts it."""
    if not hasattr(os, "sched_setaffinity"):
        return module_launcher.start()
    allowed = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(allowed)})
    try:
        # One CPU for a server that starts with a thread per CPU: give it time.
        return module_launcher.start(startup_seconds=90)
    finally:
        os.sched_setaffinity(0, allowed)


def test_a_statement_without_rows_answers_its_finished_document(server):
    if hasattr(os, "sched_setaffinity"):
        # The forcing step: every thread of the server shares one CPU.
        assert len(server_cpus(pid=server.process.pid)) == 1, server_cpus(pid=server.process.pid)
    elif sys.platform not in ("darwin", "win32"):
        pytest.fail(reason=f"no CPU affinity on {sys.platform}: say here how this test holds the server to one CPU")

    headers = {"Authorization": f"Bearer {server.token()}"}
    session = f"{server.base_url}/chargewire/v1/sessions/{server.session(token=headers['Authorization'][7:])}"
    one_row, no_count = {"kind": "update", "rows_affected": 1}, {"kind": "update"}

    def post(path: str, body: dict, accept: str | None = ARROW) -> httpx.Response:
        # A connection per request, as curl makes one.
        return httpx.post(url=f"{session}{path}", json=body, timeout=120,
                          headers={**headers, **({"Accept": accept} if accept else {})})

    created = post(path="/statements", body={"sql": "CREATE TABLE one_step (v INTEGER)"}, accept=None)
    assert created.status_code == 201 and created.json()["state"] == "succeeded", created.text
    prepared = post(path="/prepared", body={"sql": "INSERT INTO one_step VALUES (?)"}, accept=None)
    assert prepared.status_code == 201, prepared.text
    execute = f"/prepared/{prepared.json()['prepared_id']}/execute"

    seen = 0
    for round_ in range(ROUNDS):
        # The documented sequence: a streamed query, then a streamed statement without rows.
        rows = post(path="/statements",
                    body={"sql": "SELECT i, i * 2 AS twice FROM range(3) t(i) WHERE i >= ?", "parameters": [1]})
        assert rows.status_code == 200 and rows.headers["content-type"].startswith(ARROW), rows.text
        statements = [
            ("/statements", {"sql": f"CREATE TABLE notes_{round_} AS SELECT 1 AS id, 'hello' AS body"}, one_row),
            ("/statements", {"sql": "INSERT INTO one_step VALUES (1)"}, one_row),
            (execute, {"parameters": [2]}, one_row),
            ("/statements", {"sql": "SET enable_progress_bar = false"}, no_count),
            ("/statements", {"sql": "BEGIN"}, no_count),
            ("/statements", {"sql": "COMMIT"}, no_count),
        ]
        for path, body, result in statements:
            response = post(path=path, body=body)
            assert response.status_code == 200, (round_, body, response.text)
            assert response.headers["content-type"].startswith("application/json"), (round_, body, response.text)
            doc = response.json()
            assert (doc["state"], doc["result"]) == ("succeeded", result), (round_, body, doc)
            assert "finished_at" in doc and "progress" not in doc, (round_, body, doc)
            seen += 1
    assert seen == ROUNDS * 6

    if hasattr(os, "sched_setaffinity"):
        # Still on one CPU at the end: the threads the server started on the way inherited it.
        assert len(server_cpus(pid=server.process.pid)) == 1
    counted = post(path="/statements", body={"sql": "UPDATE one_step SET v = v"}, accept=None)
    assert counted.json()["result"] == {"kind": "update", "rows_affected": 2 * ROUNDS}, counted.text
