"""Regression tests for credential redaction in logs, instrumentation and the catalog
logs. A security review found that comments, DuckDB parse errors, ATTACH connection
strings and credential settings all got clear text past the redactor.

Each case plants a unique marker and asserts it appears in none of the durable sinks:
the server's log output, _hardcharger_instr.sql_statements / .sql_executions, the
_hardcharger_logs catalog, and (for a store user) the access-control audit record. Each has
a positive twin: an ordinary statement is still logged in full.
"""

import time
import uuid

import pytest


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(
        args=["--enable-instrumentation", "--enable-catalog-logging", "--print-queries", "true",
              "--log-format", "json"])


@pytest.fixture
def admin(server):
    return server.admin()


def marker() -> str:
    return "MK" + uuid.uuid4().hex[:12]


def drain(admin) -> None:
    # Let the instrumentation and catalog-log writer threads flush.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if admin.scalar(sql="SELECT count(*) FROM _hardcharger_instr.sql_statements") >= 0:
            time.sleep(0.4)
            return


def sinks(server, admin) -> str:
    """Everything a marker must not appear in."""
    drain(admin=admin)
    rows = admin.query(sql="SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements").to_pylist()
    execs = admin.query(sql="SELECT error_message FROM _hardcharger_instr.sql_executions").to_pylist()
    logs = admin.query(sql="SELECT message, fields::VARCHAR AS f FROM _hardcharger_logs.main.logs").to_pylist()
    return server.log() + repr(rows) + repr(execs) + repr(logs)


def assert_hidden(server, admin, marker_value: str):
    text = sinks(server=server, admin=admin)
    assert marker_value not in text, f"marker leaked: {marker_value}"


def assert_logged(server, admin, marker_value: str):
    # A positive twin: an ordinary statement with the marker is kept in full somewhere.
    admin.execute(sql=f"SELECT '{marker_value}' AS ordinary")
    assert marker_value in sinks(server=server, admin=admin), "an ordinary statement was redacted"


# ---- 1. comments hiding the keyword ----

@pytest.mark.parametrize("template", [
    "CREATE /* x */ SECRET {s} (TYPE s3, KEY_ID 'k', SECRET '{m}')",
    "CREATE -- x\n SECRET {s} (TYPE s3, KEY_ID 'k', SECRET '{m}')",
    "CREATE OR/**/REPLACE SECRET {s} (TYPE s3, KEY_ID 'k', SECRET '{m}')",
    "CREATE /**/ USER {s} WITH PASSWORD '{m}'",
    "alter /* c */ user {s} WITH PASSWORD '{m}'",
    "ALTER--x\nUSER {s} WITH PASSWORD '{m}'",
])
def test_comments_do_not_hide_credential_ddl(server, admin, template):
    m = marker()
    sql = template.format(s="u_" + m.lower(), m=m)
    try:
        admin.execute(sql=sql)
    except Exception:
        pass  # whether it runs or errors, the marker must not be logged
    assert_hidden(server=server, admin=admin, marker_value=m)


def test_comment_hidden_ddl_through_prepared(server, admin):
    m = marker()
    try:
        admin.prepare(sql=f"CREATE /**/ SECRET s_{m.lower()} (TYPE s3, KEY_ID 'k', SECRET '{m}')")
    except Exception:
        pass
    assert_hidden(server=server, admin=admin, marker_value=m)


# ---- 2. DuckDB parse errors echoing the statement ----

@pytest.mark.parametrize("sql_template", [
    "CREATE SECRET s_{m} (TYPE s3, KEY_ID 'k', SECRET '{m}' BOGUS)",
    "CREATE SECRET s_{m} (TYPE s3, KEY_ID 'k', SECRET '{m}",
])
def test_parse_errors_of_credential_ddl_are_redacted(server, admin, sql_template):
    # The error goes back to its author (who typed the secret); it must not reach the
    # durable sinks an administrator reads.
    m = marker()
    admin.error(sql=sql_template.format(m=m))
    assert_hidden(server=server, admin=admin, marker_value=m)


def test_parse_error_under_access_control(launcher):
    running = launcher.start(args=["--enable-instrumentation", "--enable-catalog-logging",
                                   "--print-queries", "true", "--log-format", "json"])
    admin = running.admin()
    user = running.as_role(role="user", subject="bob", catalog_access=[{"catalog": "*", "access": "write"}])
    m = marker()
    user.error(sql=f"SELECT 1; CREATE USER bob_{m.lower()} WITH PASSWORD '{m}'")
    assert_hidden(server=running, admin=admin, marker_value=m)


