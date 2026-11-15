"""CLI connection-mode options against the feature-enabled server."""

import http.client
import http.server
import json
import pathlib
import re
import subprocess
import sys
import threading
import time

import pexpect
import pytest

from conftest import CLIENT_BINARY, TIMEOUT_SCALE, free_port
from test_client_cli import PROMPT, client_env, run

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")


@pytest.mark.parametrize("use_environment", [False, True])
def test_cli_reader_and_ordinary_writer(launcher, use_environment):
    server = launcher.start()
    args = [] if use_environment else ["--read-only"]
    env = client_env(server=server, **({"HARDCHARGER_CLIENT_READ_ONLY": "true"} if use_environment else {}))
    result = run(server, *args, "--command", "SELECT 42 AS answer", env=env)
    assert result.returncode == 0, result.stderr
    assert "42" in result.stdout
    result = run(server, *args, "--command", "CREATE TABLE cli_readonly_write (n INTEGER)", env=env)
    assert result.returncode != 0
    assert "read-only" in result.stderr.lower(), result.stderr
    result = run(server, "--command", "CREATE TABLE cli_permitted_write (n INTEGER)")
    assert result.returncode == 0, result.stderr
    assert server.admin().scalar(sql="SELECT count(*) FROM cli_permitted_write") == 0


def test_cli_reader_with_session_tags(launcher):
    server = launcher.start()
    result = run(server, "--read-only", "--session-tag", '{"app":"reader"}',
                 "--query-tag", '{"purpose":"test"}', "--command", "SELECT 42")
    assert result.returncode == 0, result.stderr
    assert "42" in result.stdout


