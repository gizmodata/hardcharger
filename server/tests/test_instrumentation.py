"""Port of GizmoSQL 1.x tests/integration/test_instrumentation.cpp (17 cases).

Instrumentation (--enable-instrumentation,
HARDCHARGER_ENABLE_INSTRUMENTATION) records instances, sessions, statements and executions
in _hardcharger_instr (a DuckDB file next to the database, or in the working directory for
an in-memory one), with 1.x's tables, views and columns. 1.x's in-process manager cases
run against real servers here: stale cleanup after a SIGKILL, closed records after a
SIGTERM. 1.x's migration of a legacy (naive-timestamp) file and its license gate are not
applicable: Hardcharger creates its own schema and has no license. The failed-write case is a C++ unit
test, as are the batching writer's own cases (a full queue, the backlog at shutdown).

Execution timeline: the engine-side times (start, end) are exact; fetch_start_time and
fetch_end_time are when the server took the first and last batch from DuckDB, and
cursor_close_time when it let the result go (the Chargewire SDK has no delivery callback, so
these are not the moments the client received the bytes). The invariants 1.x asserts hold.
"""

import re
import time

import pytest

from conftest import STOP_SIGNAL, run_unit_tests

CLUSTER_ID = "12345678-1234-5678-1234-567812345678"


def start(launcher, **kwargs):
    args = kwargs.pop("args", [])
    return launcher.start(args=["--enable-instrumentation", *args], **kwargs)


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation", "--cluster-id", CLUSTER_ID])


@pytest.fixture
def admin(server):
    return server.admin()


def eventually(session, sql: str, predicate=bool, timeout: float = 10):
    """The first column of `sql`'s first row once `predicate` holds for it (None while
    there are no rows yet: the instrumentation writer is asynchronous)."""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        rows = session.query(sql=sql).to_pylist()
        value = next(iter(rows[0].values())) if rows else None
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(f"{sql} -> {value!r}")


def test_instrumentation_records_created(admin):
    assert admin.scalar(sql="SELECT 1 AS test_col") == 1
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    eventually(admin, f"SELECT count(*) FROM _hardcharger_instr.sessions WHERE session_id = '{sid}'", lambda n: n == 1)
    eventually(admin, f"SELECT count(*) FROM _hardcharger_instr.sql_statements WHERE session_id = '{sid}'", lambda n: n >= 2)


def test_execution_timeline_consistent(admin):
    assert admin.query(sql="SELECT i AS timeline_probe FROM range(100) t(i)").num_rows == 100
    probe = ("FROM _hardcharger_instr.execution_details WHERE sql_text LIKE '%timeline_probe%' "
             "AND sql_text NOT LIKE '%execution_details%' AND cursor_close_time IS NOT NULL")
    eventually(admin, f"SELECT count(*) {probe}", lambda n: n == 1)
    assert admin.scalar(sql=f"SELECT count(*) {probe} AND execution_end_time IS NOT NULL AND fetch_start_time IS NOT NULL "
                            "AND fetch_end_time IS NOT NULL AND duration_ms IS NOT NULL AND total_duration_ms IS NOT NULL "
                            "AND rows_fetched = 100 AND status = 'success'") == 1
    assert admin.scalar(sql="SELECT count(*) FROM _hardcharger_instr.sql_executions WHERE cursor_close_time IS NOT NULL "
                            "AND (date_diff('millisecond', execution_start_time, execution_end_time) <> duration_ms "
                            "OR date_diff('millisecond', execution_start_time, fetch_end_time) <> total_duration_ms "
                            "OR total_duration_ms < duration_ms OR fetch_end_time < execution_end_time "
                            "OR cursor_close_time < fetch_end_time "
                            "OR (fetch_start_time IS NOT NULL AND fetch_start_time > fetch_end_time))") == 0


