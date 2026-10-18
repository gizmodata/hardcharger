"""Port of GizmoSQL 1.x tests/integration/test_client_cli.cpp (18 cases), plus the client's
interactive and connection behaviour.

The 1.x cases run `gizmosql_client` as a subprocess against a server; here they run
`hardcharger` (HARDCHARGER_TEST_CLIENT_BINARY) against a real hardcharger-server, with the
same arguments in Hardcharger's names (GIZMOSQL_PASSWORD is HARDCHARGER_PASSWORD). All 18
apply and keep their 1.x names (doc_quick_start_single_query is DocQuickStartSingleQuery).

Added (not in 1.x): the shell at a real terminal (pexpect, a pty): the banner and the
dynamic prompt, multi-line statements, history, tab completion, the pager, the password
prompt, Ctrl-C cancelling a running statement through Warp SQL's cancel while the shell
goes on; Ctrl-C in a script; .once and .read; option errors; TLS (verification, the host
name, roots), mutual TLS (with --tls-skip-verify too, which 1.x refused), and the
server's browser sign-in (--auth-type external) with a mock identity provider. Every
negative case asserts its error text and exit code, next to its positive twin.
"""

import datetime
import ipaddress
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

import pexpect
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from conftest import BASH, CLIENT_BINARY, TIMEOUT_SCALE, clean_environment, free_port, home_env, interrupt, interruptible

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

INIT_SQL = (
    "CREATE TABLE employees (id INTEGER, name VARCHAR, dept VARCHAR, salary DOUBLE);"
    "INSERT INTO employees VALUES (1, 'Alice', 'Engineering', 120000);"
    "INSERT INTO employees VALUES (2, 'Bob', 'Marketing', 85000);"
    "INSERT INTO employees VALUES (3, 'Charlie', 'Engineering', 110000);"
    "INSERT INTO employees VALUES (4, 'Dave', 'Sales', 95000);"
    "INSERT INTO employees VALUES (5, 'Eve', 'Engineering', 130000);"
    "CREATE TABLE region (r_regionkey INTEGER, r_name VARCHAR, r_comment VARCHAR);"
    "INSERT INTO region VALUES (0, 'AFRICA', 'comment0');"
    "INSERT INTO region VALUES (1, 'AMERICA', 'comment1');"
    "INSERT INTO region VALUES (2, 'ASIA', 'comment2');"
    "INSERT INTO region VALUES (3, 'EUROPE', 'comment3');"
    "INSERT INTO region VALUES (4, 'MIDDLE EAST', 'comment4');"
)
SLOW_SQL = "SELECT count(*) FROM range(100000000000) a(i) WHERE i % 7 = 3"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def binary() -> str:
    if not CLIENT_BINARY.exists():
        pytest.fail(reason=f"{CLIENT_BINARY} is missing: build the server first")
    return str(CLIENT_BINARY)


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--init-sql-commands", INIT_SQL], username="testuser")


@pytest.fixture
def home(tmp_path):
    """A HOME of the test's own (history, init file, exported results)."""
    path = tmp_path / "home"
    path.mkdir()
    return path


def client_env(server=None, home: pathlib.Path | None = None, **extra: str) -> dict[str, str]:
    """A clean environment (no HARDCHARGER_* from this shell) with the server's password."""
    env = clean_environment()
    if home is not None:
        env.update(home_env(home=home))
    if server is not None:
        env["HARDCHARGER_PASSWORD"] = server.password
    env.update(extra)
    return env


def conn_args(server) -> list[str]:
    return ["--host", "localhost", "--port", str(server.port), "--username", server.username]


def run(server, *args: str, env: dict[str, str] | None = None, input: str | None = None,
        timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args=[binary(), *conn_args(server), *args], env=env or client_env(server=server),
                          input=input, capture_output=True, text=True, timeout=timeout)


def run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args=[BASH, "-c", script], env=env, capture_output=True, text=True, timeout=120)


# ---- 1.x: test_client_cli.cpp ----


def test_doc_quick_start_single_query(server):
    result = run(server, "--command", "SELECT * FROM employees ORDER BY id")
    assert result.returncode == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "Alice" in output and "Bob" in output
    assert "5 rows" in output, output


def test_repeated_exit_closes_session_without_crashing(server):
    for attempt in range(30):
        result = run(server, "--command", "SELECT 42", "--csv", "--no-header")
        assert result.returncode == 0, (attempt, result.stderr)
        assert result.stdout.strip() == "42", (attempt, result.stdout)