# ---- 3. ATTACH connection strings ----

@pytest.mark.parametrize("target", [
    "ducklake:postgres:dbname=x user=u password={m} host=127.0.0.1 port=1",
    "postgres:dbname=x user=u password={m} host=127.0.0.1 port=1",
])
def test_attach_connection_strings_are_redacted(server, admin, target):
    m = marker()
    options = " (TYPE postgres)" if target.startswith("postgres:") else ""
    admin.error(sql=f"ATTACH '{target.format(m=m)}' AS dl_{m.lower()}{options}")
    assert_hidden(server=server, admin=admin, marker_value=m)


# ---- 4. credential settings ----

@pytest.mark.parametrize("setting", ["s3_secret_access_key", "http_proxy_password"])
def test_credential_settings_are_redacted(server, admin, setting):
    m = marker()
    try:
        admin.execute(sql=f"SET GLOBAL {setting} = '{m}'")
    except Exception:
        pass
    assert_hidden(server=server, admin=admin, marker_value=m)


# ---- positive twin ----

def test_ordinary_statements_are_logged_in_full(server, admin):
    m = marker()
    assert_logged(server=server, admin=admin, marker_value=m)


# ---- audit chain: a record is redacted before it is sealed ----

def test_audit_record_is_redacted_before_sealing(launcher, store, tmp_path):
    """A store user's refused credential statement is written to the audit chain. The
    marker must be gone AND the chain must still verify: redaction happens before the
    record is sealed, not after (which would break the hash)."""
    import subprocess

    from conftest import BINARY
    from rbac import start_rbac_server

    key = "redaction-audit-key"
    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_AUDIT_CHAIN_KEY": key,
                                    "HARDCHARGER_PRINT_QUERIES": "false"})
    admin = server.admin()
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql="CREATE USER cleo WITH PASSWORD 'cleo-pass-1'")
    admin.run(sql="GRANT readers TO cleo")
    cleo = server.user(name="cleo", password="cleo-pass-1")
    m = marker()
    try:
        cleo.run(sql=f"CREATE /* hide */ SECRET s_{m.lower()} (TYPE s3, KEY_ID 'k', SECRET '{m}')")
    except Exception:
        pass
    log = server.log()
    assert '"kind":"access_control"' in log and '"status":"denied"' in log, "no audit record written"
    assert m not in log, "the marker reached the audit log"
    path = tmp_path / "audit.log"
    path.write_text(log)
    out = subprocess.run(args=[str(BINARY), "--verify-audit-log", str(path)],
                         capture_output=True, text=True, env={"PATH": "/usr/bin", "HARDCHARGER_AUDIT_CHAIN_KEY": key})
    assert out.returncode == 0, f"chain did not verify after redaction:\n{out.stdout}"
    assert "intact" in out.stdout, out.stdout


# ---- credential values in ordinary statements: every sink of statement text ----
#
# A statement that is not credential DDL can still give a credential name a value (a named
# argument, an ATTACH option, a struct key). Each test below sends such a statement with a
# marker as the value and a tag (an alias, a name) as text that must survive, and reads the
# sinks: the tag is there (the statement was recorded, so a test cannot pass on silence)
# and the marker is not.

import json  # noqa: E402
import threading  # noqa: E402

from conftest import TIMEOUT_SCALE  # noqa: E402

# A macro with a credential-named parameter: `=>` and `:=` then run without an extension.
PROBE = "CREATE OR REPLACE MACRO probe_m(a, token := 'd') AS a"

