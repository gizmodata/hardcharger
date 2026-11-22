"""Progress is observable before execution completes, without issuing observer SQL."""
import time

from conftest import PERF_SCALE, TIMEOUT_SCALE


def samples(session, sql, parameters=None):
    response = session.submit(sql=sql, parameters=parameters, headers={"Prefer": "respond-async"})
    assert response.status_code in (200, 201), response.text
    doc = response.json()
    deadline = time.monotonic() + 45 * PERF_SCALE
    seen = []
    while doc["state"] not in ("succeeded", "failed", "cancelled"):
        seen.append(doc)
        assert time.monotonic() < deadline, doc
        time.sleep(0.05)
        doc = session.status(statement_id=doc["statement_id"])
    assert doc["state"] == "succeeded", doc
    return seen, doc


def test_duckdb_progress_is_enabled_without_printing(launcher):
    running = launcher.start()
    session = running.admin()
    assert session.scalar(sql="SELECT current_setting('enable_progress_bar')") is True
    assert session.scalar(sql="SELECT current_setting('enable_progress_bar_print')") is False
    session.execute(sql="SET threads=1; SET progress_bar_time=0")
    seen, _ = samples(session=session, sql="SELECT sum(hash(i)) FROM range(300000000) t(i)")
    estimates = [d["progress"]["fraction"] for d in seen if "fraction" in d.get("progress", {})]
    assert estimates, seen[:3]
    assert all(0 <= f <= 1 for f in estimates)


def test_no_estimate_before_progress_bar_time_and_a_long_statement_gets_one(launcher):
    """DuckDB computes no estimate before a statement has run progress_bar_time, then at most every
    100 ms (Hardcharger Engine progress sampling). Unpatched, it walks every pipeline
    of the plan after every task under a lock the worker threads need, which stalled plans with
    many parts (2.9.0 to 2.13.1). A status read whose answer arrives before progress_bar_time has
    passed since the submit must show no estimate (whatever the load: the statement started after
    the submit), and a long statement must get one after it."""
    running = launcher.start()
    session = running.admin()
    assert session.scalar(sql="SELECT current_setting('progress_bar_time')") == 2000
    delay = 4.0  # seconds; longer than the default for room on a loaded host's first status reads
    session.execute(sql=f"SET threads=1; SET progress_bar_time={int(delay * 1000)}")
    began = time.monotonic()
    response = session.submit(sql="SELECT sum(hash(i)) FROM range(1000000000000) t(i)",
                              headers={"Prefer": "respond-async"})
    assert response.status_code in (200, 201), response.text
    statement_id = response.json()["statement_id"]
    early, late = [], []
    deadline = began + delay + 45 * PERF_SCALE
    try:
        while not late:
            doc = session.status(statement_id=statement_id)
            answered = time.monotonic() - began
            assert time.monotonic() < deadline, (early, doc)
            if doc["state"] == "queued":
                time.sleep(0.05)
                continue
            assert doc["state"] == "running", doc
            fraction = doc.get("progress", {}).get("fraction")
            if answered < delay:
                early.append(fraction)
            elif fraction not in (None, 0):
                late.append(fraction)
            time.sleep(0.05)
    finally:
        cancel = session.http.post(url=f"{session.url}/statements/{statement_id}/cancel", headers=session.headers())
        assert cancel.status_code == 202, cancel.text
        assert session.wait_done(statement_id=statement_id, timeout=30 * TIMEOUT_SCALE)["state"] == "cancelled"
    assert early, "no status read was answered within progress_bar_time of the submit"
    # Between the statement's start and its first task DuckDB reports 0; never a computed estimate.
    assert all(f is None or f == 0 for f in early), early
    assert 0 < late[0] <= 1, late


def test_cli_progress_is_transient_and_never_written_to_pipes(launcher):
    import errno
    import os
    import select
    import subprocess
    import sys
    import pytest
    if sys.platform == "win32":
        pytest.skip("PTY assertion uses POSIX terminals")
    import pty
    from conftest import CLIENT_BINARY, clean_environment
    running = launcher.start()
    env = {**clean_environment(), "HARDCHARGER_PASSWORD": running.password, "HARDCHARGER_TLS": "false"}
    command = [str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(running.port), "--username", running.username,
               "-c", "SET threads=1; SELECT sum(hash(i)) AS answer FROM range(1000000000) t(i)"]
    master, slave = pty.openpty()
    process = subprocess.Popen(args=command, env=env, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    # Keep reading through completion, including on loaded CI hosts and under TSan.
    # Waiting after the read deadline can otherwise miss a successful query's final rows.
    query_timeout = 120 * PERF_SCALE
    deadline = time.monotonic() + query_timeout
    try:
        while True:
            assert time.monotonic() < deadline, f"CLI did not finish: {output.decode(errors='replace')}"
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:  # Linux PTYs signal EOF this way.
                        break
                    raise
                if not chunk:
                    break
                output += chunk
            elif process.poll() is not None:
                break
        assert process.wait(timeout=5 * TIMEOUT_SCALE) == 0, output.decode(errors="replace")
        assert b"% (estimated)" in output, output.decode(errors="replace")
        assert b"\r\x1b[2K" in output
        assert b"answer" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    # Same SQL path, with redirected output: no progress and no escape sequences.
    result = subprocess.run(args=command, env=env, capture_output=True, timeout=query_timeout)
    assert result.returncode == 0, result.stderr
    assert b"answer" in result.stdout
    assert b"estimated" not in result.stderr and b"\x1b" not in result.stderr