# The environment variable's spellings, and the flag's precedence over it: only the lower-case
# `true` path was tested. A value that is neither true nor false fails closed (no session at
# all), so a typo never yields a session that writes when a reader was asked for.
@pytest.mark.parametrize("value,outcome", [
    ("TRUE", "read-only"), ("On", "read-only"), ("1", "read-only"),
    ("", "read-write"), ("0", "read-write"), ("No", "read-write"), ("false", "read-write"),
    (" true", "refused"), ("true ", "refused"), ("2", "refused"), ("enabled", "refused"),
])
def test_the_environment_variable_is_read_as_a_boolean_or_refused(launcher, value, outcome):
    server = launcher.start()
    table = "cli_env_write"
    env = client_env(server=server, HARDCHARGER_CLIENT_READ_ONLY=value)
    result = run(server, "--command", f"CREATE TABLE {table} (n INTEGER)", env=env)
    made = server.admin().scalar(sql=f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{table}'")
    if outcome == "read-write":
        assert result.returncode == 0 and made == 1, result.stderr
    elif outcome == "read-only":
        assert result.returncode != 0 and made == 0, result.stderr
        assert "read-only" in result.stderr.lower(), result.stderr
        assert run(server, "--command", "SELECT 42 AS answer", env=env).returncode == 0  # the twin: it reads
    else:
        assert result.returncode != 0 and made == 0, result.stderr
        assert "true or false" in result.stderr.lower(), result.stderr
        assert run(server, "--command", "SELECT 42 AS answer", env=env).returncode != 0  # no session of either kind


@pytest.mark.parametrize("flag", ["--read-only=false", "--no-read-only"])
def test_the_flag_takes_precedence_over_the_environment(launcher, flag):
    server = launcher.start()
    env = client_env(server=server, HARDCHARGER_CLIENT_READ_ONLY="true")
    result = run(server, flag, "--command", "CREATE TABLE cli_flag_write (n INTEGER)", env=env)
    assert result.returncode == 0, result.stderr
    assert server.admin().scalar(sql="SELECT count(*) FROM cli_flag_write") == 0
    result = run(server, "--read-only", "--command", "CREATE TABLE cli_flag_refused (n INTEGER)",
                 env=client_env(server=server, HARDCHARGER_CLIENT_READ_ONLY="false"))
    assert result.returncode != 0 and "read-only" in result.stderr.lower(), result.stderr


# ---- The session the CLI works in is one the server confirmed read-only, wherever it comes from ----


class Recorder:
    """A stand-in in front of a real server (127.0.0.1:`upstream_port`) that passes every request
    through and records them: what each session creation asked for and what the server answered,
    and which session each statement went to. With `older`, it answers as a server from before
    read-only sessions (2.12 and earlier) does: no read_only_sessions capability, no "read_only"
    among a session's options, and for a create that asks for one either "ignores" (the session is
    created and its answer just lacks the option, as a server that ignores unknown options does)
    or "refuses" (400 unknown_option, which Hardcharger 2.12's DuckDB sessions answered)."""

    def __init__(self, upstream_port: int, older: str | None = None):
        self.upstream_port = upstream_port
        self.older = older
        self.created: list[tuple[dict, int, dict]] = []  # (options asked for, status, answer)
        self.statements: list[str] = []  # the session id of each statement submitted
        self.calls: list[tuple[str, str]] = []  # (method, path)
        self.lock = threading.Lock()
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # quiet
                pass

            def reply(self, status: int, headers: list[tuple[str, str]], data: bytes) -> None:
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                parts = path.strip("/").split("/")
                creating = self.command == "POST" and path == "/chargewire/v1/sessions"
                asked = (json.loads(body or b"{}").get("options") or {}) if creating else {}
                with recorder.lock:
                    recorder.calls.append((self.command, path))
                    if self.command == "POST" and len(parts) == 5 and parts[4] == "statements":
                        recorder.statements.append(parts[3])
                if creating and "read_only" in asked and recorder.older == "refuses":
                    # As Hardcharger 2.12 answered an option its sessions don't have.
                    doc = {"type": "urn:chargewire:error:unknown_option", "title": "Unknown session option",
                           "status": 400, "code": "unknown_option", "detail": "read_only: unknown session option"}
                    with recorder.lock:
                        recorder.created.append((asked, 400, doc))
                    self.reply(status=400, headers=[("Content-Type", "application/problem+json")],
                               data=json.dumps(doc).encode())
                    return
                if creating and recorder.older:
                    # The option never reaches the engine, so the session made is an ordinary one.
                    body = json.dumps({"options": {k: v for k, v in asked.items() if k != "read_only"}}).encode()
                upstream = http.client.HTTPConnection(host="127.0.0.1", port=recorder.upstream_port,
                                                      timeout=60 * TIMEOUT_SCALE)
                try:
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ("host", "connection", "content-length")}
                    upstream.request(method=self.command, url=self.path, body=body, headers=headers)
                    response = upstream.getresponse()
                    data = response.read()
                    answer_headers = response.getheaders()
                    status = response.status
                finally:
                    upstream.close()
                is_json = any(k.lower() == "content-type" and "json" in v for k, v in answer_headers)
                if recorder.older and status in (200, 201) and is_json and data:
                    doc = json.loads(data)
                    if path in ("/chargewire", "/chargewire/"):
                        doc["capabilities"] = [c for c in doc.get("capabilities", []) if c != "read_only_sessions"]
                    if isinstance(doc.get("options"), dict):
                        doc["options"].pop("read_only", None)
                    data = json.dumps(doc).encode()
                if creating:
                    with recorder.lock:
                        recorder.created.append((asked, status, json.loads(data) if data else {}))
                self.reply(status=status, headers=answer_headers, data=data)

            do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any

        self.server = http.server.ThreadingHTTPServer(server_address=("127.0.0.1", 0), RequestHandlerClass=Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def confirmed_read_only(self) -> set[str]:
        """The sessions that were asked to be read-only and that the server's answer showed so."""
        with self.lock:
            return {answer["session_id"] for asked, status, answer in self.created
                    if asked.get("read_only") is True and status in (200, 201)
                    and answer.get("options", {}).get("read_only") is True}

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def recorder_for():
    recorders = []

    def make(server, **kwargs) -> Recorder:
        recorder = Recorder(upstream_port=server.port, **kwargs)
        recorders.append(recorder)
        return recorder

    yield make
    for recorder in recorders:
        recorder.stop()


def client_args(port: int, username: str) -> list[str]:
    """The CLI's arguments for a server (or a stand-in) at 127.0.0.1:`port`, over plain HTTP."""
    return [str(CLIENT_BINARY), "--host", "127.0.0.1", "--port", str(port), "--tls=false", "--username", username]


def admin_scalar(server, sql: str):
    """`sql`'s one value, from an administrator's session that is closed again (a test counts the
    sessions left open)."""
    admin = server.admin()
    try:
        return admin.scalar(sql=sql)
    finally:
        admin.close()


def table_exists(server, table: str) -> bool:
    return admin_scalar(server=server, sql=f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{table}'") == 1


def wait_for_file(path: pathlib.Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30 * TIMEOUT_SCALE
    while not path.exists():
        assert process.poll() is None and time.monotonic() < deadline, process.communicate(timeout=5)
        time.sleep(0.05)


READ_ONLY_REFUSAL = "Error: CREATE is not allowed in a read-only session\n"
# What `hardcharger --read-only` says to a server from before read-only sessions: one that
# ignores the option (the SDK's own check of the answer), and one that refuses it (the server's
# detail, as a real 2.12.1 server answers: 400 unknown_option "read_only: unknown session option").
REFUSALS = {
    "ignores": ('Error: the server did not confirm that the session is read-only (its answer has no "read_only": '
                "true among the session's options): it has no read-only sessions, and the session was closed\n"),
    "refuses": "Error: read_only: unknown session option\n",
}


@pytest.mark.parametrize("how", ["killed", "restarted"])
@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "twin"])
def test_a_replaced_session_is_read_only_too(launcher, recorder_for, tmp_path, how, read_only):
    """The server loses the CLI's session between two statements: an administrator KILLs it
    (session_ended) or the server restarts (session_not_found). The CLI opens a new session, and
    with --read-only asks for a read-only one and runs nothing until the server has confirmed it:
    the write after the replacement is refused with the read-only error and nothing is written.
    The twin, the same flow without --read-only, writes."""
    port = free_port()
    args = ["--database", str(tmp_path / "replaced.duckdb"), "--allow-cross-instance-tokens"]
    first = launcher.start(args=args, port=port, username="testuser")
    recorder = recorder_for(first)
    ready, go = tmp_path / "ready", tmp_path / "go"
    waiter = tmp_path / "wait.py"
    waiter.write_text("import pathlib, sys, time\n"
                      f"pathlib.Path({str(ready)!r}).touch()\n"
                      f"for _ in range({int(600 * TIMEOUT_SCALE)}):\n"
                      f"    if pathlib.Path({str(go)!r}).exists(): sys.exit(0)\n"
                      "    time.sleep(0.1)\n"
                      "sys.exit(1)\n")
    script = ("SELECT 'before';\n"
              f'.shell "{sys.executable}" -I "{waiter}"\n'
              "SELECT 'found it gone';\n"
              "CREATE TABLE replaced_write (n INTEGER);\n"
              "SELECT 'after the write';\n")
    client = subprocess.Popen(
        args=[*client_args(port=recorder.port, username="testuser"), *(["--read-only"] if read_only else []),
              "--csv", "--no-header", "--command", script],
        env=client_env(server=first), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_for_file(path=ready, process=client)
        assert len(recorder.created) == 1, recorder.created
        original = recorder.created[0][2]["session_id"]
        if how == "killed":
            first.admin().execute(sql=f"KILL SESSION '{original}'")
            now = first
        else:
            first.stop()
            now = launcher.start(args=args, port=port, username="testuser", password=first.password,
                                 secret_key=first.secret_key)
        go.touch()
        stdout, stderr = client.communicate(timeout=60 * TIMEOUT_SCALE)
    finally:
        if client.poll() is None:
            client.kill()
    # The replacement, and the warning that says so.
    assert "a new session is open" in stderr, stderr
    assert len(recorder.created) == 2, recorder.created
    replacement = recorder.created[1][2]["session_id"]
    assert replacement != original
    # A lost session's statement runs again in the new one; a killed one's is reported, not run.
    # The write's own output is OK, when it runs.
    assert stdout == ("before\n" + ("found it gone\n" if how == "restarted" else "")
                      + ("" if read_only else "OK\n") + "after the write\n"), (stdout, stderr)
    # A statement failed: the refused write, or the one the kill caught.
    assert client.returncode == (1 if read_only or how == "killed" else 0), stderr
    if read_only:
        # Both sessions were asked for read-only and confirmed so before any statement went to them.
        assert [asked for asked, _, _ in recorder.created] == [{"read_only": True}] * 2, recorder.created
        assert recorder.confirmed_read_only() == {original, replacement}, recorder.created
        assert set(recorder.statements) <= recorder.confirmed_read_only(), (recorder.statements, recorder.created)
        assert recorder.statements.count(replacement) >= 2, recorder.statements  # the write ran there
        assert READ_ONLY_REFUSAL in stderr, stderr
        assert not table_exists(server=now, table="replaced_write"), stderr
    else:
        assert all("read_only" not in asked for asked, _, _ in recorder.created), recorder.created
        assert "read-only" not in stderr, stderr
        assert table_exists(server=now, table="replaced_write"), stderr


@pytest.mark.parametrize("shell", [
    "script",
    pytest.param("terminal", marks=[pytest.mark.pty,
                                    pytest.mark.skipif(sys.platform == "win32", reason="pexpect needs a POSIX pty")]),
])
@pytest.mark.parametrize("target", ["other", "same"])
@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "twin"])
def test_connect_from_a_read_only_shell_stays_read_only(launcher, recorder_for, tmp_path, shell, target, read_only):
    """`.connect` from a shell started with --read-only, to another server or to the same one
    again, opens a session the server there confirmed read-only: a write on it is refused with
    the read-only error and nothing is written. The twin, a shell without --read-only, writes."""
    first = launcher.start(username="testuser")
    second = launcher.start(username="other") if target == "other" else first
    started = recorder_for(first)
    connected = recorder_for(second) if target == "other" else started  # the same address again
    table = f"connected_write_{target}"
    connect = f".connect 127.0.0.1 {connected.port} {second.username} {second.password} --no-tls"
    args = [*client_args(port=started.port, username="testuser"), *(["--read-only"] if read_only else [])]
    env = client_env(server=first, home=tmp_path)
    if shell == "script":
        result = subprocess.run(args=[*args, "--csv", "--no-header"], env=env, capture_output=True, text=True,
                                timeout=120 * TIMEOUT_SCALE,
                                input=f"SELECT 'before';\n{connect}\nCREATE TABLE {table} (n INTEGER);\n"
                                      "SELECT 'after the write';\n")
        assert f"Connected to 127.0.0.1:{connected.port}" in result.stdout, (result.stdout, result.stderr)
        assert result.stdout.endswith(("" if read_only else "OK\n") + "after the write\n"), result.stdout
        assert result.returncode == (1 if read_only else 0), result.stderr
        assert (READ_ONLY_REFUSAL in result.stderr) == read_only, result.stderr
    else:
        child = pexpect.spawn(command=args[0], args=args[1:], env=env, encoding="utf-8", dimensions=(40, 200),
                              timeout=30 * TIMEOUT_SCALE)
        try:
            child.expect(PROMPT)
            child.send(connect + "\r")
            child.expect(f"Connected to 127.0.0.1:{connected.port}")
            child.expect(PROMPT)
            child.send(f"CREATE TABLE {table} (n INTEGER);\r")
            # The refusal, or the write's OK, then the prompt again.
            shown = child.expect([re.escape(READ_ONLY_REFUSAL.strip()), r"\bOK\b"])
            assert shown == (0 if read_only else 1), child.before
            child.expect(PROMPT)
            child.send(".quit\r")
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)
    sessions = [answer.get("session_id") for _, _, answer in connected.created]
    if target == "same":
        sessions = sessions[1:]  # the first is the shell's own, from the start
    assert len(sessions) == 1, connected.created
    if read_only:
        assert sessions[0] in connected.confirmed_read_only(), connected.created
        # Every statement on the server .connect reached went to a session confirmed read-only.
        assert set(connected.statements) <= connected.confirmed_read_only(), (connected.statements, connected.created)
        assert sessions[0] in connected.statements, connected.statements
        assert not table_exists(server=second, table=table)
    else:
        assert all("read_only" not in asked for asked, _, _ in connected.created), connected.created
        assert table_exists(server=second, table=table)


