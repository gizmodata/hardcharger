"""DuckDB's parser is never handed text it cannot take safely, however the text reaches it.

The server checks every text DuckDB parses, in DuckDB's parser_override hook (parse_guard.hpp),
before DuckDB's own parser sees it. It refuses:

- Parentheses opened and never closed. Such a text cannot parse, and DuckDB 2.0's parser takes
  time quadratic in their number to say so: 8 KB of them held a session for 17 seconds and
  500 KB for hours, one core each, with neither the statement timeout nor a cancel able to stop
  it.
- Brackets nested deeper than max_sql_nesting, more than four times that many tables in one
  FROM clause, or a dotted name of more parts. DuckDB's parser recurses once per level and
  table, and its binder takes time quadratic in a name's parts (20,000 parts: 45 s).

The hook sees a statement, a request of several, a prepare, the init SQL, the SQL query() and
query_table() are given (a literal or computed), json_serialize_sql(), json_serialize_plan(),
SQL parsed on DuckDB's own worker threads, and IMPORT DATABASE's load.sql. No session can
switch the hook off (allow_parser_override_extension) or raise max_expression_depth past the
limit.

The threads that run statements have 8 MB stacks on every platform (the Chargewire SDK), so
the limit is DuckDB's own default of 1000 everywhere: on macOS, where a thread used to get 512
KB, statements DuckDB accepts by default stopped the server process.
"""

import pathlib
import time

import pytest

from chargewire_client import TERMINAL
from conftest import TIMEOUT_SCALE

# The reviewer's two inputs: 8 KB took 17 s, 500 KB did not come back.
SIZES = [8_000, 500_000]
# What a refusal may take: it is one pass over the text. (Generous for a busy machine.)
REFUSAL_SECONDS = 2.0 * TIMEOUT_SCALE
LIMIT = 1000  # max_sql_nesting's default: what the statement threads' 8 MB stacks take


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


def timed_scalar(session, sql: str):
    started = time.monotonic()
    value = session.scalar(sql=sql)
    return value, time.monotonic() - started


def nesting_limit(session) -> int:
    return int(session.scalar(sql="SELECT current_setting('max_expression_depth')"))