def test_cluster_id_exposed_in_views(admin):
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    eventually(admin, f"SELECT count(*) FROM _hardcharger_instr.active_sessions WHERE session_id = '{sid}'", lambda n: n == 1)
    assert admin.scalar(sql=f"SELECT DISTINCT cluster_id::VARCHAR FROM _hardcharger_instr.session_activity "
                            f"WHERE session_id = '{sid}'") == CLUSTER_ID
    assert admin.scalar(sql=f"SELECT DISTINCT cluster_id::VARCHAR FROM _hardcharger_instr.active_sessions "
                            f"WHERE session_id = '{sid}'") == CLUSTER_ID
    assert admin.scalar(sql="SELECT HARDCHARGER_CURRENT_CLUSTER()") == CLUSTER_ID


def test_detach_instrumentation_prevented(admin):
    assert "Cannot DETACH" in admin.error(sql="DETACH _hardcharger_instr")["detail"]


@pytest.mark.parametrize("sql", ["DELETE FROM _hardcharger_instr.sql_statements",
                                 "INSERT INTO _hardcharger_instr.sessions (session_id, instance_id, username, role, peer) "
                                 "VALUES (uuid(), uuid(), 'x', 'admin', 'p')",
                                 "UPDATE _hardcharger_instr.sessions SET username = 'hacked'",
                                 "EXPLAIN ANALYZE DELETE FROM _hardcharger_instr.sql_statements",
                                 "USE _hardcharger_instr",
                                 "SET search_path = '_hardcharger_instr.main'"])
def test_modify_instrumentation_prevented(admin, sql):
    detail = admin.error(sql=sql)["detail"]
    assert "Cannot modify" in detail or "read-only" in detail or "Access denied" in detail, detail


def test_active_sessions_view(admin):
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    eventually(admin, f"SELECT count(*) FROM _hardcharger_instr.active_sessions WHERE session_id = '{sid}'", lambda n: n == 1)


def test_current_session_function(admin):
    sid = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    assert sid == admin.id and sid.startswith("s_")  # Hardcharger: the Chargewire session id (1.x: a UUID)
    recorded = eventually(admin, f"SELECT session_id::VARCHAR FROM _hardcharger_instr.sessions WHERE session_id = '{sid}'")
    assert recorded == sid


def test_current_instance_function(admin):
    instance = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    assert len(instance) == 36
    assert admin.scalar(sql=f"SELECT instance_id::VARCHAR FROM _hardcharger_instr.instances "
                            f"WHERE instance_id = '{instance}' AND status = 'running'") == instance


def test_user_function(server, admin):
    assert admin.scalar(sql="SELECT HARDCHARGER_USER()") == server.username


def test_role_function(admin):
    assert admin.scalar(sql="SELECT HARDCHARGER_ROLE()") == "admin"


def test_instrumentation_sql_functions(admin):
    row = admin.rows(sql="SELECT HARDCHARGER_INSTRUMENTATION_ENABLED() AS enabled, HARDCHARGER_INSTRUMENTATION_CATALOG() AS c, "
                         "HARDCHARGER_INSTRUMENTATION_SCHEMA() AS s")[0]
    assert row == {"enabled": True, "c": "_hardcharger_instr", "s": "main"}, row


def test_timing_columns_are_timestamp_tz(admin):
    rows = admin.rows(sql="SELECT table_name, column_name, data_type FROM information_schema.columns "
                          "WHERE table_catalog = '_hardcharger_instr' AND column_name IN ('start_time', 'stop_time', "
                          "'created_time', 'execution_start_time', 'execution_end_time') "
                          "AND table_name IN ('instances', 'sessions', 'sql_statements', 'sql_executions')")
    assert len(rows) == 7 and all(r["data_type"] == "TIMESTAMP WITH TIME ZONE" for r in rows), rows


