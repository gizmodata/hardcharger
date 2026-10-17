"""Regression tests for credential redaction in logs, instrumentation and the catalog
logs. A security review found that comments, DuckDB parse errors, ATTACH connection
strings and credential settings all got clear text past the redactor.

Each case plants a unique marker and asserts it appears in none of the durable sinks:
the server's log output, _gizmosql_instr.sql_statements / .sql_executions, the
_gizmosql_logs catalog, and (for a store user) the access-control audit record. Each has
a positive twin: an ordinary statement is still logged in full.
"""

import time
import uuid

import pytest


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(
        features=["instrumentation"],
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
        if admin.scalar(sql="SELECT count(*) FROM _gizmosql_instr.sql_statements") >= 0:
            time.sleep(0.4)
            return


def sinks(server, admin) -> str:
    """Everything a marker must not appear in."""
    drain(admin=admin)
    rows = admin.query(sql="SELECT sql_text, prepare_error FROM _gizmosql_instr.sql_statements").to_pylist()
    execs = admin.query(sql="SELECT error_message FROM _gizmosql_instr.sql_executions").to_pylist()
    logs = admin.query(sql="SELECT message, fields::VARCHAR AS f FROM _gizmosql_logs.main.logs").to_pylist()
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
    running = launcher.start(features=["instrumentation", "catalog_permissions"],
                             args=["--enable-instrumentation", "--enable-catalog-logging",
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
