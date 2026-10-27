"""Port of GizmoSQL 1.x tests/integration/test_metrics.cpp (11 cases: 7 unit, 4 server).

Runtime metrics: enable_metrics (--enable-metrics,
HARDCHARGER_ENABLE_METRICS) serves GET /metrics on metrics_port (Prometheus text, 404
elsewhere) and hardcharger_metrics() in SQL, from the same registry, under 1.x's metric
names. The registry's unit cases are C++ (server/unit/metrics_test.cc), run here. 1.x
read its registry in-process; here the server cases read it through hardcharger_metrics(),
which counts as a statement itself, and the deltas allow for that.

All 11 apply. The exit-marker case (a read-only server never touches the marker) runs
against real servers on a database file.
"""

import datetime
import ipaddress
import math
import pathlib
import sys
import threading
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from conftest import TIMEOUT_SCALE, free_port, run_unit_tests
from test_mtls_identity import Pki
from test_mtls_identity import start as start_tls

UNIT_CASES = ["UpdatesAreAtomicUnderContention", "NothingIsCountedWhileDisabled",
              "HistogramsAreCumulativeAndCountEveryOutcome", "SerializationEscapesLabelsAndHasOneTypePerFamily",
              "ParsedStatementClassification", "ErrorClassification"]


@pytest.fixture(scope="module")
def unit_results():
    return run_unit_tests(gtest_filter="MetricsRegistryTest.*")


@pytest.mark.parametrize("case", UNIT_CASES)
def test_unit_case(unit_results, case):
    assert unit_results[f"MetricsRegistryTest.{case}"] is True


@pytest.fixture(scope="module")
def metrics_port():
    return free_port()


@pytest.fixture(scope="module")
def server(module_launcher, metrics_port):
    return module_launcher.start(args=["--enable-metrics", "--metrics-port", str(metrics_port),
                                                             "--metrics-bind-address", "127.0.0.1"])


@pytest.fixture
def session(server):
    return server.admin()


def metric(session, name: str, **labels) -> float:
    where = " AND ".join([f"name = '{name}'"] + [f"labels['{k}'] = '{v}'" for k, v in labels.items()])
    return session.scalar(sql=f"SELECT value FROM hardcharger_metrics() WHERE {where}")


def test_read_only_servers_never_touch_the_exit_marker(launcher, tmp_path):
    database = tmp_path / "metrics_marker.duckdb"
    marker = tmp_path / "metrics_marker.duckdb.hardcharger-metrics-state"
    writer = launcher.start(args=["--enable-metrics", "--metrics-port", "0", "--database", str(database)])
    assert marker.exists()
    assert metric(writer.admin(), "hardcharger_last_exit_unclean") == 0
    writer.stop()
    marker.unlink()
    reader = launcher.start(args=["--enable-metrics", "--metrics-port", "0", "--database",
                                                        str(database), "--read-only"])
    assert math.isnan(metric(reader.admin(), "hardcharger_last_exit_unclean"))
    reader.stop()
    assert not marker.exists()


def test_http_and_sql_expose_the_same_registry(server, session, metrics_port):
    response = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics")
    assert response.status_code == 200 and "text/plain" in response.headers["content-type"]
    assert "hardcharger_build_info{" in response.text
    assert session.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_metrics'") == "true"
    before = metric(session, "hardcharger_statements_total", status="ok")
    session.execute(sql="CREATE OR REPLACE TABLE metrics_ledger(n INTEGER)")
    doc = session.execute(sql="INSERT INTO metrics_ledger VALUES (1)")
    after = metric(session, "hardcharger_statements_total", status="ok")
    assert after - before == 3  # two statements, and the first metrics read itself
    session.status(statement_id=doc["statement_id"])  # re-reading a statement counts nothing
    assert metric(session, "hardcharger_statements_total", status="ok") - after == 1
    assert metric(session, "hardcharger_session_limit") == 0
    assert session.scalar(sql="SELECT labels['server'] FROM hardcharger_metrics() WHERE name = 'hardcharger_build_info'") \
        == "hardcharger"
    assert "hardcharger_session_limit 0\n" in httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics").text
    assert httpx.get(url=f"http://127.0.0.1:{metrics_port}/not-metrics").status_code == 404


def test_collector_refreshes_without_scrapes(session):
    assert metric(session, "hardcharger_metrics_collection_success") == 1
    assert metric(session, "hardcharger_metrics_last_collection_success_seconds") > time.time() - 60
    assert metric(session, "process_resident_memory_bytes") > 0
    assert metric(session, "hardcharger_duckdb_memory_limit_bytes") > 0


