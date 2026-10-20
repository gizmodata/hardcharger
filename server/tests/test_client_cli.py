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


def conn_args(server, tls: bool = False) -> list[str]:
    """The flags for the server: --tls=false for the plain-HTTP test servers (TLS is the
    client's default), nothing for a TLS one."""
    return ["--host", "localhost", "--port", str(server.port), *([] if tls else ["--tls=false"]),
            "--username", server.username]


def run(server, *args: str, env: dict[str, str] | None = None, input: str | None = None,
        timeout: float = 120, tls: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args=[binary(), *conn_args(server, tls=tls), *args], env=env or client_env(server=server),
                          input=input, capture_output=True, text=True, timeout=timeout)


def run_tls(server, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return run(server, *args, tls=True, **kwargs)


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
    assert output.startswith("OK\nOK, 1 row affected\n"), output
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
    result = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(server.port), "--tls=false", "--command", "SELECT 1"],
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


def test_licenses_prints_the_third_party_notices():
    """The client carries THIRD_PARTY_NOTICES.md too (the libraries it links are among them)."""
    result = subprocess.run(args=[binary(), "--licenses"], env=client_env(), capture_output=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr
    notices = (pathlib.Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md").read_bytes()
    # Byte for byte, on every platform: .gitattributes keeps the file LF in every checkout
    # (Windows' too), and --licenses writes stdout in binary mode there.
    assert b"\r" not in notices, "THIRD_PARTY_NOTICES.md has CR line ends in this checkout: see .gitattributes"
    assert result.stdout == notices
    assert b"### replxx\n" in notices


def test_bad_port_value_is_an_error():
    result = subprocess.run(args=[binary(), "--port", "abc"], env=client_env(), capture_output=True, text=True)
    assert result.returncode == 1
    assert "the argument ('abc') for option '--port' is invalid" in result.stderr, result.stderr


def test_uri_cannot_be_combined_with_connection_flags(server):
    # A URI names the host, the port and TLS; those flags beside it are refused. The user and
    # the other TLS settings come from their own flags, as with the ADBC driver's options.
    uri = f"hardcharger://localhost:{server.port}?tls=false"
    for flag in (["--host", "localhost"], ["--port", str(server.port)], ["--tls"], ["--tls=false"]):
        result = subprocess.run(args=[binary(), uri, *flag, "--username", server.username, "-c", "SELECT 1"],
                                env=client_env(server=server), capture_output=True, text=True, timeout=60)
        assert result.returncode == 1
        assert result.stderr.strip() == f"Error: --uri cannot be combined with {flag[0].split('=')[0]}", result.stderr
    ok = subprocess.run(args=[binary(), uri, "--username", server.username, "--csv", "--no-header", "-c", "SELECT 'uri'"],
                        env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and ok.stdout.strip() == "uri", ok.stderr


def test_uri_parameters_of_1x_are_refused_by_name(server):
    uri = f"hardcharger://localhost:{server.port}?username={server.username}&useEncryption=false"
    result = subprocess.run(args=[binary(), uri, "-c", "SELECT 1"], env=client_env(server=server),
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        f"Error: invalid uri {uri}: unknown parameter(s) useEncryption, username (useEncryption: TLS is on unless the "
        "URI says ?tls=false; username: --username)"), result.stderr
    assert "Connected" not in result.stdout


def test_hardcharger_plus_http_is_refused():
    result = subprocess.run(args=[binary(), "hardcharger+http://localhost:31337", "-c", "SELECT 1"],
                            env=client_env(), capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Error: hardcharger+http:// is not a Hardcharger URI scheme: use hardcharger://host:port for HTTPS, or "
        "hardcharger://host:port?tls=false for plain HTTP"), result.stderr


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


# A non-interactive shell starts a background job (`cmd &`) with SIGINT ignored. The
# client's signal watcher took SIGINT with sigwait() and was woken at exit by a SIGINT sent
# to its own thread, which an ignored SIGINT never delivers: the client printed its result
# and then waited forever. Each way a script starts it in the background, the job must end
# on its own, with its exit code.
BACKGROUND_STARTS = {
    "plain": "{client} -c 'SELECT 41 + 1' > {out} &",
    "stdin_dev_null": "{client} -c 'SELECT 41 + 1' > {out} < /dev/null &",
    "stdin_closed_pipe": ": | {client} -c 'SELECT 41 + 1' > {out} &",
    "script_on_a_pipe": "echo 'SELECT 41 + 1;' | {client} > {out} &",
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells' background jobs (SIGINT ignored)")
@pytest.mark.parametrize("start", BACKGROUND_STARTS)
def test_a_background_job_in_a_script_exits_by_itself(server, tmp_path, start):
    out = tmp_path / "out.csv"
    client = shlex.join([binary(), *conn_args(server), "--csv", "--no-header"])
    deadline = int(20 * TIMEOUT_SCALE)
    # The script waits for the job with a deadline of its own and kills it if it hangs, so
    # nothing outlives the test.
    script = (BACKGROUND_STARTS[start].format(client=client, out=shlex.quote(str(out))) + "\n"
              "job=$!\n"
              f"for _ in $(seq {deadline * 10}); do kill -0 $job 2>/dev/null || break; sleep 0.1; done\n"
              "if kill -0 $job 2>/dev/null; then kill -9 $job; echo hung; exit 0; fi\n"
              "wait $job; echo \"exit=$?\"\n")
    started = time.monotonic()
    result = run_bash(script=script, env=client_env(server=server))
    assert result.stdout.strip() == "exit=0", (result.stdout, result.stderr, out.read_text() if out.exists() else "")
    assert out.read_text() == "42\n"
    assert time.monotonic() - started < 15 * TIMEOUT_SCALE


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
    # The client already signed in again and opened a new session: it says what became of
    # the statement, not the server's advice to do what it just did.
    assert stderr == ("Warning: the server ended this session: an administrator ended it (killed); a new session is "
                      "open. Temporary tables and session settings are gone.\n"
                      "Error: The statement did not complete: its session ended (killed). It was not run again; run "
                      "it again to run it in the new session.\n"), stderr
    assert "Sign in again" not in stderr, stderr
    assert result.returncode == 1  # the statement the kill caught failed


def test_a_password_change_is_reported_with_its_reason(launcher, store, tmp_path):
    # Another session changes this user's password mid-script: the server refuses the
    # session's token and says why (Warp SQL §4.2's reason), and the client says that, not
    # the server's wording.
    from rbac import start_rbac_server
    rbac = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    admin = rbac.admin()
    admin.run(sql="CREATE USER pat WITH PASSWORD 'pw-1'")
    changer = tmp_path / "change.py"
    changer.write_text(
        "import os, subprocess, sys, time\n"
        f"client = [{binary()!r}, '--host', 'localhost', '--port', '{rbac.running.port}', '--tls=false', '--username', 'admin']\n"
        f"env = {{**os.environ, 'HARDCHARGER_PASSWORD': {rbac.running.password!r}}}\n"
        "subprocess.run(client + ['-c', \"ALTER USER pat WITH PASSWORD 'pw-2'\"], env=env, check=True,\n"
        "               capture_output=True)\n"
        f"time.sleep({2 * TIMEOUT_SCALE})  # access control sees the change within about a second\n")
    script = f"SELECT 'before';\n.shell {sys.executable} -I {changer}\nSELECT 'after';"
    result = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(rbac.running.port), "--tls=false", "--username", "pat",
                                  "--csv", "--no-header", "--command", script],
                            env=client_env(HARDCHARGER_PASSWORD="pw-1"), capture_output=True, text=True, timeout=120)
    stderr = strip_ansi(result.stderr)
    assert result.stdout == "before\n", (result.stdout, stderr)
    assert stderr == ("Error: The server no longer accepts this sign-in: the user's password or credentials changed "
                      "(credentials_changed). Sign in again (.connect).\n"), stderr
    assert result.returncode == 1
    # The twin: with the new password the user signs in and runs it.
    again = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(rbac.running.port), "--tls=false", "--username", "pat",
                                 "--csv", "--no-header", "--command", "SELECT 'after'"],
                           env=client_env(HARDCHARGER_PASSWORD="pw-2"), capture_output=True, text=True, timeout=120)
    assert again.returncode == 0 and again.stdout == "after\n", again.stderr


