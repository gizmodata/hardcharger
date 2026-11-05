"""The CLI and a server that is resuming (Chargewire §11.2, server_resuming).

A stopped GizmoData Cloud cluster with auto-resume has a small holder at its address
that answers 503 server_resuming with a Retry-After while the cluster starts. Here a stub
stands in for it: it answers the first requests (or every one, or every statement) that
way, then passes the rest through to a real server. The CLI waits as the server asks, the
sign-in included, says so while it waits (a line per answer in a log, a countdown in a
terminal), gives up at --resume-timeout (HARDCHARGER_RESUME_TIMEOUT, default 600 s, 0: no
wait), and stops waiting on Ctrl-C.
"""

import subprocess
import sys
import time

import pexpect
import pytest

from conftest import CLIENT_BINARY, clean_environment, interrupt, interruptible
from resuming_holder import Holder

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(username="testuser")


@pytest.fixture
def holder_for(server):
    holders = []

    def make(**kwargs) -> Holder:
        holder = Holder(upstream_port=server.port, **kwargs)
        holders.append(holder)
        return holder

    yield make
    for holder in holders:
        holder.stop()


def args(server, holder: Holder, *more: str) -> list[str]:
    return [str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(holder.port), "--tls=false",
            "--username", server.username, *more]


def env(server, **extra: str) -> dict[str, str]:
    return {**clean_environment(), "HARDCHARGER_PASSWORD": server.password, **extra}


def run(server, holder: Holder, *more: str, timeout: float = 120, **extra_env: str):
    started = time.monotonic()
    result = subprocess.run(args=args(server, holder, *more), env=env(server, **extra_env), capture_output=True,
                            text=True, timeout=timeout)
    return result, time.monotonic() - started


def test_the_cli_waits_for_a_resuming_server_and_says_so(server, holder_for):
    holder = holder_for(resuming=3)
    result, took = run(server, holder, "--csv", "--no-header", "-c", "SELECT 42 AS answer")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42", result.stdout
    lines = [line for line in result.stderr.splitlines() if line.startswith("The server is resuming")]
    assert len(lines) == 3, result.stderr  # a line per answer in a log, not per second
    assert lines[0].startswith("The server is resuming; retrying in 1 s ("), lines  # the stock detail isn't repeated
    assert took >= 2.5, took
    # The sign-in was held and sent again as it was, credential included.
    sign_ins = [body for method, path, body in holder.held if path == "/chargewire/v1/auth/token"]
    assert sign_ins and all(body == sign_ins[0] for body in sign_ins), holder.held


def test_a_holders_own_detail_is_shown(server, holder_for):
    holder = holder_for(resuming=1, detail="cluster warehouse is starting, about 40 s left")
    result, _ = run(server, holder, "--csv", "--no-header", "-c", "SELECT 1")
    assert result.returncode == 0, result.stderr
    assert ("The server is resuming (cluster warehouse is starting, about 40 s left); retrying in 1 s"
            in result.stderr), result.stderr


def test_quiet_waits_without_a_word(server, holder_for):
    holder = holder_for(resuming=2)
    result, _ = run(server, holder, "-q", "--csv", "--no-header", "-c", "SELECT 1")
    assert result.returncode == 0, result.stderr
    assert "resuming" not in result.stderr, result.stderr


@pytest.mark.parametrize("how", ["flag", "environment"])
def test_resume_timeout_zero_reports_the_first_answer(server, holder_for, how):
    holder = holder_for(resuming=-1)
    more = ("--resume-timeout", "0") if how == "flag" else ()
    extra = {"HARDCHARGER_RESUME_TIMEOUT": "0"} if how == "environment" else {}
    result, took = run(server, holder, *more, "-c", "SELECT 1", **extra)
    assert result.returncode != 0
    assert "the server is resuming; retry after 1 s" in result.stderr, result.stderr
    assert "retrying in" not in result.stderr, result.stderr
    assert len(holder.held) == 1, holder.held
    assert took < 5, took


def test_the_resume_timeout_gives_up_with_the_servers_error(server, holder_for):
    holder = holder_for(resuming=-1)
    result, took = run(server, holder, "--resume-timeout", "3", "-c", "SELECT 1")
    assert result.returncode != 0
    assert "still resuming after" in result.stderr, result.stderr
    assert 1.5 <= took < 10, took


def test_the_flag_wins_over_the_environment(server, holder_for):
    holder = holder_for(resuming=2)
    result, _ = run(server, holder, "--resume-timeout", "30", "--csv", "--no-header", "-c", "SELECT 7",
                    HARDCHARGER_RESUME_TIMEOUT="0")
    assert result.returncode == 0 and result.stdout.strip() == "7", result.stderr