def test_statements_record_their_kind_in_chargewire_terms(admin):
    """sql_statements.statement_kind says which Chargewire endpoint a statement came by
    ('statement' or 'prepared'); 0.3.0 named it flight_method, after GizmoSQL's Flight SQL."""
    columns = [r["column_name"] for r in admin.rows(sql="SELECT column_name FROM information_schema.columns WHERE "
                                                        "table_catalog = '_hardcharger_instr' AND "
                                                        "table_name = 'sql_statements'")]
    assert "statement_kind" in columns and not any("flight" in c for c in columns), columns
    marker = f"kind_probe_{time.time_ns()}"
    admin.scalar(sql=f"SELECT '{marker}' AS direct")
    prepared = admin.prepare(sql=f"SELECT '{marker}' AS via_prepare")["prepared_id"]
    assert admin.execute_prepared(prepared_id=prepared).status_code == 200
    kinds = eventually(admin, "SELECT string_agg(DISTINCT statement_kind, ',' ORDER BY statement_kind) FROM "
                              f"_hardcharger_instr.sql_statements WHERE sql_text LIKE '%{marker}%'",
                       lambda v: v == "prepared,statement")
    assert kinds == "prepared,statement"


# ---- Ids: a statement is recorded under the Chargewire id its client holds ----

I = "_hardcharger_instr.main"
CHARGEWIRE_ID = r"^[a-z]_[a-z2-7]{32}$"


def marker(name: str) -> str:
    return f"{name}_{time.time_ns()}"


def statement_id_of(response) -> str:
    statement_id = response.headers["Chargewire-Statement-Id"]
    assert re.match(CHARGEWIRE_ID, statement_id) and statement_id.startswith("q_"), statement_id
    return statement_id


def executions(session, where: str, expected: int) -> list[dict]:
    """sql_executions rows matching `where`, once there are `expected` of them, finished."""
    eventually(session, f"SELECT count(*) FROM {I}.sql_executions WHERE ({where}) AND cursor_close_time IS NOT NULL",
               lambda n: n == expected)
    return session.rows(sql=f"SELECT execution_id, statement_id, status, error_message, cancel_reason "
                            f"FROM {I}.sql_executions WHERE {where} ORDER BY execution_id")


def test_a_submitted_statement_is_recorded_under_the_id_its_client_holds(admin):
    """The Chargewire-Statement-Id a client gets (also in errors and hardcharger_active_connections())
    finds the statement and its execution: they share it."""
    probe = marker("id_probe")
    response = admin.submit(sql=f"SELECT 42 AS {probe}", arrow=True)
    assert response.status_code == 200, response.text
    q = statement_id_of(response)
    assert executions(admin, f"execution_id = '{q}'", 1)[0]["statement_id"] == q
    row = admin.rows(sql=f"SELECT statement_id, execution_id, session_id, sql_text, status FROM {I}.execution_details "
                         f"WHERE execution_id = '{q}'")
    assert row == [{"statement_id": q, "execution_id": q, "session_id": admin.id,
                    "sql_text": f"SELECT 42 AS {probe}", "status": "success"}], row
    assert admin.scalar(sql=f"SELECT statement_kind FROM {I}.sql_statements WHERE statement_id = '{q}'") == "statement"


def test_a_prepared_statement_is_one_p_row_with_an_execution_per_request(admin):
    probe = marker("prepared_probe")
    prepared = admin.prepare(sql=f"SELECT ? + 1 AS {probe}")["prepared_id"]
    assert re.match(CHARGEWIRE_ID, prepared) and prepared.startswith("p_"), prepared
    ran = []
    for i in range(3):
        response = admin.execute_prepared(prepared_id=prepared, parameters=[i])
        assert response.status_code == 200, response.text
        ran.append(statement_id_of(response))
    assert len(set(ran)) == 3
    rows = executions(admin, f"statement_id = '{prepared}'", 3)
    assert sorted(r["execution_id"] for r in rows) == sorted(ran), (rows, ran)
    assert {r["status"] for r in rows} == {"success"}
    assert admin.rows(sql=f"SELECT statement_id, statement_kind, prepare_success FROM {I}.sql_statements "
                          f"WHERE sql_text LIKE '%{probe}%' AND sql_text NOT LIKE '%sql_statements%'") == [
        {"statement_id": prepared, "statement_kind": "prepared", "prepare_success": True}]