def test_parser_and_catalog_failures_are_counted(session):
    before = metric(session, "hardcharger_statements_total", status="error")
    session.error(sql="SELECT FROM")
    session.error(sql="SELECT * FROM metrics_missing_table")
    assert metric(session, "hardcharger_statements_total", status="error") - before == 2
    assert metric(session, "hardcharger_statement_errors_total", **{"class": "syntax"}) >= 1
    assert metric(session, "hardcharger_statement_errors_total", **{"class": "catalog"}) >= 1


def test_transaction_states_follow_the_session(server, session):
    watcher = server.admin()
    session.execute(sql="CREATE OR REPLACE TABLE metrics_transactions(n INTEGER PRIMARY KEY)")
    session.execute(sql="INSERT INTO metrics_transactions VALUES (1)")
    session.execute(sql="BEGIN TRANSACTION")
    assert session.scalar(sql="SELECT 42") == 42
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction") == 1
    # DuckDB keeps a transaction usable after a syntax error: still open, not aborted, and
    # the session document says the same (Warp SQL §5.2).
    session.error(sql="SELEC 1")
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction") == 1
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction_aborted") == 0
    assert session.scalar(sql="SELECT 43") == 43
    session.error(sql="INSERT INTO metrics_transactions VALUES (1)")
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction_aborted") == 1
    session.execute(sql="ROLLBACK")
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction") == 0
    assert metric(watcher, "hardcharger_sessions", state="idle_in_transaction_aborted") == 0


def test_instrumentation_writer_gauges(launcher, server, session):
    """With instrumentation on, /metrics shows its writer: the backlog and the records it
    dropped (a full queue, or left at the shutdown deadline) or failed to write. Without
    it, no such family."""
    port = free_port()
    instrumented = launcher.start(args=["--enable-metrics", "--metrics-port", str(port), "--metrics-bind-address",
                                        "127.0.0.1", "--enable-instrumentation"])
    admin = instrumented.admin()
    assert admin.scalar(sql="SELECT 1") == 1
    assert metric(admin, "hardcharger_instrumentation_dropped_total") == 0
    assert metric(admin, "hardcharger_instrumentation_failed_total") == 0
    assert metric(admin, "hardcharger_instrumentation_queue_depth") >= 0
    text = httpx.get(url=f"http://127.0.0.1:{port}/metrics").text
    assert "# TYPE hardcharger_instrumentation_dropped_total counter\nhardcharger_instrumentation_dropped_total 0\n" \
        in text, text
    assert session.scalar(sql="SELECT count(*) FROM hardcharger_metrics() WHERE name LIKE 'hardcharger_instrumentation%'") \
        == 0


# ---- The engine, host and process gauges GizmoData Cloud reads (1.x's names, hardcharger_ prefix) ----

TEST_STARTED = time.time()


# A scrape answers in milliseconds (at most 12 ms while test_spill_and_temporary_storage spills,
# on an M-series Mac), but a loaded Windows runner once stalled one past httpx's 5 s default
# (2026-10-27). The tests check the values, not the latency: wait as long as any other request.
SCRAPE_TIMEOUT = 30 * TIMEOUT_SCALE


def scrape(port: int) -> dict[str, float]:
    """The unlabelled samples of one /metrics scrape, by name."""
    text = httpx.get(url=f"http://127.0.0.1:{port}/metrics", timeout=SCRAPE_TIMEOUT).text
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "{" not in line:
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def test_engine_host_and_process_gauges(session, metrics_port):
    scraped = scrape(port=metrics_port)
    threads = session.scalar(sql="SELECT current_setting('threads')")
    assert threads >= 1
    for read in (scraped.get, lambda name: metric(session, name)):
        assert read("hardcharger_duckdb_threads") == threads
        assert read("hardcharger_host_memory_bytes") > 0
        assert read("hardcharger_host_cpus") >= 1
        # Started by this test run (the module's server), and not in the future.
        assert TEST_STARTED - 60 <= read("process_start_time_seconds") <= time.time() + 1
        assert read("process_threads") >= 1
        assert read("hardcharger_duckdb_temp_storage_bytes") >= 0
        assert read("hardcharger_duckdb_spill_files") >= 0
        assert read("hardcharger_duckdb_spill_bytes") >= 0
        assert 0 <= read("hardcharger_metrics_collection_duration_seconds") < 5
        assert 0 <= read("hardcharger_health_check_duration_seconds") < 5
    if sys.platform != "win32":
        assert scraped["process_open_fds"] > 0
        assert metric(session, "process_open_fds") > 0
    # threads follows SET GLOBAL threads.
    session.execute(sql=f"SET GLOBAL threads = {threads + 1}")
    try:
        assert metric(session, "hardcharger_duckdb_threads") == threads + 1
    finally:
        session.execute(sql=f"SET GLOBAL threads = {threads}")
    # An in-memory database has no file: no file or WAL size, not a zero.
    assert "hardcharger_database_file_bytes" not in scraped and "hardcharger_wal_bytes" not in scraped
    # No TLS: no certificate expiry.
    assert "hardcharger_tls_certificate_expiry_seconds" not in scraped
    # A container's limits, when this runs in one (Linux cgroups); never on macOS or Windows.
    for name in ("hardcharger_cgroup_memory_limit_bytes", "hardcharger_cgroup_cpu_quota_cores"):
        if sys.platform != "linux":
            assert name not in scraped
        elif name in scraped:
            assert scraped[name] > 0


