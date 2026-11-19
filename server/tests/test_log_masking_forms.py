"""What the statement log masks, form by form (README.md, "What the statement log masks").

One test per form the README lists. Each sends a statement that carries an obviously fake
canary (s3cr3t-CANARY-...) in that form and then reads every place statement text or error
text is kept: the server's log (run at debug level, so debug lines count), the statement and
failure records, every table of the instrumentation catalog (sql_statements, sql_executions
with its error_message and query_profile, and whatever else is there), the catalog log, the
monitoring functions, and under access control the audit record. The canary must be in none
of them, and the statement must be in them (found by a tag that is part of its text, so a
test cannot pass on silence).

Each form has a positive twin: the neighbouring ordinary statement, which must be kept as
written, with its error.

The review these tests come from (2.13.0's masking) found secrets logged because a password
had punctuation in it, because the masker's own lexer read `$` and U+2060 differently from
DuckDB, and because common forms were outside what it recognised; and found ordinary
statements losing their text, errors and profiles. Both directions are tested here.
"""

import json
import secrets
import threading
import time
import unicodedata
import uuid

import pyarrow as pa
import pytest

from chargewire_client import TERMINAL
from conftest import TIMEOUT_SCALE

SERVER_ARGS = ["--enable-instrumentation", "--enable-catalog-logging", "--print-queries", "true", "--log-format", "json",
               "--log-level", "debug", "--query-log-level", "debug", "--auth-log-level", "debug",
               "--session-log-level", "debug"]


@pytest.fixture(scope="module")
def server(module_launcher):
    running = module_launcher.start(args=SERVER_ARGS)
    admin = running.admin()
    admin.execute(sql="CREATE TABLE mask_users (name VARCHAR, password VARCHAR, age INTEGER)")
    admin.execute(sql="INSERT INTO mask_users VALUES ('seed', 'seed', 1)")
    admin.execute(sql="CREATE OR REPLACE MACRO mask_echo(a, token := 'd') AS error(token)")
    admin.execute(sql="CREATE OR REPLACE MACRO mask_probe(a, token := 'd', name := 'n') AS a")
    return running


@pytest.fixture
def admin(server):
    return server.admin()


def canary() -> str:
    return "s3cr3t-CANARY-" + secrets.token_hex(nbytes=5)


def marker() -> str:
    return "kept-MARKER-" + secrets.token_hex(nbytes=5)


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


def instrumentation_tables(admin) -> list[str]:
    rows = admin.query(sql="SELECT table_name FROM duckdb_tables() WHERE database_name = '_hardcharger_instr' "
                           "ORDER BY table_name").to_pylist()
    names = [row["table_name"] for row in rows]
    assert "sql_statements" in names and "sql_executions" in names, names
    return names


def every_sink(server, admin) -> dict[str, str]:
    """All the text every sink holds. (None of these queries carries a canary or a marker.)"""
    sinks = {"the server log": server.log()}
    for table in instrumentation_tables(admin=admin):
        sinks[f"_hardcharger_instr.{table}"] = repr(admin.query(sql=f"SELECT * FROM _hardcharger_instr.main.{table}").to_pylist())
    sinks["the catalog log"] = repr(admin.query(
        sql="SELECT message, fields::VARCHAR AS fields FROM _hardcharger_logs.main.logs").to_pylist())
    for function in ("hardcharger_live_queries", "hardcharger_active_connections", "hardcharger_sessions"):
        sinks[f"{function}()"] = repr(admin.query(sql=f"SELECT * FROM {function}()").to_pylist())
    return sinks


def statement_records(server, tag: str) -> list[dict]:
    return [record for record in server.log_records() if tag in json.dumps(obj=record)]


def stored(admin, tag: str) -> list[dict]:
    """The statement tagged `tag` in sql_statements, with its executions' errors."""
    rows = admin.query(sql="SELECT s.sql_text, s.prepare_error, e.status, e.error_message "
                           "FROM _hardcharger_instr.sql_statements s "
                           "LEFT JOIN _hardcharger_instr.sql_executions e USING (statement_id)").to_pylist()
    return [row for row in rows if tag in row["sql_text"]]


def assert_in_no_sink(server, admin, secret: str, also: tuple[str, ...] = ()):
    for name, text in every_sink(server=server, admin=admin).items():
        for value in (secret, *also):
            assert value not in text, f"{value!r} reached {name}"


def run(admin, sql: str, parameters=None) -> str | None:
    """Sends the statement; returns its error's text when it fails."""
    response = admin.submit(sql=sql, parameters=parameters)
    if response.status_code not in (200, 201):
        return response.json().get("detail", response.text)
    document = response.json()
    if document["state"] not in TERMINAL:
        document = admin.wait_done(statement_id=document["statement_id"])
    if document["state"] == "succeeded":
        return None
    return (document.get("error") or {}).get("detail", str(document))


# ---- The forms -------------------------------------------------------------------------
#
# (id, the statement with the credential, its ordinary twin, whether the stored error may be
# replaced whole because the value is computed). {c}: the canary; {k}: the twin's marker;
# {t}: the tag.