def test_the_statements_of_a_script_are_numbered_executions_of_its_id(admin):
    probe = marker("script_probe")
    response = admin.submit(sql=f"SELECT 1; SELECT 2; SELECT 3 AS {probe}")
    q = statement_id_of(response)
    rows = executions(admin, f"statement_id = '{q}'", 3)
    assert [r["execution_id"] for r in rows] == [f"{q}.1", f"{q}.2", f"{q}.3"], rows
    assert admin.scalar(sql=f"SELECT count(*) FROM {I}.sql_statements WHERE statement_id = '{q}'") == 1


def test_a_scripts_numbers_are_places_in_the_script_commands_included(admin):
    """A SET hardcharger.* command is the script's first statement but runs no execution:
    the SELECT after it is still .2."""
    response = admin.submit(sql=f"SET hardcharger.query_tag = '{{}}'; SELECT 1 AS {marker('command_probe')}")
    q = statement_id_of(response)
    assert [r["execution_id"] for r in executions(admin, f"statement_id = '{q}'", 1)] == [f"{q}.2"]


def test_failed_and_refused_statements_are_recorded_under_their_ids(admin):
    probe = marker("failed_probe")
    failed = admin.submit(sql=f"SELECT error('{probe}')")
    assert failed.status_code >= 400, failed.text
    q = statement_id_of(failed)
    [row] = executions(admin, f"execution_id = '{q}'", 1)
    assert row["status"] == "error" and probe in row["error_message"], row
    refused = admin.submit(sql=f"SELEC '{probe}'")
    assert refused.status_code >= 400, refused.text
    r = statement_id_of(refused)
    eventually(admin, f"SELECT count(*) FROM {I}.sql_statements WHERE statement_id = '{r}' AND NOT prepare_success",
               lambda n: n == 1)


def test_a_cancelled_statement_is_recorded_under_its_id(server, admin):
    response = admin.submit(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b",
                            headers={"Prefer": "respond-async"})
    q = response.json()["statement_id"]
    reader = server.admin()  # the first session is busy running it
    eventually(reader, f"SELECT count(*) FROM {I}.sql_executions WHERE execution_id = '{q}' AND status = 'executing'",
               lambda n: n == 1)
    cancel = admin.http.post(url=f"{admin.url}/statements/{q}/cancel", headers=admin.headers())
    assert cancel.status_code in (200, 202, 204), cancel.text
    assert admin.wait_done(statement_id=q)["state"] == "cancelled"
    [row] = executions(reader, f"execution_id = '{q}'", 1)
    assert row["status"] == "cancelled" and row["cancel_reason"] == "client", row


def test_the_query_log_names_statements_by_the_same_ids(server, admin):
    probe = marker("log_probe")
    q = statement_id_of(admin.submit(sql=f"SELECT 1 AS {probe}", arrow=True))
    prepared = admin.prepare(sql=f"SELECT 2 AS {probe}")["prepared_id"]
    p_run = statement_id_of(admin.execute_prepared(prepared_id=prepared))

    def lines_with(text: str, count: int) -> list[str]:
        deadline = time.monotonic() + 10
        while True:
            found = [line for line in server.log().splitlines() if text in line]
            if len(found) >= count or time.monotonic() > deadline:
                return found
            time.sleep(0.05)

    # The statement, and its execution's success.
    submitted = lines_with(text=f"statement_id={q} ", count=2)
    assert any(probe in line for line in submitted) and any("succeeded" in line for line in submitted), submitted
    assert any(probe in line for line in lines_with(text=f"prepared_id={prepared} ", count=1))
    # A prepared execution's line names its own id and its prepared statement's.
    executed = lines_with(text=f"statement_id={p_run} ", count=1)
    assert any(f"prepared_id={prepared} " in line and "succeeded" in line for line in executed), executed


