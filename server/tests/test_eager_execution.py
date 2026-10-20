"""Port of GizmoSQL 1.x tests/integration/test_eager_execution.cpp (15 cases).

Writes take effect when submitted, before (and without) any fetch; reading a result
again never runs the statement again; transactions are honoured. 1.x expressed these with
Flight tickets; here with Warp SQL statements (§6), prepared statements (§7) and session
transactions (§5.5). All 15 apply, two re-expressed:

- EvictedTicketsFailWithoutRepeatingWrites: 1.x's per-session cache of 1024 results
  evicted the oldest; Warp keeps statement documents until the session ends (results
  are bounded by a byte quota instead). The case asserts what matters: 1025 inserts run
  exactly once each.
- RebindWhileStreamingIsRejectedAsBusy: 1.x refused a second execution of a prepared
  statement while its first stream was open ("busy"). Warp SQL runs a session's
  statements one at a time, so the second execution waits its turn and then succeeds;
  the case asserts that (and that nothing is lost), not a "busy" error.
"""

import threading
import time

import httpx
import pyarrow as pa
import pytest

from warp_client import ARROW_STREAM, read_batches, read_table


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start()


@pytest.fixture
def session(server):
    return server.admin()


def async_submit(session, sql: str) -> dict:
    """Submits without waiting for, or fetching, anything."""
    response = session.submit(sql=sql, headers={"Prefer": "respond-async"})
    assert response.status_code == 201, response.text
    return session.wait_done(statement_id=response.json()["statement_id"])