FORMS = [
    # a credential name given a value: named arguments, option lists, struct and MAP keys
    ("named-argument-arrow", "SELECT {t}(token => '{c}')", "SELECT {t}(name => '{k}')", False),
    ("named-argument-assign", "SELECT {t}(password := '{c}')", "SELECT {t}(name := '{k}')", False),
    ("named-argument-equals", "SELECT {t}(api_key = '{c}')", "SELECT {t}(name = '{k}')", False),
    ("named-argument-macro", "SELECT mask_probe(1, token => '{c}') AS {t}", "SELECT mask_probe(1, name => '{k}') AS {t}", False),
    ("option-list", "ATTACH '/no-such-dir/{t}.duckdb' AS {t} (ENCRYPTION_KEY '{c}')",
     "ATTACH '/no-such-dir/{t}.duckdb' AS \"{k}\" (READ_ONLY)", False),
    ("option-list-bare-word", "ATTACH '/no-such-dir/{t}.duckdb' AS {t} (TOKEN {c_word})",
     "ATTACH '/no-such-dir/{t}.duckdb' AS {t} (TYPE {k_word})", False),
    ("struct-key", "SELECT {{'password': '{c}'}} AS {t}", "SELECT {{'name': '{k}'}} AS {t}", False),
    ("struct-key-unquoted", "SELECT {{secret: '{c}'}} AS {t}", "SELECT {{note: '{k}'}} AS {t}", False),
    ("map-key", "SELECT MAP {{'api_key': '{c}'}} AS {t}", "SELECT MAP {{'name': '{k}'}} AS {t}", False),
    ("key-function", "SELECT 1 AS {t}; PRAGMA add_parquet_key('k', '{c}')", "SELECT 1 AS {t}; SELECT '{k}'", False),
    ("set-variable", "SET VARIABLE {t}_api_key = '{c}'", "SET VARIABLE {t}_name = '{k}'", False),
    # comparisons of a credential column, and values inserted into one
    ("comparison", "SELECT 1 AS {t} FROM mask_users WHERE password = '{c}'", "SELECT 1 AS {t} FROM mask_users WHERE name = '{k}'", False),
    ("comparison-not-equal", "SELECT 1 AS {t} FROM mask_users WHERE password <> '{c}'", "SELECT 1 AS {t} FROM mask_users WHERE name <> '{k}'", False),
    ("comparison-in", "SELECT 1 AS {t} FROM mask_users WHERE password IN ('{c}', 'x')", "SELECT 1 AS {t} FROM mask_users WHERE name IN ('{k}', 'x')", False),
    ("comparison-like", "SELECT 1 AS {t} FROM mask_users WHERE password LIKE '{c}%'", "SELECT 1 AS {t} FROM mask_users WHERE name LIKE '{k}%'", False),
    ("comparison-between", "SELECT 1 AS {t} FROM mask_users WHERE password BETWEEN '{c}' AND '{c}z'",
     "SELECT 1 AS {t} FROM mask_users WHERE name BETWEEN '{k}' AND '{k}z'", False),
    ("comparison-reversed", "SELECT 1 AS {t} FROM mask_users WHERE '{c}' = password", "SELECT 1 AS {t} FROM mask_users WHERE '{k}' = name", False),
    ("comparison-wrapped", "SELECT 1 AS {t} FROM mask_users WHERE lower(password) = '{c}'", "SELECT 1 AS {t} FROM mask_users WHERE lower(name) = '{k}'", False),
    ("comparison-cast", "SELECT 1 AS {t} FROM mask_users WHERE password::VARCHAR = '{c}'", "SELECT 1 AS {t} FROM mask_users WHERE name::VARCHAR = '{k}'", False),
    ("update-set", "UPDATE mask_users SET password = '{c}' WHERE name = '{t}'", "UPDATE mask_users SET name = '{k}' WHERE name = '{t}'", False),
    ("insert-values", "INSERT INTO mask_users (name, password, age) VALUES ('{t}', '{c}', 3), ('{t}2', '{c}2', 4)",
     "INSERT INTO mask_users (name, age) VALUES ('{t}', 3), ('{k}', 4)", False),
    ("key-value-pair", "SELECT json_object('api_key', '{c}') AS {t}", "SELECT json_object('name', '{k}') AS {t}", False),
    # connection strings and URLs inside a literal
    ("libpq-keyword", "ATTACH 'host=127.0.0.1 port=1 user=u password={c} dbname={t}' AS {t} (TYPE postgres)",
     "ATTACH 'host=127.0.0.1 port=1 user={k} dbname={t}' AS {t} (TYPE postgres)", False),
    ("ducklake-over-postgres", "ATTACH 'ducklake:postgres:host=127.0.0.1 port=1 user=u password={c} dbname={t}' AS {t}",
     "ATTACH 'ducklake:postgres:host=127.0.0.1 port=1 user={k} dbname={t}' AS {t}", False),
    ("url-password", "ATTACH 'postgresql://u:{c}@127.0.0.1:1/{t}' AS {t} (TYPE postgres)",
     "ATTACH 'postgresql://{k}@127.0.0.1:1/{t}' AS {t} (TYPE postgres)", False),
    ("url-token-as-user", "SELECT * FROM read_csv('https://{c}@127.0.0.1:1/{t}.csv')", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}/{k}.csv')", False),
    ("url-token-and-dummy-password", "SELECT * FROM read_csv('https://{c}:x-oauth-basic@127.0.0.1:1/{t}.csv')",
     "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?version={k}')", False),
    ("url-query-auth", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?auth={c}')", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?format={k}')", False),
    ("url-query-api-key", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?v=1&api-key={c}&z=2')", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?v=1&page={k}&z=2')", False),
    ("url-query-jwt", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?jwt={c}')", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?job={k}')", False),
    ("url-query-token", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?access_token={c}')", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?accessed={k}')", False),
    ("url-query-signature", "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?X-Amz-Signature={c}&X-Amz-Expires=60')",
     "SELECT * FROM read_csv('https://127.0.0.1:1/{t}.csv?Expires={k}')", False),
    ("url-in-a-quoted-name", "SELECT * FROM \"https://u:{c}@127.0.0.1:1/{t}.csv\"", "SELECT * FROM \"https://127.0.0.1:1/{t}/{k}.csv\"", False),
    ("azure-connection-string", "SELECT 'DefaultEndpointsProtocol=https;AccountName={t};AccountKey={c};EndpointSuffix=core.windows.net' AS x",
     "SELECT 'DefaultEndpointsProtocol=https;AccountName={t};EndpointSuffix={k}' AS x", False),
    ("json-in-a-literal", "SELECT '{{\"user\": \"{t}\", \"password\": \"{c}\"}}' AS x", "SELECT '{{\"user\": \"{t}\", \"note\": \"{k}\"}}' AS x", False),
    # HTTP headers
    ("header-authorization", "SELECT * FROM {t}('https://x', headers => MAP {{'Authorization': 'Bearer {c}'}})",
     "SELECT * FROM {t}('https://x', headers => MAP {{'Accept': '{k}'}})", False),
    ("header-proxy-authorization", "SELECT * FROM {t}('https://x', headers => MAP {{'proxy-authorization': 'Basic {c}'}})",
     "SELECT * FROM {t}('https://x', headers => MAP {{'user-agent': '{k}'}})", False),
    ("header-x-api-key", "SELECT * FROM {t}('https://x', headers => MAP {{'X-API-Key': '{c}'}})", "SELECT * FROM {t}('https://x', headers => MAP {{'X-Request-Id': '{k}'}})", False),
    ("header-cookie", "SELECT * FROM {t}('https://x', headers => {{'Cookie': 'sessionid={c}'}})", "SELECT * FROM {t}('https://x', headers => {{'Accept-Language': '{k}'}})", False),
    ("header-two-lists", "SELECT * FROM {t}('https://x', headers => MAP(['Accept', 'AUTHORIZATION'], ['text/csv', 'Bearer {c}']))",
     "SELECT * FROM {t}('https://x', headers => MAP(['Accept', 'Range'], ['text/csv', '{k}']))", False),
    ("bearer-literal", "SELECT {t}('https://x', 'Bearer {c}')", "SELECT {t}('https://x', 'Carrier {k}')", False),
    # SQL given to a function as text
    ("sql-as-text-alter-role", "CALL {t}('pg', $$ALTER ROLE app PASSWORD '{c}'$$)", "CALL {t}('pg', $$ALTER ROLE app VALID UNTIL '{k}'$$)", False),
    ("sql-as-text-create-role", "CALL {t}('pg', 'CREATE ROLE app LOGIN PASSWORD ''{c}''')", "CALL {t}('pg', 'CREATE ROLE app LOGIN CONNECTION LIMIT ''{k}''')", False),
    ("sql-as-text-identified-by", "CALL {t}('my', 'GRANT ALL ON db.* TO app IDENTIFIED BY ''{c}''')", "CALL {t}('my', 'GRANT ALL ON db.* TO ''{k}''')", False),
    ("sql-as-text-nested-twice", "SELECT * FROM {t}('SELECT * FROM q(''ALTER ROLE app PASSWORD ''''{c}'''''')')",
     "SELECT * FROM {t}('SELECT * FROM q(''SELECT ''''{k}'''''')')", False),
    # a credential built from pieces: the whole expression
    ("built-concatenation", "ATTACH ('host=127.0.0.1 port=1 dbname={t} password=' || '{c}') AS {t} (TYPE postgres)",
     "ATTACH ('host=127.0.0.1 port=1 dbname={t} user=' || '{k}') AS {t} (TYPE postgres)", False),
    ("built-format", "SELECT * FROM read_csv(format('https://127.0.0.1:1/{t}.csv?token={{}}', '{c}'))",
     "SELECT * FROM read_csv(format('https://127.0.0.1:1/{t}.csv?version={{}}', '{k}'))", False),
    ("expression-value", "SELECT {t}(token => upper('{c}') || 'x')", "SELECT {t}(name => upper('{k}') || 'x')", True),
    # comments
    ("comment-block", "SELECT 1 AS {t} /* was: f(token => '{c}') */", "SELECT 1 AS {t} /* was: f(name => '{k}') */", False),
    ("comment-line", "SELECT 1 AS {t} -- password = '{c}'", "SELECT 1 AS {t} -- name = '{k}'", False),
]