def test_rows_affected_counts_one_row_and_many(server):
    result = run(server, "--command", "CREATE TEMPORARY TABLE counted (x INTEGER); INSERT INTO counted VALUES (1); "
                                      "INSERT INTO counted VALUES (2), (3), (4); UPDATE counted SET x = 0 WHERE x > 9;")
    assert result.returncode == 0, result.stderr
    assert strip_ansi(result.stdout) == "OK\nOK, 1 row affected\nOK, 3 rows affected\nOK\n", result.stdout


def test_lossless_wide_integers_print_as_exact_integers(server):
    # With arrow_lossless_conversion DuckDB sends HUGEINT and UHUGEINT as Arrow's opaque
    # 16-byte type; the client prints them as the integers they are, extremes included.
    values = ["170141183460469231731687303715884105727", "-170141183460469231731687303715884105728",
              "340282366920938463463374607431768211455", "0", "-1"]
    select = ("SELECT CAST('{}' AS HUGEINT) AS hmax, CAST('{}' AS HUGEINT) AS hmin, CAST('{}' AS UHUGEINT) AS umax, "
              "CAST('{}' AS UHUGEINT) AS z, CAST('{}' AS HUGEINT) AS m1, NULL::HUGEINT AS n").format(*values)
    lossless = "SET SESSION arrow_lossless_conversion = true; "
    csv = run(server, "--csv", "--no-header", "--command", lossless + select)
    assert csv.returncode == 0, csv.stderr
    assert csv.stdout == "OK\n" + ",".join(values) + ",NULL\n", csv.stdout
    as_json = run(server, "--json", "--command", lossless + select)
    assert as_json.returncode == 0, as_json.stderr
    assert ('"hmax": 170141183460469231731687303715884105727, "hmin": -170141183460469231731687303715884105728, '
            '"umax": 340282366920938463463374607431768211455, "z": 0, "m1": -1, "n": null') in as_json.stdout, as_json.stdout
    box = run(server, "--box", "--command", lossless + select)
    assert box.returncode == 0, box.stderr
    type_row = strip_ansi(box.stdout).splitlines()[3]
    assert [cell.strip() for cell in type_row.strip("│").split("│")] == [
        "hugeint", "hugeint", "uhugeint", "uhugeint", "hugeint", "hugeint"], box.stdout
    # The twin: without the option the same values that fit decimal128(38, 0) print the same.
    plain = run(server, "--csv", "--no-header", "--command", "SELECT CAST('-1' AS HUGEINT), CAST('0' AS UHUGEINT)")
    assert plain.returncode == 0 and plain.stdout == "-1,0\n", plain.stdout


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
    child.expect(re.escape("Not connected. Use '.connect HOST PORT USERNAME' (add --no-tls for a server without "
                           "TLS) or '.connect URI' to connect."))
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
        result = run_tls(server, *extra, "--csv", "--no-header", "-c", "SELECT 1")
        if result.returncode == 0 or time.monotonic() > deadline or "Could not connect" not in result.stderr:
            return result
        time.sleep(0.2)


