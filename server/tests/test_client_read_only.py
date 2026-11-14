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