def fill(template: str, tag: str, value: str, key: str) -> str:
    # (A bare word cannot hold '-': the canary's and the marker's bare forms use '_'.)
    return template.format(t=tag, **{key: value, key + "_word": value.replace("-", "_")})


@pytest.mark.parametrize("form,template,twin,computed", FORMS, ids=[form[0] for form in FORMS])
def test_form_is_masked_in_every_sink(server, admin, form, template, twin, computed):
    secret, tag = canary(), tagged()
    sql = fill(template=template, tag=tag, value=secret, key="c")
    failure = run(admin=admin, sql=sql)
    records = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement"])
    assert records, f"the log has no record of the statement tagged {tag}"
    assert "***" in records[0]["sql"], records[0]
    rows = wait_for(read=lambda: stored(admin=admin, tag=tag))
    assert rows and "***" in rows[0]["sql_text"], rows
    if failure is not None:
        failed = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"])
        errors = [r["error"] for r in failed] + [row["error_message"] or row["prepare_error"] or "" for row in rows]
        assert any(errors), (failed, rows)
        if not computed:
            # The reason is kept: nothing is replaced whole.
            assert not any(error.startswith("[REDACTED") for error in errors), errors
    assert_in_no_sink(server=server, admin=admin, secret=secret, also=(secret.replace("-", "_"), secret.upper()))


@pytest.mark.parametrize("form,template,twin,computed", FORMS, ids=[form[0] for form in FORMS])
def test_the_neighbouring_ordinary_statement_is_kept_as_written(server, admin, form, template, twin, computed):
    kept, tag = marker(), tagged()
    sql = fill(template=twin, tag=tag, value=kept, key="k")
    failure = run(admin=admin, sql=sql)
    records = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement"])
    assert records and records[0]["sql"] == sql, (sql, records)
    rows = wait_for(read=lambda: stored(admin=admin, tag=tag))
    assert rows and rows[0]["sql_text"] == sql, rows
    if failure is not None:
        failed = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"])
        assert failed and failed[0]["sql"] == sql, failed
        # Its error is kept: the first line of what the client was told.
        reason = failure.splitlines()[0][:60]
        assert reason in failed[0]["error"], (failure, failed)
        assert not failed[0]["error"].startswith("[REDACTED"), failed
        recorded = wait_for(read=lambda: [row for row in stored(admin=admin, tag=tag) if row["error_message"] or row["prepare_error"]])
        assert recorded and reason in (recorded[0]["error_message"] or recorded[0]["prepare_error"]), (failure, recorded)


# ---- Statements replaced whole ---------------------------------------------------------

@pytest.mark.parametrize("template,placeholder", [
    ("CREATE SECRET {t} (TYPE s3, KEY_ID 'k', SECRET '{c}')", "[REDACTED: secret DDL]"),
    ("CREATE OR REPLACE TEMPORARY SECRET {t} (TYPE s3, KEY_ID {c_word}, SECRET 'x')", "[REDACTED: secret DDL]"),
    ("CREATE SECRET {t} (TYPE s3, KEY_ID 'k', SECRET '{c}', REGIN 'eu')", "[REDACTED: secret DDL]"),
    ("SET s3_secret_access_key = '{c}'", "[REDACTED: a credential setting]"),
    ("SET GLOBAL s3_secret_access_key TO '{c}'", "[REDACTED: a credential setting]"),
    ("SET {t}_api_key TO '{c}'", "[REDACTED: a credential setting]"),
    ("PRAGMA http_proxy_password = '{c}'", "[REDACTED: a credential setting]"),
    ("SELECT 1 AS {t}; CREATE SECRET s_{t} (TYPE s3, KEY_ID 'k', SECRET '{c}'); SELECT 2 AS {t}_after",
     "SELECT 1 AS {t}; [REDACTED: secret DDL]; SELECT 2 AS {t}_after"),
])
def test_credential_ddl_is_replaced_whole_and_its_error_keeps_the_reason(server, admin, template, placeholder):
    secret, tag = canary(), tagged()
    sql = fill(template=template, tag=tag, value=secret, key="c")
    expected = placeholder.format(t=tag)
    before = len([r for r in server.log_records() if r.get("sql") == expected])
    failure = run(admin=admin, sql=sql)
    records = wait_for(read=lambda: [r for r in server.log_records() if r.get("sql") == expected][before:])
    assert records, f"no record with {expected!r}"
    if failure is not None:
        failed = [r for r in records if r["message"] == "statement failed"]
        assert failed and not failed[0]["error"].startswith("[REDACTED"), (failure, records)
    assert_in_no_sink(server=server, admin=admin, secret=secret, also=(secret.replace("-", "_"),))


def test_user_ddl_and_the_audit_record_under_access_control(launcher, store):
    """Under access control: CREATE / ALTER USER with a password is replaced whole, and a
    refused statement's text and error go into the audit record as the log would show them."""
    from rbac import start_rbac_server

    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "true",
                                    "HARDCHARGER_ENABLE_INSTRUMENTATION": "true", "HARDCHARGER_LOG_LEVEL": "debug"})
    admin = server.admin()
    secret = canary()
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql=f"CREATE USER cleo WITH PASSWORD '{secret}-0'")
    admin.run(sql=f"ALTER USER cleo PASSWORD '{secret}-1'")
    admin.run(sql="ALTER USER cleo ENABLE")  # no password: an ordinary statement
    admin.run(sql="GRANT readers TO cleo")
    admin.run(sql="CREATE TABLE guarded (i INTEGER, password VARCHAR)")
    cleo = server.user(name="cleo", password=f"{secret}-1")
    refused = {
        "comparison": "SELECT i AS {t} FROM guarded WHERE password = '{c}'",
        "url": "SELECT i, 'postgresql://u:{c}@h/db' AS {t} FROM guarded",
        "keyword": "SELECT i, 'host=h password=Zk7;{c})x dbname=d' AS {t} FROM guarded",
        "header": "SELECT i, MAP {{'Authorization': 'Bearer {c}'}} AS {t} FROM guarded",
        "sql-as-text": "SELECT i, $$ALTER ROLE a PASSWORD '{c}'$$ AS {t} FROM guarded",
        "attach": "ATTACH 'host=127.0.0.1 port=1 password=Zk7){c} dbname={t}' AS p (TYPE postgres)",
    }
    tags = {}
    for name, template in refused.items():
        tags[name] = tagged()
        with pytest.raises(Exception, match="Hardcharger blocked"):
            cleo.run(sql=template.format(t=tags[name], c=secret))
    kept, twin_tag = marker(), tagged()
    with pytest.raises(Exception, match="Hardcharger blocked"):
        cleo.run(sql=f"SELECT i, '{kept}' AS {twin_tag} FROM guarded")
    log = wait_for(read=lambda: twin_tag in server.log() and server.log())
    audit = [json.loads(line) for line in log.splitlines() if '"kind":"access_control"' in line and '"status":"denied"' in line]
    for name, tag in tags.items():
        records = [record for record in audit if tag in record.get("sql", "")]
        assert len(records) == 1 and "***" in records[0]["sql"], (name, records)
        assert "Hardcharger blocked" in records[0]["error"], records
    twins = [record for record in audit if twin_tag in record.get("sql", "")]
    assert len(twins) == 1 and twins[0]["sql"] == f"SELECT i, '{kept}' AS {twin_tag} FROM guarded", twins
    assert '"sql":"ALTER USER cleo ENABLE"' in log, "user DDL without a password is an ordinary statement"
    assert log.count("[REDACTED: user DDL]") >= 2
    assert secret not in log and "Zk7" not in log, "the canary reached the log"
    for table in ("sql_statements", "sql_executions"):
        rows = repr(admin.run(sql=f"SELECT * FROM _hardcharger_instr.main.{table}").to_pylist())
        assert secret not in rows and "Zk7" not in rows, f"the canary reached {table}"


