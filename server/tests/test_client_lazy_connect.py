"""The CLI never starts a stopped server for nobody (Chargewire §3.1, §11.2 "Lazy connect").

A stopped GizmoData Cloud cluster with auto-resume has a small holder at its address, and a
sign-in there starts the cluster. The holder's discovery document says "state": "stopped".
The CLI that sees it connects without signing in and without opening a session: its banner,
prompt, completion and .show send nothing more, and the first statement signs in, runs the
CLI's own SETs (--session-tag, --query-tag), then runs. A wrong password is reported by that
first statement. Exiting, and Ctrl-C, never sign in, even late in a token's life (the client
renews a sign-in shortly before its token expires, on the next request that does work).

The holder here (resuming_holder.Holder) passes everything through to a real server, says
"stopped" until the first sign-in, and records every request.
"""

import json
import subprocess
import sys
import time

import pexpect
import pytest

from conftest import CLIENT_BINARY, clean_environment, interrupt, interruptible
from resuming_holder import Holder

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

DISCOVERY = {("GET", "/chargewire"), ("GET", "/chargewire/")}
SIGN_IN = ("POST", "/chargewire/v1/auth/token")
SESSIONS = ("POST", "/chargewire/v1/sessions")
SESSION_TAG = '{"lazy":"session"}'
QUERY_TAG = '{"lazy":"query"}'
PROMPT = r"hardcharger> "  # before the server is asked where the session is
DYNAMIC_PROMPT = r"memory\.main(\x1b\[[0-9;]*m)*> "
ANY_PROMPT = rf"({PROMPT}|{DYNAMIC_PROMPT})"
LONG_AGGREGATE = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b"


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


def env(server, password: str | None = None) -> dict[str, str]:
    return {**clean_environment(), "HARDCHARGER_PASSWORD": server.password if password is None else password}


def spawn(server, holder: Holder, tmp_path, *more: str, password: str | None = None) -> pexpect.spawn:
    return pexpect.spawn(command=str(CLIENT_BINARY), args=args(server, holder, *more)[1:],
                         env={**env(server, password=password), "HOME": str(tmp_path)}, encoding="utf-8",
                         dimensions=(40, 200), timeout=30)


def statements(holder: Holder) -> list[str]:
    """The SQL of every statement submitted, in order."""
    out = []
    with holder.lock:
        for method, path, body, _ in holder.requests:
            if method == "POST" and path.endswith("/statements") and body:
                out.append(json.loads(body)["sql"])
    return out


@pytest.mark.pty
@pytest.mark.skipif(sys.platform == "win32", reason="pexpect needs a POSIX pty")
def test_a_stopped_server_gets_only_discovery_until_the_first_statement(server, holder_for, tmp_path):
    """Starting the CLI with tags configured, the banner, the prompt, tab completion and .show:
    only discovery. Then the first statement signs in, opens the session, sets the tags and runs
    (after the server_resuming wait the sign-in meets), and the prompt shows where it is."""
    holder = holder_for(stopped=True)
    child = spawn(server, holder, tmp_path, "--session-tag", SESSION_TAG, "--query-tag", QUERY_TAG)
    try:
        child.expect(f"Connected to 127.0.0.1:{holder.port}")
        child.expect(ANY_PROMPT)
        time.sleep(0.5)
        assert set(holder.calls()) <= DISCOVERY, holder.calls()  # no sign-in, no session, no prompt query
        assert "The server is stopped; it starts when you run the first statement." in child.before
        assert child.after.startswith(PROMPT), child.after
        child.send("SEL\t")  # completion: keywords only, no catalog fetch
        time.sleep(0.5)
        child.send("\x15")  # clear the line
        child.send(".show\r")
        child.expect(r"state: stopped \(it starts when you run the first statement\)")
        child.expect(PROMPT)
        time.sleep(0.5)
        assert set(holder.calls()) <= DISCOVERY, holder.calls()

        # The server starts now: hold the sign-in for two answers, as a holder does while it starts.
        with holder.lock:
            holder.resuming = 2
        child.send("SELECT 6 * 7 AS answer;\r")
        child.expect("The server is resuming")
        child.expect("42")
        child.expect(DYNAMIC_PROMPT)
        child.send(".quit\r")
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)

    work = [call for call in holder.calls() if call not in DISCOVERY]
    assert work[0] == SIGN_IN and SESSIONS in work, work
    assert work.index(SIGN_IN) < work.index(SESSIONS), work
    sql = statements(holder)
    assert sql[0] == f"SET hardcharger.session_tag = '{SESSION_TAG}'", sql
    assert sql[1] == f"SET hardcharger.query_tag = '{QUERY_TAG}'", sql
    assert sql[2] == "SELECT 6 * 7 AS answer", sql
    # The CLI names itself and its version.
    agents = {agent for _, _, _, agent in holder.requests}
    assert len(agents) == 1 and next(iter(agents)).startswith("hardcharger-client/"), agents