def test_tls_verifies_the_server(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=LOCAL_NAMES)
    server = start_tls(launcher=launcher, certs=certs)
    ok = wait_tls(server, extra=["--tls-roots", str(certs["ca"])])
    assert ok.returncode == 0 and ok.stdout.strip() == "1", ok.stderr
    wrong = run_tls(server, "--tls-roots", str(certs["other_ca"]), "-c", "SELECT 1")
    assert wrong.returncode == 1
    assert "certificate verify failed" in wrong.stderr, wrong.stderr
    skipped = run_tls(server, "--tls-skip-verify", "--csv", "--no-header", "-c", "SELECT 2")
    assert skipped.returncode == 0 and skipped.stdout.strip() == "2", skipped.stderr
    # Plain HTTP to a TLS port is no Warp SQL server.
    plain = run(server, "-c", "SELECT 1")
    assert plain.returncode == 1 and plain.stderr.startswith("Error: "), plain.stderr


def test_tls_checks_the_host_name(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=[x509.DNSName("db.example.com")])
    server = start_tls(launcher=launcher, certs=certs)
    assert wait_tls(server, extra=["--tls-skip-verify"]).returncode == 0
    mismatch = run_tls(server, "--tls-roots", str(certs["ca"]), "-c", "SELECT 1")
    assert mismatch.returncode == 1
    assert "certificate verify failed" in mismatch.stderr, mismatch.stderr


def test_mutual_tls(launcher, tmp_path):
    certs = make_certificates(directory=tmp_path, names=LOCAL_NAMES)
    server = start_tls(launcher=launcher, certs=certs, mutual=True)
    mtls = ["--mtls-cert", str(certs["client_cert"]), "--mtls-key", str(certs["client_key"])]
    ok = wait_tls(server, extra=["--tls-roots", str(certs["ca"]), *mtls])
    assert ok.returncode == 0 and ok.stdout.strip() == "1", ok.stderr
    without = run_tls(server, "--tls-roots", str(certs["ca"]), "-c", "SELECT 1")
    assert without.returncode == 1 and without.stderr.startswith("Error: "), without.stderr
    # --tls-skip-verify with a client certificate (1.x refused the pair: Arrow Flight
    # dropped the certificate): the certificate is sent, and the server lets it in.
    skipping = run_tls(server, "--tls-skip-verify", *mtls, "--csv", "--no-header", "-c", "SELECT 3")
    assert skipping.returncode == 0 and skipping.stdout.strip() == "3", skipping.stderr
    half = run_tls(server, "--tls-skip-verify", "--mtls-cert", str(certs["client_cert"]), "-c", "SELECT 1")
    assert half.returncode == 1
    assert "Error: mTLS private key must be provided with mTLS certificate" in half.stderr, half.stderr