def test_every_family_has_help_and_type(metrics_port):
    text = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics").text
    families = {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}
    described = {line.split()[2] for line in text.splitlines() if line.startswith("# HELP ")}
    assert {"hardcharger_duckdb_threads", "process_start_time_seconds", "hardcharger_host_cpus",
            "hardcharger_metrics_collection_duration_seconds"} <= families
    assert families == described, families - described


def test_cgroup_limits_unit_cases():
    """cgroup v1 and v2 limits, and "no limit" in each (server/unit/platform_test.cc)."""
    results = run_unit_tests(gtest_filter="CGroupLimits.*:ProcessStats.*")
    assert len(results) == 6 and all(results.values()), results


def test_database_file_and_wal_bytes(launcher, tmp_path):
    database = tmp_path / "metrics_files.duckdb"
    port = free_port()
    running = launcher.start(args=["--enable-metrics", "--metrics-port", str(port), "--metrics-bind-address",
                                   "127.0.0.1", "--database", str(database)])
    admin = running.admin()
    admin.execute(sql="CHECKPOINT")
    assert scrape(port=port)["hardcharger_database_file_bytes"] == database.stat().st_size
    assert metric(admin, "hardcharger_database_file_bytes") == database.stat().st_size
    wal = pathlib.Path(f"{database}.wal")
    assert scrape(port=port)["hardcharger_wal_bytes"] == (wal.stat().st_size if wal.exists() else 0)
    # A write goes to the WAL until a checkpoint moves it into the file.
    admin.execute(sql="CREATE TABLE metrics_wal AS SELECT range AS n FROM range(1000)")
    grown = scrape(port=port)["hardcharger_wal_bytes"]
    assert grown > 0 and grown == wal.stat().st_size
    admin.execute(sql="CHECKPOINT")
    after = scrape(port=port)
    assert after["hardcharger_wal_bytes"] == (wal.stat().st_size if wal.exists() else 0) < grown
    assert after["hardcharger_database_file_bytes"] == database.stat().st_size


def test_spill_and_temporary_storage(launcher, tmp_path):
    """A sort larger than memory_limit spills to the temp directory: while it runs, the
    spill and temporary-storage gauges show it; afterwards they fall back."""
    port = free_port()
    running = launcher.start(args=["--enable-metrics", "--metrics-port", str(port), "--metrics-bind-address",
                                   "127.0.0.1", "--database", str(tmp_path / "metrics_spill.duckdb")])
    admin = running.admin()
    admin.execute(sql="SET GLOBAL memory_limit = '256MB'")
    admin.execute(sql="SET GLOBAL threads = 2")
    admin.execute(sql="SET GLOBAL preserve_insertion_order = false")
    done = threading.Event()
    failure: list[Exception] = []

    def sort():
        try:
            sorter = running.admin()
            response = sorter.submit(sql="CREATE TABLE metrics_spilled AS SELECT range AS n, random() AS r "
                                         "FROM range(30000000) ORDER BY r",
                                     headers={"Prefer": "respond-async"})
            assert response.status_code in (200, 201, 202), response.text
            doc = sorter.wait_done(statement_id=response.json()["statement_id"], timeout=120 * TIMEOUT_SCALE)
            assert doc["state"] == "succeeded", doc
        except Exception as error:  # noqa: BLE001 - reported below
            failure.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=sort)
    worker.start()
    peak = {"hardcharger_duckdb_spill_bytes": 0.0, "hardcharger_duckdb_spill_files": 0.0,
            "hardcharger_duckdb_temp_storage_bytes": 0.0, "hardcharger_metrics_collection_duration_seconds": 0.0}
    slowest = 0.0  # the slowest scrape, end to end: reported with any failure below
    while not done.is_set():
        started = time.monotonic()
        scraped = scrape(port=port)
        slowest = max(slowest, time.monotonic() - started)
        for name, highest in peak.items():
            peak[name] = max(highest, scraped[name])
        time.sleep(0.05)
    worker.join()
    timings = f"slowest scrape {slowest:.3f} s; {peak}"
    assert not failure, (failure, timings)
    assert peak["hardcharger_duckdb_spill_bytes"] > 0, timings
    assert peak["hardcharger_duckdb_spill_files"] >= 1, timings
    assert peak["hardcharger_duckdb_temp_storage_bytes"] > 0, timings
    admin.execute(sql="DROP TABLE metrics_spilled")
    deadline = time.monotonic() + 10
    while scrape(port=port)["hardcharger_duckdb_spill_bytes"] > 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert scrape(port=port)["hardcharger_duckdb_spill_bytes"] == 0