def test_doc_output_mode_csv(server):
    result = run(server, "--csv", "--command", "SELECT id, name FROM employees ORDER BY id LIMIT 2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["id,name", "1,Alice", "2,Bob"], result.stdout


def test_doc_output_mode_csv_no_header(server):
    result = run(server, "--csv", "--no-header", "--command", "SELECT name FROM employees ORDER BY id LIMIT 1")
    assert result.returncode == 0, result.stderr
    assert "name" not in result.stdout
    assert result.stdout.splitlines() == ["Alice"], result.stdout


def test_doc_output_mode_json(server):
    result = run(server, "--json", "--command", "SELECT name, salary FROM employees ORDER BY id LIMIT 2")
    assert result.returncode == 0, result.stderr
    import json
    rows = json.loads(result.stdout)
    assert rows == [{"name": "Alice", "salary": 120000.0}, {"name": "Bob", "salary": 85000.0}], result.stdout


def test_doc_output_mode_markdown(server):
    result = run(server, "--markdown", "--command", "SELECT name, dept FROM employees ORDER BY id LIMIT 3")
    assert result.returncode == 0, result.stderr
    assert "|" in result.stdout and "---" in result.stdout
    assert "Alice" in result.stdout and "Engineering" in result.stdout


def test_doc_output_mode_box(server):
    result = run(server, "--command", "SELECT 42 AS answer")
    assert result.returncode == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "┌" in output, output
    assert "42" in output and "answer" in output


def test_doc_dot_command_tables(server):
    result = run(server, "--command", ".tables --flat")
    assert result.returncode == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert "employees" in output and "region" in output, output


def test_doc_dot_command_describe(server):
    result = run(server, "--command", ".describe employees")
    assert result.returncode == 0, result.stderr
    output = strip_ansi(result.stdout)
    for column in ("id", "name", "salary"):
        assert column in output, output


def test_doc_pipe_from_stdin(server):
    script = (f"echo 'SELECT 42 AS answer;' | {shlex.quote(binary())} "
              f"{shlex.join(conn_args(server))} --quiet")
    result = run_bash(script=script, env=client_env(server=server))
    assert result.returncode == 0, result.stderr
    assert "42" in strip_ansi(result.stdout), result.stdout


def test_doc_file_mode(server, tmp_path):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("SELECT COUNT(*) AS total FROM employees;\n")
    result = run(server, "--quiet", "--csv", "--no-header", "--file", str(sql_file))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5", result.stdout


def test_doc_output_to_file(server, tmp_path):
    out_file = tmp_path / "output.csv"
    result = run(server, "--csv", "--no-header", "--output", str(out_file),
                 "--command", "SELECT id, name FROM employees ORDER BY id")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert out_file.read_text().splitlines()[0] == "1,Alice"


def test_doc_multiple_statements(server):
    result = run(server, "--command", "CREATE TABLE doc_test (x INTEGER); "
                                      "INSERT INTO doc_test VALUES (42); SELECT * FROM doc_test;")
    assert result.returncode == 0, result.stderr
    output = strip_ansi(result.stdout)
    assert output.startswith("OK\nOK, 1 rows affected\n"), output
    assert "42" in output
    assert run(server, "--command", "DROP TABLE IF EXISTS doc_test").returncode == 0


def test_doc_quiet_mode(server):
    result = run(server, "--quiet", "--command", "SELECT 1 AS x")
    assert result.returncode == 0, result.stderr
    assert "Hardcharger Client" not in strip_ansi(result.stdout)


def test_doc_error_handling(server):
    result = run(server, "--bail", "--command", "SELECT * FROM nonexistent_table_xyz")
    assert result.returncode == 1, result.stderr
    stderr = strip_ansi(result.stderr)
    assert stderr.startswith("Error: Catalog Error: Table with name nonexistent_table_xyz does not exist"), stderr
    # The positive twin.
    assert run(server, "--bail", "--command", "SELECT 1").returncode == 0


def test_feature_last_result_underscore(server):
    result = run(server, "--csv", "--no-header", "--command",
                 "SELECT * FROM region WHERE r_regionkey < 2; SELECT count(*) FROM _")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == "2", result.stdout
    assert lines[:2] == ["0,AFRICA,comment0", "1,AMERICA,comment1"], result.stdout


def test_doc_heredoc_style(server):
    script = (f"{shlex.quote(binary())} {shlex.join(conn_args(server))} --quiet --csv --no-header <<'SQLEOF'\n"
              "SELECT r_name FROM region WHERE r_regionkey = 2;\n"
              "SQLEOF\n")
    result = run_bash(script=script, env=client_env(server=server))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ASIA", result.stdout


def test_connect_without_username_fails(server):
    result = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(server.port), "--command", "SELECT 1"],
                            env=client_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout
    stderr = strip_ansi(result.stderr)
    assert "authentication required" in stderr, stderr
    assert "Connected to" not in strip_ansi(result.stdout)
    # The positive twin: with a user name it connects.
    assert run(server, "--command", "SELECT 1").returncode == 0


