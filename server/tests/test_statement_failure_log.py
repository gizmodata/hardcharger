"""A statement that fails while its rows are read: after the server answered 200 and began the
Arrow stream, so the stream ends without its end-of-stream marker (Chargewire §6.6) and the
statement's document holds the error. The server logs it as it logs a statement that fails
before ("statement failed", with its statement_id and the error), and records it in the
instrumentation, the error made loggable (a connection string's password removed) in both.
Before, only the statement's start was logged and the record kept the raw message."""

import time

import pytest

from chargewire_client import ChargewireError

# Rows stream out before the statement reaches the rows that fail.
FAILS_MID_STREAM = ("SELECT CASE WHEN range < 500000 THEN range ELSE error('boom mid-stream password=hunter2') END AS v "
                    "FROM range(1000000)")


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--log-format", "json", "--enable-instrumentation"])


def eventually(check, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.1)


def test_a_statement_failing_mid_stream_is_logged_and_recorded(server):
    admin = server.admin()
    with pytest.raises(ChargewireError) as raised:
        admin.query(sql=FAILS_MID_STREAM)
    # The client reads the statement's document, as §6.6 says, and sees why.
    assert "boom mid-stream" in str(raised.value), raised.value
    failed = eventually(lambda: [r for r in server.log_records()
                                 if r.get("message") == "statement failed" and "boom mid-stream" in r.get("error", "")])
    assert len(failed) == 1, failed
    assert failed[0]["statement_id"].startswith("q_") and failed[0]["status"] == "error", failed
    assert "hunter2" not in server.log(), "the log carries the error's password"
    rows = eventually(lambda: admin.query(
        sql="SELECT status, error_message FROM _hardcharger_instr.sql_executions "
            "WHERE error_message LIKE '%boom mid-stream%'").to_pylist())
    assert [r["status"] for r in rows] == ["error"], rows
    assert "hunter2" not in rows[0]["error_message"], rows


def test_a_statement_failing_before_its_rows_is_logged_once(server):
    # The positive twin of the path that already logged: one line, not two.
    admin = server.admin()
    with pytest.raises(ChargewireError):
        admin.query(sql="SELECT * FROM no_such_table_for_the_failure_log")
    failed = eventually(lambda: [r for r in server.log_records() if r.get("message") == "statement failed"
                                 and "no_such_table_for_the_failure_log" in r.get("error", "")])
    assert len(failed) == 1, failed