# (statement, whether it runs). {m}: the marker; {t}: the tag.
SPELLINGS = [
    ("SELECT probe_m(1, token => '{m}') AS {t}", True),
    ("SELECT probe_m(1, token := '{m}') AS {t}", True),
    ("SELECT probe_m(1, TOKEN=>'{m}') AS {t}", True),
    ("SELECT probe_m(1, TOKEN:='{m}') AS {t}", True),
    ("SELECT probe_m(1, token /* c */ => '{m}') AS {t}", True),
    ("SELECT probe_m(1, token => /* c */ '{m}') AS {t}", True),
    ("SELECT probe_m(1, token =>\n-- c\n'{m}') AS {t}", True),
    ("SELECT probe_m(1, token\n=>\n'{m}') AS {t}", True),
    ("SELECT probe_m(1, token => $${m}$$) AS {t}", True),
    ("SELECT probe_m(1, token => $q${m}$q$) AS {t}", True),
    ("SELECT probe_m(1, token => E'{m}') AS {t}", True),
    ("SELECT probe_m(1, token => 'a''{m}') AS {t}", True),
    ("SELECT probe_m(1, token => 'a' || '{m}') AS {t}", True),
    ("SELECT probe_m(1, \"token\" => '{m}') AS {t}", True),
    ("SELECT 1; SELECT probe_m(1, token => '{m}') AS {t}", True),
    ("SELECT probe_m(1, `token` => '{m}') AS {t}", True),
    ("SELECT probe_m(1, token\u00a0=>\u00a0'{m}') AS {t}", True),  # no-break spaces
    ("SELECT probe_m(1, -- c\r token => '{m}') AS {t}", True),      # a line comment ended by a carriage return
    ("SELECT 1 AS {t} /* was: probe_m(1, token => '{m}') */", True),  # a statement commented out
    # `name = value` in a call that does not exist: the statement fails, and is recorded
    # all the same.
    ("SELECT {t}(token /* c */ = '{m}')", False),
    ("SELECT {t}(token =\n-- c\n'{m}')", False),
    ("SELECT {t}(token = E'{m}')", False),
    ("SELECT {t}(token = 'a''{m}')", False),
    ("SELECT {t}(token = 'a' || '{m}')", False),
    ("SELECT {t}(\"token\" = '{m}')", False),
    ("SELECT struct_pack(password := '{m}') AS {t}", True),
    ("SELECT {{password: '{m}'}} AS {t}", True),
    ("SELECT struct_pack(api_key := '{m}', account_key := '{m}', passphrase := '{m}', private_key := '{m}') AS {t}", True),
    ("SET VARIABLE {t}_api_key = '{m}'", True),
    ("ATTACH '/no-such-dir/{t}.duckdb' AS {t} (ENCRYPTION_KEY '{m}')", False),
    ("ATTACH '/no-such-dir/{t}.duckdb' AS {t} (TOKEN '{m}')", False),
    ("ATTACH '/no-such-dir/{t}.duckdb' AS {t} (ENCRYPTION_KEY {m})", False),  # a bare word as the value
    ("attach '/no-such-dir/{t}.duckdb' as {t} (password'{m}')", False),
    ("SELECT 1; ATTACH '/no-such-dir/{t}.duckdb' AS {t} (TOKEN '{m}')", False),
    ("PRAGMA add_parquet_key('k', '{m}') /* {t} */", False),
    ("ATTACH 'dbname={t} user=u password=''{m} {m}'' host=127.0.0.1 port=1' AS {t} (TYPE postgres)", False),
    # The value begins before the log's 4000-byte cut and ends after it.
    ("SELECT '" + "x" * 3800 + "' AS {t}, probe_m(1, token => '" + "{m}" * 30 + "')", True),
]


@pytest.fixture(scope="module")
def probe_server(server):
    server.admin().execute(sql=PROBE)
    return server


def tagged() -> str:
    return "tag_" + uuid.uuid4().hex[:10]


def wait_for(read, timeout: float = 10):
    """`read()`'s first true value, within `timeout` (scaled as every wait is)."""
    deadline = time.monotonic() + timeout * TIMEOUT_SCALE
    while True:
        value = read()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.05)


def records_with(server, tag: str) -> list[dict]:
    """The JSON log records that carry the tag."""
    return [record for record in server.log_records() if tag in json.dumps(obj=record)]


def rows_with(admin, sql: str, column: str, tag: str) -> list[dict]:
    # The tag is looked for here, not in the query: a query that named it would be a
    # statement carrying the tag itself, in every sink.
    return [row for row in admin.query(sql=sql).to_pylist() if tag in row[column]]


# What each sink holds of the statement tagged `tag`.
SINKS = {
    "the log": lambda server, admin, tag: records_with(server=server, tag=tag),
    "sql_statements": lambda server, admin, tag: rows_with(
        admin=admin, sql="SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements", column="sql_text", tag=tag),
    "the catalog log": lambda server, admin, tag: rows_with(
        admin=admin, sql="SELECT message, fields::VARCHAR AS fields FROM _hardcharger_logs.main.logs "
                         "WHERE message IN ('statement', 'prepare')", column="fields", tag=tag),
    "hardcharger_live_queries()": lambda server, admin, tag: rows_with(
        admin=admin, sql="SELECT sql_text, error_message FROM hardcharger_live_queries()", column="sql_text", tag=tag),
}


