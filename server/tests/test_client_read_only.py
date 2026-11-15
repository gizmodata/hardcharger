"""CLI connection-mode options against the feature-enabled server."""

import pytest

from test_client_cli import client_env, run


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
