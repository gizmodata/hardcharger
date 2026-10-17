"""Port of GizmoSQL 1.x tests/integration/test_health_check.cpp (8 cases: 5 server, 3 unit).

The health check runs health_check_query (GIZMOSQL_HEALTH_CHECK_QUERY, default SELECT 1)
every health_check_interval_seconds on a background thread; a probe (GET
/warp/v1/health, and /health on the plaintext health_port that GizmoData Cloud's kubelet
probes) answers from the last outcome, so it never waits on the database. An outcome
older than health_check_staleness_seconds counts as failed, so a hung query turns the
check red. 1.x's three unit cases (stale, recover, shutdown with a hung check) run here
against the server. 1.x's gRPC health service is HTTP here (Warp SQL §3.2: 200 serving,
503 not_serving).
"""

import time

import httpx

from conftest import free_port


def health(server, health_port: int | None = None) -> httpx.Response:
    if health_port is not None:
        return httpx.get(url=f"http://127.0.0.1:{health_port}/health", timeout=5)
    return httpx.get(url=f"{server.base_url}/warp/v1/health", timeout=5)


def eventually(server, status: int, timeout: float = 15, health_port: int | None = None) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if health(server=server, health_port=health_port).status_code == status:
            return time.monotonic() - started
        time.sleep(0.2)
    raise AssertionError(f"health never became {status}: {health(server=server).text}")


def start(launcher, query: str | None = None, interval: int = 1, staleness: int = 3, **kwargs):
    env = {"GIZMOSQL_HEALTH_CHECK_INTERVAL_SECONDS": str(interval),
           "GIZMOSQL_HEALTH_CHECK_STALENESS_SECONDS": str(staleness), **kwargs.pop("env", {})}
    if query is not None:
        env["GIZMOSQL_HEALTH_CHECK_QUERY"] = query
    return launcher.start(env=env, **kwargs)


def test_serving_with_default_query(launcher):
    port = free_port()
    server = start(launcher=launcher, env={"HEALTH_PORT": str(port)})
    eventually(server=server, status=200)
    response = health(server=server)
    assert response.status_code == 200 and response.json() == {"status": "serving"}, response.text
    assert health(server=server, health_port=port).status_code == 200


def test_serving_with_custom_query(launcher):
    server = start(launcher=launcher, query="SELECT 42 AS answer")
    assert eventually(server=server, status=200) < 5


def test_hung_query_reports_not_serving(launcher):
    port = free_port()
    server = start(launcher=launcher, query="SELECT sleep_ms(10000)", env={"HEALTH_PORT": str(port)},
                   wait_for="/warp")
    assert eventually(server=server, status=503, health_port=port) < 15
    body = health(server=server).json()
    assert body["status"] == "not_serving", body
    # The server itself still answers.
    assert server.admin().scalar(sql="SELECT 1") == 1


def test_failing_query_reports_not_serving(launcher):
    server = start(launcher=launcher, query="SELECT * FROM no_such_table")
    assert eventually(server=server, status=503) < 15


def test_stops_within_ten_seconds_with_a_hung_check(launcher):
    server = start(launcher=launcher, query="SELECT sleep_ms(30000)")
    eventually(server=server, status=503)
    started = time.monotonic()
    server.process.terminate()
    server.process.wait(timeout=15)
    assert time.monotonic() - started < 10


def test_false_then_true_check_recovers(launcher, tmp_path):
    database = tmp_path / "health.duckdb"
    server = start(launcher=launcher, query="SELECT * FROM health_flag", args=["--database", str(database)])
    assert eventually(server=server, status=503) < 15
    server.admin().execute(sql="CREATE TABLE health_flag AS SELECT 1 AS ok")
    assert eventually(server=server, status=200) < 15


def test_stale_check_reports_not_serving_and_recovers(launcher, tmp_path):
    database = tmp_path / "hang.duckdb"
    # Hangs while health_hang holds a large value; passes once it is 0.
    server = start(launcher=launcher, args=["--database", str(database)],
                   env={"HARDCHARGER_INIT_SQL_COMMANDS": "CREATE TABLE health_hang AS SELECT 0 AS ms"},
                   query="SELECT sleep_ms((SELECT max(ms) FROM health_hang))", interval=1, staleness=2)
    eventually(server=server, status=200)
    admin = server.admin()
    admin.execute(sql="UPDATE health_hang SET ms = 5000")
    assert eventually(server=server, status=503) < 15
    admin.execute(sql="UPDATE health_hang SET ms = 0")
    assert eventually(server=server, status=200, timeout=20) < 20