# ---- Added: scripting ----


def test_unknown_option_is_an_error():
    result = subprocess.run(args=[binary(), "--no-such-flag"], env=client_env(), capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("Error: unrecognised option '--no-such-flag'\n"), result.stderr
    version = subprocess.run(args=[binary(), "--version"], env=client_env(), capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.startswith("Hardcharger Client "), version.stdout


def test_bad_port_value_is_an_error():
    result = subprocess.run(args=[binary(), "--port", "abc"], env=client_env(), capture_output=True, text=True)
    assert result.returncode == 1
    assert "the argument ('abc') for option '--port' is invalid" in result.stderr, result.stderr


def test_uri_cannot_be_combined_with_connection_flags(server):
    uri = f"hardcharger://localhost:{server.port}?username={server.username}"
    result = subprocess.run(args=[binary(), uri, "--host", "localhost", "-c", "SELECT 1"],
                            env=client_env(server=server), capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == "Error: --uri cannot be combined with --host", result.stderr
    ok = subprocess.run(args=[binary(), uri, "--csv", "--no-header", "-c", "SELECT 'uri'"],
                        env=client_env(server=server), capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "uri", ok.stderr


def test_once_redirects_only_the_next_result(server, tmp_path):
    once = tmp_path / "once.csv"
    result = run(server, "--csv", "--no-header", "--command",
                 f".once {once}\nSELECT 'to the file';\nSELECT 'to stdout';")
    assert result.returncode == 0, result.stderr
    assert once.read_text() == "to the file\n"
    assert result.stdout == "to stdout\n", result.stdout


def test_read_runs_a_file(server, tmp_path):
    script = tmp_path / "script.sql"
    script.write_text("SELECT 'from the file' AS a;\nSELECT 1 + 1 AS b;\n")
    result = run(server, "--csv", "--no-header", "--command", f".read {script}\nSELECT 'after';")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["from the file", "2", "after"], result.stdout
    missing = run(server, "--command", f".read {tmp_path / 'missing.sql'}")
    assert missing.returncode == 1
    assert f"Error: cannot open file '{tmp_path / 'missing.sql'}'" in strip_ansi(missing.stderr), missing.stderr


def test_unknown_dot_command_fails_the_script(server):
    result = run(server, "--command", ".nonexistent")
    assert result.returncode == 1
    assert "Error: unknown command '.nonexistent'" in strip_ansi(result.stderr), result.stderr
    assert run(server, "--command", ".mode csv").returncode == 0


def test_ctrl_c_cancels_the_running_statement_and_the_script_goes_on(server):
    process = subprocess.Popen(args=[binary(), *conn_args(server), "--csv", "--no-header", "--command",
                                     f"{SLOW_SQL}; SELECT 'after the cancel';"],
                               env=client_env(server=server), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               **interruptible())
    # Interrupt only once the statement runs: a Ctrl+C before that (the client still
    # starting or signing in) exits at once with 130 and says nothing, as it should. A
    # fixed sleep lost that race on a loaded Windows runner (stderr empty).
    admin = server.admin()
    deadline = time.monotonic() + 30 * TIMEOUT_SCALE
    while admin.scalar(sql="SELECT count(*) FROM hardcharger_sessions() WHERE running AND NOT is_current") == 0:
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, "the slow statement never started"
        time.sleep(0.05)
    started = time.monotonic()
    interrupt(process=process)
    stdout, stderr = process.communicate(timeout=30)
    assert time.monotonic() - started < 15
    assert strip_ansi(stderr).strip() == "Query cancelled", stderr
    assert stdout == "after the cancel\n", stdout
    assert process.returncode == 1  # a statement failed
    # The positive twin: not interrupted, the same script runs both statements.
    twin = run(server, "--csv", "--no-header", "--command", "SELECT 1; SELECT 'after the cancel';")
    assert twin.returncode == 0 and twin.stdout == "1\nafter the cancel\n"


def test_ctrl_c_stops_a_script_waiting_for_input(server):
    process = subprocess.Popen(args=[binary(), *conn_args(server), "--quiet"], env=client_env(server=server),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               **interruptible())
    process.stdin.write("SELECT 'before';\n")
    process.stdin.flush()
    time.sleep(1.5)
    interrupt(process=process)
    assert process.wait(timeout=10) == 130
    assert "before" in process.stdout.read()


def test_a_session_the_server_lost_is_reopened(launcher, server):
    # The server evicts a session idle for a second (it sweeps every 5 s); the next
    # statement finds it gone (session_not_found), and the client opens a new session,
    # says so, and runs it.
    idle = launcher.start(args=["--session-idle-timeout", "1"], username="testuser")
    script = "SET VARIABLE x = 1;\n.shell sleep 7\nSELECT 'after the idle';"
    result = run(idle, "--csv", "--no-header", "--command", script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK\nafter the idle\n", result.stdout
    assert result.stderr == ("Warning: the server no longer had this session (idle too long, or restarted); "
                             "a new session is open. Temporary tables and session settings are gone.\n"), result.stderr
    # The positive twin: a server that keeps idle sessions, no warning.
    kept = run(server, "--csv", "--no-header", "--command", script)
    assert kept.returncode == 0 and kept.stderr == "" and kept.stdout == "OK\nafter the idle\n", kept.stderr


def test_a_killed_session_is_reported_and_reopened(server, tmp_path):
    # Another session KILLs this one (session_ended, reason killed): the next statement
    # fails with that error, and the one after runs in a new session.
    if sys.platform == "win32":
        # .shell runs its command with cmd.exe there: the killer is a Python script.
        killer = tmp_path / "kill.py"
        killer.write_text(
            "import subprocess\n"
            f"client = [{binary()!r}, *{conn_args(server)!r}]\n"
            "ids = subprocess.run(client + ['--csv', '--no-header', '-c', "
            "'SELECT session_id FROM hardcharger_sessions() WHERE NOT is_current'], "
            "capture_output=True, text=True, check=True).stdout.split()\n"
            "for id in ids:\n"
            "    subprocess.run(client + ['-c', f\"KILL SESSION '{id}'\"], capture_output=True, check=True)\n")
        command = f"{sys.executable} -I {killer}"
    else:
        killer = tmp_path / "kill.sh"
        killer.write_text(
            "#!/bin/sh\n"
            f"for id in $({shlex.quote(binary())} {shlex.join(conn_args(server))} --csv --no-header -c "
            f"\"SELECT session_id FROM hardcharger_sessions() WHERE NOT is_current\"); do\n"
            f"  {shlex.quote(binary())} {shlex.join(conn_args(server))} -c \"KILL SESSION '$id'\" >/dev/null\n"
            "done\n")
        killer.chmod(0o755)
        command = str(killer)
    script = f"SELECT 'before';\n.shell {command}\nSELECT 'killed';\nSELECT 'reopened';"
    result = run(server, "--csv", "--no-header", "--command", script)
    stderr = strip_ansi(result.stderr)
    assert result.stdout == "before\nreopened\n", (result.stdout, stderr)
    assert "Warning: the server ended this session (killed); a new session is open." in stderr, stderr
    assert "Error: This session has been killed by an administrator" in stderr, stderr
    assert result.returncode == 1  # the statement the kill caught failed


# ---- Added: comments, color, .help and sign-in errors ----


def test_trailing_and_standalone_comments_are_no_ops(server, tmp_path):
    # A script may end with a comment after its last statement, and hold comment-only lines
    # anywhere; none of them is sent to the server as a statement (1.x and DuckDB's shell
    # alike). Every input path: -c, -f, a pipe, a heredoc.
    script = ("-- a header comment\n"
              "SELECT 1 AS a; -- after the statement\n"
              "/* a block\n   comment */\n"
              "SELECT 2 AS b;\n"
              "/* note */;\n"
              "-- the last line is a comment")
    expected = ["1", "2"]
    sql_file = tmp_path / "commented.sql"
    sql_file.write_text(script + "\n")
    quoted = f"{shlex.quote(binary())} {shlex.join(conn_args(server))} --quiet --csv --no-header"
    runs = {
        "-c": run(server, "--csv", "--no-header", "--command", script),
        "-f": run(server, "--csv", "--no-header", "--file", str(sql_file)),
        "pipe": run_bash(script=f"cat {shlex.quote(str(sql_file))} | {quoted}", env=client_env(server=server)),
        "heredoc": run_bash(script=f"{quoted} <<'SQLEOF'\n{script}\nSQLEOF\n", env=client_env(server=server)),
    }
    for how, result in runs.items():
        assert (result.returncode, result.stderr) == (0, ""), (how, result.stderr)
        assert result.stdout.splitlines() == expected, (how, result.stdout)
    only = run(server, "--command", "-- nothing but a comment")
    assert (only.returncode, only.stdout, only.stderr) == (0, "", ""), only
    # The negative twin: a comment doesn't hide a real statement's error.
    failing = run(server, "--command", "-- first\nSELECT * FROM no_such_table_after_a_comment; -- last")
    assert failing.returncode == 1, failing.stderr
    assert "no_such_table_after_a_comment does not exist" in failing.stderr, failing.stderr


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_no_color_off_a_terminal(server):
    # Errors are plain text when stderr is a pipe or a file: no escape codes in logs.
    result = run(server, "--command", "SELECT * FROM no_such_table_for_color")
    assert result.returncode == 1
    assert result.stderr.startswith("Error: Catalog Error: Table with name no_such_table_for_color"), result.stderr
    assert "\x1b" not in result.stderr, repr(result.stderr)
    tables = run(server, "--command", ".tables")
    assert tables.returncode == 0 and "employees" in tables.stdout, tables.stderr
    assert "\x1b" not in tables.stdout, repr(tables.stdout)


@pytest.mark.pty
def test_no_color_is_honoured_at_a_terminal(server, home):
    # At a terminal the prompt and errors are colored, unless NO_COLOR is set.
    child = spawn(server, home)
    child.expect(PROMPT)
    assert SGR.search(child.after), repr(child.after)  # the positive twin: colored
    enter(child, "SELECT * FROM no_such_table_for_color;")
    child.expect(r"\x1b\[1;31mError: Catalog Error")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)

    child = spawn(server, home, env=client_env(server=server, home=home, NO_COLOR="1"))
    child.expect(PROMPT)
    banner_and_prompt = child.before + child.after
    assert not SGR.search(banner_and_prompt), repr(banner_and_prompt)
    enter(child, "SELECT * FROM no_such_table_for_color;")
    child.expect("Error: Catalog Error")
    child.expect(PROMPT)
    assert not SGR.search(child.before + child.after), repr(child.before + child.after)
    enter(child, ".quit")
    child.expect(pexpect.EOF)
    # An empty NO_COLOR is unset, as the convention has it.
    child = spawn(server, home, env=client_env(server=server, home=home, NO_COLOR=""))
    child.expect(PROMPT)
    assert SGR.search(child.after), repr(child.after)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


def help_rows(text: str) -> tuple[int, list[str]]:
    """The description column of a .help listing and its lines (ANSI removed)."""
    lines = strip_ansi(text).replace("\r", "").splitlines()
    connect = next(line for line in lines if line.startswith(".connect "))
    return connect.index("Connect to a"), lines


def check_help(text: str, width: int) -> None:
    column, lines = help_rows(text)
    assert lines and all(len(line) <= width for line in lines), [line for line in lines if len(line) > width]
    for line in lines:
        if line.startswith("."):
            # The command, at least two spaces, then the description at the column.
            assert line[column - 2:column] == "  " and line[column] != " ", (column, line)
        else:
            assert line.startswith(" " * column) and line[column] != " ", (column, line)


def test_help_aligns_its_columns_and_wraps_off_a_terminal(server):
    result = run(server, "--command", ".help")
    assert result.returncode == 0, result.stderr
    check_help(text=result.stdout, width=80)
    # The longest command, with its description beside it.
    assert ".tables [PATTERN] [--flat]" in result.stdout
    assert ".connect URI | HOST PORT USER  Connect to a Hardcharger server" in result.stdout


@pytest.mark.pty
def test_help_wraps_at_the_terminal_width(server, home):
    child = pexpect.spawn(command=binary(), args=[*conn_args(server)], env=client_env(server=server, home=home),
                          encoding="utf-8", dimensions=(60, 60), timeout=30)
    child.expect(PROMPT)
    enter(child, ".help")
    child.expect(r"\.timer on\|off\s+Show query execution time\.\s+Default:\s+off\r\n")
    text = child.before + child.after
    text = text[text.index(".about"):]
    check_help(text=text, width=60)
    _, lines = help_rows(text)
    assert any(line.startswith(" ") for line in lines), lines  # something did wrap
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


def test_sign_in_errors_show_the_servers_refusal(server):
    result = run(server, "--command", "SELECT 1",
                 env=client_env(server=server, HARDCHARGER_PASSWORD="not-the-password"))
    assert result.returncode == 1, result.stdout
    assert result.stderr == (f"Error: Authentication failed for http://localhost:{server.port}: invalid_credentials "
                             "(the user name or password is not correct)\n"), result.stderr
    assert "not-the-password" not in result.stderr + result.stdout
    # The positive twin: the right password signs in.
    assert run(server, "--command", "SELECT 1").returncode == 0


# ---- Added: the shell at a terminal ----


def spawn(server, home: pathlib.Path, *args: str, env: dict[str, str] | None = None) -> pexpect.spawn:
    child = pexpect.spawn(command=binary(), args=[*conn_args(server), *args],
                          env=env or client_env(server=server, home=home), encoding="utf-8",
                          dimensions=(40, 160), timeout=30)
    return child


PROMPT = r"memory\.main(\x1b\[[0-9;]*m)*> "


def enter(child: pexpect.spawn, text: str) -> None:
    """Types a line and Enter (replxx reads a carriage return as Enter)."""
    child.send(text + "\r")


@pytest.mark.pty
def test_interactive_session(server, home):
    child = spawn(server, home)
    child.expect(r"Hardcharger Client \d+\.\d+\.\d+")
    child.expect(f"Connected to localhost:{server.port}")
    child.expect(PROMPT)
    # A statement over two lines: the continuation prompt, then the result.
    enter(child, "SELECT 'multi' ||")
    child.expect("-> ")
    enter(child, "'line' AS joined;")
    child.expect("multiline")
    child.expect(PROMPT)
    enter(child, ".mode csv")
    child.expect(PROMPT)
    enter(child, "SELECT 6 * 7 AS answer;")
    child.expect("answer\r\n42\r\n")
    child.expect(PROMPT)
    enter(child, ".exit")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    history = (home / ".hardcharger_history").read_text()
    assert "SELECT 6 * 7 AS answer;" in history, history
    assert ".mode csv" in history


@pytest.mark.pty
def test_interactive_ctrl_c_cancels_and_the_shell_goes_on(server, home):
    child = spawn(server, home)
    child.expect(PROMPT)
    enter(child, f"{SLOW_SQL};")
    child.expect("\r\n")  # replxx ends the line on Enter: the statement is being sent
    time.sleep(1.5)
    started = time.monotonic()
    child.sendcontrol("c")
    child.expect("Query cancelled")
    assert time.monotonic() - started < 15
    child.expect(PROMPT)
    enter(child, "SELECT 40 + 2 AS still_here;")
    child.expect("still_here")
    child.expect("42")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


@pytest.mark.pty
def test_interactive_tab_completion(server, home):
    child = spawn(server, home)
    child.expect(PROMPT)
    child.send("SELECT r_name FROM regi")
    child.send("\t")
    child.send(" WHERE r_regionkey = 3;\r")
    child.expect("EUROPE")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


@pytest.mark.pty
def test_interactive_pager(server, home):
    child = spawn(server, home)
    child.expect(PROMPT)
    enter(child, "SELECT i FROM range(200) t(i);")
    child.expect(r"-- Page 1/\d+")
    child.send("q")
    child.expect(PROMPT)
    enter(child, ".pager off")
    child.expect(PROMPT)
    enter(child, ".mode csv")
    child.expect(PROMPT)
    enter(child, "SELECT count(*) AS n FROM range(200);")
    child.expect("n\r\n200\r\n")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


@pytest.mark.pty
def test_interactive_init_file(server, home):
    (home / ".hardchargerrc").write_text(".mode csv\n.headers off\n")
    child = spawn(server, home)
    child.expect(PROMPT)
    enter(child, "SELECT 'from rc' AS a;")
    child.expect("\r\nfrom rc\r\n")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)
    # --no-init skips it: box mode, with the header.
    child = spawn(server, home, "--no-init")
    child.expect(PROMPT)
    enter(child, "SELECT 'from rc' AS a;")
    child.expect("varchar")
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)


@pytest.mark.pty
def test_interactive_password_prompt(server, home):
    env = client_env(home=home)  # no HARDCHARGER_PASSWORD: the client asks
    child = spawn(server, home, env=env)
    child.expect("Password: ")
    enter(child, server.password)
    child.expect(PROMPT)
    enter(child, ".quit")
    child.expect(pexpect.EOF)
    # The negative twin: a wrong password is refused, exit code 1.
    child = spawn(server, home, env=env)
    child.expect("Password: ")
    enter(child, "not-the-password")
    child.expect(f"Error: Authentication failed for http://localhost:{server.port}: invalid_credentials "
                 r"\(the user name or password is not correct\)")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 1


@pytest.mark.pty
def test_interactive_without_a_server_starts_disconnected(home):
    child = pexpect.spawn(command=binary(), args=[], env=client_env(home=home), encoding="utf-8",
                          dimensions=(40, 160), timeout=30)
    child.expect("Not connected. Use '.connect HOST PORT USERNAME' to connect.")
    child.expect("hardcharger> ")
    enter(child, "SELECT 1;")
    child.expect("Error: not connected to a server")
    child.expect("hardcharger> ")
    enter(child, ".quit")
    child.expect(pexpect.EOF)


# ---- Added: TLS, mutual TLS ----


def make_certificates(directory: pathlib.Path, names: list[x509.GeneralName]) -> dict[str, pathlib.Path]:
    """A CA, a server certificate it signs for `names`, a client certificate it signs, and
    an unrelated CA."""
    now = datetime.datetime.now(tz=datetime.UTC)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def write(name: str, cert, private_key) -> tuple[pathlib.Path, pathlib.Path]:
        cert_path, key_path = directory / f"{name}.pem", directory / f"{name}.key"
        cert_path.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
        key_path.write_bytes(private_key.private_bytes(encoding=serialization.Encoding.PEM,
                                                       format=serialization.PrivateFormat.PKCS8,
                                                       encryption_algorithm=serialization.NoEncryption()))
        return cert_path, key_path

    def ca(common_name: str):
        ca_key = key()
        subject = x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value=common_name)])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(private_key=ca_key, algorithm=hashes.SHA256()))
        return cert, ca_key

    def leaf(common_name: str, issuer, issuer_key, usage, san=None):
        leaf_key = key()
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value=common_name)]))
                   .issuer_name(issuer.subject).public_key(leaf_key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(extval=x509.ExtendedKeyUsage([usage]), critical=False))
        if san:
            builder = builder.add_extension(extval=x509.SubjectAlternativeName(san), critical=False)
        return builder.sign(private_key=issuer_key, algorithm=hashes.SHA256()), leaf_key

    ca_cert, ca_key = ca(common_name="test-ca")
    other_cert, other_key = ca(common_name="other-ca")
    server_cert, server_key = leaf("server", ca_cert, ca_key, ExtendedKeyUsageOID.SERVER_AUTH, names)
    client_cert, client_key = leaf("client", ca_cert, ca_key, ExtendedKeyUsageOID.CLIENT_AUTH)
    paths = {}
    paths["ca"], _ = write("ca", ca_cert, ca_key)
    paths["other_ca"], _ = write("other-ca", other_cert, other_key)
    paths["server_cert"], paths["server_key"] = write("server", server_cert, server_key)
    paths["client_cert"], paths["client_key"] = write("client", client_cert, client_key)
    return paths


