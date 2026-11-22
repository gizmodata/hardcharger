"""DuckDB's autocomplete extension is off unless the server's enable_sql_auto_complete option
turns it on, and then it is for administrators only.

sql_auto_complete(), sql_tokenize() and check_peg_parser() run DuckDB's SQL matcher on their
argument directly (not through the parser, so not through the server's parser check), and
duckdb_format_sql() its formatter. sql_auto_complete's input is unfinished SQL by design, and
the matcher's time grows exponentially with the brackets it leaves open: `SELECT 1 + (` six
times took 1.7 s, eight over 15 s; 16 unclosed parentheses 37 s; sql_tokenize over 120 s with
2,000. duckdb_format_sql's time and result grow with the square of its argument (4,000 nested
subqueries: 3 s and 64 MB). Neither a statement timeout nor a cancel interrupts any of them
(their work is done while DuckDB binds the call), and any signed-in user could call
sql_auto_complete() and duckdb_format_sql(). No cap on the input keeps every shape fast, so
the functions are refused unless the option is on, and to everyone but administrators when it
is. Nothing in Hardcharger's server, command-line client or web UI calls them.
"""

import time

import pytest

from chargewire_client import TERMINAL
from conftest import TIMEOUT_SCALE, installing

CALLS = {
    "sql_auto_complete": "SELECT count(*) FROM sql_auto_complete('SELE')",
    "sql_tokenize": "SELECT count(*) FROM sql_tokenize('SELECT 1')",
    "check_peg_parser": "SELECT count(*) FROM check_peg_parser('SELECT 1')",
    "duckdb_format_sql": "SELECT duckdb_format_sql('select 1')",
}
QUICK = 2.0 * TIMEOUT_SCALE


def outcome(session, sql: str) -> tuple[str | None, float]:
    started = time.monotonic()
    response = session.submit(sql=sql)
    if response.status_code in (200, 201):
        document = response.json()
        if document["state"] not in TERMINAL:
            document = session.wait_done(statement_id=document["statement_id"])
        error = None if document["state"] == "succeeded" else (document.get("error") or {}).get("detail", str(document))
    else:
        error = response.json().get("detail", response.text)
    return error, time.monotonic() - started


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--log-format", "json"])


@pytest.fixture(scope="module")
def enabled(module_launcher, server):
    # Populate the shared cache once under the install lock. An explicit INSTALL
    # reports repository errors, which DuckDB's startup autoload would suppress.
    with installing(extension="autocomplete"):
        server.admin().execute(sql="INSTALL autocomplete")
    # The enabled server loads and guards the cached extension at normal startup.
    return module_launcher.start(args=["--log-format", "json", "--enable-sql-auto-complete"], startup_seconds=180)


def user_of(running):
    return running.as_role(role="user", subject="mallory", catalog_access=[{"catalog": "*", "access": "read"}])


OFF = "the autocomplete extension is turned off on this server"
ADMINISTRATOR_ONLY_BY_ACCESS_CONTROL = "under access control it is available to the administrator only"


@pytest.mark.parametrize("function", sorted(CALLS))
def test_they_are_off_by_default_for_everyone(server, function):
    admin = server.admin()
    error, took = outcome(session=admin, sql=CALLS[function])
    assert error is not None and OFF in error and "enable_sql_auto_complete" in error, error
    assert took < QUICK
    user = user_of(server)
    error, took = outcome(session=user, sql=CALLS[function])
    if function in ("sql_tokenize", "check_peg_parser"):
        # (Under access control these were the administrator's already.)
        assert error is not None and ADMINISTRATOR_ONLY_BY_ACCESS_CONTROL in error, error
    else:
        assert error is not None and OFF in error, error
    assert took < QUICK
    # The input that took a core for minutes: refused at once.
    error, took = outcome(session=user, sql="SELECT count(*) FROM sql_auto_complete('SELECT ' || repeat('1 + (', 8))")
    assert error is not None and OFF in error and took < QUICK, error


def test_the_extension_cannot_be_loaded_when_off(server):
    error, _ = outcome(session=server.admin(), sql="LOAD autocomplete")
    assert error is not None and OFF in error, error
    # The twin: other SQL-parsing functions are the parser check's, and run.
    assert server.admin().scalar(sql="SELECT json_serialize_sql('SELECT 1')::JSON->>'error'") == "false"
    rows = server.admin().rows(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_sql_auto_complete'")
    assert rows == [{"value": "false"}], rows


@pytest.mark.parametrize("sql", ["LOAD autocomplete", *CALLS.values()])
def test_init_sql_cannot_use_autocomplete_when_off(launcher, sql):
    failed = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": sql}, expect_up=False)
    assert failed.process.wait(timeout=60 * TIMEOUT_SCALE) == 1
    assert OFF in failed.log(), failed.log()[-800:]


def test_enabled_they_are_for_administrators(enabled):
    admin = enabled.admin()
    suggestions = admin.rows(sql="SELECT suggestion FROM sql_auto_complete('SELE')")
    assert any(r["suggestion"].strip().upper() == "SELECT" for r in suggestions), suggestions
    assert admin.scalar(sql=CALLS["sql_tokenize"]) == 2
    assert admin.scalar(sql=CALLS["check_peg_parser"]) == 0
    assert admin.scalar(sql=CALLS["duckdb_format_sql"]) == "SELECT 1"
    assert admin.scalar(sql="SELECT * FROM query('SELECT count(*) FROM sql_auto_complete(''SELE'')')") >= 1
    rows = admin.rows(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_sql_auto_complete'")
    assert rows == [{"value": "true"}], rows
    # Loaded at startup: loading it again changes nothing.
    assert outcome(session=admin, sql="LOAD autocomplete")[0] is None


@pytest.mark.parametrize("function", sorted(CALLS))
def test_enabled_they_are_refused_to_other_users(enabled, function):
    error, took = outcome(session=user_of(enabled), sql=CALLS[function])
    if function in ("sql_tokenize", "check_peg_parser"):
        assert error is not None and ADMINISTRATOR_ONLY_BY_ACCESS_CONTROL in error, error
    else:
        # (DuckDB adds the call's place in the statement on a line of its own.)
        assert error is not None and error.split("\n")[0] == f"Permission Error: {function}() is for administrators on this server: " + (
            "its time and its result grow with the square of its argument (4,000 nested subqueries: 3 s and 64 MB)"
            if function == "duckdb_format_sql" else
            "it runs DuckDB's SQL matcher on its argument, whose time grows exponentially with the brackets the argument "
            "leaves open") + ", and neither a statement timeout nor a cancel interrupts it", error
    assert took < QUICK


def test_enabled_a_view_an_administrator_made_does_not_open_them_to_a_user(enabled):
    # The check is made when the call is bound, for the session binding it: a view's body too.
    admin = enabled.admin()
    admin.execute(sql="CREATE OR REPLACE VIEW completions AS SELECT * FROM sql_auto_complete('SELE')")
    admin.execute(sql="CREATE OR REPLACE VIEW formatted AS SELECT duckdb_format_sql('select ' || repeat('(', 3)) AS f")
    assert admin.scalar(sql="SELECT count(*) FROM completions") >= 1
    user = user_of(enabled)
    for view, function in (("completions", "sql_auto_complete"), ("formatted", "duckdb_format_sql")):
        error, _ = outcome(session=user, sql=f"SELECT count(*) FROM {view}")
        assert error is not None and f"{function}() is for administrators on this server" in error, error