def everything(server, admin) -> dict[str, str]:
    """All of every sink, for the marker's absence."""
    return {
        "the log": server.log(),
        "sql_statements": repr(admin.query(sql="SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements").to_pylist()),
        "sql_executions": repr(admin.query(sql="SELECT error_message, bind_parameters FROM _hardcharger_instr.sql_executions").to_pylist()),
        "the catalog log": repr(admin.query(sql="SELECT message, fields::VARCHAR AS f FROM _hardcharger_logs.main.logs").to_pylist()),
        "hardcharger_live_queries()": repr(admin.query(sql="SELECT sql_text, error_message FROM hardcharger_live_queries()").to_pylist()),
    }


def assert_recorded_without(server, admin, tag: str, marker_value: str, sinks=tuple(SINKS)):
    for name in sinks:
        rows = wait_for(read=lambda: SINKS[name](server, admin, tag))
        assert rows, f"{name} has no record of the statement tagged {tag}"
        assert marker_value not in json.dumps(obj=rows), f"the marker reached {name}: {rows}"
    for name, text in everything(server=server, admin=admin).items():
        assert marker_value not in text, f"the marker reached {name}"


@pytest.mark.parametrize("template,runs", SPELLINGS)
def test_credential_value_spellings_are_masked_in_every_sink(probe_server, admin, template, runs):
    m, tag = marker(), tagged()
    sql = template.format(m=m, t=tag)
    if runs:
        admin.execute(sql=sql)  # the twin: it really ran
    else:
        admin.error(sql=sql)
    assert_recorded_without(server=probe_server, admin=admin, tag=tag, marker_value=m)
    # The log line is the statement's, with its other text and the mask.
    logged = [record for record in records_with(server=probe_server, tag=tag) if record["message"] == "statement"]
    assert logged and "***" in logged[0]["sql"], logged


def test_succeeded_and_failed_log_lines_mask_the_value(probe_server, admin):
    m, tag = marker(), tagged()
    admin.execute(sql=f"SELECT probe_m(1, token => '{m}') AS {tag}")
    admin.error(sql=f"SELECT {tag}_nofn(token => '{m}')")
    messages = wait_for(read=lambda: len(found := records_with(server=probe_server, tag=tag)) >= 4 and found)
    assert messages and sorted(record["message"] for record in messages) == [
        "Client SQL command execution succeeded", "statement", "statement", "statement failed"], messages
    assert all("token => ***" in record["sql"] for record in messages), messages
    assert m not in probe_server.log()


def test_prepare_log_line_and_record_mask_the_value(probe_server, admin):
    m, tag = marker(), tagged()
    prepared = admin.prepare(sql=f"SELECT probe_m(1, token => '{m}') AS {tag}")
    assert admin.execute_prepared(prepared_id=prepared["prepared_id"]).status_code == 200
    assert_recorded_without(server=probe_server, admin=admin, tag=tag, marker_value=m,
                            sinks=("the log", "sql_statements", "the catalog log"))
    records = records_with(server=probe_server, tag=tag)
    assert [record["message"] for record in records] == ["prepare"] and "token => ***" in records[0]["sql"], records


def test_error_quoting_the_value_is_masked(probe_server, admin):
    # DuckDB's parser names the token it stopped at, here a second literal of the value.
    m, tag = marker(), tagged()
    problem = admin.error(sql=f"SELECT probe_m(1, token => '{m}' '{m}b') AS {tag}")
    assert m in problem["detail"], problem  # the twin: the error does quote it (to its author)
    assert_recorded_without(server=probe_server, admin=admin, tag=tag, marker_value=m, sinks=("sql_statements",))
    row = SINKS["sql_statements"](probe_server, admin, tag)[0]
    assert "syntax error" in row["prepare_error"], row


def test_unreadable_statement_is_recorded_redacted_whole(probe_server, admin):
    m = marker()
    unreadable = ("SELECT sql_text, prepare_error FROM _hardcharger_instr.sql_statements "
                  "WHERE sql_text = '[REDACTED: a statement that could not be read]'")
    before = admin.query(sql=unreadable).num_rows
    problem = admin.error(sql=f"SELECT probe_m(1, token => '{m}")  # the literal never ends
    assert "unterminated" in problem["detail"], problem
    rows = wait_for(read=lambda: admin.query(sql=unreadable).to_pylist()[before:])
    assert rows == [{"sql_text": "[REDACTED: a statement that could not be read]",
                     "prepare_error": "[REDACTED: error of a statement that could not be read]"}], rows
    for name, text in everything(server=probe_server, admin=admin).items():
        assert m not in text, f"the marker reached {name}"