LOCAL_NAMES = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]


def start_tls(launcher, certs, mutual: bool = False):
    args = ["--tls-cert", str(certs["server_cert"]), "--tls-key", str(certs["server_key"])]
    if mutual:
        args += ["--mtls-ca-cert", str(certs["ca"])]
    return launcher.start(args=args, expect_up=False)


def wait_tls(server, extra: list[str]):
    deadline = time.monotonic() + 30
    while True:
        result = run(server, "--tls", *extra, "--csv", "--no-header", "-c", "SELECT 1")
        if result.returncode == 0 or time.monotonic() > deadline or "Could not connect" not in result.stderr:
            return result
        time.sleep(0.2)


def test_tls_verifies_the_server(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=LOCAL_NAMES)
    server = start_tls(launcher=launcher, certs=certs)
    ok = wait_tls(server, extra=["--tls-roots", str(certs["ca"])])
    assert ok.returncode == 0 and ok.stdout.strip() == "1", ok.stderr
    wrong = run(server, "--tls", "--tls-roots", str(certs["other_ca"]), "-c", "SELECT 1")
    assert wrong.returncode == 1
    assert "certificate verify failed" in wrong.stderr, wrong.stderr
    skipped = run(server, "--tls", "--tls-skip-verify", "--csv", "--no-header", "-c", "SELECT 2")
    assert skipped.returncode == 0 and skipped.stdout.strip() == "2", skipped.stderr
    # Plain HTTP to a TLS port is no Warp SQL server.
    plain = run(server, "-c", "SELECT 1")
    assert plain.returncode == 1 and plain.stderr.startswith("Error: "), plain.stderr