def test_a_script_against_a_stopped_server_signs_in_at_its_first_statement(server, holder_for):
    """-c: the sign-in, the session, the tag (which the server took: no warning), the statement."""
    holder = holder_for(stopped=True)
    result = subprocess.run(args=args(server, holder, "--session-tag", SESSION_TAG, "--csv", "--no-header", "-c",
                                      "SELECT 11 AS eleven"),
                            env=env(server), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "11", result.stdout
    assert "Warning" not in result.stderr, result.stderr
    assert statements(holder) == [f"SET hardcharger.session_tag = '{SESSION_TAG}'", "SELECT 11 AS eleven"]
    work = [call for call in holder.calls() if call not in DISCOVERY]
    assert work[:2] == [SIGN_IN, SESSIONS], work


def test_a_lost_session_gets_the_tags_again_before_the_statement_runs_again(server, holder_for):
    """After a lazy connect the client still knows the server as stopped, so the replacement for
    a lost session opens at the statement that runs again: the tags go first, then the statement."""
    holder = holder_for(stopped=True, lose_session_on="SELECT 22")
    result = subprocess.run(args=args(server, holder, "--session-tag", SESSION_TAG, "--csv", "--no-header", "-c",
                                      "SELECT 21;\nSELECT 22;"),
                            env=env(server), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["21", "22"], result.stdout
    tag = f"SET hardcharger.session_tag = '{SESSION_TAG}'"
    assert statements(holder) == [tag, "SELECT 21", "SELECT 22", tag, "SELECT 22"], statements(holder)
    assert holder.calls().count(SESSIONS) == 2, holder.calls()


@pytest.mark.pty
@pytest.mark.skipif(sys.platform == "win32", reason="pexpect needs a POSIX pty")
def test_a_wrong_password_is_reported_by_the_first_statement(server, holder_for, tmp_path):
    """Connecting can't know the password is wrong (it sends no sign-in): the first statement
    says so, as connecting would have, the SETs don't turn it into a warning, and the next
    statement tries again."""
    holder = holder_for(stopped=True)
    child = spawn(server, holder, tmp_path, "--session-tag", SESSION_TAG, password="not-the-password")
    try:
        refused_at_connect = child.expect([PROMPT, "Error: Authentication failed"])
        assert refused_at_connect == 0, "connecting signed in: " + repr(holder.calls())
        assert set(holder.calls()) <= DISCOVERY, holder.calls()
        child.send("SELECT 1;\r")
        child.expect(rf"Authentication failed for http://127\.0\.0\.1:{holder.port}: invalid_credentials")
        child.expect(PROMPT)
        child.send("SELECT 2;\r")
        child.expect("Authentication failed for")
        child.expect(PROMPT)
        child.send(".quit\r")
        child.expect(pexpect.EOF)
        output = child.before or ""
    finally:
        child.close(force=True)
    assert "Warning: failed to set" not in output
    assert holder.sign_ins() == 2, holder.calls()
    assert SESSIONS not in holder.calls(), holder.calls()


def test_a_wrong_password_fails_a_script_at_its_first_statement(server, holder_for):
    holder = holder_for(stopped=True)
    result = subprocess.run(args=args(server, holder, "-c", "SELECT 1"), env=env(server, password="wrong"),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert f"Authentication failed for http://127.0.0.1:{holder.port}: invalid_credentials" in result.stderr, \
        result.stderr


@pytest.mark.pty
@pytest.mark.skipif(sys.platform == "win32", reason="pexpect needs a POSIX pty")
def test_a_serving_server_is_signed_in_at_connect(server, holder_for, tmp_path):
    """The twin: without "state": "stopped", connecting signs in, opens the session, sets the
    tags and reads the prompt's catalog and schema, before any statement, as always."""
    holder = holder_for()
    child = spawn(server, holder, tmp_path, "--session-tag", SESSION_TAG)
    try:
        child.expect(f"Connected to 127.0.0.1:{holder.port}")
        child.expect(DYNAMIC_PROMPT)
        before = holder.calls()
        child.send(".quit\r")
        child.expect(pexpect.EOF)
        output = child.before or ""
    finally:
        child.close(force=True)
    assert "The server is stopped" not in output
    assert SIGN_IN in before and SESSIONS in before, before
    assert statements(holder)[0] == f"SET hardcharger.session_tag = '{SESSION_TAG}'"


def test_exit_late_in_a_tokens_life_does_not_sign_in(server, holder_for):
    """A token that expires in 61 s is one the client renews after a second, on its next request
    that does work. Exiting then (closing the session) must not: a sign-in at a stopped server's
    holder starts it again for nobody."""
    holder = holder_for(expires_in=61)
    process = subprocess.Popen(args=args(server, holder, "--csv", "--no-header"), env=env(server),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write("SELECT 5 AS five;\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "5"
        time.sleep(2)  # the token is now within a minute of expiring
        process.stdin.close()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, process.stderr.read()
    assert any(method == "DELETE" and path.startswith("/chargewire/v1/sessions/")
               for method, path in holder.calls()), holder.calls()  # the session was closed
    assert holder.sign_ins() == 1, holder.calls()


def test_ctrl_c_late_in_a_tokens_life_does_not_sign_in(server, holder_for):
    """The cancel Ctrl-C sends, and the close after it, carry the token the client has."""
    holder = holder_for(expires_in=61)
    process = subprocess.Popen(args=args(server, holder, "-c", LONG_AGGREGATE), env=env(server),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **interruptible())
    try:
        deadline = time.monotonic() + 30
        while not any(path.endswith("/statements") for method, path in holder.calls() if method == "POST"):
            assert process.poll() is None and time.monotonic() < deadline, process.communicate(timeout=5)
            time.sleep(0.05)
        time.sleep(2)  # the token is now within a minute of expiring
        interrupt(process)
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert "cancelled" in stderr.lower(), stderr
    assert any(path.endswith("/cancel") for _, path in holder.calls()), holder.calls()
    assert holder.sign_ins() == 1, holder.calls()
