"""Port of GizmoSQL 1.x tests/drivers/test_metrics_configuration.py (4 functions, 13 cases).

1.x never ran this in CI; here it runs with every suite. Enabling metrics without the
'metrics' license feature stops the server (by flag or environment, with or without a
license); an explicit --enable-metrics false beats GIZMOSQL_ENABLE_METRICS=true;
with metrics off gizmosql_metrics() refuses and the port stays closed; licensed, GET
/metrics serves "gizmosql_session_limit 0", and --metrics-port 0 keeps the HTTP
endpoint closed while SQL still works; an unclean exit sets gizmosql_last_exit_unclean
and counts gizmosql_unclean_exits_total, and a clean stop clears the badge.
"""

import signal
import socket

import httpx
import pytest

from conftest import free_port


def port_closed(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def scalar(server, sql: str):
    return server.admin().scalar(sql=sql)


def test_unclean_exit_is_detected_and_clean_shutdown_clears_badge(launcher, tmp_path):
    database = str(tmp_path / "unclean.duckdb")
    args = ["--enable-metrics", "true", "--metrics-port", "0", "--database", database]
    for expected_badge, expected_count, stop in [(0, 0, "kill"), (1, 1, "clean"), (0, 1, "clean")]:
        server = launcher.start(features=["metrics"], args=args)
        assert scalar(server, "SELECT value FROM gizmosql_metrics() WHERE name = 'gizmosql_last_exit_unclean'") == expected_badge
        assert scalar(server, "SELECT value FROM gizmosql_metrics() WHERE name = 'gizmosql_unclean_exits_total'") == expected_count
        if stop == "kill":
            server.process.send_signal(signal.SIGKILL)
            server.process.wait(timeout=10)
        else:
            server.stop()


@pytest.mark.parametrize("license_kind", ["none", "without_metrics"])
@pytest.mark.parametrize("enable_source", ["cli", "env"])
def test_unlicensed_enablement_fails(launcher, license_kind, enable_source):
    args, env = [], {}
    if enable_source == "cli":
        args = ["--enable-metrics", "true"]
    else:
        env["GIZMOSQL_ENABLE_METRICS"] = "true"
    features = None if license_kind == "none" else ["instrumentation", "statement_queue"]
    server = launcher.start(args=args, env=env, features=features, expect_up=False)
    assert server.process.wait(timeout=20) == 1
    assert "'metrics'" in server.log() and "license" in server.log().lower(), server.log()


@pytest.mark.parametrize("license_kind", ["none", "metrics"])
def test_disabled_sql_returns_explicit_error(launcher, license_kind):
    metrics_port = free_port()
    server = launcher.start(args=["--enable-metrics", "false", "--metrics-port", str(metrics_port)],
                            env={"GIZMOSQL_ENABLE_METRICS": "true"},  # the explicit false wins
                            features=None if license_kind == "none" else ["metrics"])
    session = server.admin()
    assert session.scalar(sql="SELECT value FROM gizmosql_settings() WHERE name = 'gizmosql.enable_metrics'") == "false"
    detail = session.error(sql="SELECT * FROM gizmosql_metrics()")["detail"]
    assert "disabled" in detail and "license" in detail, detail
    assert port_closed(metrics_port)


@pytest.mark.parametrize("http_enabled", [True, False])
def test_metrics_license_enables_sql_and_optional_http(launcher, http_enabled):
    metrics_port = free_port()
    server = launcher.start(features=["metrics"], env={"GIZMOSQL_ENABLE_METRICS": "yes"},
                            args=["--metrics-port", str(metrics_port) if http_enabled else "0",
                                  "--metrics-bind-address", "127.0.0.1"])
    session = server.admin()
    assert session.scalar(sql="SELECT value FROM gizmosql_settings() WHERE name = 'gizmosql.enable_metrics'") == "true"
    assert session.scalar(sql="SELECT value FROM gizmosql_metrics() WHERE name = 'gizmosql_session_limit'") == 0.0
    if http_enabled:
        response = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics")
        assert response.status_code == 200 and "gizmosql_session_limit 0\n" in response.text
    else:
        assert port_closed(metrics_port)