# ---- Added: URIs, with the Hardcharger ADBC driver's rules (HTTPS unless ?tls=false) ----


class FirstBytes:
    """A TCP listener on 127.0.0.1 that keeps the first bytes each connection sends, then
    closes it: what a client puts on the wire before any server answers."""

    def __init__(self):
        import socket
        import threading
        self.received: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    self.received.append(conn.recv(4096))
                except OSError:
                    self.received.append(b"")

    def close(self):
        self.sock.close()


@pytest.fixture
def first_bytes():
    listener = FirstBytes()
    yield listener
    listener.close()


def run_uri(uri: str, *args: str, password: str = "never-sent-in-plaintext") -> subprocess.CompletedProcess:
    return subprocess.run(args=[binary(), uri, "--username", "joe", *args, "-c", "SELECT 1"],
                          env=client_env(HARDCHARGER_PASSWORD=password), capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("query", ["?tls=true", "", "?tls=1", "?tls=TRUE"])
def test_uri_with_tls_never_sends_plaintext(first_bytes, query):
    # The 1.x parser ignored tls= (and used plain HTTP unless useEncryption=true), so
    # hardcharger://host?tls=true sent its sign-in, password included, in plaintext.
    result = run_uri(f"hardcharger://127.0.0.1:{first_bytes.port}{query}")
    assert result.returncode == 1, result.stdout
    assert result.stderr.startswith("Error: "), result.stderr
    assert first_bytes.received, "the client never connected"
    for data in first_bytes.received:
        assert data[:1] == b"\x16", f"not a TLS handshake: {data[:40]!r}"  # a TLS record: handshake
        assert b"HTTP/" not in data and b"never-sent-in-plaintext" not in data, data[:200]


def test_uri_with_tls_false_is_plain_http_to_its_base_path(first_bytes):
    # The twin: ?tls=false is plain HTTP, to /warp, or to the URI's own path.
    run_uri(f"hardcharger://127.0.0.1:{first_bytes.port}?tls=false")
    run_uri(f"hardcharger://127.0.0.1:{first_bytes.port}/custom/prefix/?tls=false")
    assert len(first_bytes.received) >= 2, first_bytes.received
    assert first_bytes.received[0].startswith(b"GET /warp HTTP/1.1\r\n"), first_bytes.received[0][:80]
    assert first_bytes.received[-1].startswith(b"GET /custom/prefix HTTP/1.1\r\n"), first_bytes.received[-1][:80]


def test_uri_against_plain_and_tls_servers(launcher, server, tmp_path):
    plain = f"hardcharger://localhost:{server.port}"
    ok = subprocess.run(args=[binary(), f"{plain}?tls=false", "--username", server.username, "--csv", "--no-header",
                              "-c", "SELECT 'plain'"], env=client_env(server=server), capture_output=True, text=True,
                        timeout=60)
    assert ok.returncode == 0 and ok.stdout.strip() == "plain", ok.stderr
    # Without ?tls=false the URI is HTTPS, which a plain-HTTP server can't answer.
    tls_to_plain = subprocess.run(args=[binary(), plain, "--username", server.username, "-c", "SELECT 1"],
                                  env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert tls_to_plain.returncode == 1
    assert tls_to_plain.stderr == plain_http_hint(host="localhost", port=server.port), tls_to_plain.stderr

    certs = make_certificates(directory=tmp_path, names=LOCAL_NAMES)
    tls_server = start_tls(launcher=launcher, certs=certs)
    assert wait_tls(tls_server, extra=["--tls-roots", str(certs["ca"])]).returncode == 0
    uri = f"hardcharger://localhost:{tls_server.port}"
    https = subprocess.run(args=[binary(), uri, "--username", tls_server.username, "--tls-roots", str(certs["ca"]),
                                 "--csv", "--no-header", "-c", "SELECT 'https'"],
                           env=client_env(server=tls_server), capture_output=True, text=True, timeout=60)
    assert https.returncode == 0 and https.stdout.strip() == "https", https.stderr
    # The same server, with the roots it isn't signed by: verification still applies.
    wrong = subprocess.run(args=[binary(), uri, "--username", tls_server.username, "--tls-roots",
                                 str(certs["other_ca"]), "-c", "SELECT 1"],
                           env=client_env(server=tls_server), capture_output=True, text=True, timeout=60)
    assert wrong.returncode == 1 and "certificate verify failed" in wrong.stderr, wrong.stderr
    plain_to_tls = subprocess.run(args=[binary(), f"{uri}?tls=false", "--username", tls_server.username, "-c",
                                        "SELECT 1"], env=client_env(server=tls_server), capture_output=True, text=True,
                                  timeout=60)
    assert plain_to_tls.returncode == 1, plain_to_tls.stdout
    # Plain HTTP to a TLS server: the reason, and the way back to TLS.
    assert plain_to_tls.stderr.startswith(f"Error: Could not connect to http://localhost:{tls_server.port}: "), \
        plain_to_tls.stderr
    assert plain_to_tls.stderr.endswith(". If the server uses TLS, drop --no-tls (or --tls=false, ?tls=false).\n"), \
        plain_to_tls.stderr


def test_dot_connect_takes_a_uri_then_the_user_and_password(server):
    script = f".connect hardcharger://localhost:{server.port}?tls=false {server.username} {server.password}\n" \
             "SELECT 'dot connect' AS val;\n"
    ok = subprocess.run(args=[binary(), "-q", "--csv", "--no-header"], env=client_env(), input=script,
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert "Connected to localhost:" in ok.stdout and "dot connect" in ok.stdout, ok.stdout
    # A URI with 1.x's parameters is refused, and nothing connects.
    old = subprocess.run(args=[binary(), "-q"], env=client_env(), capture_output=True, text=True, timeout=60,
                         input=f".connect hardcharger://localhost:{server.port}?username={server.username}\n")
    assert "unknown parameter(s) username (username: --username)" in old.stderr, old.stderr
    assert "Connected to" not in old.stdout


# ---- Added: TLS is the default, with or without a URI (1.x's client was plaintext) ----


def plain_http_hint(host: str, port: int) -> str:
    return (f"Error: The server at {host}:{port} speaks plain HTTP, not TLS. Add --no-tls (or --tls=false; in a "
            "hardcharger:// URI ?tls=false; after .connect HOST PORT USERNAME, --no-tls) to connect without "
            "encryption.\n")


class RecordingProxy:
    """Forwards 127.0.0.1:<port> to a server and keeps every byte the client sends."""

    def __init__(self, target_port: int):
        import socket
        import threading
        self.target_port = target_port
        self.sent: list[bytes] = []  # per client connection, everything it sent
        self.lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import socket
        import threading
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self.target_port))
            with self.lock:
                index = len(self.sent)
                self.sent.append(b"")

            def pump(src, dst, record):
                try:
                    while data := src.recv(65536):
                        if record:
                            with self.lock:
                                self.sent[index] += data
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for end in (src, dst):
                        try:
                            end.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            threading.Thread(target=pump, args=(client, upstream, True), daemon=True).start()
            threading.Thread(target=pump, args=(upstream, client, False), daemon=True).start()

    def close(self):
        self.sock.close()


@pytest.fixture
def proxy(server):
    recording = RecordingProxy(target_port=server.port)
    yield recording
    recording.close()


def test_the_default_sends_a_client_hello(first_bytes):
    result = subprocess.run(args=[binary(), "--host", "127.0.0.1", "--port", str(first_bytes.port), "--username", "joe",
                                  "-c", "SELECT 1"], env=client_env(HARDCHARGER_PASSWORD="never-sent-in-plaintext"),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout
    assert first_bytes.received, "the client never connected"
    for data in first_bytes.received:
        assert data[:1] == b"\x16", f"not a TLS handshake: {data[:40]!r}"
        assert b"HTTP/" not in data and b"never-sent-in-plaintext" not in data, data[:200]


@pytest.mark.parametrize("form", ["flags", "uri"])
def test_the_default_against_a_plain_server_says_so_and_never_sends_the_password(server, proxy, form):
    target = (["--host", "127.0.0.1", "--port", str(proxy.port)] if form == "flags"
              else [f"hardcharger://127.0.0.1:{proxy.port}"])
    result = subprocess.run(args=[binary(), *target, "--username", server.username, "-c", "SELECT 1"],
                            env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout
    assert result.stderr == plain_http_hint(host="127.0.0.1", port=proxy.port), result.stderr
    # On the wire: TLS ClientHellos only (the handshake, then the probe that tells HTTP from
    # other TLS failures). No HTTP request, no password, no fallback to plaintext.
    assert proxy.sent, "the client never connected"
    for data in proxy.sent:
        assert data[:1] == b"\x16", f"not a TLS handshake: {data[:40]!r}"
        assert b"HTTP/" not in data, data[:200]
        assert server.password.encode() not in data


def test_tls_false_is_plain_http(server, proxy):
    result = subprocess.run(args=[binary(), "--host", "127.0.0.1", "--port", str(proxy.port), "--tls=false",
                                  "--username", server.username, "--csv", "--no-header", "-c", "SELECT 'plain'"],
                            env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "plain", result.stderr
    assert proxy.sent and proxy.sent[0].startswith(b"GET /warp HTTP/1.1\r\n"), proxy.sent[:1]


def test_hardcharger_tls_false_in_the_environment(server, first_bytes):
    env = client_env(server=server, HARDCHARGER_TLS="false")
    ok = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(server.port), "--username",
                              server.username, "--csv", "--no-header", "-c", "SELECT 'env'"],
                        env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and ok.stdout.strip() == "env", ok.stderr
    # --tls on the command line wins over the variable: a ClientHello, never plaintext.
    flag = subprocess.run(args=[binary(), "--host", "127.0.0.1", "--port", str(first_bytes.port), "--tls",
                                "--username", "joe", "-c", "SELECT 1"], env=env, capture_output=True, text=True,
                          timeout=60)
    assert flag.returncode == 1
    assert first_bytes.received and first_bytes.received[0][:1] == b"\x16", first_bytes.received[:1]


def test_tls_takes_true_or_false():
    result = subprocess.run(args=[binary(), "--tls=maybe", "-c", "SELECT 1"], env=client_env(), capture_output=True,
                            text=True)
    assert result.returncode == 1
    assert result.stderr.startswith(
        "Error: the argument ('maybe') for option '--tls' is invalid: want true or false\n"), result.stderr


def test_help_says_tls_is_the_default():
    result = subprocess.run(args=[binary(), "--help"], env=client_env(), capture_output=True, text=True)
    assert result.returncode == 0
    help_text = " ".join(result.stdout.split())
    assert ("--tls[=BOOL] TLS (HTTPS), on by default; --no-tls (or --tls=false, --tls false) for plain HTTP"
            in help_text), result.stdout
    # How to connect without TLS is among the first lines, before the options.
    head = result.stdout.splitlines()[:6]
    assert any(line.strip().startswith("hardcharger --no-tls") for line in head), head


def test_the_environment_never_changes_a_uri(server):
    # HARDCHARGER_TLS=true and HARDCHARGER_HOST must not change what a URI names.
    env = client_env(server=server, HARDCHARGER_TLS="true", HARDCHARGER_HOST="elsewhere.invalid", HARDCHARGER_PORT="9")
    ok = subprocess.run(args=[binary(), f"hardcharger://localhost:{server.port}?tls=false", "--username",
                              server.username, "--csv", "--no-header", "-c", "SELECT 'env'"],
                        env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "env", ok.stderr
    # Its twin: with flags, the environment applies (HARDCHARGER_HOST sends it elsewhere).
    flags = subprocess.run(args=[binary(), "--username", server.username, "-c", "SELECT 1"], env=env,
                           capture_output=True, text=True, timeout=60)
    assert flags.returncode == 1 and "elsewhere.invalid" in flags.stderr, flags.stderr


# ---- Added: the server's browser sign-in (--auth-type external) ----


@pytest.fixture
def idp():
    from mock_idp import MockIdP
    provider = MockIdP(audience="hardcharger-oauth-test", client_id="test-client-id", client_secret="test-client-secret")
    yield provider
    provider.stop()


def fake_browser(tmp_path: pathlib.Path) -> pathlib.Path | str:
    """A "browser" that signs in at once: it opens the authorization URL, and the mock
    identity provider approves and redirects it to the server's callback, as a provider
    does once the user has signed in."""
    script = tmp_path / "browser.py"
    script.write_text("import sys, urllib.request\nurllib.request.urlopen(sys.argv[1], timeout=30).read()\n")
    if sys.platform == "win32":
        # There HARDCHARGER_BROWSER is a command line; the client appends the URL.
        return f'"{sys.executable}" -I "{script}"'
    launcher = tmp_path / "browser.sh"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} \"$1\"\n")
    launcher.chmod(0o755)
    return launcher


def test_external_auth_signs_in_through_the_browser(launcher, idp, tmp_path):
    port = free_port()
    server = launcher.start(args=["--token-allowed-issuer", idp.issuer, "--token-allowed-audience", idp.audience,
                                  "--token-default-role", "admin", "--token-authorized-emails", "*@gizmodata.com",
                                  "--oauth-client-id", idp.client_id, "--oauth-client-secret", idp.client_secret,
                                  "--oauth-base-url", f"http://127.0.0.1:{port}", "--oauth-provider-name", "Mock IdP"],
                            port=port)
    args = [binary(), "--host", "127.0.0.1", "--port", str(server.port), "--tls=false", "--auth-type", "external",
            "--csv", "--no-header", "-c", "SELECT HARDCHARGER_USER()"]
    env = client_env(HARDCHARGER_BROWSER=str(fake_browser(tmp_path=tmp_path)))
    idp.deny = False
    result = subprocess.run(args=args, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "testuser@gizmodata.com"
    assert "Waiting for the browser sign-in with Mock IdP" in result.stderr
    # The negative twin: the identity provider refuses the sign-in.
    idp.deny = True
    try:
        refused = subprocess.run(args=args, env=env, capture_output=True, text=True, timeout=60)
    finally:
        idp.deny = False
    assert refused.returncode == 1
    assert "Browser sign-in failed (access_denied)" in refused.stderr, refused.stderr


def test_external_auth_needs_the_capability(launcher, tmp_path):
    server = launcher.start()
    args = [binary(), "--host", "127.0.0.1", "--port", str(server.port), "--tls=false", "--auth-type", "external",
            "-c", "SELECT 1"]
    result = subprocess.run(args=args, env=client_env(HARDCHARGER_BROWSER=str(fake_browser(tmp_path=tmp_path))),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "does not offer browser sign-in (capability oauth_browser)" in result.stderr, result.stderr


def test_the_old_oauth_port_option_is_gone():
    result = subprocess.run(args=[binary(), "--oauth-port", "31339", "-c", "SELECT 1"], env=client_env(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "oauth-port" in result.stderr, result.stderr


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


# ---- Added: connecting without TLS, and saying why a connection failed ----


@pytest.mark.parametrize("spelling", [["--no-tls"], ["--tls=false"], ["--tls", "false"], ["--tls", "0"],
                                      ["--tls", "no"], ["--tls", "off"], ["--tls", "OFF"], ["--tls=no"]])
def test_every_spelling_of_tls_off_reaches_a_plain_server(server, spelling):
    result = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(server.port), *spelling,
                                  "--username", server.username, "--csv", "--no-header", "-c", "SELECT 'plain'"],
                            env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "plain", (spelling, result.stderr)


@pytest.mark.parametrize("spelling", [["--tls"], ["--tls", "true"], ["--tls", "1"], ["--tls", "yes"], ["--tls", "on"],
                                      ["--tls=on"]])
def test_every_spelling_of_tls_on_means_tls(server, spelling):
    # The twin: these mean TLS, which the plain-HTTP server can't answer (so the value after
    # the space was read as the option's, not as a URI).
    result = subprocess.run(args=[binary(), "--host", "localhost", "--port", str(server.port), *spelling,
                                  "--username", server.username, "-c", "SELECT 1"],
                            env=client_env(server=server), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert result.stderr == plain_http_hint(host="localhost", port=server.port), (spelling, result.stderr)


def test_tls_takes_only_boolean_words_after_a_space(server):
    # Anything else after --tls stays the positional argument (the URI), and says so.
    result = subprocess.run(args=[binary(), "--tls", "maybe", "-c", "SELECT 1"], env=client_env(),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert result.stderr == "Error: --uri cannot be combined with --tls (the argument 'maybe' is read as the URI)\n", \
        result.stderr
    twice = subprocess.run(args=[binary(), "--tls", "--no-tls", "-c", "SELECT 1"], env=client_env(),
                           capture_output=True, text=True, timeout=60)
    assert twice.returncode == 1
    assert twice.stderr.startswith("Error: option '--tls' cannot be specified more than once\n"), twice.stderr
    valued = subprocess.run(args=[binary(), "--no-tls=true", "-c", "SELECT 1"], env=client_env(),
                            capture_output=True, text=True, timeout=60)
    assert valued.returncode == 1
    assert valued.stderr.startswith("Error: option '--no-tls' takes no value\n"), valued.stderr


def test_nothing_listening_says_why():
    port = free_port()
    for spelling in (["--tls=false"], ["--no-tls"]):
        result = subprocess.run(args=[binary(), *spelling, "--port", str(port), "-c", "SELECT 1"], env=client_env(),
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 1
        assert result.stderr == (f"Error: Could not connect to http://localhost:{port}: connection refused. "
                                 "Is the server running on that port?\n"), result.stderr
    tls = subprocess.run(args=[binary(), "--port", str(port), "-c", "SELECT 1"], env=client_env(),
                         capture_output=True, text=True, timeout=60)
    assert tls.returncode == 1
    assert tls.stderr == (f"Error: Could not connect to https://localhost:{port}: connection refused. "
                          "Is the server running on that port?\n"), tls.stderr


def test_a_script_without_connection_settings_connects_when_it_needs_the_server():
    # No host, port, TLS or user anywhere: the first statement connects with the defaults
    # (https://localhost:31337), and its error is the connection's, never just "not
    # connected". (What answers there differs by machine; whatever it is, the reason is said.)
    result = subprocess.run(args=[binary(), "-c", "SELECT 1"], env=client_env(), capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 1
    assert "not connected" not in result.stderr, result.stderr
    assert result.stderr.startswith("Error: ") and "localhost:31337" in result.stderr, result.stderr
    # A script that needs no server still runs without one.
    offline = subprocess.run(args=[binary(), "-c", ".help connect"], env=client_env(), capture_output=True,
                             text=True, timeout=60)
    assert offline.returncode == 0 and ".connect" in offline.stdout, offline.stderr


def test_dot_connect_host_port_takes_tls_off_and_on(server):
    for tls_off in ("--no-tls", "--tls=false", "--tls false"):
        script = (f".connect localhost {server.port} {server.username} {server.password} {tls_off}\n"
                  "SELECT 'dot connect' AS val;\n")
        ok = subprocess.run(args=[binary(), "-q", "--csv", "--no-header"], env=client_env(), input=script,
                            capture_output=True, text=True, timeout=60)
        assert ok.returncode == 0, (tls_off, ok.stderr)
        assert ok.stdout.strip().endswith("dot connect"), (tls_off, ok.stdout)
        # The flag may come before the user, too.
        early = subprocess.run(args=[binary(), "-q", "--csv", "--no-header"], env=client_env(),
                               input=f".connect localhost {server.port} {tls_off} {server.username} "
                                     f"{server.password}\nSELECT 'early';\n",
                               capture_output=True, text=True, timeout=60)
        assert early.returncode == 0 and early.stdout.strip().endswith("early"), (tls_off, early.stderr)
    # The twins: TLS (the default, or --tls) against the plain server fails, with the hint.
    for tls_on in ("", "--tls"):
        script = f".connect localhost {server.port} {server.username} {server.password} {tls_on}\nSELECT 1;\n"
        bad = subprocess.run(args=[binary(), "-q"], env=client_env(), input=script, capture_output=True, text=True,
                             timeout=60)
        assert bad.returncode == 1
        assert bad.stderr.startswith(plain_http_hint(host="localhost", port=server.port)), (tls_on, bad.stderr)
    # A URI says it itself.
    uri = subprocess.run(args=[binary(), "-q"], env=client_env(),
                         input=f".connect hardcharger://localhost:{server.port} {server.username} --no-tls\n",
                         capture_output=True, text=True, timeout=60)
    assert uri.returncode == 1
    assert uri.stderr.startswith("Error: a URI says whether to use TLS itself (?tls=false): drop --no-tls\n"), \
        uri.stderr
    bad_value = subprocess.run(args=[binary(), "-q"], env=client_env(),
                               input=f".connect localhost {server.port} {server.username} --tls=maybe\n",
                               capture_output=True, text=True, timeout=60)
    assert bad_value.returncode == 1
    assert bad_value.stderr.startswith("Error: --tls=maybe: want --tls=true or --tls=false\n"), bad_value.stderr


def test_dot_help_and_the_not_connected_hint_say_how_to_turn_tls_off(server):
    result = run(server, "--command", ".help connect")
    assert result.returncode == 0, result.stderr
    assert "HOST PORT USER --no-tls: without TLS" in " ".join(result.stdout.split()), result.stdout
    usage = subprocess.run(args=[binary(), "-q"], env=client_env(), input=".connect\n", capture_output=True,
                           text=True, timeout=60)
    assert ".connect HOST PORT USERNAME [PASSWORD] [--no-tls | --tls]" in usage.stderr, usage.stderr
