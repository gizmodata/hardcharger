"""Port of GizmoSQL 1.x tests/drivers/test_metrics_configuration.py (4 functions, 13 cases).

1.x never ran this in CI; here it runs with every suite. 1.x's "unlicensed enablement
fails" cases (4) don't apply: metrics need no license. An explicit --enable-metrics
false beats HARDCHARGER_ENABLE_METRICS=true; with metrics off hardcharger_metrics()
refuses and the port stays closed; on, GET /metrics serves "hardcharger_session_limit
0", and --metrics-port 0 keeps the HTTP
endpoint closed while SQL still works; an unclean exit sets hardcharger_last_exit_unclean
and counts hardcharger_unclean_exits_total, and a clean stop clears the badge.
"""

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
        server = launcher.start(args=args)
        assert scalar(server, "SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_last_exit_unclean'") == expected_badge
        assert scalar(server, "SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_unclean_exits_total'") == expected_count
        if stop == "kill":
            server.process.kill()
            server.process.wait(timeout=10)
        else:
            server.stop()


def test_disabled_sql_returns_explicit_error(launcher):
    metrics_port = free_port()
    server = launcher.start(args=["--enable-metrics", "false", "--metrics-port", str(metrics_port)],
                            env={"HARDCHARGER_ENABLE_METRICS": "true"})  # the explicit false wins
    session = server.admin()
    assert session.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_metrics'") == "false"
    detail = session.error(sql="SELECT * FROM hardcharger_metrics()")["detail"]
    assert "disabled" in detail and "enable_metrics" in detail, detail
    assert port_closed(metrics_port)


@pytest.mark.parametrize("http_enabled", [True, False])
def test_metrics_enable_sql_and_optional_http(launcher, http_enabled):
    metrics_port = free_port()
    server = launcher.start(env={"HARDCHARGER_ENABLE_METRICS": "yes"},
                            args=["--metrics-port", str(metrics_port) if http_enabled else "0",
                                  "--metrics-bind-address", "127.0.0.1"])
    session = server.admin()
    assert session.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_metrics'") == "true"
    assert session.scalar(sql="SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_session_limit'") == 0.0
    if http_enabled:
        response = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics")
        assert response.status_code == 200 and "hardcharger_session_limit 0\n" in response.text
    else:
        assert port_closed(metrics_port)


# ---- Saying where /metrics listens turns metrics on ----
#
# Hardcharger 0.3.0 served nothing for --metrics-port N without --enable-metrics, and its
# ready line said "metrics_port=" (empty). A metrics_port (other than 0) or a
# metrics_bind_address now turns metrics on, from a flag, the environment or the file; an
# explicit enable_metrics false still wins, and says so.

def ready_line(server) -> str:
    lines = [line for line in server.log().splitlines() if "Hardcharger is ready" in line]
    assert len(lines) == 1, server.log()
    return lines[0]


@pytest.mark.parametrize("source", ["flag", "env", "file"])
def test_metrics_port_alone_turns_metrics_on(launcher, tmp_path, source):
    metrics_port = free_port()
    args, env = ["--metrics-bind-address", "127.0.0.1"] if source != "file" else [], {}
    if source == "flag":
        args += ["--metrics-port", str(metrics_port)]
    elif source == "env":
        env["HARDCHARGER_METRICS_PORT"] = str(metrics_port)
    else:
        config = tmp_path / "hardcharger.yaml"
        config.write_text(f"metrics_port: {metrics_port}\nmetrics_bind_address: 127.0.0.1\n")
        args += ["--config", str(config)]
    server = launcher.start(args=args, env=env)
    session = server.admin()
    assert session.scalar(sql="SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.enable_metrics'") == "true"
    response = httpx.get(url=f"http://127.0.0.1:{metrics_port}/metrics")
    assert response.status_code == 200 and "hardcharger_session_limit 0\n" in response.text
    assert f"metrics_port={metrics_port}" in ready_line(server)


def test_metrics_bind_address_alone_turns_metrics_on(launcher):
    # With metrics_port 0 (metrics in SQL only), so the test binds no fixed port.
    server = launcher.start(args=["--metrics-bind-address", "127.0.0.1", "--metrics-port", "0"])
    assert scalar(server, "SELECT value FROM hardcharger_metrics() WHERE name = 'hardcharger_session_limit'") == 0.0
    assert "metrics_port=none" in ready_line(server)


def test_metrics_port_zero_alone_leaves_metrics_off(launcher):
    server = launcher.start(args=["--metrics-port", "0"])
    detail = server.admin().error(sql="SELECT * FROM hardcharger_metrics()")["detail"]
    assert "disabled" in detail, detail
    assert "metrics_port=off" in ready_line(server)


def test_explicit_false_wins_over_a_metrics_port_and_says_so(launcher):
    metrics_port = free_port()
    server = launcher.start(args=["--enable-metrics", "false", "--metrics-port", str(metrics_port)])
    assert port_closed(metrics_port)
    assert "metrics_port=off" in ready_line(server)
    assert "enable_metrics is false, so metrics_port and metrics_bind_address are unused" in server.log()