@pytest.mark.parametrize("table, columns", [
    # 0.3.0 and the builds after it: UUID statement ids (and, in 0.3.0, a flight_method column).
    ("sql_statements", "statement_id UUID NOT NULL, session_id VARCHAR NOT NULL, sql_text VARCHAR NOT NULL, "
                       "flight_method VARCHAR, is_internal BOOLEAN NOT NULL, prepare_success BOOLEAN NOT NULL, "
                       "prepare_error VARCHAR, created_time TIMESTAMP WITH TIME ZONE NOT NULL, query_tag JSON"),
    ("sql_executions", "execution_id UUID NOT NULL, statement_id UUID NOT NULL, bind_parameters VARCHAR, "
                       "execution_start_time TIMESTAMP WITH TIME ZONE NOT NULL, "
                       "execution_end_time TIMESTAMP WITH TIME ZONE, enqueue_time TIMESTAMP WITH TIME ZONE, "
                       "fetch_start_time TIMESTAMP WITH TIME ZONE, fetch_end_time TIMESTAMP WITH TIME ZONE, "
                       "cursor_close_time TIMESTAMP WITH TIME ZONE, rows_fetched BIGINT, status VARCHAR NOT NULL, "
                       "error_message VARCHAR, duration_ms BIGINT, total_duration_ms BIGINT, query_profile JSON, "
                       "cancel_reason VARCHAR"),
])
def test_a_table_with_uuid_statement_ids_is_refused_with_what_to_do(launcher, tmp_path, table, columns):
    path = tmp_path / "old_instrumentation.db"
    maker = launcher.start(args=["--database", str(path)])
    maker.admin().execute(sql=f"CREATE TABLE {table} ({columns})")
    maker.stop()
    server = start(launcher, args=["--instrumentation-db-path", str(path)], expect_up=False)
    assert server.process.wait(timeout=60) != 0
    column = "statement_id" if table == "sql_statements" else "execution_id"
    log = server.log()
    assert (f'Hardcharger can\'t use instrumentation table "_hardcharger_instr"."main"."{table}": its {column} '
            "is a UUID") in log, log
    assert "Chargewire ids clients hold (q_..." in log and "Drop the instrumentation tables" in log, log


def test_non_admins_cannot_read_it(server):
    user = server.as_role(role="user", subject="instr-user")
    detail = user.error(sql="SELECT * FROM _hardcharger_instr.sessions")["detail"]
    assert "Only administrators" in detail, detail


def test_env_var_enables_instrumentation(launcher):
    server = launcher.start(env={"HARDCHARGER_ENABLE_INSTRUMENTATION": "1"})
    assert server.admin().scalar(sql="SELECT HARDCHARGER_INSTRUMENTATION_ENABLED()") is True


def read_file(launcher, path, sql: str):
    reader = launcher.start().admin()
    reader.execute(sql=f"ATTACH '{path}' AS old_instr (READ_ONLY)")
    return reader.rows(sql=sql.replace("{I}", "old_instr.main"))


def test_sigterm_closes_records(launcher, tmp_path):
    server = start(launcher=launcher, args=["--database", str(tmp_path / "graceful.duckdb")])
    session = server.admin()
    for i in range(3):
        session.scalar(sql=f"SELECT {i}")
    server.process.send_signal(STOP_SIGNAL)
    assert server.process.wait(timeout=20) == 0
    path = tmp_path / "hardcharger_instrumentation.db"
    rows = read_file(launcher, path, "SELECT (SELECT count(*) FROM {I}.instances WHERE status = 'running') AS running, "
                                     "(SELECT count(*) FROM {I}.sessions WHERE status = 'active') AS active, "
                                     "(SELECT count(*) FROM {I}.sql_executions WHERE status = 'executing') AS executing, "
                                     "(SELECT any_value(status || '/' || stop_reason) FROM {I}.instances) AS instance")
    assert rows == [{"running": 0, "active": 0, "executing": 0, "instance": "stopped/graceful"}], rows


