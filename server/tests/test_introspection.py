"""Introspection: hardcharger_active_connections(), hardcharger_cancel(),
hardcharger_server_info() and the result_retention_seconds option.

hardcharger_active_connections() has one row per open session, from the server's own
session tables (no instrumentation or metrics needed): who, from where, over what, what it
runs and in what state, its results and uploads. hardcharger_cancel(session_id) stops what
a session runs and keeps the session; KILL SESSION runs the same cancel, then ends the
session. Both are proven to stop the work itself (DuckDB stops, results and uploads are cut
off), not only the client's wait. Who sees and cancels what: an administrator without
catalog_access rules, every session; anyone else, its own principal's (hardcharger_sessions()
follows the same rule).
"""

import base64
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pytest
from cryptography.hazmat.primitives import serialization

from conftest import BINARY, TIMEOUT_SCALE, clean_environment, run_unit_tests
from rbac import expect_allowed, start_rbac_server
from test_mtls_identity import Pki, start as start_tls, thumbprint_of
from warp_client import ARROW_STREAM, ipc_bytes

# A statement that keeps every core busy for minutes, interruptible between chunks.
CPU_BOUND = "SELECT count(*) FROM range(10000000000) a, range(1000) b"
# A result that streams for hours.
STREAM = "SELECT a.range * 2 AS v FROM range(100000000000) a"
# One row whose evaluation DuckDB can't interrupt for a while: a cancel waits for it, so the
# statement visibly unwinds.
UNINTERRUPTIBLE = "SELECT len(list_sort(list_transform(range(30000000), lambda x: (x * 7919) % 30000001))) AS n"
# How long a cancel (or KILL) may take to stop the work.
BOUND = 2.0 * TIMEOUT_SCALE
END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"

COLUMNS = [
    ("session_id", "VARCHAR"), ("instance_id", "VARCHAR"), ("cluster_id", "VARCHAR"), ("username", "VARCHAR"),
    ("role", "VARCHAR"), ("tenant", "VARCHAR"), ("auth_method", "VARCHAR"), ("client_address", "VARCHAR"),
    ("client_info", "VARCHAR"), ("session_tags", "MAP(VARCHAR, VARCHAR)"), ("tls", "BOOLEAN"),
    ("tls_version", "VARCHAR"), ("certificate_identity", "VARCHAR"), ("certificate_thumbprint", "VARCHAR"),
    ("token_certificate_bound", "BOOLEAN"), ("state", "VARCHAR"), ("transaction", "VARCHAR"),
    ("statement_id", "VARCHAR"), ("statement", "VARCHAR"), ("statement_started_at", "TIMESTAMP WITH TIME ZONE"),
    ("queued_since", "TIMESTAMP WITH TIME ZONE"), ("session_started_at", "TIMESTAMP WITH TIME ZONE"),
    ("last_activity_at", "TIMESTAMP WITH TIME ZONE"), ("cancel_reason", "VARCHAR"), ("result_partitions", "INTEGER"),
    ("open_result_streams", "INTEGER"), ("open_ingest_parts", "INTEGER"), ("retained_results", "INTEGER"),
    ("retained_rows", "BIGINT"), ("is_current", "BOOLEAN"),
]

SERVER_INFO_PUBLIC = [
    ("hardcharger_version", "VARCHAR"), ("duckdb_version", "VARCHAR"), ("arrow_version", "VARCHAR"),
    ("instance_id", "VARCHAR"), ("cluster_id", "VARCHAR"), ("instance_tag", "VARCHAR"),
    ("start_time", "TIMESTAMP WITH TIME ZONE"), ("uptime", "INTERVAL"), ("uptime_seconds", "BIGINT"),
]
SERVER_INFO_ADMIN = [
    ("hostname", "VARCHAR"), ("os_platform", "VARCHAR"), ("os_name", "VARCHAR"), ("os_version", "VARCHAR"),
    ("cpu_arch", "VARCHAR"), ("cpu_model", "VARCHAR"), ("cpu_count", "INTEGER"), ("memory_total_bytes", "BIGINT"),
    ("memory_resident_bytes", "BIGINT"), ("database_path", "VARCHAR"), ("read_only", "BOOLEAN"),
    ("listen_address", "VARCHAR"), ("port", "INTEGER"), ("health_port", "INTEGER"), ("metrics_address", "VARCHAR"),
    ("metrics_port", "INTEGER"), ("oauth_enabled", "BOOLEAN"), ("tls_enabled", "BOOLEAN"), ("mtls_enabled", "BOOLEAN"),
    ("mtls_client_certificate_required", "BOOLEAN"), ("server_certificate_sha256", "VARCHAR"),
    ("web_ui_enabled", "BOOLEAN"), ("rbac_store_type", "VARCHAR"), ("session_count", "INTEGER"),
    ("instrumentation_queued", "BIGINT"), ("instrumentation_dropped", "BIGINT"), ("instrumentation_failed", "BIGINT"),
]


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(args=["--enable-instrumentation", "--print-queries"])


# ---- Helpers ----

def connections(viewer, where: str = "true") -> list[dict]:
    return viewer.rows(sql=f"SELECT * FROM hardcharger_active_connections() WHERE {where} ORDER BY session_id")


def row_of(viewer, session_id: str) -> dict | None:
    rows = connections(viewer=viewer, where=f"session_id = '{session_id}'")
    return rows[0] if rows else None