# ---- Finding 1: a password with punctuation in it ----------------------------------------

HARD = [";", ")", '"', "'", "@", ":", "/", "%", " ", "\\", "é", ";)\"'@:/% \\", ""]


def libpq_bare(password: str) -> str:
    return "".join("\\" + ch if ch in " '\\" else ch for ch in password)


def libpq_quoted(password: str) -> str:
    return "'" + "".join("\\" + ch if ch in "'\\" else ch for ch in password) + "'"


def literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@pytest.mark.parametrize("inside", HARD, ids=[repr(ch) for ch in HARD])
@pytest.mark.parametrize("form", ["keyword", "keyword-quoted", "keyword-last", "url", "ducklake", "error-only"])
def test_a_password_with_punctuation_is_masked_whole(server, admin, inside, form):
    """`Zk7<punctuation><canary>`: before, only the part up to the punctuation was masked
    (keyword form) or nothing was (URL form), in the statement and in the stored error."""
    secret, tag = canary(), tagged()
    password = f"Zk7{inside}{secret}" if inside != "" else ""
    if form == "keyword":
        target = f"host=127.0.0.1 port=1 user=u password={libpq_bare(password)} dbname={tag}"
    elif form == "keyword-quoted":
        target = f"host=127.0.0.1 port=1 user=u password={libpq_quoted(password)} dbname={tag}"
    elif form == "keyword-last":
        target = f"host=127.0.0.1 port=1 user=u dbname={tag} password={libpq_bare(password)}"
    elif form == "ducklake":
        target = f"ducklake:postgres:host=127.0.0.1 port=1 user=u password={libpq_quoted(password)} dbname={tag}"
    else:
        target = f"postgresql://u:{password}@127.0.0.1:1/{tag}"
    if form == "error-only":
        # The statement that fails does not hold the string: only DuckDB's error quotes it.
        assert run(admin=admin, sql=f"SET VARIABLE dsn_{tag} = {literal(target)}") is None
        sql = f"ATTACH getvariable('dsn_{tag}') AS {tag} (TYPE postgres)"
    else:
        sql = f"ATTACH {literal(target)} AS {tag}" + ("" if form == "ducklake" else " (TYPE postgres)")
    failure = run(admin=admin, sql=sql)
    assert failure is not None, "nothing listens on port 1"
    failed = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"])
    assert failed, f"no failure record for {tag}"
    # The reason survives, in the log and in the stored record.
    assert not failed[-1]["error"].startswith("[REDACTED"), failed
    rows = wait_for(read=lambda: [row for row in stored(admin=admin, tag=tag) if row["error_message"]])
    assert rows and not rows[-1]["error_message"].startswith("[REDACTED"), rows
    if password:
        assert "***" in failed[-1]["sql"] + failed[-1]["error"], failed
    assert_in_no_sink(server=server, admin=admin, secret=secret)
    if inside != "":
        assert_in_no_sink(server=server, admin=admin, secret="Zk7" + inside[:1] if inside[:1] not in "\\\"" else "Zk7")


# ---- Finding 2: where DuckDB's tokens are -------------------------------------------------

def test_a_dollar_in_a_name_does_not_open_a_literal(server, admin, tmp_path):
    secret, tag = canary(), tagged()
    statements = [
        f"ATTACH '{tmp_path}/{tag}_1.db' AS {tag}_a$q$ (ENCRYPTION_KEY '{secret}') -- b$q$",
        f"SELECT 1 AS a$q$; ATTACH '{tmp_path}/{tag}_2.db' AS {tag}_d2 (ENCRYPTION_KEY '{secret}'); SELECT 2 AS b$q$",
        f"CREATE TABLE {tag}_acct (bal$usd$ INTEGER, password VARCHAR)",
        f"UPDATE {tag}_acct SET bal$usd$ = 1, password = lower('{secret}') WHERE bal$usd$ = 0",
    ]
    for sql in statements:
        assert run(admin=admin, sql=sql) is None, sql  # they run: DuckDB reads a$q$ as a name
    records = wait_for(read=lambda: len(found := [r for r in statement_records(server=server, tag=tag)
                                                  if r["message"] == "statement"]) >= len(statements) and found)
    logged = [record["sql"] for record in records]
    assert logged == [
        f"ATTACH '{tmp_path}/{tag}_1.db' AS {tag}_a$q$ (ENCRYPTION_KEY ***) -- b$q$",
        f"SELECT 1 AS a$q$; ATTACH '{tmp_path}/{tag}_2.db' AS {tag}_d2 (ENCRYPTION_KEY ***); SELECT 2 AS b$q$",
        f"CREATE TABLE {tag}_acct (bal$usd$ INTEGER, password VARCHAR)",
        f"UPDATE {tag}_acct SET bal$usd$ = 1, password = *** WHERE bal$usd$ = 0",
    ], logged
    assert_in_no_sink(server=server, admin=admin, secret=secret)


def test_every_space_duckdb_reads_separates_a_name_from_its_value(server, admin):
    """The live differential for white space: every code point of the Unicode space, format
    and control categories is put between SELECT and 42, and between a credential name and
    `=>`. Wherever DuckDB reads it as white space (the statement runs), the value after the
    name must be masked: the masker reads the statement through DuckDB's own tokenizer."""
    candidates = [cp for cp in range(0x80, 0x30000)
                  if unicodedata.category(chr(cp)) in ("Zs", "Zl", "Zp", "Cf", "Cc")] + [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20]
    tag, spaces, masked = tagged(), [], {}
    secrets_by_cp = {}
    for cp in candidates:
        ch = chr(cp)
        if run(admin=admin, sql=f"SELECT{ch}42") is None:
            spaces.append(cp)
        secrets_by_cp[cp] = canary()
        run(admin=admin, sql=f"SELECT {tag}_{cp:x}(token{ch}=> '{secrets_by_cp[cp]}')")
    # What DuckDB 2.0 takes for white space (Parser::StripUnicodeSpaces and the tokenizer's own six).
    assert sorted(spaces) == sorted([0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, *range(0x2000, 0x200C), 0x202F, 0x205F,
                                     0x2060, 0x3000, 0xFEFF]), [hex(cp) for cp in spaces]
    wait_for(read=lambda: f"{tag}_{candidates[-1]:x}" in server.log())
    log = server.log()
    for cp in spaces:
        assert secrets_by_cp[cp] not in log, f"U+{cp:04X} is white space to DuckDB, and the value after it was logged"
    sinks = every_sink(server=server, admin=admin)
    for cp in spaces:
        for name, text in sinks.items():
            assert secrets_by_cp[cp] not in text, f"U+{cp:04X}: the value reached {name}"
    # And where DuckDB reads the code point as part of the name (U+1680, U+180E, U+200C ...: it
    # then refuses the statement for the name it does not know), the value typed after it is
    # masked all the same: a credential name is matched without its invisible characters.
    for cp in candidates:
        assert secrets_by_cp[cp] not in log, f"U+{cp:04X}: the value after the name was logged"


