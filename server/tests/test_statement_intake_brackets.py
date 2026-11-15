"""The statement intake refuses what DuckDB's parser cannot take, before the parser sees it.

Two things a signed-in user could send (a review of 2.13.0 found the first; testing its fix
found the second):

- Parentheses opened and never closed. Such a statement cannot parse, and DuckDB 2.0's parser
  takes time quadratic in their number to say so: 8 KB of them held a session for 17 seconds
  and 500 KB for hours, one core each, with neither the statement timeout nor a cancel able
  to stop it.
- Nesting deeper than the stack of the thread that runs the statement takes. DuckDB recurses
  once per level; its own limit (max_expression_depth, 1000) assumes an 8 MB stack and a
  session could raise it. On macOS, where a thread gets 512 KB, a concatenation of 900 terms,
  a 300-table join or 460 nested lists stopped the server process, and anywhere a session
  that raised the limit could.

Each way statement text enters is covered: a statement, a request of several, a prepare,
the init SQL, and the SQL given to query() as a literal.
"""

import time

import pytest

from chargewire_client import TERMINAL
from conftest import TIMEOUT_SCALE

# The reviewer's two inputs: 8 KB took 17 s, 500 KB did not come back.
SIZES = [8_000, 500_000]
# What a refusal may take: it is one pass over the text. (Milliseconds; generous for a busy machine.)
REFUSAL_SECONDS = 2.0 * TIMEOUT_SCALE


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation", "--log-format", "json"])


@pytest.fixture
def user(server):
    # Not an administrator: any signed-in user could do it.
    return server.as_role(role="user", subject="mallory", catalog_access=[{"catalog": "*", "access": "write"}])


def outcome(session, sql: str, parameters=None) -> tuple[str | None, float]:
    """The statement's error (None when it succeeded) and how long the answer took."""
    started = time.monotonic()
    response = session.submit(sql=sql, parameters=parameters)
    if response.status_code in (200, 201):
        document = response.json()
        if document["state"] not in TERMINAL:
            document = session.wait_done(statement_id=document["statement_id"])
        error = None if document["state"] == "succeeded" else (document.get("error") or {}).get("detail", str(document))
    else:
        error = response.json().get("detail", response.text)
    return error, time.monotonic() - started


def nesting_limit(session) -> int:
    return int(session.scalar(sql="SELECT current_setting('max_expression_depth')"))


# ---- Parentheses never closed ----------------------------------------------------------