def test_eager_writes_honor_transactions(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_transactions(n INTEGER)")
    assert session.set_options(options={"autocommit": False}).status_code == 200
    async_submit(session=session, sql="INSERT INTO eager_transactions VALUES (1)")
    assert session.http.post(url=f"{session.url}/rollback", headers=session.headers()).status_code == 204
    assert session.scalar(sql="SELECT count(*) FROM eager_transactions") == 0
    async_submit(session=session, sql="INSERT INTO eager_transactions VALUES (2)")
    assert session.http.post(url=f"{session.url}/commit", headers=session.headers()).status_code == 204
    session.set_options(options={"autocommit": True})
    assert session.scalar(sql="SELECT sum(n) FROM eager_transactions") == 2


def test_completed_statements_cannot_be_read_by_another_session(server, session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_owned(n INTEGER)")
    doc = async_submit(session=session, sql="INSERT INTO eager_owned VALUES (1)")
    other = server.as_role(role="admin", subject="someone-else")
    response = other.http.get(url=f"{session.url}/statements/{doc['statement_id']}", headers=other.headers())
    assert response.status_code == 404 and response.json()["code"] == "session_not_found", response.text
    assert session.scalar(sql="SELECT sum(n) FROM eager_owned") == 1


def test_completed_statements_are_not_cancellable(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_cancel(n INTEGER)")
    doc = async_submit(session=session, sql="INSERT INTO eager_cancel VALUES (42)")
    response = session.http.post(url=f"{session.url}/statements/{doc['statement_id']}/cancel", headers=session.headers())
    assert response.status_code == 202 and response.json()["state"] == "succeeded", response.text
    assert session.scalar(sql="SELECT sum(n) FROM eager_cancel") == 42


def test_many_statements_never_repeat_writes(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_eviction(n INTEGER)")
    first = async_submit(session=session, sql="INSERT INTO eager_eviction VALUES (1)")
    for _ in range(1024):
        session.execute(sql="INSERT INTO eager_eviction VALUES (1)")
    assert session.status(statement_id=first["statement_id"])["state"] == "succeeded"
    assert session.scalar(sql="SELECT count(*) FROM eager_eviction") == 1025


def test_copy_and_attach_side_effects_finish_without_fetching(session, tmp_path):
    target = tmp_path / "eager_copy.csv"
    async_submit(session=session, sql=f"COPY (SELECT 42 AS n) TO '{target}'")
    assert target.exists() and "42" in target.read_text()
    async_submit(session=session, sql="ATTACH ':memory:' AS eager_attached")
    assert session.scalar(sql="SELECT count(*) FROM duckdb_databases() WHERE database_name = 'eager_attached'") == 1
    async_submit(session=session, sql="DETACH eager_attached")
    assert session.scalar(sql="SELECT count(*) FROM duckdb_databases() WHERE database_name = 'eager_attached'") == 0


def test_unprepared_write_commits_without_fetching(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_ledger (token VARCHAR, n BIGINT)")
    async_submit(session=session, sql="INSERT INTO eager_ledger VALUES ('no_fetch', 1)")
    assert session.scalar(sql="SELECT count(*) FROM eager_ledger WHERE token = 'no_fetch'") == 1


def test_unprepared_replay_does_not_repeat_write(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_replay (token VARCHAR, n BIGINT)")
    session.execute(sql="INSERT INTO eager_replay VALUES ('replay', 0)")
    doc = async_submit(session=session, sql="UPDATE eager_replay SET n = n + 1 WHERE token = 'replay'")
    for _ in range(3):
        assert session.status(statement_id=doc["statement_id"])["result"]["rows_affected"] == 1
    assert session.scalar(sql="SELECT n FROM eager_replay WHERE token = 'replay'") == 1


def test_prepared_replay_and_reuse_are_distinct_executions(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_prepared (token VARCHAR, n BIGINT)")
    session.execute(sql="INSERT INTO eager_prepared VALUES ('prepared', 0)")
    prepared = session.prepare(sql="UPDATE eager_prepared SET n = n + 1 WHERE token = 'prepared'")
    first = session.execute_prepared(prepared_id=prepared["prepared_id"], arrow=False).json()
    assert session.scalar(sql="SELECT n FROM eager_prepared") == 1
    second = session.execute_prepared(prepared_id=prepared["prepared_id"], arrow=False).json()
    assert session.scalar(sql="SELECT n FROM eager_prepared") == 2
    for doc in (first, second):
        session.status(statement_id=doc["statement_id"])
    assert session.scalar(sql="SELECT n FROM eager_prepared") == 2


def test_one_prepared_insert_can_bind_and_execute_many_times(session):
    session.execute(sql="CREATE OR REPLACE TABLE eager_bound (token BIGINT PRIMARY KEY, n BIGINT)")
    prepared = session.prepare(sql="INSERT INTO eager_bound VALUES (?, ?)")
    assert session.scalar(sql="SELECT count(*) FROM eager_bound") == 0
    for token in range(1, 21):
        response = session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[token, token * 7],
                                            arrow=False)
        assert response.status_code in (200, 201), response.text
        assert session.scalar(sql="SELECT count(*) FROM eager_bound") == token
    # Replaying every statement document changes nothing.
    assert session.scalar(sql="SELECT count(*) FROM eager_bound") == 20
    assert session.scalar(sql="SELECT sum(n) FROM eager_bound") == 1470
    assert session.scalar(sql="SELECT count(*) FROM eager_bound WHERE n <> token * 7") == 0


def test_metrics_enablement_is_discoverable_and_startup_only(session):
    row = session.rows(sql="SELECT value, settable, cli_flag FROM hardcharger_settings() WHERE name = 'hardcharger.enable_metrics'")
    assert row == [{"value": "false", "settable": False, "cli_flag": "--enable-metrics"}], row
    assert "--enable-metrics" in session.error(sql="SET hardcharger.enable_metrics = true")["detail"]


def test_eager_ddl_and_merge_complete_without_fetching(session):
    for sql in ["CREATE TABLE eager_ddl (token INTEGER PRIMARY KEY, n INTEGER)",
                "ALTER TABLE eager_ddl ADD COLUMN extra INTEGER DEFAULT 0",
                "INSERT INTO eager_ddl(token, n) VALUES (1, 0), (2, 0)",
                "MERGE INTO eager_ddl t USING (VALUES (1, 3), (3, 5)) s(token,n) ON t.token=s.token "
                "WHEN MATCHED THEN UPDATE SET n=t.n+1 WHEN NOT MATCHED THEN INSERT(token,n) VALUES(s.token,s.n)",
                "DELETE FROM eager_ddl WHERE token=2"]:
        async_submit(session=session, sql=sql)
    assert session.scalar(sql="SELECT count(*) FROM eager_ddl") == 2
    assert session.scalar(sql="SELECT n FROM eager_ddl WHERE token=1") == 1
    assert session.scalar(sql="SELECT sum(n) FROM eager_ddl") == 6
    async_submit(session=session, sql="DROP TABLE eager_ddl")
    assert session.scalar(sql="SELECT count(*) FROM duckdb_tables() WHERE table_name='eager_ddl'") == 0


def test_side_effect_statements_execute_without_fetching(session):
    session.execute(sql="CREATE SCHEMA IF NOT EXISTS eager_side_effects")
    session.execute(sql="CREATE OR REPLACE TABLE eager_side_effects.ledger(n INTEGER)")
    async_submit(session=session, sql="SET search_path = 'eager_side_effects'")
    assert session.scalar(sql="SELECT current_setting('search_path')") == "eager_side_effects"
    assert session.scalar(sql="SELECT count(*) FROM ledger") == 0
    async_submit(session=session, sql="BEGIN TRANSACTION")
    session.execute(sql="INSERT INTO ledger VALUES (1)")
    async_submit(session=session, sql="ROLLBACK")
    assert session.scalar(sql="SELECT count(*) FROM ledger") == 0
    async_submit(session=session, sql="BEGIN TRANSACTION")
    session.execute(sql="INSERT INTO ledger VALUES (2)")
    async_submit(session=session, sql="COMMIT")
    assert session.scalar(sql="SELECT count(*) FROM ledger") == 1
    async_submit(session=session, sql="RESET search_path")
    assert session.scalar(sql="SELECT current_setting('search_path')") != "eager_side_effects"


# Serial: it streams 20 million rows while a second execution waits behind them. When
# that wait outlasts the server's synchronous answer (60 s at most, spec §6.1; ASan CI
# took 112 s), the second comes back as a 200 with its statement document, still queued,
# instead of a stream; a client follows it (Session.table_of), as the next test checks.
@pytest.mark.serial
def test_rebind_while_streaming_waits_its_turn(session):
    # 20 million rows (160 MB): more than the server buffers ahead of a reader that has
    # stopped (32 MB plus the encoding window), so the first statement is still producing
    # when the second arrives. A result that fits the buffer finishes at once and frees
    # the session for the next statement.
    session.execute(sql="CREATE OR REPLACE TABLE busy_stream AS SELECT range AS v FROM range(20000000)")
    prepared = session.prepare(sql="SELECT v FROM busy_stream WHERE v >= ?")
    url = f"{session.url}/prepared/{prepared['prepared_id']}/execute"
    headers = session.headers(**{"Accept": ARROW_STREAM})
    rows_first = 0
    second = {}
    with httpx.Client(timeout=120) as first_client:
        with first_client.stream("POST", f"{session.client.base_url}{url}", headers=headers,
                                 json={"parameters": [0]}) as stream:
            chunks = stream.iter_bytes()
            body = next(chunks)

            def run_second():
                with httpx.Client(timeout=120) as c:
                    second["response"] = c.post(url=f"{session.client.base_url}{url}", headers=headers,
                                                json={"parameters": [19999997]})

            worker = threading.Thread(target=run_second)
            worker.start()
            time.sleep(0.5)
            assert worker.is_alive(), "the second execution must wait for the open stream"
            for chunk in chunks:
                body += chunk
        rows_first = read_table(content=body).num_rows
    worker.join(timeout=60)
    assert rows_first == 20000000
    assert second["response"].status_code == 200, second["response"].text
    assert session.table_of(response=second["response"]).num_rows == 3


@pytest.mark.serial
def test_a_streamed_execution_still_waiting_its_turn_answers_its_document(session):
    """The slow case of the test above, made certain with Prefer: wait=1: an execution
    whose outcome isn't known when the server's wait ends answers 200 with its statement
    document (queued), not an Arrow stream, and that statement then runs and its result
    can be read (once: it isn't retained)."""
    session.execute(sql="CREATE OR REPLACE TABLE busy_document AS SELECT range AS v FROM range(20000000)")
    prepared = session.prepare(sql="SELECT v FROM busy_document WHERE v >= ?")
    url = f"{session.client.base_url}{session.url}/prepared/{prepared['prepared_id']}/execute"
    headers = session.headers(**{"Accept": ARROW_STREAM})
    with httpx.Client(timeout=120) as first_client:
        with first_client.stream("POST", url, headers=headers, json={"parameters": [0]}) as stream:
            chunks = stream.iter_bytes()
            body = next(chunks)
            with httpx.Client(timeout=120) as c:
                second = c.post(url=url, headers={**headers, "Prefer": "wait=1"}, json={"parameters": [19999997]})
            assert second.status_code == 200, second.text
            assert second.headers["content-type"].startswith("application/json"), second.headers
            doc = second.json()
            assert doc["state"] == "queued" and doc["result"] is None, doc
            assert second.headers["warp-statement-id"] == doc["statement_id"]
            for chunk in chunks:
                body += chunk
        assert read_table(content=body).num_rows == 20000000
    assert session.table_of(response=second).num_rows == 3
    again = session.result(statement_id=doc["statement_id"])
    assert again.status_code == 409 and again.json()["code"] == "result_not_resumable", again.text


def test_sequential_reexecute_after_cancelled_stream_succeeds(session):
    session.execute(sql="CREATE OR REPLACE TABLE busy_cancel AS SELECT range AS v FROM range(5000000)")
    prepared = session.prepare(sql="SELECT v FROM busy_cancel WHERE v >= ?")
    url = f"{session.client.base_url}{session.url}/prepared/{prepared['prepared_id']}/execute"
    headers = session.headers(**{"Accept": ARROW_STREAM})
    for i in range(50):
        with httpx.Client(timeout=60) as c:
            with c.stream("POST", url, headers=headers, json={"parameters": [i]}) as stream:
                assert stream.status_code == 200, f"iteration {i}: {stream.read()}"
                next(stream.iter_bytes())  # take the first piece, abandon the rest
    small = session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[4999998])
    assert read_table(content=small.content).num_rows == 2


def test_tight_sequential_loop_never_busy(session):
    prepared = session.prepare(sql="SELECT 'TABLE' AS kind FROM duckdb_tables() WHERE ? < 0 "
                                   "UNION ALL SELECT 'ROW' WHERE ? >= 0")
    for i in range(300):
        response = session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[i, i])
        assert response.status_code == 200, f"iteration {i}: {response.text}"
        assert read_table(content=response.content).num_rows == 1, f"iteration {i}"