def test_running_statement_is_masked_in_active_connections(probe_server, admin):
    m, tag = marker(), tagged()
    worker = probe_server.admin()
    # Runs until it is cancelled: the test holds it for as long as it reads the view.
    slow = f"SELECT count(*), {{'password': '{m}'}} AS {tag} FROM range(10000000000) a, range(1000) b"
    thread = threading.Thread(target=lambda: worker.submit(sql=slow, timeout=300), daemon=True)
    thread.start()
    try:
        rows = wait_for(read=lambda: rows_with(
            admin=admin, sql="SELECT statement FROM hardcharger_active_connections() "
                             f"WHERE session_id = '{worker.id}' AND statement IS NOT NULL", column="statement", tag=tag),
            timeout=30)
        assert rows == [{"statement": f"SELECT count(*), {{'password': ***}} AS {tag} FROM range(10000000000) a, range(1000) b"}]
    finally:
        assert admin.scalar(sql=f"SELECT hardcharger_cancel('{worker.id}')") is True
        thread.join(timeout=60 * TIMEOUT_SCALE)
    assert m not in probe_server.log()


def test_text_log_format_masks_the_value(launcher):
    running = launcher.start(args=["--print-queries", "true"])
    admin = running.admin()
    m, tag = marker(), tagged()
    admin.execute(sql=f"SELECT struct_pack(token := '{m}') AS {tag}")
    lines = wait_for(read=lambda: len(found := [line for line in running.log().splitlines() if tag in line]) >= 2 and found)
    assert lines and len(lines) == 2, lines
    assert all(line.endswith(f"sql=SELECT struct_pack(token := ***) AS {tag}") for line in lines), lines
    assert m not in running.log()


@pytest.mark.parametrize("statement", [
    "ATTACH '/no-such-dir/{t}.duckdb' AS a (TOKEN '{m}' BOGUS)",
    "SELECT {t}(token => '{m}')",
    "SELECT 1; SELECT CAST(struct_pack(password := '{m}') AS {t})",
])
def test_init_sql_failure_line_masks_the_value(launcher, statement):
    # A failing init statement stops the server with DuckDB's error, which quotes the line.
    m, tag = marker(), tagged()
    failed = launcher.start(args=["--init-sql-commands", statement.format(m=m, t=tag)], expect_up=False)
    assert failed.process.wait(timeout=60 * TIMEOUT_SCALE) != 0
    log = failed.log()
    assert "init SQL failed" in log, log  # the twin: this is the failure's line
    assert m not in log, log


def test_user_ddl_error_and_audit_record_under_access_control(launcher, store):
    """Under access control: a mistyped CREATE / ALTER USER's error names the token it
    stopped at (the password, when it is the one mistyped), and a refused statement's text
    goes into the audit record."""
    from rbac import start_rbac_server

    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "true",
                                    "HARDCHARGER_ENABLE_INSTRUMENTATION": "true"})
    admin = server.admin()
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql="CREATE USER cleo WITH PASSWORD 'cleo-pass-1'")
    admin.run(sql="GRANT readers TO cleo")
    admin.run(sql="CREATE TABLE guarded (i INTEGER)")
    m = marker()
    mistyped = [f"CREATE USER dana PASSWORD {m}",          # the quotes forgotten
                f"ALTER USER cleo PASSWORD {m}",
                f"CREATE USER dana PASSWORD 'x' '{m}'"]   # a stray second literal
    for sql in mistyped:
        with pytest.raises(Exception) as refused:
            admin.run(sql=sql)
        assert m in str(refused.value), refused.value  # the twin: the error does name it (to its author)
    cleo = server.user(name="cleo", password="cleo-pass-1")
    tag = tagged()
    with pytest.raises(Exception, match="Hardcharger blocked"):
        cleo.run(sql=f"SELECT i, {{'token': '{m}'}} AS {tag} FROM guarded")
    failed = wait_for(read=lambda: len(found := [line for line in server.log().splitlines()
                                                 if '"statement failed"' in line and "[REDACTED: user DDL]" in line])
                      >= len(mistyped) and found)
    assert failed and len(failed) == len(mistyped), failed
    audit = [line for line in server.log().splitlines() if '"kind":"access_control"' in line and tag in line]
    assert len(audit) == 1 and '"status":"denied"' in audit[0] and "{'token': ***}" in audit[0], audit
    assert m not in server.log(), "the marker reached the log"
    errors = wait_for(read=lambda: len(found := admin.run(
        sql="SELECT error_message FROM _hardcharger_instr.sql_executions WHERE error_message IS NOT NULL").to_pylist())
        >= len(mistyped) + 1 and found)
    assert errors and m not in repr(errors), errors
    statements = admin.run(sql="SELECT sql_text FROM _hardcharger_instr.sql_statements").to_pylist()
    assert m not in repr(statements) and tag in repr(statements), statements