@pytest.mark.parametrize("count", SIZES)
@pytest.mark.parametrize("shape", ["statement", "second-of-several", "after-valid-text", "function-calls", "subqueries"])
def test_unclosed_parentheses_are_refused_at_once(server, user, count, shape):
    sql = {
        "statement": "SELECT " + "(" * count,
        "second-of-several": "SELECT 1; SELECT " + "(" * count + "; SELECT 2",
        "after-valid-text": "SELECT (1 + 2), 'x' FROM range(3) WHERE " + "(" * count + "1",
        "function-calls": "SELECT " + "abs(" * (count // 4) + "1",
        "subqueries": "SELECT * FROM " + "(SELECT * FROM " * (count // 15) + "t",
    }[shape]
    error, took = outcome(session=user, sql=sql)
    assert error is not None and "Parser Error: syntax error: " in error and "never closed" in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # The session is free at once, and the server answers others.
    assert user.scalar(sql="SELECT 42") == 42
    assert server.admin().scalar(sql="SELECT 43") == 43


@pytest.mark.parametrize("count", SIZES)
def test_unclosed_parentheses_are_refused_when_prepared(user, count):
    started = time.monotonic()
    response = user.http.post(url=f"{user.url}/prepared", headers=user.headers(), json={"sql": "SELECT " + "(" * count})
    took = time.monotonic() - started
    assert response.status_code != 201 and "never closed" in response.text, response.text
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"


@pytest.mark.parametrize("count", SIZES)
def test_unclosed_parentheses_in_the_sql_given_to_query_are_refused(server, count):
    # DuckDB parses the literal when it binds the call: the same parser, the same time.
    admin = server.admin()
    for sql in ("SELECT * FROM query('SELECT " + "(" * count + "')", "SELECT * FROM query($q$SELECT " + "(" * count + "$q$)",
                "SELECT * FROM (SELECT * FROM QUERY ( 'SELECT 1 WHERE ''x'' = " + "(" * count + "'))"):
        error, took = outcome(session=admin, sql=sql)
        assert error is not None and "never closed (in the SQL given to query()" in error, error
        assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # The twin: query() with SQL that parses runs.
    assert admin.scalar(sql="SELECT * FROM query('SELECT ((40) + (2))')") == 42


@pytest.mark.parametrize("count", SIZES)
def test_init_sql_with_unclosed_parentheses_stops_the_server_at_once(launcher, count):
    started = time.monotonic()
    failed = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE t (i INTEGER);\nSELECT " + "(" * count},
                            expect_up=False)
    assert failed.process.wait(timeout=30 * TIMEOUT_SCALE) == 1
    assert time.monotonic() - started < 10 * TIMEOUT_SCALE, "the server did not stop at once"
    log = failed.log()
    assert f"init SQL failed: Parser Error: syntax error: {count} opening parentheses are never closed" in log, log[-600:]


def test_the_refusal_says_how_many_and_where_and_is_recorded(server, user):
    error, _ = outcome(session=user, sql="SELECT abs(1), ((2 + 3)")
    assert error == 'Parser Error: syntax error: 1 opening parenthesis is never closed (the first "(" at position 16)', error
    error, _ = outcome(session=user, sql="SELECT 1; SELECT ((2 + (3)")
    assert error == 'Parser Error: syntax error: 2 opening parentheses are never closed (the first "(" at position 18)', error
    admin = server.admin()
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while True:
        rows = admin.query(sql="SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements "
                               "WHERE prepare_error LIKE '%never closed%'").to_pylist()
        if len(rows) >= 2 or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert {"sql_text": "SELECT abs(1), ((2 + 3)",
            "prepare_error": 'Parser Error: syntax error: 1 opening parenthesis is never closed (the first "(" at position 16)'} in rows, rows


@pytest.mark.parametrize("sql,expected", [
    # Parentheses that are no tokens: in a literal, a dollar-quoted literal, a quoted name, a comment.
    ("SELECT '((((' AS a, $$((($$ AS b, $q$(($q$ AS c, 1 AS \"(((\" -- (((\n /* ((( */", ["((((", "(((", "((", 1]),
    ("SELECT E'\\'((' AS a, length('(') AS b", ["'((", 1]),
    ("SELECT ((1 + (2 * 3))) AS a, (SELECT (4)) AS b", [7, 4]),
])
def test_parentheses_in_literals_names_and_comments_are_not_counted(user, sql, expected):
    assert list(user.query(sql=sql).to_pylist()[0].values()) == expected


def test_one_closing_parenthesis_too_many_is_duckdbs_to_refuse(user):
    error, took = outcome(session=user, sql="SELECT 1" + ")" * 8000)
    assert error is not None and "syntax error at or near" in error, error
    assert took < REFUSAL_SECONDS


# ---- Nesting deeper than the stack takes -------------------------------------------------

def test_the_nesting_limit_is_what_the_statement_threads_stacks_take(user):
    limit = nesting_limit(session=user)
    # 4 KB of stack a level, at most DuckDB's own default: 1000 with Linux's 8 MB, 128 with macOS's 512 KB.
    assert 32 <= limit <= 1000, limit


DEEP = {
    "parentheses": lambda n: "SELECT " + "(" * n + "1" + ")" * n,
    "lists": lambda n: "SELECT " + "[" * n + "1" + "]" * n + " IS NULL",
    "structs": lambda n: "SELECT " + "{a:" * n + "1" + "}" * n + " IS NULL",
    "function-calls": lambda n: "SELECT " + "abs(" * n + "1" + ")" * n,
    "from-subqueries": lambda n: "SELECT * FROM " + "(SELECT * FROM " * n + "(SELECT 1)" + ")" * n,
    "scalar-subqueries": lambda n: "SELECT " + "(SELECT " * n + "1" + ")" * n,
}
# Nesting without a bracket: DuckDB's own limit, which the server sets, refuses these.
CHAINS = {
    "concatenation": lambda n: "SELECT " + "'a' || " * n + "'a'",
    "addition": lambda n: "SELECT " + "1 + " * n + "1",
    "casts": lambda n: "SELECT 1" + "::INT" * n,
    "case": lambda n: "SELECT " + "CASE WHEN true THEN " * n + "1" + " END" * n,
    "not": lambda n: "SELECT " + "NOT " * n + "true",
    "joins": lambda n: "SELECT 1 FROM (SELECT 1 AS i) t0 " + " ".join(f"JOIN (SELECT 1 AS i) t{k} USING (i)" for k in range(1, n)),
}


@pytest.mark.parametrize("shape", sorted(DEEP))
@pytest.mark.parametrize("depth", [3_000, 8_000, 50_000])
def test_brackets_nested_deeper_than_the_limit_are_refused_at_once(server, user, shape, depth):
    # (3,000 nested lists stopped a server whose session had raised max_expression_depth; 8,000
    # nested subqueries stop one whatever the setting: DuckDB's parser recurses before its check.)
    error, took = outcome(session=user, sql=DEEP[shape](depth))
    assert error is not None and "brackets nest" in error and "max_sql_nesting" in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


FROM_CLAUSES = {
    "joined-subqueries": lambda n: "SELECT 1 FROM (SELECT 1 AS i) t0 " + " ".join(f"JOIN (SELECT 1 AS i) t{k} USING (i)" for k in range(1, n)),
    "joined-tables": lambda n: "SELECT 1 FROM t0 " + " ".join(f"JOIN t{k} ON true" for k in range(1, n)),
    "comma-list": lambda n: "SELECT 1 FROM " + ", ".join(f"t{k}" for k in range(n)),
    "in-a-subquery": lambda n: "SELECT * FROM (SELECT 1 FROM " + ", ".join(f"t{k}" for k in range(n)) + ") s",
}


@pytest.mark.parametrize("shape", sorted(FROM_CLAUSES))
@pytest.mark.parametrize("tables", [5_000, 20_000])
def test_a_from_clause_with_thousands_of_tables_is_refused_at_once(server, user, shape, tables):
    # (20,000 tables in one FROM clause stopped the server: the parser recurses once per table,
    # before DuckDB's own limit refuses the join. No such statement binds.)
    error, took = outcome(session=user, sql=FROM_CLAUSES[shape](tables))
    assert error is not None and f"names {tables} tables" in error and "max_sql_nesting" in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


def test_wide_lists_that_are_no_from_clause_are_not_counted_as_tables(user):
    # The twins: thousands of commas after a FROM that is no clause, or outside one.
    assert user.scalar(sql="SELECT 1 IS DISTINCT FROM 2, " + ", ".join("1" for _ in range(6_000))) is True
    assert user.scalar(sql="SELECT count(*) FROM range(3) WHERE range IN (" + ", ".join(str(k) for k in range(6_000)) + ")") == 3
    assert user.scalar(sql="SELECT count(*) FROM range(3) ORDER BY " + ", ".join("1" for _ in range(6_000))) == 3
    assert user.scalar(sql="SELECT extract(year FROM DATE '2026-01-01'), " + ", ".join("1" for _ in range(6_000))) == 2026
    assert user.scalar(sql="SELECT count(*) FROM range(2) a JOIN range(2) b ON true JOIN range(2) c ON true, range(2) d") == 16


@pytest.mark.parametrize("shape", sorted(CHAINS))
@pytest.mark.parametrize("depth", [990, 5_000])
def test_nesting_without_brackets_is_refused_by_duckdbs_limit_not_by_a_crash(server, user, shape, depth):
    # (Each of these with 900 to 990 levels stopped the server on macOS, under DuckDB's default limit.)
    limit = nesting_limit(session=user)
    error, took = outcome(session=user, sql=CHAINS[shape](depth))
    if depth > limit:
        assert error is not None and ("Max expression depth limit" in error or "names" in error), error
    assert took < 10 * TIMEOUT_SCALE
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


@pytest.mark.parametrize("shape", sorted(DEEP) + sorted(CHAINS))
def test_ordinary_deep_nesting_still_runs(user, shape):
    # Twenty levels of anything, on every platform: more than statements people or tools write.
    # (DuckDB counts a nested subquery as several levels: about 29 of them fit the limit of 128
    # a macOS build has; with Linux's 1000, over 200.)
    build = DEEP.get(shape) or CHAINS[shape]
    error, _ = outcome(session=user, sql=build(20))
    assert error is None, error


def test_wide_statements_are_not_nesting(user):
    assert user.scalar(sql="SELECT count(*) FROM (VALUES " + "(1)," * 50_000 + "(1))") == 50_001
    assert user.scalar(sql="SELECT 1 WHERE 1 IN (" + "1," * 50_000 + "1)") == 1
    assert user.scalar(sql="SELECT " + "true OR " * 20_000 + "true") is True
    assert user.scalar(sql="SELECT sum(i) FROM (SELECT 1 AS i " + "UNION ALL SELECT 1 " * 2_000 + ")") == 2_001
    assert user.scalar(sql="SELECT " + ", ".join(f"({k})" for k in range(5_000)) + ", 'last'") == 0


def test_no_session_raises_duckdbs_limit_past_the_servers(server, user):
    limit = nesting_limit(session=user)
    admin = server.admin()
    for session in (user, admin):
        for sql in ("SET max_expression_depth TO 10000000", f"SET max_expression_depth = {limit + 1}",
                    "SET SESSION max_expression_depth = 5000", "SET GLOBAL max_expression_depth = 5000",
                    "PRAGMA max_expression_depth = 5000", "SET max_expression_depth = 1000 * 1000",
                    "SELECT 1; SET max_expression_depth TO 5000"):
            error, _ = outcome(session=session, sql=sql)
            assert error is not None and f"up to {limit} on this server" in error and "max_sql_nesting" in error, (sql, error)
        response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                     json={"sql": "SET max_expression_depth TO 5000"})
        assert response.status_code != 201 and "max_sql_nesting" in response.text, response.text
        assert nesting_limit(session=session) == limit
    # The twin: lowering it, and resetting it, are the session's own business.
    assert outcome(session=user, sql="SET max_expression_depth TO 20")[0] is None
    assert nesting_limit(session=user) == 20
    assert outcome(session=user, sql="RESET max_expression_depth")[0] is None
    assert nesting_limit(session=user) == limit


def test_max_sql_nesting_sets_the_limit(launcher):
    running = launcher.start(args=["--max-sql-nesting", "40"])
    admin = running.admin()
    assert nesting_limit(session=admin) == 40
    assert admin.scalar(sql=DEEP["parentheses"](39)) == 1
    error, _ = outcome(session=admin, sql=DEEP["function-calls"](41))
    assert error == "Parser Error: the statement's brackets nest 41 deep, and this server takes 40 (its max_sql_nesting option)", error
    error, _ = outcome(session=admin, sql="SET max_expression_depth = 41")
    assert error is not None and "up to 40 on this server" in error, error
    rows = admin.query(sql="SELECT value, cli_flag FROM hardcharger_settings() "
                           "WHERE name = 'hardcharger.max_sql_nesting'").to_pylist()
    assert rows == [{"value": "40", "cli_flag": "--max-sql-nesting"}], rows


def test_an_init_sql_that_raises_duckdbs_limit_does_not_outlast_startup(launcher):
    running = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "SET GLOBAL max_expression_depth = 100000"})
    admin = running.admin()
    assert 32 <= nesting_limit(session=admin) <= 1000
    error, _ = outcome(session=admin, sql=DEEP["from-subqueries"](8_000))
    assert error is not None and "brackets nest" in error, error
    assert admin.scalar(sql="SELECT 1") == 1