def test_tls_checks_the_host_name(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=[x509.DNSName("db.example.com")])
    server = start_tls(launcher=launcher, certs=certs)
    assert wait_tls(server, extra=["--tls-skip-verify"]).returncode == 0
    mismatch = run(server, "--tls", "--tls-roots", str(certs["ca"]), "-c", "SELECT 1")
    assert mismatch.returncode == 1
    assert "certificate verify failed" in mismatch.stderr, mismatch.stderr


def test_mutual_tls(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=LOCAL_NAMES)
    server = start_tls(launcher=launcher, certs=certs, mutual=True)
    mtls = ["--mtls-cert", str(certs["client_cert"]), "--mtls-key", str(certs["client_key"])]
    ok = wait_tls(server, extra=["--tls-roots", str(certs["ca"]), *mtls])
    assert ok.returncode == 0 and ok.stdout.strip() == "1", ok.stderr
    without = run(server, "--tls", "--tls-roots", str(certs["ca"]), "-c", "SELECT 1")
    assert without.returncode == 1 and without.stderr.startswith("Error: "), without.stderr
    # --tls-skip-verify with a client certificate (1.x refused the pair: Arrow Flight
    # dropped the certificate): the certificate is sent, and the server lets it in.
    skipping = run(server, "--tls", "--tls-skip-verify", *mtls, "--csv", "--no-header", "-c", "SELECT 3")
    assert skipping.returncode == 0 and skipping.stdout.strip() == "3", skipping.stderr
    half = run(server, "--tls", "--tls-skip-verify", "--mtls-cert", str(certs["client_cert"]), "-c", "SELECT 1")
    assert half.returncode == 1
    assert "Error: mTLS private key must be provided with mTLS certificate" in half.stderr, half.stderr


