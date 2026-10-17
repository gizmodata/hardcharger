"""Port of GizmoSQL 1.x tests/integration/test_statement_queue.cpp (8 cases).

The statement queue: max_concurrent_statements
(--max-concurrent-statements) statements run at once, the rest wait their turn; admins
skip it unless they SET SESSION hardcharger.bypass_queue = false. A queued statement can
be killed (KILL SESSION) or time out (Warp-Statement-Timeout, 1.x: a gRPC deadline),
and then never runs once a slot frees.

A queued statement is in instrumentation while it waits (status 'queued'), with its
enqueue_time; QueuedExecutionRecordsEnqueueTime also checks the queue-wait metric, and
KilledWhileQueuedRecordsCancelled also that the victim fails with session_ended.
"""

import threading
import time

import pytest

from conftest import TIMEOUT_SCALE


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--max-concurrent-statements", "2", "--enable-metrics", "--metrics-port", "0",
                                       "--enable-instrumentation"])


def queued_session(server):
    session = server.admin()
    session.execute(sql="SET SESSION hardcharger.bypass_queue = false")
    return session


def hold_slots(server, count: int, ms: int) -> list[threading.Thread]:
    threads = []
    for _ in range(count):
        session = queued_session(server=server)
        thread = threading.Thread(target=lambda s=session: s.execute(sql=f"SELECT sleep_ms({ms})"))
        thread.start()
        threads.append(thread)
    return threads


def wait_until(predicate, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def active(server) -> float:
    return server.admin().scalar(sql="SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_queue_depth' "
                                     "AND labels['state'] = 'active'")


def test_admin_can_toggle_bypass_queue(server):
    session = server.admin()
    for value in ("true", "false", "1"):
        session.execute(sql=f"SET SESSION hardcharger.bypass_queue = {value}")


def test_bypass_queue_rejects_invalid_value(server):
    assert "bypass_queue" in server.admin().error(sql="SET SESSION hardcharger.bypass_queue = 'maybe'")["detail"]


def test_non_admins_cannot_lift_their_own_queueing(server):
    user = server.as_role(role="user", subject="queued-user")
    problem = user.error(sql="SET SESSION hardcharger.bypass_queue = true")
    assert problem["code"] == "permission_denied" and "Only admin users" in problem["detail"], problem


def test_queries_execute_through_the_queue(server):
    session = queued_session(server=server)
    assert session.scalar(sql="SELECT 42") == 42
    assert session.query(sql="SELECT * FROM range(10)").num_rows == 10


def test_settings_table_is_composable(server):
    table = server.admin().query(sql="SELECT name, value, settable FROM hardcharger_settings() WHERE name LIKE 'hardcharger.max%'")
    assert table.num_rows == 5 and table.num_columns == 3, table.to_pylist()


def test_queue_serializes_concurrent_statements(server):
    # The holders must still hold their slots when the probe arrives, however slow the
    # build: they sleep 2 s (stretched by TIMEOUT_SCALE); the probe must wait 0.8 s.
    holders = hold_slots(server=server, count=2, ms=int(2000 * TIMEOUT_SCALE))
    assert wait_until(lambda: active(server) == 2)
    probe = queued_session(server=server)
    started = time.monotonic()
    assert probe.scalar(sql="SELECT 1") == 1
    waited = time.monotonic() - started
    for thread in holders:
        thread.join()
    assert waited >= 0.8, f"the probe should have queued for a slot; waited {waited:.2f}s"


def recorded(server, sql: str, timeout: float = 10):
    """The first row of `sql` (an admin's read of instrumentation), once there is one."""
    admin = server.admin()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = admin.rows(sql=sql)
        if rows:
            return rows[0]
        time.sleep(0.1)
    raise AssertionError(f"nothing recorded: {sql}")


def test_queued_execution_records_enqueue_time(server):
    metric = "SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_queue_wait_seconds_sum'"
    before = server.admin().scalar(sql=metric)
    holders = hold_slots(server=server, count=2, ms=1500)
    assert wait_until(lambda: active(server) == 2)
    probe = queued_session(server=server)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("value", probe.scalar(sql="SELECT 4242 AS qprobe")))
    worker.start()
    # While it waits, a monitor sees it queued. (Spelled in two parts, so the
    # monitor's own statement doesn't match.)
    waiting = recorded(server=server, sql="SELECT status FROM _hardcharger_instr.execution_details "
                                          "WHERE sql_text LIKE '%qpr' || 'obe%' AND status = 'queued'")
    assert waiting == {"status": "queued"}
    worker.join(timeout=10)
    assert outcome == {"value": 4242}
    for thread in holders:
        thread.join()
    row = recorded(server=server, sql="SELECT queue_wait_ms, status FROM _hardcharger_instr.execution_details "
                                      "WHERE sql_text LIKE '%qpr' || 'obe%' AND status = 'success' "
                                      "AND queue_wait_ms IS NOT NULL")
    assert row["queue_wait_ms"] >= 500, row
    assert server.admin().scalar(sql=metric) - before >= 0.5


def test_killed_while_queued_records_cancelled(server):
    holders = hold_slots(server=server, count=2, ms=3000)
    assert wait_until(lambda: active(server) == 2)
    victim = queued_session(server=server)
    victim_id = server.admin().scalar(sql="SELECT session_id FROM hardcharger_sessions() WHERE NOT is_current "
                                          "ORDER BY start_time DESC LIMIT 1")
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("problem", victim.error(sql="SELECT 'never' AS v")))
    worker.start()
    time.sleep(0.5)
    started = time.monotonic()
    server.admin().execute(sql=f"KILL SESSION '{victim_id}'")
    worker.join(timeout=10)
    assert time.monotonic() - started < 2.5, "the killed queued statement should end at once"
    assert outcome["problem"]["code"] == "session_ended", outcome
    for thread in holders:
        thread.join()
    row = recorded(server=server, sql="SELECT status, error_message FROM _hardcharger_instr.execution_details "
                                      "WHERE sql_text LIKE '%''nev' || 'er'' AS v%'")
    assert row == {"status": "cancelled", "error_message": "cancelled while queued"}, row


def test_expired_queued_write_never_executes_after_slot_release(server):
    writer = queued_session(server=server)
    writer.execute(sql="CREATE OR REPLACE TABLE queue_expired (n INTEGER)")
    holders = hold_slots(server=server, count=2, ms=2500)
    assert wait_until(lambda: active(server) == 2)
    response = writer.submit(sql="INSERT INTO queue_expired VALUES (1)", headers={"Warp-Statement-Timeout": "500"})
    doc = response.json()
    if doc.get("state") not in ("failed", "cancelled", "succeeded"):
        doc = writer.wait_done(statement_id=doc["statement_id"])
    assert doc["state"] == "cancelled" and doc["cancel_reason"] == "timeout", doc
    for thread in holders:
        thread.join()
    time.sleep(0.5)
    assert writer.scalar(sql="SELECT count(*) FROM queue_expired") == 0, "the expired write ran after all"