# ---- Finding 3: errors and bound parameters, by value ---------------------------------------

def test_an_error_does_not_quote_what_the_statement_masked(server, admin):
    secret, tag = canary(), tagged()
    for value, quoted in [(f"'{secret}'", secret), (f"upper('{secret}') || ''", secret.upper()),
                          (f"E'{secret[:3]}\\x{ord(secret[3]):02x}{secret[4:]}'", secret), (secret.replace("-", "_"), secret.replace("-", "_"))]:
        failure = run(admin=admin, sql=f"SELECT mask_echo(1, token => {value}) AS {tag}")
        assert failure is not None and (quoted in failure or quoted.lower() in failure.lower()), failure  # its author is told
    failed = wait_for(read=lambda: len(found := [r for r in statement_records(server=server, tag=tag)
                                                 if r["message"] == "statement failed"]) >= 4 and found)
    assert failed and len(failed) == 4, failed
    # A literal's error keeps its reason with the value replaced; a computed value's is replaced whole.
    assert "***" in failed[0]["error"] and not failed[0]["error"].startswith("[REDACTED"), failed[0]
    assert failed[1]["error"] == "[REDACTED: error of a statement that computes a credential value]", failed[1]
    assert_in_no_sink(server=server, admin=admin, secret=secret, also=(secret.upper(), secret.replace("-", "_")))


@pytest.mark.parametrize("binding", ["json", "json-prepared", "arrow-prepared"])
def test_an_error_does_not_quote_what_is_bound_to_a_masked_placeholder(server, admin, binding):
    secret, kept, tag = canary(), marker(), tagged()
    masked_sql = f"SELECT mask_echo(1, token => ?) AS {tag}"
    ordinary_sql = f"SELECT CAST(? AS INTEGER) AS {tag}_twin"
    for sql, value in ((masked_sql, secret), (ordinary_sql, kept)):
        if binding == "json":
            response = admin.submit(sql=sql, parameters=[value])
        else:
            prepared = admin.prepare(sql=sql)
            if binding == "json-prepared":
                response = admin.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[value], arrow=False)
            else:
                response = admin.execute_prepared(prepared_id=prepared["prepared_id"], arrow=False,
                                                  arrow_parameters=pa.table({"p1": [value]}))
        assert response.status_code == 422 and value in response.text, response.text  # its author is told
    def recorded():
        rows = admin.query(sql="SELECT error_message FROM _hardcharger_instr.sql_executions "
                               "WHERE error_message IS NOT NULL").to_pylist()
        # The twin: a parameter bound where nothing is masked is no credential, and its error quotes it.
        return [row["error_message"] for row in rows if kept in row["error_message"]]

    errors = wait_for(read=recorded)
    assert errors, "the ordinary parameter's error was not recorded with its value"
    failed = wait_for(read=lambda: [r for r in server.log_records() if r["message"] == "statement failed"
                                    and "***" in r["error"] and r.get("session_id") == admin.id])
    assert failed and not failed[0]["error"].startswith("[REDACTED"), failed  # the reason, with the value replaced
    assert_in_no_sink(server=server, admin=admin, secret=secret)


def test_a_bound_url_parameter_has_its_credential_masked_in_the_error(server, admin):
    secret, tag = canary(), tagged()
    failure = run(admin=admin, sql=f"SELECT * FROM read_csv(?) AS {tag}", parameters=[f"https://u:{secret}@127.0.0.1:1/{tag}.csv?auth={secret}"])
    assert failure is not None
    failed = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"])
    assert failed and not failed[0]["error"].startswith("[REDACTED"), failed
    assert_in_no_sink(server=server, admin=admin, secret=secret)


# ---- Finding 4: ordinary statements keep their text, errors and profiles -------------------

def test_a_commented_statement_and_a_token_join_keep_error_profile_and_comment(server, admin):
    tag = tagged()
    admin.execute(sql=f"CREATE TABLE {tag}_a (id INTEGER, access_token VARCHAR)")
    admin.execute(sql=f"CREATE TABLE {tag}_b (id INTEGER, access_token VARCHAR)")
    admin.execute(sql="SET hardcharger.capture_query_profile = 'standard'")
    dbt = f'/* {{"app": "dbt", "node_id": "model.{tag}.y"}} */'
    ran = [f"{dbt} SELECT count(*) AS {tag}_dbt FROM {tag}_a",
           f"SELECT count(*) AS {tag}_metabase FROM {tag}_a -- Metabase:: userID: 5 queryType: native",
           f"SELECT count(*) AS {tag}_join FROM {tag}_a a JOIN {tag}_b b ON a.access_token = b.access_token WHERE a.id > 0",
           f"SELECT E'tab\\there' AS {tag}_escaped"]
    failing = [f"{dbt} SELECT * FROM {tag}_no_such_table_dbt",
               f"SELECT * FROM {tag}_no_such_table_line -- Metabase:: userID: 5",
               f"SELECT {tag}_nofn(1) FROM {tag}_a a JOIN {tag}_b b ON a.access_token = b.access_token"]
    try:
        for sql in ran:
            assert run(admin=admin, sql=sql) is None, sql
        failures = [run(admin=admin, sql=sql) for sql in failing]
    finally:
        admin.execute(sql="SET hardcharger.capture_query_profile = 'off'")
    assert all(failures), failures
    rows = wait_for(read=lambda: len(found := admin.query(
        sql="SELECT s.sql_text, e.status, e.error_message, e.query_profile IS NOT NULL AS has_profile "
            "FROM _hardcharger_instr.sql_statements s JOIN _hardcharger_instr.sql_executions e USING (statement_id) "
            "WHERE e.status IN ('success', 'error')").to_pylist()) and
        len(mine := [row for row in found if tag in row["sql_text"] and "CREATE TABLE" not in row["sql_text"]
                     and (row["status"] == "error" or row["has_profile"])]) >= len(ran) + len(failing) and mine)
    assert rows, "the statements were not all recorded with their profiles"
    by_text = {row["sql_text"]: row for row in rows}
    for sql in ran:
        assert sql in by_text, (sql, list(by_text))  # the text as written, comment and all
        assert by_text[sql]["has_profile"], f"no query profile for: {sql}"
    for sql, failure in zip(failing, failures):
        assert sql in by_text, (sql, list(by_text))
        assert by_text[sql]["error_message"].startswith(failure.splitlines()[0][:50]), (failure, by_text[sql])
    for function, column in (("hardcharger_live_queries", "sql_text"),):
        shown = [row[column] for row in admin.query(sql=f"SELECT {column} FROM {function}()").to_pylist()]
        assert ran[0] in shown, shown  # the monitoring functions show the tag that says which model ran