def wait_until(predicate, timeout: float = 10 * TIMEOUT_SCALE, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting")


def wait_state(viewer, session_id: str, state: str) -> dict:
    return wait_until(lambda: (row := row_of(viewer=viewer, session_id=session_id)) and row["state"] == state and row)


def in_background(target) -> tuple[threading.Thread, dict]:
    outcome = {}

    def run():
        try:
            outcome["value"] = target()
        except BaseException as error:  # noqa: BLE001 (the test reads it)
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def cancel(admin, session_id: str) -> bool:
    return admin.scalar(sql=f"SELECT hardcharger_cancel('{session_id}')")


def kill(admin, session_id: str) -> None:
    admin.execute(sql=f"KILL SESSION '{session_id}'")


STOPS = {"cancel": cancel, "kill": kill}
REASONS = {"cancel": "cancelled", "kill": "killed"}


def cpu_seconds(pid: int) -> float:
    """The CPU time process `pid` has used."""
    if sys.platform.startswith("linux"):
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    if sys.platform == "win32":
        # GetProcessTimes, not a PowerShell per reading: on windows-arm64 (x64 Python and
        # PowerShell emulated) each one took seconds, so the test's 0.5 s windows were not.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            times = [wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            return sum(((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7 for t in times[2:])
        finally:
            kernel32.CloseHandle(handle)
    out = subprocess.run(args=["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=True)
    minutes, seconds = out.stdout.strip().split(":")[-2:]
    return int(minutes) * 60 + float(seconds)


def cpu_used(pid: int, seconds: float) -> float:
    before = cpu_seconds(pid=pid)
    time.sleep(seconds)
    return cpu_seconds(pid=pid) - before


def read_to_end(reader: "StreamRead", deadline: float) -> bool:
    """Reads the rest of `reader`'s body; True when it ended with Arrow's end-of-stream
    marker (complete), False when it was cut off (Warp SQL §6.6). Fails past `deadline`."""
    tail = b""
    try:
        for chunk in reader.chunks:
            tail = (tail + chunk)[-len(END_OF_STREAM):]
            if time.monotonic() > deadline:
                raise AssertionError("the result kept streaming")
    except (httpx.RemoteProtocolError, httpx.ReadError):
        return False
    return tail == END_OF_STREAM


class StreamRead:
    """A streamed result read on a connection of its own (a context manager)."""

    def __init__(self, server, session, url: str, method: str = "GET", json_body=None, headers=None):
        self.client = httpx.Client(base_url=server.base_url, timeout=120)
        request = self.client.build_request(method=method, url=url, json=json_body,
                                            headers=session.headers(**{"Accept": ARROW_STREAM, **(headers or {})}))
        self.response = self.client.send(request=request, stream=True)
        # One iterator for the whole read: dropping one closes the response.
        self.chunks = self.response.iter_bytes()

    def first_chunk(self) -> bytes:
        return next(self.chunks)

    def close(self) -> None:
        self.response.close()
        self.client.close()

    def __enter__(self) -> "StreamRead":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def slow_upload(server, session, url: str, params: dict | None = None):
    """POSTs an Arrow stream of ~20 MB to `url` over ~15 s, in the background; returns
    (thread, outcome) once the server has its first chunks."""
    body = ipc_bytes(table=pa.table(data={"v": pa.array(obj=range(2_500_000), type=pa.int64())}))
    sending = threading.Event()

    def chunks():
        for offset in range(0, len(body), 64 * 1024):
            if offset >= 4 * 64 * 1024:
                sending.set()
            yield body[offset:offset + 64 * 1024]
            time.sleep(0.05)

    def upload():
        with httpx.Client(base_url=server.base_url, timeout=120) as client:
            return client.post(url=url, params=params or {}, content=chunks(),
                               headers=session.headers(**{"Content-Type": ARROW_STREAM}))

    thread, outcome = in_background(target=upload)
    assert sending.wait(timeout=30), "the upload never began"
    return thread, outcome


def recorded(admin, sql: str, timeout: float = 10):
    return wait_until(lambda: admin.rows(sql=sql), timeout=timeout)[0]


def describe(session, function: str) -> list[tuple[str, str]]:
    return [(r["column_name"], r["column_type"]) for r in session.rows(sql=f"DESCRIBE SELECT * FROM {function}()")]


# ---- hardcharger_active_connections(): what a row says ----

def test_the_columns(server):
    assert describe(session=server.admin(), function="hardcharger_active_connections") == COLUMNS


def test_it_needs_no_instrumentation_or_metrics(launcher):
    plain = launcher.start()
    admin, other = plain.admin(), plain.admin()
    assert admin.scalar(sql="SELECT HARDCHARGER_INSTRUMENTATION_ENABLED()") is False
    assert "hardcharger_metrics() is disabled" in admin.error(sql="SELECT * FROM hardcharger_metrics()")["detail"]
    ids = {r["session_id"] for r in connections(viewer=admin)}
    assert ids == {admin.id, other.id}, ids


def test_a_row_describes_the_session_and_its_latest_request(server):
    admin = server.admin()
    victim = server.client().new_session(options={"session_tag": {"app": "etl", "run": 7}})
    victim.execute(sql="""SET hardcharger.session_tag = '{"run": "8", "owner": "ops"}'""")
    victim.execute(sql="SELECT 1", headers={"User-Agent": "etl-runner/1.0"})
    row = row_of(viewer=admin, session_id=victim.id)
    instance = admin.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    assert {k: row[k] for k in ("instance_id", "cluster_id", "username", "role", "tenant", "auth_method",
                                "client_info", "tls", "tls_version", "certificate_identity",
                                "certificate_thumbprint", "token_certificate_bound", "state", "transaction",
                                "statement", "statement_started_at", "queued_since", "cancel_reason",
                                "open_result_streams", "open_ingest_parts", "is_current")} == {
        "instance_id": instance, "cluster_id": None, "username": "admin", "role": "admin", "tenant": None,
        "auth_method": "password", "client_info": "etl-runner/1.0", "tls": False, "tls_version": None,
        "certificate_identity": None, "certificate_thumbprint": None, "token_certificate_bound": False,
        "state": "idle", "transaction": "none", "statement": None, "statement_started_at": None,
        "queued_since": None, "cancel_reason": None, "open_result_streams": 0, "open_ingest_parts": 0,
        "is_current": False}, row
    # The option's tags, then SET hardcharger.session_tag's, which win; non-text values as JSON.
    assert dict(row["session_tags"]) == {"app": "etl", "run": "8", "owner": "ops"}, row
    host, port = row["client_address"].rsplit(":", 1)
    assert host == "127.0.0.1" and int(port) > 0, row
    assert row["session_started_at"] <= row["last_activity_at"] <= datetime.datetime.now(tz=datetime.UTC)
    # The caller's own row: running the very statement that reads it.
    own = row_of(viewer=admin, session_id=admin.id)
    assert own["is_current"] and own["state"] == "running", own
    assert own["statement"] == f"SELECT * FROM hardcharger_active_connections() WHERE session_id = '{admin.id}' " \
                               "ORDER BY session_id", own
    assert own["statement_id"].startswith("q_") and own["statement_started_at"] is not None, own


def test_the_state_follows_the_transaction(server):
    admin, victim = server.admin(), server.admin()
    states = []
    for sql in ["BEGIN", "SELECT * FROM no_such_table_here", "ROLLBACK"]:
        try:
            victim.execute(sql=sql)
        except AssertionError:
            pass  # the failing one
        row = row_of(viewer=admin, session_id=victim.id)
        states.append((row["state"], row["transaction"]))
    assert states == [("idle_in_transaction", "open"), ("idle_in_transaction_aborted", "aborted"), ("idle", "none")]


def test_running_then_streaming(server):
    admin, victim = server.admin(), server.admin()
    thread, _ = in_background(target=lambda: victim.error(sql=CPU_BOUND))
    row = wait_state(viewer=admin, session_id=victim.id, state="running")
    assert row["statement"] == CPU_BOUND and row["statement_id"].startswith("q_"), row
    assert row["statement_started_at"] is not None and row["queued_since"] is None, row
    assert cancel(admin=admin, session_id=victim.id) is True
    thread.join(timeout=30)
    # Rows on their way out: streaming, with the reader's stream open.
    with StreamRead(server=server, session=victim, url=f"{victim.url}/statements", method="POST",
                    json_body={"sql": STREAM}) as reader:
        assert reader.response.status_code == 200
        reader.first_chunk()
        row = wait_state(viewer=admin, session_id=victim.id, state="streaming")
        assert row["statement"] == STREAM and row["open_result_streams"] == 1, row
        assert cancel(admin=admin, session_id=victim.id) is True


def test_a_queued_statement_shows_its_text_as_the_log_does(launcher):
    """A queued statement says so, since when, and what it is, its credentials redacted
    exactly as the statement log redacts them (the same code)."""
    queued_server = launcher.start(args=["--max-concurrent-statements", "1", "--print-queries"])
    admin = queued_server.admin()
    holder = queued_server.as_role(role="user", subject="holder")
    victim = queued_server.admin()
    victim.execute(sql="SET SESSION hardcharger.bypass_queue = false")
    holding, _ = in_background(target=lambda: holder.error(sql=CPU_BOUND))
    wait_state(viewer=admin, session_id=holder.id, state="running")
    secret = "CREATE SECRET queued_s3 (TYPE s3, KEY_ID 'AKIAQUEUEDEXAMPLE', SECRET 'hunter2-queued-secret')"
    queued, outcome = in_background(target=lambda: victim.execute(sql=secret))
    row = wait_state(viewer=admin, session_id=victim.id, state="queued")
    assert row["queued_since"] is not None and row["statement_started_at"] is None, row
    assert "hunter2-queued-secret" not in json.dumps(row, default=str) and "AKIAQUEUEDEXAMPLE" not in row["statement"]
    logged = [line.split(" sql=", 1)[1] for line in queued_server.log().splitlines()
              if f"session_id={victim.id}" in line and " statement " in line]
    assert logged[-1:] == [row["statement"]] == ["[REDACTED: secret DDL]"], (logged, row)
    # The twin: it was the secret's statement, which runs once the slot is free.
    assert cancel(admin=admin, session_id=holder.id) is True
    queued.join(timeout=30)
    assert "error" not in outcome, outcome
    assert admin.scalar(sql="SELECT count(*) FROM duckdb_secrets() WHERE name = 'queued_s3'") == 1
    assert wait_state(viewer=admin, session_id=victim.id, state="idle")


def test_retained_results_and_result_partitions(server):
    admin, victim = server.admin(), server.admin()
    submitted = victim.submit(sql="SELECT * FROM range(5000)", headers={"Prefer": "respond-async"}).json()
    victim.wait_done(statement_id=submitted["statement_id"])
    row = row_of(viewer=admin, session_id=victim.id)
    assert (row["retained_results"], row["retained_rows"], row["state"]) == (1, 5000, "idle"), row
    # A result split for a client that reads partitions at once.
    with StreamRead(server=server, session=victim, url=f"{victim.url}/statements", method="POST",
                    json_body={"sql": STREAM}, headers={"Warp-Result-Partitions": "4"}) as reader:
        assert reader.response.headers["warp-result-partitions"] == "4", reader.response.headers
        reader.first_chunk()
        row = wait_state(viewer=admin, session_id=victim.id, state="streaming")
        assert row["result_partitions"] == 4 and row["open_result_streams"] == 1, row
        assert cancel(admin=admin, session_id=victim.id) is True


def test_an_upload_in_flight_is_counted(server):
    admin, victim = server.admin(), server.admin()
    opened = victim.http.post(url=f"{victim.url}/ingests", headers=victim.headers(),
                              json={"table": "counted_parts", "mode": "create_append"})
    assert opened.status_code == 201, opened.text
    ingest_url = f"{victim.url}/ingests/{opened.json()['ingest_id']}"
    thread, outcome = slow_upload(server=server, session=victim, url=f"{ingest_url}/parts/0")
    row = wait_until(lambda: (r := row_of(viewer=admin, session_id=victim.id)) and r["open_ingest_parts"] == 1 and r)
    assert row["open_ingest_parts"] == 1, row
    assert cancel(admin=admin, session_id=victim.id) is True
    thread.join(timeout=30)
    assert wait_until(lambda: row_of(viewer=admin, session_id=victim.id)["open_ingest_parts"] == 0)


def test_tls_and_client_certificate_columns(launcher, tmp_path):
    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    tls_server = start_tls(launcher=launcher, pki=pki, probe=ops,
                           args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    bound = tls_server.client(identity=ops, token=tls_server.certificate_token(identity=ops))
    by_password = tls_server.admin(identity=ops)  # a password token, over the same certificate
    rows = {r["session_id"]: r for r in bound.run(sql="SELECT * FROM hardcharger_active_connections()").to_pylist()}
    expected = {"tls": True, "tls_version": "TLSv1.3", "certificate_identity": "ops@example.com",
                "certificate_thumbprint": thumbprint_of(identity=ops)}
    for session, bound_token, method in [(bound, True, "client_certificate"), (by_password, False, "password")]:
        row = rows[session.session_id]
        assert {k: row[k] for k in expected} == expected, row
        assert row["token_certificate_bound"] is bound_token and row["auth_method"] == method, row


# ---- Who sees whom ----

def test_the_unrestricted_admin_sees_every_session(server):
    bob = server.as_role(role="user", subject="see-bob")
    restricted = server.as_role(role="admin", subject="see-rules", catalog_access=[{"catalog": "*", "access": "read"}])
    admin = server.admin()
    ids = {r["session_id"] for r in connections(viewer=admin)}
    assert {bob.id, restricted.id, admin.id} <= ids
    assert {bob.id, restricted.id, admin.id} <= {r["session_id"] for r in admin.rows(
        sql="SELECT session_id FROM hardcharger_sessions()")}


def test_a_user_sees_its_own_principals_sessions_and_never_anyone_elses_statements(server):
    bob1 = server.as_role(role="user", subject="vis-bob")
    bob2 = server.as_role(role="user", subject="vis-bob")
    carol = server.as_role(role="user", subject="vis-carol")
    marker = "carol-private-7Q"
    running, _ = in_background(target=lambda: carol.error(sql=f"SELECT '{marker}' AS m, count(*) FROM "
                                                              "range(10000000000) a, range(1000) b"))
    admin = server.admin()
    wait_state(viewer=admin, session_id=carol.id, state="running")
    for function in ("hardcharger_active_connections", "hardcharger_sessions"):
        rows = bob1.rows(sql=f"SELECT * FROM {function}()")
        assert {r["session_id"] for r in rows} == {bob1.id, bob2.id}, (function, rows)
        assert {r["is_current"] for r in rows if r["session_id"] == bob1.id} == {True}
        dump = json.dumps(rows, default=str)
        assert marker not in dump and carol.id not in dump and admin.id not in dump, dump
    # The twin: the administrator sees carol's statement.
    assert marker in row_of(viewer=admin, session_id=carol.id)["statement"]
    assert cancel(admin=admin, session_id=carol.id) is True
    running.join(timeout=30)


def test_a_restricted_admin_sees_only_its_own_principals_sessions(server):
    """An administrator token with catalog_access rules is held to them: it sees its own
    principal's sessions, never the unrestricted administrator's (even under the same name)."""
    rules = [{"catalog": "*", "access": "read"}]
    first = server.as_role(role="admin", subject="admin", catalog_access=rules)
    second = server.as_role(role="admin", subject="admin", catalog_access=rules)
    full = server.admin()  # also named admin
    bystander = server.as_role(role="user", subject="vis-bystander")
    for function in ("hardcharger_active_connections", "hardcharger_sessions"):
        ids = {r["session_id"] for r in first.rows(sql=f"SELECT session_id FROM {function}()")}
        assert ids == {first.id, second.id}, (function, ids)
    # The twin: the unrestricted administrator sees all four.
    assert {first.id, second.id, full.id, bystander.id} <= {r["session_id"] for r in connections(viewer=full)}


def test_a_tenants_user_sees_its_tenant_and_only_its_sessions(launcher, store):
    rbac = start_rbac_server(launcher=launcher, store_uri=store.new_database())
    admin = rbac.admin()
    for sql in ["CREATE TENANT acme", "CREATE USER acme/joe", "CREATE TENANT beta", "CREATE USER beta/joe"]:
        expect_allowed(client=admin, sql=sql)
    joe = rbac.user(name="acme/joe")
    joe_again = rbac.user(name="acme/joe")
    beta_joe = rbac.user(name="beta/joe")
    rows = joe.run(sql="SELECT session_id, username, tenant FROM hardcharger_active_connections()").to_pylist()
    assert sorted(rows, key=lambda r: r["session_id"]) == sorted(
        [{"session_id": s.session_id, "username": "acme/joe", "tenant": "acme"} for s in (joe, joe_again)],
        key=lambda r: r["session_id"]), rows
    assert beta_joe.session_id not in json.dumps(rows)
    # The twin: the administrator sees both tenants' sessions, with their tenants.
    tenants = {r["session_id"]: r["tenant"] for r in admin.run(
        sql="SELECT session_id, tenant FROM hardcharger_active_connections()").to_pylist()}
    assert tenants[beta_joe.session_id] == "beta" and tenants[joe.session_id] == "acme", tenants


# ---- hardcharger_cancel() ----

NOT_YOURS = ("Session '{id}' is not one of your sessions: only an administrator without catalog_access rules "
             "may cancel another user's session")


def test_cancelling_an_idle_session_returns_false_and_keeps_it(server):
    admin, victim = server.admin(), server.admin()
    assert cancel(admin=admin, session_id=victim.id) is False
    assert victim.scalar(sql="SELECT 42") == 42
    assert row_of(viewer=admin, session_id=victim.id)["state"] == "idle"


def test_cancelling_someone_elses_session_is_refused(server):
    carol = server.as_role(role="user", subject="cancel-carol")
    carol_again = server.as_role(role="user", subject="cancel-carol")
    bob = server.as_role(role="user", subject="cancel-bob")
    restricted = server.as_role(role="admin", subject="cancel-rules", catalog_access=[{"catalog": "*", "access": "read"}])
    admin = server.admin()
    running, outcome = in_background(target=lambda: carol.error(sql=CPU_BOUND))
    wait_state(viewer=admin, session_id=carol.id, state="running")
    for caller in (bob, restricted):
        refused = caller.error(sql=f"SELECT hardcharger_cancel('{carol.id}')")
        assert refused["code"] == "permission_denied", refused
        assert refused["detail"] == "Permission Error: " + NOT_YOURS.format(id=carol.id), refused
        # A session that doesn't exist gets the same answer: nothing about others' sessions leaks.
        unknown = caller.error(sql="SELECT hardcharger_cancel('s_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')")
        assert unknown["detail"] == "Permission Error: " + NOT_YOURS.format(id="s_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert row_of(viewer=admin, session_id=carol.id)["state"] == "running"  # untouched
    # The twin: carol's other session may cancel it (the same principal), by any case.
    assert carol_again.scalar(sql=f"SELECT hardcharger_cancel('{carol.id.upper()}')") is True
    running.join(timeout=BOUND + 5)
    assert not running.is_alive() and outcome["value"]["code"] == "statement_cancelled", outcome


def test_what_cancel_refuses_and_returns(server):
    admin = server.admin()
    own = admin.error(sql=f"SELECT hardcharger_cancel('{admin.id}')")
    assert own["detail"] == "Invalid Input Error: hardcharger_cancel() can't cancel the session that calls it: " \
                            "call it from another session", own
    missing = admin.error(sql="SELECT hardcharger_cancel('s_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')")
    assert missing["detail"] == "Invalid Input Error: Session 's_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' not found", missing
    assert admin.scalar(sql="SELECT hardcharger_cancel(NULL)") is None


def test_cancel_through_a_query_over_the_connections(server):
    admin = server.admin()
    dave = [server.as_role(role="user", subject="cancel-dave") for _ in range(2)]
    threads = [in_background(target=lambda s=s: s.error(sql=CPU_BOUND))[0] for s in dave]
    for session in dave:
        wait_state(viewer=admin, session_id=session.id, state="running")
    stopped = admin.scalar(sql="SELECT count(*) FILTER (WHERE hardcharger_cancel(session_id)) FROM "
                               "hardcharger_active_connections() WHERE username = 'cancel-dave'")
    assert stopped == 2
    for thread in threads:
        thread.join(timeout=BOUND + 5)
        assert not thread.is_alive()


# ---- Cancel and KILL stop the work, not just the wait ----
# Each of these fails with the cancel taken out of KILL SESSION's path (CancelSessionWork
# in Session::RunCommand): ending a session waits for its work, it doesn't stop it.

@pytest.mark.parametrize("stop", list(STOPS))
def test_a_cpu_bound_statement_stops_within_bounds(server, stop):
    admin, victim = server.admin(), server.admin()
    thread, outcome = in_background(target=lambda: victim.error(sql=CPU_BOUND))
    wait_state(viewer=admin, session_id=victim.id, state="running")
    pid = server.process.pid
    # The positive control: the statement keeps the server's CPU busy. Counted since it began
    # running, not as most of a core in half a second: a loaded runner (CI's linux-amd64 has
    # 2 CPUs for 6 test workers) may give it only part of one. A quiet server uses far less.
    before = cpu_seconds(pid=pid)
    assert wait_until(lambda: cpu_seconds(pid=pid) - before > 1.0, timeout=10 * TIMEOUT_SCALE, interval=0.1)
    started = time.monotonic()
    STOPS[stop](admin=admin, session_id=victim.id)
    thread.join(timeout=BOUND)
    assert not thread.is_alive(), f"the client still waits {BOUND} s after {stop}"
    assert time.monotonic() - started < BOUND
    problem = outcome["value"]
    assert problem["code"] == "statement_cancelled", problem
    assert problem["detail"] == f"the statement was cancelled ({REASONS[stop]})", problem
    # DuckDB itself stopped: the session's engine is free (its row idle, or gone), and the
    # server's CPU is quiet.
    if stop == "cancel":
        assert wait_until(lambda: row_of(viewer=admin, session_id=victim.id)["state"] == "idle", timeout=BOUND)
    else:
        assert wait_until(lambda: row_of(viewer=admin, session_id=victim.id) is None, timeout=BOUND)
    # Within the bound, not at once: an instrumented (TSan) server's last work after the
    # cancel can spill into the first second it is measured (0.29 s once).
    used = []
    assert wait_until(lambda: used.append(cpu_used(pid=pid, seconds=1.0)) or used[-1] < 0.25,
                      timeout=BOUND + 2, interval=0), f"the server still used {used} s of CPU per second"
    # Instrumentation records why.
    row = recorded(admin=admin, sql="SELECT status, cancel_reason FROM _hardcharger_instr.execution_details "
                                    f"WHERE session_id = '{victim.id}' AND sql_text = '{CPU_BOUND}' "
                                    "AND status <> 'executing'")
    assert row == {"status": "cancelled", "cancel_reason": REASONS[stop]}, row
    if stop == "cancel":
        # A cancelled session goes on.
        assert victim.scalar(sql="SELECT 42") == 42
    else:
        # A killed one is gone: its token's next request says so, and it can't be killed twice.
        problem = victim.error(sql="SELECT 1")
        assert problem["code"] == "session_ended" and problem["reason"] == "killed", problem
        assert admin.error(sql=f"KILL SESSION '{victim.id}'")["detail"] == f"Session '{victim.id}' not found"
        assert victim.close().status_code == 204


@pytest.mark.parametrize("stop", list(STOPS))
def test_the_reason_shows_while_the_statement_unwinds(server, stop):
    admin, victim = server.admin(), server.admin()
    thread, outcome = in_background(target=lambda: victim.error(sql=UNINTERRUPTIBLE))
    wait_state(viewer=admin, session_id=victim.id, state="running")
    STOPS[stop](admin=admin, session_id=victim.id)
    row = row_of(viewer=admin, session_id=victim.id)
    assert row is not None and row["state"] == "running", row  # DuckDB finishes the row it is on
    assert row["cancel_reason"] == REASONS[stop], row
    thread.join(timeout=30)
    assert outcome["value"]["detail"] == f"the statement was cancelled ({REASONS[stop]})", outcome


@pytest.mark.parametrize("stop", list(STOPS))
def test_a_streaming_result_is_cut_off(server, stop):
    admin, victim = server.admin(), server.admin()
    with StreamRead(server=server, session=victim, url=f"{victim.url}/statements", method="POST",
                    json_body={"sql": STREAM}) as reader:
        assert reader.response.status_code == 200
        reader.first_chunk()
        wait_state(viewer=admin, session_id=victim.id, state="streaming")
        STOPS[stop](admin=admin, session_id=victim.id)
        assert read_to_end(reader=reader, deadline=time.monotonic() + BOUND) is False


@pytest.mark.parametrize("stop", list(STOPS))
def test_partitioned_results_are_cut_off(server, stop):
    admin, victim = server.admin(), server.admin()
    first = StreamRead(server=server, session=victim, url=f"{victim.url}/statements", method="POST",
                       json_body={"sql": STREAM}, headers={"Warp-Result-Partitions": "3"})
    readers = [first]
    try:
        assert first.response.headers["warp-result-partitions"] == "3", first.response.headers
        statement_id = first.response.headers["warp-statement-id"]
        for partition in (1, 2):
            readers.append(StreamRead(server=server, session=victim,
                                      url=f"{victim.url}/statements/{statement_id}/result?partition={partition}"))
        for reader in readers:
            assert reader.response.status_code == 200
        results = [None] * len(readers)

        def read(index: int):
            results[index] = read_to_end(reader=readers[index], deadline=time.monotonic() + 60)

        threads = [threading.Thread(target=read, args=(i,), daemon=True) for i in range(len(readers))]
        for thread in threads:
            thread.start()
        wait_until(lambda: row_of(viewer=admin, session_id=victim.id)["open_result_streams"] == 3)
        STOPS[stop](admin=admin, session_id=victim.id)
        deadline = time.monotonic() + BOUND
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), f"a partition kept streaming after {stop}"
        assert results == [False, False, False], results
    finally:
        for reader in readers:
            reader.close()


@pytest.mark.parametrize("stop", list(STOPS))
def test_an_upload_in_flight_is_aborted(server, stop):
    admin, victim = server.admin(), server.admin()
    table = f"aborted_parts_{stop}"
    opened = victim.http.post(url=f"{victim.url}/ingests", headers=victim.headers(),
                              json={"table": table, "mode": "create_append"})
    assert opened.status_code == 201, opened.text
    ingest_url = f"{victim.url}/ingests/{opened.json()['ingest_id']}"
    thread, outcome = slow_upload(server=server, session=victim, url=f"{ingest_url}/parts/0")
    wait_until(lambda: row_of(viewer=admin, session_id=victim.id)["open_ingest_parts"] == 1)
    STOPS[stop](admin=admin, session_id=victim.id)
    if stop == "cancel":
        ingest = victim.http.get(url=ingest_url, headers=victim.headers()).json()
        assert ingest["state"] == "aborted", ingest
        assert ingest["error"]["detail"] == "the ingest was aborted (cancelled)", ingest
    # The upload's answer is its refusal. (The client sends until the server closes the
    # connection, which it does after a lingering close of up to 2 s, so the client's
    # sending stops a little after the ingest did; the upload alone would take ~15 s.)
    thread.join(timeout=BOUND + 3)
    assert not thread.is_alive(), f"the upload went on after {stop}"
    assert "error" in outcome or outcome["value"].status_code != 200, outcome
    if stop == "cancel":
        assert victim.scalar(sql="SELECT 42") == 42
    # Nothing was written.
    assert admin.scalar(sql=f"SELECT count(*) FROM duckdb_tables() WHERE table_name = '{table}'") == 0


# ---- hardcharger_server_info() ----

def test_server_info_columns_and_what_everyone_sees(server):
    admin = server.admin()
    user = server.as_role(role="user", subject="info-user")
    assert describe(session=admin, function="hardcharger_server_info") == SERVER_INFO_PUBLIC + SERVER_INFO_ADMIN
    first = user.rows(sql="SELECT * FROM hardcharger_server_info()")[0]
    assert first["hardcharger_version"] == admin.scalar(sql="SELECT HARDCHARGER_VERSION()")
    assert first["duckdb_version"] == admin.scalar(sql="SELECT version()")
    assert first["instance_id"] == admin.scalar(sql="SELECT HARDCHARGER_CURRENT_INSTANCE()")
    assert first["start_time"] <= datetime.datetime.now(tz=datetime.UTC)
    # Everything else is the unrestricted administrator's: NULL, not an error.
    assert {name: first[name] for name, _ in SERVER_INFO_ADMIN} == {name: None for name, _ in SERVER_INFO_ADMIN}
    time.sleep(1.1)
    later = user.rows(sql="SELECT * FROM hardcharger_server_info()")[0]
    assert later["uptime_seconds"] >= first["uptime_seconds"] + 1 and later["start_time"] == first["start_time"]
    # A restricted administrator sees what a user sees.
    restricted = server.as_role(role="admin", subject="info-rules", catalog_access=[{"catalog": "*", "access": "read"}])
    row = restricted.rows(sql="SELECT * FROM hardcharger_server_info()")[0]
    assert {name: row[name] for name, _ in SERVER_INFO_ADMIN} == {name: None for name, _ in SERVER_INFO_ADMIN}


def test_server_info_for_the_administrator_matches_the_instances_row(server):
    admin = server.admin()
    info = admin.rows(sql="SELECT * FROM hardcharger_server_info()")[0]
    assert info["hostname"] == socket.gethostname() and info["cpu_count"] == os.cpu_count(), info
    assert info["memory_total_bytes"] > 0 and 0 < info["memory_resident_bytes"] < info["memory_total_bytes"], info
    assert info["port"] == server.port and info["listen_address"] == "127.0.0.1", info
    assert (info["tls_enabled"], info["mtls_enabled"], info["server_certificate_sha256"]) == (False, False, None)
    assert (info["web_ui_enabled"], info["rbac_store_type"], info["read_only"]) == (False, "none", False), info
    sessions = admin.scalar(sql="SELECT count(*) FROM hardcharger_sessions()")
    assert info["session_count"] == sessions, (info, sessions)
    # The instrumentation writer's backlog and losses (this server records instrumentation).
    assert info["instrumentation_queued"] >= 0 and (info["instrumentation_dropped"], info["instrumentation_failed"]) == (0, 0)
    # One collection at startup feeds both: no drift.
    shared = ["instance_id", "hardcharger_version", "duckdb_version", "arrow_version", "hostname", "os_platform",
              "os_name", "os_version", "cpu_arch", "cpu_model", "cpu_count", "memory_total_bytes", "start_time"]
    instance = admin.rows(sql=f"SELECT {', '.join(shared)} FROM _hardcharger_instr.instances "
                              f"WHERE instance_id = '{info['instance_id']}'")[0]
    instance["instance_id"] = str(instance["instance_id"])
    assert instance == {k: info[k] for k in shared}, (instance, info)
    assert info["cpu_model"], info  # recorded now (it was NULL before), on ARM Linux too


def test_cpu_model_from_cpuinfo_names_arm_cores_by_their_part():
    """Most ARM kernels (AWS Graviton, Azure Cobalt, GitHub's arm64 runners) name no model in
    /proc/cpuinfo; the CPU implementer and part do (server/unit/platform_test.cc)."""
    results = run_unit_tests(gtest_filter="CpuModelFromCpuinfo.*")
    assert len(results) == 5 and all(results.values()), results


def test_server_info_with_tls(launcher, tmp_path):
    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    tls_server = start_tls(launcher=launcher, pki=pki, probe=ops,
                           args=["--mtls-certificate-sign-in", "--mtls-admin-identities", "ops@example.com"])
    der = pki.server[2].public_bytes(encoding=serialization.Encoding.DER)
    fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    info = tls_server.admin(identity=ops).run(sql="SELECT * FROM hardcharger_server_info()").to_pylist()[0]
    assert (info["tls_enabled"], info["mtls_enabled"], info["mtls_client_certificate_required"]) == (True, True, True)
    assert info["server_certificate_sha256"] == fingerprint, info
    # No instrumentation here: its writer's columns are NULL.
    assert (info["instrumentation_queued"], info["instrumentation_dropped"]) == (None, None), info


# ---- result_retention_seconds ----

def expires_after_finish(doc: dict) -> float:
    finished = datetime.datetime.fromisoformat(doc["finished_at"])
    expires = datetime.datetime.fromisoformat(doc["result"]["expires_at"])
    return (expires - finished).total_seconds()


def retained(session) -> dict:
    submitted = session.submit(sql="SELECT 1 AS one", headers={"Prefer": "respond-async"}).json()
    return session.wait_done(statement_id=submitted["statement_id"])


def test_result_retention_seconds_sets_expires_at_and_the_expiry(launcher, tmp_path):
    # The default is an hour.
    assert expires_after_finish(retained(session=launcher.start().admin())) == pytest.approx(3600, abs=1)
    short = launcher.start(args=["--result-retention-seconds", "2"])
    session = short.admin()
    doc = retained(session=session)
    assert expires_after_finish(doc) == pytest.approx(2, abs=1)
    assert session.result(statement_id=doc["statement_id"]).status_code == 200
    # Gone once it expires (the server sweeps every 5 s).
    response = wait_until(lambda: (r := session.result(statement_id=doc["statement_id"])).status_code == 410 and r,
                          timeout=15 * TIMEOUT_SCALE, interval=0.5)
    assert response.json()["code"] == "result_expired", response.text
    # From the environment and from YAML too.
    by_env = launcher.start(env={"HARDCHARGER_RESULT_RETENTION_SECONDS": "120"})
    assert expires_after_finish(retained(session=by_env.admin())) == pytest.approx(120, abs=1)
    config = tmp_path / "retention.yaml"
    config.write_text("result_retention_seconds: 300\n")
    by_yaml = launcher.start(args=["--config", str(config)])
    assert expires_after_finish(retained(session=by_yaml.admin())) == pytest.approx(300, abs=1)


def test_a_read_in_progress_when_the_result_expires_runs_to_the_end(launcher):
    """Expiry applies to reads that start afterwards (410 result_expired); one that has started
    gets every batch and the end-of-stream marker (Warp SQL spec §6.5). Windows CI once saw a
    poll of a 2 s retention cut mid-body ("incomplete chunked read") by the sweep. Here the read
    stalls mid-stream (a result far larger than what the sockets buffer) until a new read gets 410,
    while the server still counts it as an open stream: the expiry happened during the read."""
    short = launcher.start(args=["--result-retention-seconds", "1"])
    session, admin = short.admin(), short.admin()
    rows = 8_000_000  # 64 MB of BIGINT
    submitted = session.submit(sql=f"SELECT range AS v FROM range({rows})", headers={"Prefer": "respond-async"})
    doc = session.wait_done(statement_id=submitted.json()["statement_id"])
    assert doc["state"] == "succeeded", doc
    url = f"{session.url}/statements/{doc['statement_id']}/result"
    with StreamRead(server=short, session=session, url=url) as read:
        assert read.response.status_code == 200
        body = bytearray(read.first_chunk())
        # Gone for a read that starts now (the server sweeps every 5 s); polled from past the
        # last batch, so each poll before the expiry is a schema and the end, not 64 MB ...
        def poll():
            r = session.result(statement_id=doc["statement_id"], from_batch=1_000_000)
            return r.status_code == 410 and r

        expired = wait_until(poll, timeout=15 * TIMEOUT_SCALE, interval=0.25)
        assert expired.json()["code"] == "result_expired", expired.text
        # ... while this one is still being sent.
        assert row_of(viewer=admin, session_id=session.id)["open_result_streams"] == 1, "the expiry ended the read"
        for chunk in read.chunks:
            body += chunk
    assert body.endswith(END_OF_STREAM), "the read that started before the expiry was cut"
    table = pa.ipc.open_stream(source=pa.BufferReader(obj=bytes(body))).read_all()
    assert table.num_rows == rows
    assert pc.sum(table.column(i="v")).as_py() == rows * (rows - 1) // 2
    # The batches went when that read ended.
    assert wait_until(lambda: row_of(viewer=admin, session_id=session.id)["retained_results"] == 0)


@pytest.mark.parametrize("value", ["0", "-1", "2592001"])
def test_result_retention_seconds_is_checked(value):
    out = subprocess.run(args=[str(BINARY), "--result-retention-seconds", value], capture_output=True, text=True,
                         env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": "pw"}, timeout=30)
    assert out.returncode == 2
    assert f"result_retention_seconds must be from 1 to 2592000 (30 days), not {value}" in out.stderr, out.stderr


def test_result_retention_seconds_is_documented():
    help_text = subprocess.run(args=[str(BINARY), "--help"], capture_output=True, text=True, timeout=30).stdout
    assert "--result-retention-seconds" in help_text and "HARDCHARGER_RESULT_RETENTION_SECONDS" in help_text
    options = json.loads(subprocess.run(args=[str(BINARY), "--print-options-json"], capture_output=True, text=True,
                                        timeout=30).stdout)["options"]
    option = next(o for o in options if o["name"] == "result_retention_seconds")
    assert (option["default"], option["type"], option["yaml_key"]) == ("3600", "integer", "result_retention_seconds")