# ---- Added: the server's browser sign-in (--auth-type external) ----


@pytest.fixture
def idp():
    from mock_idp import MockIdP
    provider = MockIdP(audience="hardcharger-oauth-test", client_id="test-client-id", client_secret="test-client-secret")
    yield provider
    provider.stop()


def fake_browser(tmp_path: pathlib.Path, code: str) -> pathlib.Path | str:
    """A "browser" that signs in at once: it follows the authorization URL's redirect_uri
    with an authorization code, as the identity provider would after the user signed in."""
    script = tmp_path / "browser.py"
    script.write_text(
        "import sys, urllib.parse, urllib.request\n"
        "query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(sys.argv[1]).query))\n"
        f"params = urllib.parse.urlencode({{'code': {code!r}, 'state': query['state']}})\n"
        "urllib.request.urlopen(query['redirect_uri'] + '?' + params, timeout=30).read()\n")
    if sys.platform == "win32":
        # There HARDCHARGER_BROWSER is a command line; the client appends the URL.
        return f'"{sys.executable}" -I "{script}"'
    launcher = tmp_path / "browser.sh"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} \"$1\"\n")
    launcher.chmod(0o755)
    return launcher


def test_external_auth_signs_in_through_the_browser(launcher, idp, tmp_path):
    oauth_port = free_port()
    server = launcher.start(args=["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", idp.audience,
                                  "--token-default-role", "admin", "--token-authorized-emails", "*@gizmodata.com",
                                  "--oauth-client-id", idp.client_id, "--oauth-client-secret", idp.client_secret,
                                  "--oauth-port", str(oauth_port)])
    idp.valid_code = "browser-code-1"
    args = [binary(), "--host", "localhost", "--port", str(server.port), "--auth-type", "external",
            "--oauth-port", str(oauth_port), "--csv", "--no-header", "-c", "SELECT HARDCHARGER_USER()"]
    env = client_env(HARDCHARGER_BROWSER=str(fake_browser(tmp_path=tmp_path, code="browser-code-1")))
    result = subprocess.run(args=args, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "testuser@gizmodata.com"
    assert "Waiting for browser login" in result.stderr
    # The negative twin: the identity provider refuses the code.
    env["HARDCHARGER_BROWSER"] = str(fake_browser(tmp_path=tmp_path, code="not-the-code"))
    refused = subprocess.run(args=args, env=env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 1
    assert "Error: OAuth login failed:" in refused.stderr, refused.stderr


# ---- Split results (Warp SQL §6.10) ----


def test_result_partitions_read_a_large_result_in_order(server):
    # Over four connections at once (or the default eight), the rows come out exactly as over
    # one.
    sql = "SELECT i, 'row ' || i AS label FROM range(1000000) t(i) ORDER BY i DESC"
    one = run(server, "--csv", "--no-header", "--result-partitions", "1", "--command", sql)
    split = run(server, "--csv", "--no-header", "--result-partitions", "4", "--command", sql)
    assert one.returncode == 0 and split.returncode == 0, (one.stderr, split.stderr)
    lines = split.stdout.splitlines()
    assert len(lines) == 1_000_000 and lines[0] == "999999,row 999999" and lines[-1] == "0,row 0"
    assert split.stdout == one.stdout
    env = client_env(server=server, HARDCHARGER_RESULT_PARTITIONS="3")
    from_env = run(server, "--csv", "--no-header", "--command", sql, env=env)
    assert from_env.returncode == 0 and from_env.stdout == one.stdout, from_env.stderr
    default = run(server, "--csv", "--no-header", "--command", sql)
    assert default.returncode == 0 and default.stdout == one.stdout, default.stderr


def test_result_partitions_report_a_failure(server):
    result = run(server, "--csv", "--result-partitions", "4", "--command",
                 "SELECT CASE WHEN i < 30000000 THEN i ELSE error('boom in a split') END AS v "
                 "FROM range(100000000) t(i)")
    assert result.returncode != 0
    assert "boom in a split" in result.stderr + result.stdout