def test_a_statement_with_a_credential_has_no_profile_and_the_next_one_has(server, admin):
    secret, tag = canary(), tagged()
    admin.execute(sql="SET hardcharger.capture_query_profile = 'standard'")
    try:
        admin.query(sql=f"SELECT 41 AS {tag}_before")
        admin.query(sql=f"SELECT {{'password': '{secret}'}} AS {tag}_secret")
        admin.query(sql=f"SELECT 1 AS {tag}_commented /* token => '{secret}' */")
        admin.query(sql=f"SELECT 43 AS {tag}_after")
    finally:
        admin.execute(sql="SET hardcharger.capture_query_profile = 'off'")
    profiles = wait_for(read=lambda: len(rows := [row["p"] for row in admin.query(
        sql="SELECT query_profile::VARCHAR AS p FROM _hardcharger_instr.sql_executions WHERE query_profile IS NOT NULL").to_pylist()
        if tag in row["p"]]) >= 2 and rows)
    assert profiles and len(profiles) == 2, profiles
    assert f"{tag}_before" in repr(profiles) and f"{tag}_after" in repr(profiles)
    assert_in_no_sink(server=server, admin=admin, secret=secret)


# ---- Finding 5: no statement hides its own text -----------------------------------------------

def test_no_wording_keeps_a_statements_text_out_of_the_log(server):
    """A signed-in user (not an administrator) could keep a statement out of the statement log
    and the instrumentation tables with a name like a$q$ or the words `create secret` in a
    comment or a string, while the statement ran."""
    admin = server.admin()
    tag = tagged()
    admin.execute(sql=f"CREATE TABLE {tag}_victim AS SELECT range AS i FROM range(1, 5)")
    mallory = server.as_role(role="user", subject="mallory", catalog_access=[{"catalog": "*", "access": "write"}])
    statements = [f"DELETE FROM {tag}_victim WHERE i = 1 AND 0 = (SELECT 0 AS a$q$)",
                  f"DELETE FROM {tag}_victim WHERE i = 2 -- create secret",
                  f"DELETE FROM {tag}_victim WHERE i = 3 AND 'alter user' <> ''",
                  f"DELETE FROM {tag}_victim WHERE i = 4 AND no_such_column = 1 -- create user"]
    outcomes = [run(admin=mallory, sql=sql) for sql in statements]
    assert outcomes[:3] == [None, None, None] and "no_such_column" in outcomes[3], outcomes
    assert admin.query(sql=f"SELECT i FROM {tag}_victim").to_pylist() == [{"i": 4}]  # the first three ran
    records = wait_for(read=lambda: len(found := [r for r in statement_records(server=server, tag=f"{tag}_victim WHERE")
                                                  if r["message"] == "statement"]) >= 4 and found)
    assert [record["sql"] for record in records] == statements, records
    rows = wait_for(read=lambda: len(found := stored(admin=admin, tag=f"{tag}_victim WHERE")) >= 4 and found)
    assert sorted(row["sql_text"] for row in rows) == sorted(statements), rows
    failed = [row for row in rows if row["status"] == "error"]
    assert len(failed) == 1 and "no_such_column" in failed[0]["error_message"], failed
    assert "[REDACTED" not in json.dumps(obj=statement_records(server=server, tag=tag))


@pytest.mark.parametrize("ending,reason", [
    ("'{c}", "an unterminated string literal"), ("\"{c}", "an unterminated quoted identifier"),
    ("/* {c}", "an unterminated /* comment"), ("$q${c}", "an unterminated dollar-quoted string"),
])
def test_text_duckdb_cannot_tokenize_is_hidden_whole_with_the_reason(server, admin, ending, reason):
    secret = canary()
    placeholder = f"[REDACTED: a statement that could not be read: {reason}]"
    recorded = ("SELECT prepare_error FROM _hardcharger_instr.sql_statements "
                f"WHERE sql_text = '{placeholder.replace(chr(39), chr(39) * 2)}'")
    before = admin.query(sql=recorded).num_rows
    failure = run(admin=admin, sql="SELECT mask_probe(1, token => " + ending.format(c=secret))
    assert failure is not None, "DuckDB cannot parse it either"
    errors = wait_for(read=lambda: admin.query(sql=recorded).to_pylist()[before:])
    assert errors and errors[-1]["prepare_error"] == f"[REDACTED: error of a statement that could not be read: {reason}]", errors
    assert_in_no_sink(server=server, admin=admin, secret=secret)


# ---- Session monitoring while a statement runs ---------------------------------------------------

def test_running_statements_are_masked_in_the_monitoring_functions(server, admin):
    secret, tag = canary(), tagged()
    worker = server.admin()
    slow = (f"SELECT count(*), 'postgresql://u:Zk7;{secret}@h/db' AS {tag}, MAP {{'Authorization': 'Bearer {secret}'}} AS h "
            "FROM range(10000000000) a, range(1000) b /* dbt: kept */")
    thread = threading.Thread(target=lambda: worker.submit(sql=slow, timeout=300), daemon=True)
    thread.start()
    try:
        rows = wait_for(read=lambda: [row for row in admin.query(
            sql="SELECT statement FROM hardcharger_active_connections() WHERE statement IS NOT NULL").to_pylist()
            if tag in row["statement"]], timeout=30)
        assert rows == [{"statement": f"SELECT count(*), 'postgresql://u:***@h/db' AS {tag}, MAP {{'Authorization': ***}} AS h "
                                      "FROM range(10000000000) a, range(1000) b /* dbt: kept */"}], rows
        live = [row for row in admin.query(sql="SELECT sql_text FROM hardcharger_live_queries()").to_pylist() if tag in row["sql_text"]]
        assert live and secret not in repr(live), live
        assert_in_no_sink(server=server, admin=admin, secret=secret)
    finally:
        assert admin.scalar(sql=f"SELECT hardcharger_cancel('{worker.id}')") is True
        thread.join(timeout=60 * TIMEOUT_SCALE)
    assert_in_no_sink(server=server, admin=admin, secret=secret)


# ---- The review of 2.14.0's masking (findings 01-06) --------------------------------------
#
# Each test is a finding's repro, with a password that holds what ends a value somewhere:
# ; ) " ' @ : / %. Every sink is read: the server log, every instrumentation table, the
# catalog log, the monitoring functions, and under access control the audit record.

def hard_canary() -> str:
    return "Zk7;)\"'@:/%" + canary()


def assert_hard_in_no_sink(server, admin, secret: str):
    assert_in_no_sink(server=server, admin=admin, secret=secret.split("%", 1)[1], also=("Zk7;", "Zk7)"))


def nested(sql: str, depth: int) -> str:
    """`sql` given to query() as text, `depth` levels down."""
    for _ in range(depth):
        sql = f"SELECT * FROM query({literal(text=sql)})"
    return sql


@pytest.fixture(scope="module")
def review_tables(server):
    admin = server.admin()
    admin.execute(sql="CREATE TABLE mask_int (id INTEGER, password INTEGER)")
    admin.execute(sql="INSERT INTO mask_int VALUES (1, 1)")
    admin.execute(sql="CREATE TABLE mask_words (doc INTEGER, token VARCHAR, auth VARCHAR, first_pass INTEGER, url VARCHAR, "
                      "session_token VARCHAR, api_key VARCHAR, note VARCHAR, pwd VARCHAR)")
    admin.execute(sql="INSERT INTO mask_words VALUES (1, 'cat', 'basic', 1, 'https://example.com/cb?state=1&code=4/0Ab', 's1', 'k1', "
                      "'password', '/home')")
    return admin