def test_stale_instance_cleanup(launcher, tmp_path):
    database = str(tmp_path / "stale.duckdb")
    first = start(launcher=launcher, args=["--database", database])
    session = first.admin()
    worker_session = first.admin()
    worker = worker_session.scalar(sql="SELECT HARDCHARGER_CURRENT_SESSION()")
    long_sql = "SELECT count(*) FROM range(10000000000) a, range(1000) b"
    import threading
    def long_query():
        try:
            worker_session.error(sql=long_sql)
        except Exception:  # the server is killed under it
            pass

    threading.Thread(target=long_query, daemon=True).start()
    # Wait for the long query's own execution row. Any 'executing' row won't do: the probe's
    # own execution (and the worker's HARDCHARGER_CURRENT_SESSION() above) can still read
    # 'executing' through the asynchronous writer, so the server could be killed before the
    # long query started (Windows CI hit that: no row was left for the cleanup to fail).
    eventually(session, "SELECT count(*) FROM _hardcharger_instr.sql_executions e "
                        "JOIN _hardcharger_instr.sql_statements s USING (statement_id) "
                        f"WHERE e.status = 'executing' AND s.session_id = '{worker}' AND s.sql_text = '{long_sql}'",
               lambda n: n >= 1)
    first_instance = session.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    first.process.kill()
    first.process.wait(timeout=10)
    second = start(launcher=launcher, args=["--database", database])
    rows = second.admin().rows(
        sql=f"SELECT (SELECT status || '/' || stop_reason FROM _hardcharger_instr.instances WHERE instance_id = '{first_instance}') AS instance, "
            f"(SELECT count(*) FROM _hardcharger_instr.sessions WHERE instance_id = '{first_instance}' AND NOT (status = 'closed' AND stop_reason = 'unclean_shutdown')) AS open_sessions, "
            "(SELECT count(*) FROM _hardcharger_instr.sql_executions WHERE status = 'executing' AND execution_start_time < now() - INTERVAL 1 SECOND) AS executing, "
            "(SELECT count(*) FROM _hardcharger_instr.sql_executions WHERE error_message = 'Server shutdown unexpectedly') AS failed")
    assert rows[0]["instance"] == "stopped/unclean_shutdown", rows
    assert rows[0]["open_sessions"] == 0 and rows[0]["executing"] == 0 and rows[0]["failed"] >= 1, rows


WRITER_UNIT_CASES = [
    "LiveMonitorDoesNotDependOnHistoryWrites", "FailedWriteDoesNotAffectOtherWrites", "ABadRecordInABatchLosesOnlyItself",
                     "RowsOfEarlierBatchesAreUpdatedAndOneBatchFoldsThem", "AFullQueueDropsCountsAndReportsNewRecords",
                     "ShutdownWritesTheBacklog", "ShutdownGivesUpOnTheBacklogAtItsDeadline",
                     "InvalidUtf8IsStoredReplaced"]


@pytest.fixture(scope="module")
def writer_unit_results():
    # The PostgreSQL case runs in test_instrumentation_ducklake.py, against the services.
    return run_unit_tests(gtest_filter="InstrumentationTest.*-InstrumentationTest.Postgres*")


@pytest.mark.parametrize("case", WRITER_UNIT_CASES)
def test_writer_unit_case(writer_unit_results, case):
    """The batching writer (server/unit/instrumentation_test.cc): a failed write or a bad
    record loses only itself; rows of earlier batches are updated, one batch folds an
    execution's events into one row; a full queue drops, counts and reports new records
    without blocking; Stop() writes the backlog, or drops what is left at its deadline."""
    assert writer_unit_results.get(f"InstrumentationTest.{case}") is True, writer_unit_results


def test_writer_unit_cases_are_all_listed(writer_unit_results):
    assert sorted(writer_unit_results) == sorted(f"InstrumentationTest.{c}" for c in WRITER_UNIT_CASES)