def test_ctrl_c_stops_waiting(server, holder_for):
    """A statement held for a long Retry-After: Ctrl-C ends the wait within about a second,
    instead of after the 60 s the server asked for, and the statement never reaches it."""
    holder = holder_for(resuming=-1, retry_after="60", statements_only=True)
    process = subprocess.Popen(args=args(server, holder, "-c", "SELECT 99"), env=env(server),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **interruptible())
    try:
        deadline = time.monotonic() + 30
        while not holder.held:
            assert process.poll() is None and time.monotonic() < deadline, process.communicate(timeout=5)
            time.sleep(0.05)
        time.sleep(0.5)  # into the wait
        interrupted = time.monotonic()
        interrupt(process)
        stdout, stderr = process.communicate(timeout=15)
        assert time.monotonic() - interrupted < 5
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode != 0
    assert "99" not in stdout, stdout
    assert "cancelled" in stderr.lower(), stderr


@pytest.mark.pty
@pytest.mark.skipif(sys.platform == "win32", reason="pexpect needs a POSIX pty")
def test_a_terminal_shows_a_countdown(server, holder_for, tmp_path):
    holder = holder_for(resuming=1, retry_after="3")
    child = pexpect.spawn(command=str(CLIENT_BINARY), args=args(server, holder)[1:],
                          env={**env(server), "HOME": str(tmp_path)}, encoding="utf-8", dimensions=(40, 200),
                          timeout=30)
    try:
        for second in (3, 2, 1):
            child.expect(rf"The server is resuming: retrying in {second} s, 0:0\d so far\. Ctrl-C stops waiting\.")
        child.expect(r"> ")  # connected: the prompt
        child.send(".quit\r")
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)


def test_a_stale_none_after_begin_does_not_let_a_lost_statement_run_again(server, holder_for):
    """Servers before Chargewire 2.2.1 could answer a BEGIN before recording it, so a read of the
    session right after said "transaction": "none". The CLI must not trust that: here every
    session read says "none" and the second insert finds its session lost; the CLI refuses to run
    it again on a new session (where it would commit alone) and says the transaction was rolled
    back."""
    admin = server.admin()
    table = f"stale_{int(time.time() * 1000)}"
    admin.execute(sql=f"CREATE TABLE {table} (v INTEGER)")
    holder = holder_for(stale_transaction=True, lose_session_on=f"INSERT INTO {table} VALUES (2)")
    result, _ = run(server, holder, "--csv", "--no-header", "-c",
                    f"BEGIN;\nINSERT INTO {table} VALUES (1);\nINSERT INTO {table} VALUES (2);\nSELECT 'done';")
    assert "It was not run again; run the transaction again" in result.stderr, result.stderr
    assert admin.scalar(sql=f"SELECT count(*) FROM {table}") == 0, "nothing may commit alone"


def test_after_commit_a_lost_statement_runs_again(server, holder_for):
    """The twin: a transaction that was committed leaves nothing to lose, and the session (read
    after COMMIT, where a stale read could only lag toward "open") says so: the lost statement
    runs on a new session."""
    admin = server.admin()
    table = f"committed_{int(time.time() * 1000)}"
    admin.execute(sql=f"CREATE TABLE {table} (v INTEGER)")
    holder = holder_for(lose_session_on=f"INSERT INTO {table} VALUES (2)")
    result, _ = run(server, holder, "--csv", "--no-header", "-c",
                    f"BEGIN;\nINSERT INTO {table} VALUES (1);\nCOMMIT;\nINSERT INTO {table} VALUES (2);")
    assert result.returncode == 0, result.stderr
    assert "It was not run again" not in result.stderr, result.stderr
    assert sorted(r["v"] for r in admin.rows(sql=f"SELECT v FROM {table}")) == [1, 2]


@pytest.mark.parametrize("setting", ["USE other", "SET search_path = 'other'"])
def test_a_lost_session_after_use_or_set_does_not_run_the_statement_again(server, holder_for, setting):
    """A new session starts without what USE or SET set, so a statement run again there would
    write to another schema's table (here main.t instead of other.t) with no error. The CLI
    refuses, says why, and nothing is written to either table."""
    admin = server.admin()
    suffix = int(time.time() * 1000)
    other = f"other_{suffix}"
    admin.execute(sql=f"CREATE SCHEMA {other}")
    admin.execute(sql=f"CREATE TABLE {other}.t (v INTEGER)")
    admin.execute(sql=f"CREATE TABLE main.t_{suffix} (v INTEGER)")
    table = f"t_{suffix}" if setting.startswith("USE") else "t"
    # The same unqualified name in both schemas, so a run in the wrong one would succeed.
    admin.execute(sql=f"CREATE TABLE IF NOT EXISTS main.{table} (v INTEGER)")
    admin.execute(sql=f"CREATE TABLE IF NOT EXISTS {other}.{table} (v INTEGER)")
    holder = holder_for(lose_session_on=f"INSERT INTO {table} VALUES (2)")
    result, _ = run(server, holder, "--csv", "--no-header", "-c",
                    f"{setting.replace('other', other)};\nINSERT INTO {table} VALUES (2);")
    assert "what it was set to with USE or SET is gone with it" in result.stderr, result.stderr
    assert "It was not run again" in result.stderr, result.stderr
    assert admin.scalar(sql=f"SELECT count(*) FROM main.{table}") == 0, "written to the wrong schema"
    assert admin.scalar(sql=f"SELECT count(*) FROM {other}.{table}") == 0
