"""Port of GizmoSQL 1.x tests/integration/test_instrumentation.cpp (17 cases).

Instrumentation (--enable-instrumentation,
HARDCHARGER_ENABLE_INSTRUMENTATION) records instances, sessions, statements and executions
in _hardcharger_instr (a DuckDB file next to the database, or in the working directory for
an in-memory one), with 1.x's tables, views and columns. 1.x's in-process manager cases
run against real servers here: stale cleanup after a SIGKILL, closed records after a
SIGTERM. 1.x's migration of a legacy (naive-timestamp) file and its license gate are not
applicable: Hardcharger creates its own schema and has no license. The failed-write case is a C++ unit test.

Execution timeline: the engine-side times (start, end) are exact; fetch_start_time and
fetch_end_time are when the server took the first and last batch from DuckDB, and
cursor_close_time when it let the result go (the Warp SDK has no delivery callback, so
these are not the moments the client received the bytes). The invariants 1.x asserts hold.
"""

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
    assert len(sid) == 36
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
    import threading
    def long_query():
        try:
            worker_session.error(sql="SELECT count(*) FROM range(10000000000) a, range(1000) b")
        except Exception:  # the server is killed under it
            pass

    threading.Thread(target=long_query, daemon=True).start()
    eventually(session, "SELECT count(*) FROM _hardcharger_instr.sql_executions WHERE status = 'executing'", lambda n: n >= 1)
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


def test_failed_write_does_not_affect_other_writes():
    results = run_unit_tests(gtest_filter="InstrumentationTest.*")
    assert results.get("InstrumentationTest.FailedWriteDoesNotAffectOtherWrites") is True, results