def failed_records(server, tag: str) -> list[dict]:
    return wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"])


# (id, the statement built from (tag, the password as a literal), whether its error is replaced whole)
REVIEW_FORMS = [
    # finding 01: two statements in a row hide the same value; the second one's error quotes it
    ("f01-insert-then-update", lambda t, v: f"INSERT INTO mask_users (name, password, age) VALUES ('{t}', {v}, 1); "
                                            f"UPDATE mask_int SET password = {v} WHERE id = 1 /* {t} */", False),
    ("f01-compare-twice", lambda t, v: f"SELECT 1 AS {t} FROM mask_users WHERE password = {v}; "
                                       f"SELECT 2 AS {t} FROM mask_int WHERE password = {v}", False),
    ("f01-setting-then-compare", lambda t, v: f"SET s3_secret_access_key = {v}; SELECT 2 AS {t} FROM mask_int WHERE password = {v}", False),
    # finding 02: SQL given as text two and three levels down
    ("f02-nested-two", lambda t, v: nested(sql=f"SELECT 1 AS {t} FROM mask_int WHERE password = {v}", depth=2), False),
    ("f02-nested-three", lambda t, v: nested(sql=f"SELECT 1 AS {t} FROM mask_int WHERE password = {v}", depth=3), False),
    # finding 03: forms the README lists
    ("f03-with-insert", lambda t, v: f"WITH x AS (SELECT 1) INSERT INTO mask_users (name, password, age) VALUES ('{t}', {v}, 1)", False),
    ("f03-explain-insert", lambda t, v: f"EXPLAIN INSERT INTO mask_users (name, password, age) VALUES ('{t}', {v}, 1)", False),
    ("f03-prepare-insert", lambda t, v: f"PREPARE {t} AS INSERT INTO mask_users (name, password, age) VALUES ('{t}', {v}, 1)", False),
    ("f03-expression-on-the-left", lambda t, v: f"SELECT 1 AS {t} FROM mask_int WHERE upper({v}) = password", True),
    ("f03-function-of-a-function", lambda t, v: f"SELECT 1 AS {t} FROM mask_int WHERE trim(lower(password::VARCHAR)) = {v} AND nope = 1", False),
    ("f03-computed-option", lambda t, v: f"ATTACH '/no-such-dir/{t}.duckdb' AS {t} (ENCRYPTION_KEY (SELECT {v}))", True),
    # finding 04: a URL with a port, then a credential with an '@' in it
    ("f04-libpq-uri-with-port", lambda t, v: "ATTACH " + literal(text="postgresql://127.0.0.1:1/" + t + "?user=svc&password=P@ss" +
                                                                 v[1:-1].replace("''", "'").replace("%", "%25").replace(";", "%3B") +
                                                                 "&connect_timeout=2") + f" AS {t} (TYPE postgres)", False),
    ("f04-token-parameter-with-port", lambda t, v: "SELECT * FROM read_csv(" + literal(text=f"http://127.0.0.1:1/{t}.csv?access_token=abc@" +
                                                                                        v[1:-1].replace("''", "'").replace("%", "%25")) + ")", False),
    ("f04-json-with-a-url-and-a-password", lambda t, v: "SELECT " + literal(text='{"url": "http://db.example.com:8080/api", "user": "svc", '
                                                                             '"password": "P@ss' + v[1:-1].replace("''", "'").replace('"', '\\"') +
                                                                             '"}') + f" AS {t}", False),
]


@pytest.mark.parametrize("form,build,computed", REVIEW_FORMS, ids=[form[0] for form in REVIEW_FORMS])
def test_review_form_with_a_hard_password_is_masked_in_every_sink(server, admin, review_tables, form, build, computed):
    secret, tag = hard_canary(), tagged()
    sql = build(tag, literal(text=secret))
    failure = run(admin=admin, sql=sql)
    rows = wait_for(read=lambda: stored(admin=admin, tag=tag))
    assert rows and all("***" in row["sql_text"] for row in rows), rows
    # (A statement DuckDB refuses while preparing it has no line in the log: its record above does.)
    if not any(row["prepare_error"] for row in rows):
        records = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement"])
        assert records and all("***" in r["sql"] for r in records), records
    if form.startswith("f01") or form.startswith("f02"):
        # The value is quoted by DuckDB's error: what the finding was about.
        assert failure is not None and "Conversion Error" in failure, failure
    if failure is not None:
        # (A statement DuckDB's parser refuses has no failure line in the log: its record does.)
        failed = [r["error"] for r in statement_records(server=server, tag=tag) if r["message"] == "statement failed"]
        errors = wait_for(read=lambda: [e for e in failed + [row["error_message"] or row["prepare_error"] or ""
                                                              for row in stored(admin=admin, tag=tag)] if e])
        assert errors, f"no error recorded for {tag}"
        if not computed:
            assert not any(error.startswith("[REDACTED") for error in errors), errors
            if "Conversion Error" in failure:
                assert any("Could not convert string '***'" in error for error in errors), errors
    assert_hard_in_no_sink(server=server, admin=admin, secret=secret)


# Finding 05 (and 50): ordinary statements, none holding a credential, kept as written with their
# errors and query profiles. ({t}: the tag.)
ORDINARY = [
    "SELECT doc AS {t} FROM mask_words WHERE first_pass = 3",
    "SELECT doc AS {t} FROM mask_words WHERE first_pass IN (1, 2, 3)",
    "UPDATE mask_words SET first_pass = first_pass + 1 WHERE doc = 3 AND note <> '{t}'",
    "SELECT doc AS {t} FROM mask_words WHERE auth = 'basic'",
    "SELECT doc AS {t} FROM mask_words WHERE token IN ('cat', 'mat') AND token LIKE 'c%' AND token BETWEEN 'a' AND 'm'",
    "SELECT doc AS {t} FROM mask_words WHERE token NOT IN (SELECT token FROM mask_words WHERE doc = 2)",
    "SELECT doc AS {t} FROM mask_words WHERE session_token IN (SELECT session_token FROM mask_words WHERE doc = 3)",
    "SELECT doc AS {t} FROM mask_words WHERE api_key IN (SELECT api_key FROM mask_words WHERE doc = 3)",
    "SELECT a.doc AS {t} FROM mask_words a JOIN mask_words b ON lower(a.api_key) = lower(b.api_key)",
    "SELECT doc AS {t} FROM mask_words WHERE length(token) > 2",
    "INSERT INTO mask_words (doc, token, note) VALUES (99, 'dog', '{t}')",
    "SELECT json_object('token', 'cat', 'pos', 3) AS {t}",
    "SELECT doc AS {t} FROM mask_words WHERE note IN ('password', 'token', 'api_key')",
    "SELECT doc AS {t} FROM mask_words WHERE url LIKE 'https://example.com/p/%?code=%' OR url LIKE 'https://%@%'",
    "SELECT doc AS {t} FROM mask_words WHERE url = 'https://example.com/cb?state=1&code=4/0Ab'",
    "SELECT 'see http://wiki.local:8080/page or mail ops@example.com' AS {t}",
    "SELECT doc AS {t} FROM mask_words WHERE pwd = '/home'",
    "SELECT doc AS {t} FROM mask_words WHERE no_such_column = 1 AND token = 'cat'",
    "SELECT doc AS {t} FROM mask_words WHERE api_key IN (SELECT api_key FROM no_such_table_{t})",
]