def test_concurrent_sessions_record_every_execution(launcher):
    """N sessions x M statements at once: exactly N x M executions, each finished with its
    own row count, under the statement that ran it."""
    import concurrent.futures
    server = start(launcher=launcher)
    sessions, statements = 16, 250

    def run(_):
        session = server.admin()
        for i in range(statements):
            assert session.scalar(sql=f"SELECT {i} AS concurrency_probe") == i
        return session.id

    with concurrent.futures.ThreadPoolExecutor(max_workers=sessions) as pool:
        ids = list(pool.map(run, range(sessions)))
    reader = server.admin()
    where = (f"session_id IN ({', '.join(repr(i) for i in ids)}) AND sql_text LIKE '%concurrency' || '_probe'")
    eventually(reader, f"SELECT count(*) FROM {I}.execution_details WHERE {where} AND cursor_close_time IS NOT NULL",
               lambda n: n == sessions * statements)
    rows = reader.rows(sql=f"SELECT count(*) AS executions, count(DISTINCT execution_id) AS ids, "
                           f"count(*) FILTER (WHERE status = 'success' AND rows_fetched = 1 AND execution_id = statement_id) "
                           f"AS right, count(DISTINCT session_id) AS sessions FROM {I}.execution_details WHERE {where}")
    total = sessions * statements
    assert rows == [{"executions": total, "ids": total, "right": total, "sessions": sessions}], rows
    assert reader.scalar(sql=f"SELECT count(*) FROM {I}.sql_statements WHERE {where}") == total
    info = reader.rows(sql="SELECT instrumentation_dropped, instrumentation_failed FROM hardcharger_server_info()")
    assert info == [{"instrumentation_dropped": 0, "instrumentation_failed": 0}], info


def test_sigterm_writes_the_backlog(launcher, tmp_path):
    """Every execution a client saw finish before the server was told to stop is in the
    file afterwards: Stop() writes the backlog before it closes the rows."""
    import concurrent.futures
    server = start(launcher=launcher, args=["--database", str(tmp_path / "backlog.duckdb")])
    sessions, statements = 8, 500

    def run(_):
        session = server.admin()
        for i in range(statements):
            assert session.scalar(sql=f"SELECT {i} AS backlog_probe") == i

    with concurrent.futures.ThreadPoolExecutor(max_workers=sessions) as pool:
        list(pool.map(run, range(sessions)))
    server.process.send_signal(STOP_SIGNAL)  # at once: nothing waits for the writer
    assert server.process.wait(timeout=60) == 0
    rows = read_file(launcher, tmp_path / "hardcharger_instrumentation.db",
                     "SELECT count(*) FILTER (WHERE e.status = 'success' AND e.cursor_close_time IS NOT NULL) AS done, "
                     "count(*) AS total FROM {I}.sql_executions e JOIN {I}.sql_statements s USING (statement_id) "
                     "WHERE s.sql_text LIKE '%backlog' || '_probe'")
    assert rows == [{"done": sessions * statements, "total": sessions * statements}], rows
    log = server.log()
    assert "instrumentation stopped" in log and "dropped=0 failed=0" in log, log


@pytest.mark.parametrize("flags", [[], ["--enable-catalog-logging"]], ids=["alone", "with-catalog-logging"])
def test_a_checkpoint_right_after_a_statement_is_not_refused(launcher, tmp_path, flags):
    """The writer records each statement on a connection of its own just after it ends. While
    that connection's current database was the server's, its write to the instrumentation file
    blocked the server database's CHECKPOINT, so a client's CHECKPOINT right after a
    statement failed with "Cannot CHECKPOINT: there are other write transactions active" about a
    third of the time (2026-10-28). Each round is a fast DDL statement and then a CHECKPOINT."""
    server = start(launcher=launcher, args=[*flags, "--database", str(tmp_path / "checkpoint.duckdb")])
    admin = server.admin()
    admin.execute(sql="CREATE TABLE t (i INTEGER)")
    refused = []
    for i in range(300):
        admin.execute(sql=f"ALTER TABLE t ADD COLUMN c{i} INTEGER")
        try:
            admin.execute(sql="CHECKPOINT")
        except Exception as error:  # noqa: BLE001 - every refusal is reported
            refused.append(f"round {i}: {error}")
    assert refused == [], f"{len(refused)} of 300 refused, e.g. {refused[:2]}"