def server_certificate(pki, days: int):
    """A server certificate for 127.0.0.1 that expires `days` from now (whole seconds)."""
    key = ec.generate_private_key(curve=ec.SECP256R1())
    not_after = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0) + datetime.timedelta(days=days)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(oid=NameOID.COMMON_NAME, value="server")]))
            .issuer_name(pki.ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_after - datetime.timedelta(days=days + 1)).not_valid_after(not_after)
            .add_extension(extval=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(extval=x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                               x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(extval=x509.AuthorityKeyIdentifier.from_issuer_public_key(pki.ca_key.public_key()),
                           critical=False)
            .sign(private_key=pki.ca_key, algorithm=hashes.SHA256()))
    key_pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                                encryption_algorithm=serialization.NoEncryption())
    return cert, key_pem


def empty_crl(pki, minutes_ago: int) -> bytes:
    now = datetime.datetime.now(tz=datetime.UTC)
    return (x509.CertificateRevocationListBuilder().issuer_name(pki.ca.subject)
            .last_update(now - datetime.timedelta(minutes=minutes_ago)).next_update(now + datetime.timedelta(days=1))
            .sign(private_key=pki.ca_key, algorithm=hashes.SHA256()).public_bytes(encoding=serialization.Encoding.PEM))


def test_tls_certificate_expiry_follows_the_serving_certificate(launcher, tmp_path):
    """1.x's meaning: the serving certificate's notAfter as a Unix timestamp. When the TLS
    context is rebuilt (a client CRL reload loads the certificate chain again), the gauge
    shows the certificate now served."""
    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    cert, key_pem = server_certificate(pki=pki, days=30)
    pki.server[0].write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))
    pki.server[1].write_bytes(key_pem)
    crl = tmp_path / "ca.crl"
    crl.write_bytes(empty_crl(pki=pki, minutes_ago=5))
    port = free_port()
    server = start_tls(launcher=launcher, pki=pki, probe=ops,
                       args=["--enable-metrics", "--metrics-port", str(port), "--metrics-bind-address", "127.0.0.1",
                             "--mtls-crl-file", str(crl), "--mtls-certificate-sign-in",
                             "--mtls-admin-identities", "ops@example.com"])
    expiry = cert.not_valid_after_utc.timestamp()
    assert scrape(port=port)["hardcharger_tls_certificate_expiry_seconds"] == expiry
    rows = server.admin(identity=ops).run(
        sql="SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_tls_certificate_expiry_seconds'")
    assert rows.column(0).to_pylist() == [expiry]
    # A renewed certificate, then a CRL change: the next look (every 5 s, on a new
    # connection) rebuilds the context from both files.
    renewed, renewed_key = server_certificate(pki=pki, days=90)
    pki.server[1].write_bytes(renewed_key)
    pki.server[0].write_bytes(renewed.public_bytes(encoding=serialization.Encoding.PEM))
    crl.write_bytes(empty_crl(pki=pki, minutes_ago=1))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with server.http(identity=ops) as http:
            http.get(url="/warp")
        if scrape(port=port)["hardcharger_tls_certificate_expiry_seconds"] == renewed.not_valid_after_utc.timestamp():
            break
        time.sleep(0.5)
    assert scrape(port=port)["hardcharger_tls_certificate_expiry_seconds"] == renewed.not_valid_after_utc.timestamp()
