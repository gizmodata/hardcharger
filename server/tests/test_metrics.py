"""Port of GizmoSQL 1.x tests/integration/test_metrics.cpp (11 cases: 7 unit, 4 server).

Runtime metrics (license feature metrics): enable_metrics (--enable-metrics,
GIZMOSQL_ENABLE_METRICS) serves GET /metrics on metrics_port (Prometheus text, 404
elsewhere) and gizmosql_metrics() in SQL, from the same registry, under 1.x's metric
names. The registry's unit cases are C++ (server/unit/metrics_test.cc), run here. 1.x
read its registry in-process; here the server cases read it through gizmosql_metrics(),
which counts as a statement itself, and the deltas allow for that.

All 11 apply. The exit-marker case (a read-only server never touches the marker) runs
against real servers on a database file.
"""

import math
import time

import httpx
import pytest

from conftest import free_port, run_unit_tests

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
    return module_launcher.start(features=["metrics"], args=["--enable-metrics", "--metrics-port", str(metrics_port),
                                                             "--metrics-bind-address", "127.0.0.1"])


@pytest.fixture
def session(server):
    return server.admin()


def metric(session, name: str, **labels) -> float:
    where = " AND ".join([f"name = '{name}'"] + [f"labels['{k}'] = '{v}'" for k, v in labels.items()])
    return session.scalar(sql=f"SELECT value FROM gizmosql_metrics() WHERE {where}")


def test_metrics_feature_is_independent_of_other_enterprise_features(launcher):
    # A license with every feature but metrics does not enable it; one with only metrics does.
    others = [f for f in ("instrumentation", "kill_session", "catalog_permissions", "external_auth", "statement_queue")]
    refused = launcher.start(features=others, args=["--enable-metrics", "--metrics-port", "0"], expect_up=False)
    assert refused.process.wait(timeout=20) == 1 and "'metrics'" in refused.log(), refused.log()
    only = launcher.start(features=["metrics"], args=["--enable-metrics", "--metrics-port", "0"])
    assert only.admin().scalar(sql="SELECT value FROM gizmosql_settings() WHERE name = 'gizmosql.enable_metrics'") == "true"


def test_read_only_servers_never_touch_the_exit_marker(launcher, tmp_path):
    database = tmp_path / "metrics_marker.duckdb"
    marker = tmp_path / "metrics_marker.duckdb.gizmosql-metrics-state"
    writer = launcher.start(features=["metrics"], args=["--enable-metrics", "--metrics-port", "0", "--database", str(database)])
    assert marker.exists()
    assert metric(writer.admin(), "gizmosql_last_exit_unclean") == 0
    writer.stop()
    marker.unlink()
    reader = launcher.start(features=["metrics"], args=["--enable-metrics", "--metrics-port", "0", "--database",
                                                        str(database), "--readonly"])
    assert math.isnan(metric(reader.admin(), "gizmosql_last_exit_unclean"))
    reader.stop()
    assert not marker.exists()


def test_http_and_sql_expose_the_same_registry(server, session, metrics_port):
    response = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics")
    assert response.status_code == 200 and "text/plain" in response.headers["content-type"]
    assert "gizmosql_build_info{" in response.text
    assert session.scalar(sql="SELECT value FROM gizmosql_settings() WHERE name = 'gizmosql.enable_metrics'") == "true"
    before = metric(session, "gizmosql_statements_total", status="ok")
    session.execute(sql="CREATE OR REPLACE TABLE metrics_ledger(n INTEGER)")
    doc = session.execute(sql="INSERT INTO metrics_ledger VALUES (1)")
    after = metric(session, "gizmosql_statements_total", status="ok")
    assert after - before == 3  # two statements, and the first metrics read itself
    session.status(statement_id=doc["statement_id"])  # re-reading a statement counts nothing
    assert metric(session, "gizmosql_statements_total", status="ok") - after == 1
    assert metric(session, "gizmosql_session_limit") == 0
    assert session.scalar(sql="SELECT labels['edition'] FROM gizmosql_metrics() WHERE name = 'gizmosql_build_info'") \
        == "Enterprise"
    assert "gizmosql_session_limit 0\n" in httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics").text
    assert httpx.get(url=f"http://127.0.0.1:{metrics_port}/not-metrics").status_code == 404


def test_collector_refreshes_without_scrapes(session):
    assert metric(session, "gizmosql_metrics_collection_success") == 1
    assert metric(session, "gizmosql_metrics_last_collection_success_seconds") > time.time() - 60
    assert metric(session, "process_resident_memory_bytes") > 0
    assert metric(session, "gizmosql_duckdb_memory_limit_bytes") > 0


def test_parser_and_catalog_failures_are_counted(session):
    before = metric(session, "gizmosql_statements_total", status="error")
    session.error(sql="SELECT FROM")
    session.error(sql="SELECT * FROM metrics_missing_table")
    assert metric(session, "gizmosql_statements_total", status="error") - before == 2
    assert metric(session, "gizmosql_statement_errors_total", **{"class": "syntax"}) >= 1
    assert metric(session, "gizmosql_statement_errors_total", **{"class": "catalog"}) >= 1


def test_transaction_states_follow_the_session(server, session):
    watcher = server.admin()
    session.execute(sql="CREATE OR REPLACE TABLE metrics_transactions(n INTEGER PRIMARY KEY)")
    session.execute(sql="INSERT INTO metrics_transactions VALUES (1)")
    session.execute(sql="BEGIN TRANSACTION")
    assert session.scalar(sql="SELECT 42") == 42
    assert metric(watcher, "gizmosql_sessions", state="idle_in_transaction") == 1
    session.error(sql="INSERT INTO metrics_transactions VALUES (1)")
    assert metric(watcher, "gizmosql_sessions", state="idle_in_transaction_aborted") == 1
    session.execute(sql="ROLLBACK")
    assert metric(watcher, "gizmosql_sessions", state="idle_in_transaction") == 0
    assert metric(watcher, "gizmosql_sessions", state="idle_in_transaction_aborted") == 0