def never_closed(count: int, position: int = 8) -> str:
    noun = "parenthesis is" if count == 1 else "parentheses are"
    return f'Parser Error: syntax error: {count} opening {noun} never closed (the first "(" at position {position})'


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
    assert response.status_code == 400 and response.json()["detail"] == never_closed(count=count), response.text
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # The twin: a prepare of a statement with parentheses that close.
    response = user.http.post(url=f"{user.url}/prepared", headers=user.headers(), json={"sql": "SELECT ((1 + 2))"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("sql", [
    # query() with SQL given as a literal, a dollar-quoted literal, and computed.
    "SELECT * FROM query('SELECT " + "(" * 8_000 + "')",
    "SELECT * FROM query($q$SELECT " + "(" * 8_000 + "$q$)",
    "SELECT * FROM query('SELECT ' || repeat('(', 8000))",
    "SELECT * FROM (SELECT * FROM QUERY ( 'SELECT ' || repeat('(', 8000) ))",
])
def test_unclosed_parentheses_in_the_sql_given_to_query_are_refused(server, sql):
    # DuckDB parses that SQL when it binds the call, through the same hook. (query() is the
    # administrator's under access control.) Computed, it took 17 s before.
    admin = server.admin()
    error, took = outcome(session=admin, sql=sql)
    assert error is not None and never_closed(count=8_000) in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # The twin: query() with SQL that parses runs.
    assert admin.scalar(sql="SELECT * FROM query('SELECT ((40) + (2))')") == 42
    assert admin.scalar(sql="SELECT * FROM query('SELECT ' || repeat('(', 3) || '42' || repeat(')', 3))") == 42


@pytest.mark.parametrize("function", ["json_serialize_sql", "json_serialize_plan"])
def test_sql_a_function_parses_is_refused_for_any_user(server, user, function):
    # json_serialize_sql() is anyone's: 2,000 parentheses built by repeat() took 0.95 s, 8,000
    # took 35 s. Its result carries DuckDB's error (the function does not throw).
    session = user if function == "json_serialize_sql" else server.admin()
    answer, took = timed_scalar(session=session, sql=f"SELECT {function}('SELECT ' || repeat('(', 8000))::JSON->>'error_message'")
    assert answer == never_closed(count=8_000).removeprefix("Parser Error: "), answer
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    answer, _ = timed_scalar(session=session, sql=f"SELECT {function}('SELECT ' || repeat('[', 3000) || '1' || repeat(']', 3000))::JSON->>'error_message'")
    assert answer == f"the statement's brackets nest 3000 deep, and this server takes {LIMIT} (its max_sql_nesting option)", answer
    # The twin: SQL that parses serializes.
    assert session.scalar(sql=f"SELECT {function}('SELECT ((1)) + [2][1]')::JSON->>'error'") == "false"


def test_sql_parsed_on_duckdbs_worker_threads_is_refused(user):
    # A column of SQL over a parallel scan: DuckDB parses it on its own worker threads (512 KB
    # stacks on macOS), where 8,000 nested subqueries stopped the server.
    sql = ("SELECT max(json_serialize_sql(CASE WHEN i % 100000 = 7 THEN 'SELECT * FROM ' || repeat('(SELECT * FROM ', 8000) "
           "|| '(SELECT 1)' || repeat(')', 8000) ELSE 'SELECT 1' END)::JSON->>'error_message') FROM range(3000000) t(i)")
    answer, took = timed_scalar(session=user, sql=sql)
    assert answer == f"the statement's brackets nest 8001 deep, and this server takes {LIMIT} (its max_sql_nesting option)", answer
    assert took < 10 * TIMEOUT_SCALE
    assert user.scalar(sql="SELECT 1") == 1


def test_import_database_reads_its_load_sql_through_the_same_check(server, tmp_path):
    # DuckDB's own IMPORT DATABASE parsed load.sql with its built-in parser, which no hook sees
    # (4,000 unclosed parentheses: 4 s, quadratic).
    admin = server.admin()
    refused = tmp_path / "refused"
    refused.mkdir()
    (refused / "schema.sql").write_text("")
    (refused / "load.sql").write_text("SELECT " + "(" * 8_000)
    error, took = outcome(session=admin, sql=f"IMPORT DATABASE '{refused}'")
    assert error is not None and never_closed(count=8_000) in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # A load.sql statement that is no COPY is an error (DuckDB checked that only in debug builds).
    other = tmp_path / "other"
    other.mkdir()
    (other / "schema.sql").write_text("")
    (other / "load.sql").write_text("SELECT 1;")
    error, _ = outcome(session=admin, sql=f"IMPORT DATABASE '{other}'")
    assert error is not None and "holds a statement that is not a COPY" in error, error
    # The twin: an export imports.
    exported = tmp_path / "exported"
    admin.execute(sql="CREATE TABLE import_twin AS SELECT 'a (b' AS s, 41 AS n")
    admin.execute(sql=f"EXPORT DATABASE '{exported}'")
    admin.execute(sql="DROP TABLE import_twin")
    admin.execute(sql=f"IMPORT DATABASE '{exported}'")
    assert admin.rows(sql="SELECT s, n + 1 AS n FROM import_twin") == [{"s": "a (b", "n": 42}]
    admin.execute(sql="DROP TABLE import_twin")


@pytest.mark.parametrize("count", SIZES)
def test_init_sql_with_unclosed_parentheses_stops_the_server_at_once(launcher, count):
    started = time.monotonic()
    failed = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE t (i INTEGER);\nSELECT " + "(" * count},
                            expect_up=False)
    assert failed.process.wait(timeout=30 * TIMEOUT_SCALE) == 1
    assert time.monotonic() - started < 10 * TIMEOUT_SCALE, "the server did not stop at once"
    log = failed.log()
    assert f"init SQL failed: {never_closed(count=count)}" in log, log[-600:]


def test_the_refusal_says_how_many_and_where_and_is_recorded(server, user):
    error, _ = outcome(session=user, sql="SELECT abs(1), ((2 + 3)")
    assert error == never_closed(count=1, position=16), error
    # In a request of several, the position is in the statement that has them.
    error, _ = outcome(session=user, sql="SELECT 1; SELECT ((2 + (3)")
    assert error == never_closed(count=2, position=8), error
    admin = server.admin()
    deadline = time.monotonic() + 10 * TIMEOUT_SCALE
    while True:
        rows = admin.query(sql="SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements "
                               "WHERE prepare_error LIKE '%never closed%'").to_pylist()
        if len(rows) >= 2 or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert {"sql_text": "SELECT abs(1), ((2 + 3)", "prepare_error": never_closed(count=1, position=16)} in rows, rows


@pytest.mark.parametrize("sql,expected", [
    # Brackets that are no tokens: in a literal, a dollar-quoted literal, a quoted name, a comment.
    ("SELECT '((((' AS a, $$((($$ AS b, $q$(($q$ AS c, 1 AS \"(((\" -- (((\n /* ((( */", ["((((", "(((", "((", 1]),
    ("SELECT E'\\'((' AS a, length('(') AS b", ["'((", 1]),
    ("SELECT ']]]' AS a, '{{{' AS b, \"[[\" FROM (SELECT 1 AS \"[[\") /* [[[ {{{ */", ["]]]", "{{{", 1]),
    ("SELECT $tag$ ((( [[[ {{{ ' \" $tag$ AS a, `((` FROM (SELECT 2 AS `((`)", [" ((( [[[ {{{ ' \" ", 2]),
    ("SELECT 'a.b.c.d.e.f' AS a, \"x.y\" FROM (SELECT 3 AS \"x.y\") -- a.b.c.d", ["a.b.c.d.e.f", 3]),
    ("SELECT ((1 + (2 * 3))) AS a, (SELECT (4)) AS b", [7, 4]),
])
def test_brackets_in_literals_names_and_comments_are_not_counted(user, sql, expected):
    assert list(user.query(sql=sql).to_pylist()[0].values()) == expected


@pytest.mark.parametrize("tail,expected", [
    # DuckDB's tokenizer refuses these at once, with the right reason: theirs to answer.
    (" 'never closed", "unterminated string literal"),
    (' "never closed', "unterminated quoted identifier"),
    # (The server's statement splitter ends this statement before the comment: the text DuckDB
    # is handed is the parentheses alone, refused here.)
    (" /* never closed", never_closed(count=8_000)),
    # This one DuckDB hands on to its parser (17.8 s with 8,000 parentheses): refused here.
    (" $q$ never closed", never_closed(count=8_000)),
])
def test_unclosed_parentheses_before_an_unterminated_token(user, tail, expected):
    error, took = outcome(session=user, sql="SELECT " + "(" * 8_000 + tail)
    assert error is not None and expected in error, error
    assert took < REFUSAL_SECONDS, f"the answer took {took:.2f} s"


def test_one_closing_parenthesis_too_many_is_duckdbs_to_refuse(user):
    error, took = outcome(session=user, sql="SELECT 1" + ")" * 8000)
    assert error is not None and "syntax error at or near" in error, error
    assert took < REFUSAL_SECONDS


# ---- Nesting, tables, names: deeper than the limit -----------------------------------------

def test_the_nesting_limit_is_what_the_statement_threads_stacks_take(user):
    # 8 MB on every platform (the Chargewire SDK starts them so): DuckDB's own default.
    assert nesting_limit(session=user) == LIMIT


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
    # (8,000 nested subqueries stop a server whatever DuckDB's setting: its parser recurses
    # before its own check.)
    error, took = outcome(session=user, sql=DEEP[shape](depth))
    assert error is not None and f"brackets nest {depth + (1 if shape == 'from-subqueries' else 0)} deep" in error
    assert f"this server takes {LIMIT} (its max_sql_nesting option)" in error, error
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
    # (20,000 tables in one FROM clause stopped the server: the parser recurses once per table.)
    error, took = outcome(session=user, sql=FROM_CLAUSES[shape](tables))
    assert error is not None and f"names {tables} tables, and this server takes {4 * LIMIT} in one" in error, error
    assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


def test_a_dotted_name_of_more_parts_than_the_limit_is_refused_at_once(user):
    # DuckDB's binder takes time quadratic in a name's parts: 20,000 of them took 45 s, and no
    # statement timeout interrupts a bind.
    for parts in (1_001, 20_000, 200_000):
        error, took = outcome(session=user, sql="SELECT a" + ".b" * (parts - 1))
        assert error == f"Parser Error: a name in the statement has {parts} dotted parts, and this server takes {LIMIT} " \
                        "(its max_sql_nesting option)", error
        assert took < REFUSAL_SECONDS, f"the refusal took {took:.2f} s"
    # The twins: a name the limit takes is DuckDB's to resolve, quickly.
    error, took = outcome(session=user, sql="SELECT a" + ".b" * 999)
    assert error is not None and "Binder Error" in error, error
    assert took < 5 * TIMEOUT_SCALE
    assert user.scalar(sql="SELECT s.a.b.c FROM (SELECT {b: {c: 42}} AS a) s") == 42


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
    if shape == "joins" and depth < LIMIT:
        # (A 990-way join binds, and DuckDB's join-order optimizer then takes minutes, which no
        # statement timeout interrupts: a planning cost, not a parse or a stack one. The joins
        # DuckDB's limit refuses are tested here; a 300-way join below.)
        depth = LIMIT + 100
    error, took = outcome(session=user, sql=CHAINS[shape](depth))
    if depth > LIMIT:
        assert error is not None and ("Max expression depth limit of 1000 exceeded" in error
                                      or f"names {depth} tables" in error), error
    assert took < 30 * TIMEOUT_SCALE
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


# The shapes that stopped the server process on macOS (512 KB statement threads) under DuckDB's
# default limit, before the SDK gave statement threads 8 MB: each runs, or is refused cleanly.
UNDER_THE_LIMIT = {
    "a 900-term concatenation": (CHAINS["concatenation"](900), None),
    "460 nested lists": (DEEP["lists"](460), None),
    "990 nested function calls": (DEEP["function-calls"](990), None),
    "990 nested CASE": (CHAINS["case"](990), None),
    "240 nested FROM subqueries": (DEEP["from-subqueries"](240), None),
    "300 nested scalar subqueries": (DEEP["scalar-subqueries"](300), "Max expression depth limit of 1000 exceeded"),
    "a 300-table join": (CHAINS["joins"](300), None),
}


@pytest.mark.parametrize("label", sorted(UNDER_THE_LIMIT))
def test_deep_statements_the_limit_takes_run_on_the_statement_threads(server, user, label):
    sql, refusal = UNDER_THE_LIMIT[label]
    error, _ = outcome(session=user, sql=sql)
    if refusal is None:
        assert error is None, error
    else:
        assert error is not None and refusal in error, error
    assert server.admin().scalar(sql="SELECT 1") == 1, "the server stopped"


@pytest.mark.parametrize("shape", sorted(DEEP) + sorted(CHAINS))
def test_ordinary_deep_nesting_still_runs(user, shape):
    build = DEEP.get(shape) or CHAINS[shape]
    error, _ = outcome(session=user, sql=build(30))
    assert error is None, error


def test_realistic_deep_but_valid_statements_run(user):
    case = "SELECT CASE " + " ".join(f"WHEN i = {k} THEN 'v{k}'" for k in range(100)) + " ELSE 'other' END FROM range(1) t(i)"
    assert user.scalar(sql=case) == "v0"
    union = "SELECT sum(n) FROM (" + " UNION ALL ".join(f"SELECT {k} AS n" for k in range(50)) + ")"
    assert user.scalar(sql=union) == sum(range(50))
    nested = "SELECT * FROM " + "(SELECT * FROM " * 30 + "(SELECT 42 AS answer)" + " AS s)" * 30
    assert user.scalar(sql=nested) == 42
    window = ("SELECT max(r) FROM (SELECT rank() OVER (PARTITION BY i % 3 ORDER BY (i * (i + (1)))) AS r "
              "FROM range(10) t(i) WHERE (i IN (1, 2, (3)) OR [i][1] > {'a': 4}['a']))")
    assert user.scalar(sql=window) == 3


def test_wide_statements_are_not_nesting(user):
    assert user.scalar(sql="SELECT count(*) FROM (VALUES " + "(1)," * 50_000 + "(1))") == 50_001
    assert user.scalar(sql="SELECT 1 WHERE 1 IN (" + "1," * 50_000 + "1)") == 1
    assert user.scalar(sql="SELECT " + "true OR " * 20_000 + "true") is True
    assert user.scalar(sql="SELECT sum(i) FROM (SELECT 1 AS i " + "UNION ALL SELECT 1 " * 2_000 + ")") == 2_001
    assert user.scalar(sql="SELECT " + ", ".join(f"({k})" for k in range(5_000)) + ", 'last'") == 0


# ---- No session switches the check off --------------------------------------------------

HOOK_OFF = [
    "SET allow_parser_override_extension = 'default'",
    "SET SESSION allow_parser_override_extension = 'default'",
    "SET allow_parser_override_extension TO 'strict'",
    "SET Allow_Parser_Override_Extension = 'default'",
    "SET \"allow_parser_override_extension\" = 'default'",
    "PRAGMA allow_parser_override_extension = 'default'",
    "RESET allow_parser_override_extension",
    "RESET SESSION allow_parser_override_extension",
    "EXPLAIN ANALYZE SET allow_parser_override_extension = 'default'",
    "SELECT 1; SET allow_parser_override_extension = 'default'",
]


def test_no_session_switches_the_parser_check_off(server, user):
    admin = server.admin()
    refusal = "allow_parser_override_extension cannot be changed on this server"
    for session in (user, admin):
        for sql in HOOK_OFF + (["SET GLOBAL allow_parser_override_extension = 'default'",
                                "RESET GLOBAL allow_parser_override_extension"] if session is admin else []):
            error, _ = outcome(session=session, sql=sql)
            assert error is not None and refusal in error, (sql, error)
        response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                     json={"sql": "SET allow_parser_override_extension = 'default'"})
        assert response.status_code == 400 and refusal in response.json()["detail"], response.text
        assert session.scalar(sql="SELECT current_setting('allow_parser_override_extension')") == "fallback"
        error, took = outcome(session=session, sql="SELECT " + "(" * 8_000)
        assert error == never_closed(count=8_000) and took < REFUSAL_SECONDS, error
    # A user's SET GLOBAL is the administrator's to make: refused before the check is asked.
    error, _ = outcome(session=user, sql="SET GLOBAL allow_parser_override_extension = 'default'")
    assert error is not None and "requires the 'admin' role" in error, error
    # The twins: a variable of that name is no setting, and reading the setting is anyone's.
    assert outcome(session=user, sql="SET VARIABLE allow_parser_override_extension = 'default'")[0] is None
    assert user.scalar(sql="SELECT getvariable('allow_parser_override_extension')") == "default"


def test_no_session_raises_duckdbs_limit_past_the_servers(server, user):
    admin = server.admin()
    refusal = f"max_expression_depth can be set to a number up to {LIMIT} on this server"
    for session in (user, admin):
        for sql in ("SET max_expression_depth TO 10000000", f"SET max_expression_depth = {LIMIT + 1}",
                    "SET SESSION max_expression_depth = 5000", "PRAGMA max_expression_depth = 5000",
                    "SET max_expression_depth = 1000 * 1000", "SET max_expression_depth = NULL",
                    "SELECT 1; SET max_expression_depth TO 5000"):
            error, _ = outcome(session=session, sql=sql)
            assert error is not None and refusal in error and "max_sql_nesting" in error, (sql, error)
        response = session.http.post(url=f"{session.url}/prepared", headers=session.headers(),
                                     json={"sql": "SET max_expression_depth TO 5000"})
        assert response.status_code == 400 and "max_sql_nesting" in response.text, response.text
        assert nesting_limit(session=session) == LIMIT
    error, _ = outcome(session=admin, sql="SET GLOBAL max_expression_depth = 5000")
    assert error is not None and refusal in error, error
    error, _ = outcome(session=user, sql="SET GLOBAL max_expression_depth = 5000")
    assert error is not None and "requires the 'admin' role" in error, error
    # The twin: lowering it, and resetting it, are the session's own business.
    assert outcome(session=user, sql="SET max_expression_depth TO 20")[0] is None
    assert nesting_limit(session=user) == 20
    assert outcome(session=user, sql="RESET max_expression_depth")[0] is None
    assert nesting_limit(session=user) == LIMIT


def test_max_sql_nesting_sets_the_limit(launcher):
    running = launcher.start(args=["--max-sql-nesting", "40"])
    admin = running.admin()
    assert nesting_limit(session=admin) == 40
    assert admin.scalar(sql=DEEP["parentheses"](39)) == 1
    error, _ = outcome(session=admin, sql=DEEP["function-calls"](41))
    assert error == "Parser Error: the statement's brackets nest 41 deep, and this server takes 40 (its max_sql_nesting option)", error
    error, _ = outcome(session=admin, sql="SET max_expression_depth = 41")
    assert error is not None and "up to 40 on this server" in error, error
    # Below DuckDB's default, resetting the instance's value would raise it: refused too.
    error, _ = outcome(session=admin, sql="RESET GLOBAL max_expression_depth")
    assert error is not None and "up to 40 on this server" in error, error
    error, _ = outcome(session=admin, sql="SELECT a" + ".b" * 40)
    assert error == "Parser Error: a name in the statement has 41 dotted parts, and this server takes 40 (its max_sql_nesting option)", error
    rows = admin.query(sql="SELECT value, cli_flag FROM hardcharger_settings() "
                           "WHERE name = 'hardcharger.max_sql_nesting'").to_pylist()
    assert rows == [{"value": "40", "cli_flag": "--max-sql-nesting"}], rows


def test_an_init_sql_that_changes_either_setting_does_not_outlast_startup(launcher):
    running = launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": "SET GLOBAL max_expression_depth = 100000; "
                                                                   "SET GLOBAL allow_parser_override_extension = 'default'"})
    admin = running.admin()
    assert nesting_limit(session=admin) == LIMIT
    assert admin.scalar(sql="SELECT current_setting('allow_parser_override_extension')") == "fallback"
    error, _ = outcome(session=admin, sql=DEEP["from-subqueries"](8_000))
    assert error is not None and "brackets nest" in error, error
    assert admin.scalar(sql="SELECT 1") == 1


def test_the_init_sql_runs_on_a_thread_with_the_statement_threads_stack(launcher, tmp_path):
    # The init SQL ran on the main thread (1 MB on Windows) or an embedding's (512 KB on macOS):
    # deep statements there now get the statement threads' 8 MB.
    init = tmp_path / "init.sql"
    init.write_text("CREATE TABLE deep AS " + CHAINS["concatenation"](900) + " AS s;\n"
                    "CREATE TABLE nested AS SELECT * FROM " + "(SELECT * FROM " * 240 + "(SELECT 1 AS one)" + ")" * 240 + ";\n")
    running = launcher.start(args=["--init-sql-commands-file", str(pathlib.Path(init))])
    admin = running.admin()
    assert admin.scalar(sql="SELECT length(s) FROM deep") == 901
    assert admin.scalar(sql="SELECT one FROM nested") == 1