@pytest.mark.parametrize("template", ORDINARY, ids=range(len(ORDINARY)))
def test_review_ordinary_statement_is_kept_with_its_error_and_profile(server, admin, review_tables, template):
    tag = tagged()
    sql = template.format(t=tag)
    admin.execute(sql="SET hardcharger.capture_query_profile = 'standard'")
    try:
        failure = run(admin=admin, sql=sql)
    finally:
        admin.execute(sql="SET hardcharger.capture_query_profile = 'off'")
    records = wait_for(read=lambda: [r for r in statement_records(server=server, tag=tag) if r["message"] == "statement"])
    assert records and records[0]["sql"] == sql, (sql, records)
    rows = wait_for(read=lambda: admin.query(
        sql="SELECT s.sql_text, e.status, e.error_message, e.query_profile IS NOT NULL AS has_profile "
            "FROM _hardcharger_instr.sql_statements s JOIN _hardcharger_instr.sql_executions e USING (statement_id) "
            f"WHERE s.sql_text LIKE '%{tag}%' AND e.status IN ('success', 'error')").to_pylist())
    assert rows and rows[0]["sql_text"] == sql, rows
    if failure is None:
        assert rows[0]["has_profile"], f"no query profile for: {sql}"
    else:
        reason = failure.splitlines()[0]
        failed = failed_records(server=server, tag=tag)
        assert failed and failed[0]["sql"] == sql and failed[0]["error"].startswith(reason), (failure, failed)
        assert rows[0]["error_message"].startswith(reason), (failure, rows)


@pytest.mark.parametrize("template,expected", [
    # finding 06: a hidden value that is a word does not replace the error's own words
    ("SELECT 1 AS {t} FROM mask_users WHERE password = 'error' AND nope = 1", 'Binder Error: Referenced column "nope" not found'),
    ("SELECT 1 AS {t} FROM mask_int WHERE password = 'name'", "Conversion Error: Could not convert string '***' to INT32"),
    # a URL with a port and an e-mail address later in the text: nothing to hide
    ("SELECT error('sync from http://idp.local:8080/users failed for user bob@example.com: 404') AS {t}",
     "Invalid Input Error: sync from http://idp.local:8080/users failed for user bob@example.com: 404"),
    ("SELECT * FROM read_csv('http://127.0.0.1:1/exports/2024/bob@example.com.csv') AS {t}", "http://127.0.0.1:1/exports/2024/bob@example.com.csv"),
])
def test_review_an_ordinary_error_keeps_its_words(server, admin, review_tables, template, expected):
    tag = tagged()
    failure = run(admin=admin, sql=template.format(t=tag))
    assert failure is not None
    failed = failed_records(server=server, tag=tag)
    assert failed and expected in failed[0]["error"], (failure, failed)
    rows = wait_for(read=lambda: [row for row in stored(admin=admin, tag=tag) if row["error_message"]])
    assert rows and expected in rows[0]["error_message"], rows


def test_review_forms_with_a_hard_password_in_the_monitoring_functions(server, admin, review_tables):
    secret, tag = hard_canary(), tagged()
    worker = server.admin()
    url = literal(text="postgresql://127.0.0.1:1/db?user=svc&password=P@ss" + secret.replace("%", "%25").replace(";", "%3B"))
    slow = (f"SELECT count(*), {url} AS {tag} FROM range(10000000000) a, range(1000) b, mask_users m "
            f"WHERE trim(lower(m.password)) <> {literal(text=secret)} AND upper({literal(text=secret)}) <> m.password")
    thread = threading.Thread(target=lambda: worker.submit(sql=slow, timeout=300), daemon=True)
    thread.start()
    try:
        rows = wait_for(read=lambda: [row for row in admin.query(
            sql="SELECT statement FROM hardcharger_active_connections() WHERE statement IS NOT NULL").to_pylist()
            if tag in row["statement"]], timeout=30)
        assert rows and rows[0]["statement"] == (
            f"SELECT count(*), 'postgresql://127.0.0.1:1/db?user=svc&password=***' AS {tag} FROM range(10000000000) a, "
            "range(1000) b, mask_users m WHERE trim(lower(m.password)) <> *** AND *** <> m.password"), rows
        live = [row for row in admin.query(sql="SELECT sql_text FROM hardcharger_live_queries()").to_pylist() if tag in row["sql_text"]]
        assert live and "CANARY" not in repr(live) and "Zk7" not in repr(live), live
        assert_hard_in_no_sink(server=server, admin=admin, secret=secret)
    finally:
        assert admin.scalar(sql=f"SELECT hardcharger_cancel('{worker.id}')") is True
        thread.join(timeout=60 * TIMEOUT_SCALE)
    assert_hard_in_no_sink(server=server, admin=admin, secret=secret)


def test_review_forms_with_a_hard_password_in_the_audit_record(launcher, store):
    """Under access control, a refused statement of each form the review found goes into the audit
    record as the log shows it: masked, with a password holding ; ) " ' @ : / %."""
    from rbac import start_rbac_server

    server = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                               env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "true",
                                    "HARDCHARGER_ENABLE_INSTRUMENTATION": "true", "HARDCHARGER_LOG_LEVEL": "debug"})
    admin = server.admin()
    secret = hard_canary()
    value = literal(text=secret)
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql="CREATE USER dana WITH PASSWORD 'dana-pass-1'")
    admin.run(sql="GRANT readers TO dana")
    admin.run(sql="CREATE TABLE guarded (i INTEGER, password VARCHAR)")
    dana = server.user(name="dana", password="dana-pass-1")
    refused = {
        "f01": f"SELECT i AS {{t}} FROM guarded WHERE password = {value}; SELECT i AS {{t}}_2 FROM guarded WHERE password = {value}",
        "f02": nested(sql=f"SELECT i AS {{t}} FROM guarded WHERE password = {value}", depth=2),
        "f03-with-insert": f"WITH x AS (SELECT 1) INSERT INTO guarded (i, password) VALUES (1, {value}) RETURNING i AS {{t}}",
        "f03-left": f"SELECT i AS {{t}} FROM guarded WHERE upper({value}) = password",
        "f03-wrapped": f"SELECT i AS {{t}} FROM guarded WHERE trim(lower(password)) = {value}",
        "f04": "SELECT i, " + literal(text="postgresql://127.0.0.1:1/db?user=svc&password=P@ss" + secret.replace("%", "%25")) +
               " AS {t} FROM guarded",
    }
    tags = {}
    for name, template in refused.items():
        tags[name] = tagged()
        with pytest.raises(Exception, match="Hardcharger blocked"):
            dana.run(sql=template.replace("{t}", tags[name]))
    log = wait_for(read=lambda: all(tag in server.log() for tag in tags.values()) and server.log())
    assert log, "not every refused statement reached the log"
    audit = [json.loads(line) for line in log.splitlines() if '"kind":"access_control"' in line and '"status":"denied"' in line]
    for name, tag in tags.items():
        records = [record for record in audit if tag in record.get("sql", "")]
        assert len(records) == 1 and "***" in records[0]["sql"], (name, records)
    assert secret.split("%", 1)[1] not in log and "Zk7" not in log, "the canary reached the log"
    for table in ("sql_statements", "sql_executions"):
        rows = repr(admin.run(sql=f"SELECT * FROM _hardcharger_instr.main.{table}").to_pylist())
        assert secret.split("%", 1)[1] not in rows and "Zk7" not in rows, f"the canary reached {table}"