@pytest.mark.parametrize("older", ["ignores", "refuses"])
@pytest.mark.parametrize("use_environment", [False, True], ids=["flag", "environment"])
def test_a_server_without_read_only_sessions_is_refused(launcher, recorder_for, older, use_environment):
    """A server from before read-only sessions (a stand-in for one, in front of a real server)
    either ignores the read_only option, so its answer has no "read_only": true among the
    session's options, or refuses it as an unknown option. Either way `hardcharger --read-only`
    refuses to connect, says why, and never sends the statement; a session the server made
    anyway is closed. The twin, the same server without --read-only, runs the statement."""
    server = launcher.start(username="testuser")
    older_server = recorder_for(server, older=older)
    table = f"never_written_{older}"
    args = client_args(port=older_server.port, username="testuser")
    env = client_env(server=server, **({"HARDCHARGER_CLIENT_READ_ONLY": "true"} if use_environment else {}))
    result = subprocess.run(args=[*args, *([] if use_environment else ["--read-only"]),
                                  "--command", f"CREATE TABLE {table} (n INTEGER)"],
                            env=env, capture_output=True, text=True, timeout=120 * TIMEOUT_SCALE)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == "", result.stdout
    assert result.stderr == REFUSALS[older], result.stderr
    assert older_server.statements == [], older_server.calls  # no statement was sent
    assert [asked for asked, _, _ in older_server.created] == [{"read_only": True}], older_server.created
    if older == "ignores":
        made = older_server.created[0][2]["session_id"]
        assert ("DELETE", f"/chargewire/v1/sessions/{made}") in older_server.calls, older_server.calls
    assert not table_exists(server=server, table=table)
    # Nothing of the CLI's is left open on the server.
    assert admin_scalar(server=server, sql="SELECT count(*) FROM hardcharger_sessions() WHERE NOT is_current") == 0

    # The twin: the same server, without --read-only.
    twin = subprocess.run(args=[*args, "--command", f"CREATE TABLE {table} (n INTEGER)"],
                          env=client_env(server=server), capture_output=True, text=True, timeout=120 * TIMEOUT_SCALE)
    assert twin.returncode == 0, twin.stderr
    assert len(older_server.statements) == 1, older_server.calls
    assert table_exists(server=server, table=table)
