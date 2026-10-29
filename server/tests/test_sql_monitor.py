"""Live monitoring remains independent of async instrumentation and statement slots."""
import time

import pytest

from test_introspection import CPU_BOUND, STREAM, StreamRead


def eventually(read, predicate=bool):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = read()
        if predicate(result):
            return result
        time.sleep(0.05)
    raise AssertionError(result)


def live(admin, session):
    return admin.rows(sql=f"SELECT * FROM hardcharger_live_queries() WHERE session_id = '{session.id}'")


def cancel(admin, session):
    admin.scalar(sql=f"SELECT hardcharger_cancel('{session.id}')")


def test_disabled_and_safe_configuration(launcher):
    server = launcher.start()
    admin = server.admin()
    [info] = admin.rows(sql="SELECT * FROM hardcharger_monitor_info()")
    assert not info["instrumentation_enabled"] and not info["queueing_enabled"]
    assert info["slots_available"] is None
    assert "instrumentation" in admin.error(sql="SELECT * FROM hardcharger_live_queries()")["detail"]
    rows = admin.rows(sql="SELECT * FROM hardcharger_server_settings()")
    assert any(r["name"] == "hardcharger.enable_instrumentation" for r in rows)
    assert not any("password" in r["name"] or "private_key" in r["name"] or "secret_key" in r["name"] for r in rows)
    admin.execute(sql="SET GLOBAL hardcharger.max_concurrent_statements = 3")
    assert admin.scalar(sql="SELECT global_value FROM hardcharger_server_settings() "
                            "WHERE name = 'hardcharger.max_concurrent_statements'") == "3"


@pytest.mark.parametrize("restricted", [False, True])
def test_direct_sql_is_protected(launcher, restricted):
    server = launcher.start(args=["--enable-instrumentation"])
    user = (server.as_role(role="admin", catalog_access=[{"catalog": "*", "access": "read"}]) if restricted
            else server.as_role(role="user"))
    assert user.scalar(sql="SELECT HARDCHARGER_IS_FULL_ADMIN()") is False
    for function in ("hardcharger_live_queries", "hardcharger_monitor_info", "hardcharger_server_settings"):
        problem = user.error(sql=f"SELECT * FROM {function}()")
        assert problem["code"] == "permission_denied", problem
        assert "unrestricted administrators" in problem["detail"], problem


def test_executing_queued_and_queue_timeout(launcher):
    server = launcher.start(args=["--enable-instrumentation", "--max-concurrent-statements", "1"])
    admin, holder, waiter = server.admin(), server.admin(), server.admin()
    holder.execute(sql="SET hardcharger.bypass_queue = false")
    waiter.execute(sql="SET hardcharger.bypass_queue = false")
    waiter.execute(sql="SET hardcharger.max_queue_wait = 1")
    held = holder.submit(sql=CPU_BOUND, headers={"Prefer": "respond-async"}).json()["statement_id"]
    try:
        eventually(lambda: live(admin, holder), lambda rows: rows and rows[-1]["status"] == "executing")
        waiter.submit(sql="SELECT 42 AS queue_timeout_probe", headers={"Prefer": "respond-async"})
        queued = eventually(lambda: live(admin, waiter), lambda rows: any(r["status"] == "queued" for r in rows))
        assert any(r["enqueue_time"] is not None for r in queued)
        [info] = admin.rows(sql="SELECT * FROM hardcharger_monitor_info()")
        assert info["queueing_enabled"] and info["slots_in_use"] == 1 and info["slots_available"] == 0
        assert info["queued_statements"] == 1
        timed_out = eventually(lambda: live(admin, waiter), lambda rows: any(r["status"] == "timeout" for r in rows))
        assert any(r["cancel_reason"] == "queue_timeout" for r in timed_out)
    finally:
        cancel(admin, holder)
        holder.wait_done(statement_id=held)


def test_fetching_remains_live_and_completion_is_retained(launcher):
    server = launcher.start(args=["--enable-instrumentation"])
    admin, reader = server.admin(), server.admin()
    with StreamRead(server=server, session=reader, url=f"{reader.url}/statements", method="POST",
                    json_body={"sql": STREAM}) as stream:
        stream.first_chunk()
        [row] = eventually(lambda: live(admin, reader), lambda rows: len(rows) == 1 and rows[0]["status"] == "fetching" and rows[0]["rows_fetched"] > 0)
        assert row["execution_end_time"] is not None and row["fetch_end_time"] is None
        assert row["rows_fetched"] > 0
        cancel(admin, reader)
    eventually(lambda: live(admin, reader), lambda rows: any(r["cursor_close_time"] is not None for r in rows))
    reader.query(sql="SELECT 7 AS monitor_success")
    reader.error(sql="SELECT * FROM no_such_monitor_table")
    rows = eventually(lambda: live(admin, reader), lambda rows: {"success", "error"}.issubset({r["status"] for r in rows}))
    assert any(r["status"] == "error" and r["error_message"] for r in rows)


def test_execution_deadline_is_a_timeout_not_a_generic_cancel(launcher):
    server = launcher.start(args=["--enable-instrumentation"])
    admin, victim = server.admin(), server.admin()
    response = victim.submit(sql=CPU_BOUND, headers={"Prefer": "respond-async", "Chargewire-Statement-Timeout": "500"})
    victim.wait_done(statement_id=response.json()["statement_id"])
    rows = eventually(lambda: live(admin, victim), lambda rows: any(r["cursor_close_time"] for r in rows))
    assert rows[0]["status"] == "timeout" and rows[0]["cancel_reason"] == "timeout", rows
