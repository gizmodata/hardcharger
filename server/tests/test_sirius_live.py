"""NVIDIA's Sirius extension loaded for real (authz/sirius.hpp), against a running server that loaded it: a GPU
node, or linux_amd64 with the pair cached and SIRIUS_DISABLE=1 (no GPU: Sirius loads, queries run on the CPU).
Deselected unless HARDCHARGER_TEST_SIRIUS_URL names the server (conftest.py); without it the marker's tests fail.

- HARDCHARGER_TEST_SIRIUS_URL: the server's Chargewire base URL (e.g. a port-forward, http://127.0.0.1:31337);
- HARDCHARGER_TEST_SIRIUS_PASSWORD: its administrator's password (user HARDCHARGER_TEST_SIRIUS_ADMIN, default
  hardcharger); the server runs with access control;
- HARDCHARGER_TEST_SIRIUS_S3: an s3:// prefix the server's own secret may write (e.g. s3://bucket/run-1).
"""

import os
import secrets

import httpx
import pytest

from rbac import ChargewireError, Client, expect_allowed, expect_blocked

pytestmark = pytest.mark.sirius

URL = os.environ.get("HARDCHARGER_TEST_SIRIUS_URL", "")
S3 = os.environ.get("HARDCHARGER_TEST_SIRIUS_S3", "").rstrip("/")
PASSWORD = "Sirius-live-" + secrets.token_hex(nbytes=6)


def sign_in(user: str, password: str, read_only: bool = False) -> Client:
    response = httpx.post(url=f"{URL}/chargewire/v1/auth/token", timeout=120,
                          json={"grant_type": "password", "username": user, "password": password})
    response.raise_for_status()
    return Client(base_url=URL, token=response.json()["access_token"], options={"read_only": True} if read_only else None)


@pytest.fixture(scope="module")
def world():
    if not URL or not os.environ.get("HARDCHARGER_TEST_SIRIUS_PASSWORD"):
        pytest.fail(reason="set HARDCHARGER_TEST_SIRIUS_URL and HARDCHARGER_TEST_SIRIUS_PASSWORD (a server with Sirius)")
    admin = sign_in(user=os.environ.get("HARDCHARGER_TEST_SIRIUS_ADMIN", "hardcharger"),
                    password=os.environ["HARDCHARGER_TEST_SIRIUS_PASSWORD"])
    suffix = secrets.token_hex(nbytes=3)
    t, role, user = f"live_t_{suffix}", f"live_r_{suffix}", f"live_u_{suffix}"
    for sql in [f"CREATE TABLE {t} AS SELECT range AS id, 'ssn-' || range AS ssn, range % 7 AS grp FROM range(2000000)",
                f"CREATE ROLE {role}", f"CREATE USER {user} WITH PASSWORD '{PASSWORD}'", f"GRANT {role} TO {user}",
                f"GRANT SELECT ON {t} TO {role}", f"CREATE ROW POLICY p_{suffix} ON {t} TO {role} USING (id < 1000)"]:
        expect_allowed(client=admin, sql=sql)
    yield {"admin": admin, "t": t, "role": role, "user": sign_in(user=user, password=PASSWORD),
           "reader": sign_in(user=user, password=PASSWORD, read_only=True)}
    expect_allowed(client=admin, sql="SET GLOBAL gpu_execution = true")
    for sql in (f"DROP TABLE {t}", f"DROP USER {user}", f"DROP ROLE {role}"):
        expect_allowed(client=admin, sql=sql)


def test_sirius_is_loaded(world):
    status = expect_allowed(client=world["admin"], sql="SELECT backend_loaded FROM sirius_status()").to_pylist()
    assert status == [{"backend_loaded": True}], status


def test_s3_writes_and_cpu_reads_go_through_httpfs(world):
    if not S3:
        pytest.fail(reason="set HARDCHARGER_TEST_SIRIUS_S3 (an s3:// prefix the server may write)")
    admin, run = world["admin"], f"{S3}/{secrets.token_hex(nbytes=4)}"
    expect_allowed(client=admin, sql=f"COPY (SELECT range AS id FROM range(1000)) TO '{run}/x.csv'")
    expect_allowed(client=admin, sql="SET GLOBAL gpu_execution = false")  # a CPU read of S3
    try:
        n = expect_allowed(client=admin, sql=f"SELECT count(*) AS n FROM read_csv('{run}/x.csv')").column("n")
        assert n.to_pylist() == [1000]
    finally:
        expect_allowed(client=admin, sql="SET GLOBAL gpu_execution = true")
    lake = f"live_lake_{secrets.token_hex(nbytes=3)}"
    expect_allowed(client=admin, sql=f"ATTACH 'ducklake:/tmp/{lake}.ducklake' AS {lake} (DATA_PATH '{run}/lake/')")
    try:
        expect_allowed(client=admin, sql=f"CREATE TABLE {lake}.o AS SELECT range AS id FROM range(100000)")
        expect_allowed(client=admin, sql=f"INSERT INTO {lake}.o SELECT -1")
        # With GPU execution on: Sirius doesn't scan DuckLake tables, and refuses to replay a plan that reads S3 on
        # the CPU, so the server runs such a statement on the CPU from the start.
        n = expect_allowed(client=admin, sql=f"SELECT count(*) AS n, sum(id) AS s FROM {lake}.o").column("n").to_pylist()
        assert n == [100001], n
        n = expect_allowed(client=admin, sql=f"SELECT count(*) AS n FROM read_csv('{run}/x.csv')").column("n")
        assert n.to_pylist() == [1000]
    finally:
        expect_allowed(client=admin, sql=f"DETACH {lake}")


def test_row_policies_give_the_same_rows_on_gpu_and_cpu(world):
    admin, user, t = world["admin"], world["user"], world["t"]
    sql = f"SELECT count(*) AS n, max(id) AS m, sum(grp) AS g FROM {t}"
    answers = []
    for on in ("true", "false"):
        expect_allowed(client=admin, sql=f"SET GLOBAL gpu_execution = {on}")
        answers.append(expect_allowed(client=user, sql=sql).to_pylist())
    expect_allowed(client=admin, sql="SET GLOBAL gpu_execution = true")
    assert answers[0] == answers[1] == [{"n": 1000, "m": 999, "g": sum(i % 7 for i in range(1000))}], answers


def test_gpu_execution_and_table_name_functions_stay_the_administrators(world):
    user, t = world["user"], world["t"]
    expect_blocked(client=user, sql=f"SELECT * FROM gpu_execution('SELECT count(*) FROM {t}')", why="gpu_execution()")
    expect_blocked(client=user, sql=f"CALL pin_table(format = 'duckdb', name = '{t}', tier = 'gpu')", why="pin_table()")
    with pytest.raises(ChargewireError, match="stays the administrator's"):
        world["admin"].run(sql=f"GRANT EXECUTE ON TABLE FUNCTION gpu_execution TO {world['role']}")
    # A macro the administrator owns does not open it either: the plan check refuses it.
    with pytest.raises(ChargewireError):
        world["reader"].run(sql=f"SELECT * FROM gpu_execution('SELECT count(*) FROM {t}')")


def test_pinned_tables_keep_row_policies(world):
    admin, user, t = world["admin"], world["user"], world["t"]
    expect_allowed(client=admin, sql=f"CALL pin_table(format = 'duckdb', name = '{t}', tier = 'gpu')")
    try:
        n = expect_allowed(client=user, sql=f"SELECT count(*) AS n FROM {t}").column("n").to_pylist()
        assert n == [1000], n
    finally:
        expect_allowed(client=admin, sql=f"CALL unpin_table(format = 'duckdb', name = '{t}')")


def test_sirius_settings(world):
    admin, user = world["admin"], world["user"]
    expect_blocked(client=user, sql="SET enable_duckdb_fallback = false", why="MANAGE CONFIG")
    expect_blocked(client=user, sql="SET SESSION gpu_execution = false", why="MANAGE CONFIG")
    for sql in ("SET sirius_log_dir = '/tmp/made-by-sql'", "SET GLOBAL sirius_log_backend = 'spdlog'",
                "SET sirius_exchange_peers = '127.0.0.1:39001'", "SET pin_table_input_compression_plan_dir = '/etc'"):
        with pytest.raises(ChargewireError, match="can be set only in the server's startup SQL"):
            admin.run(sql=sql)
    with pytest.raises(ChargewireError, match="sirius_read_parquet"):
        admin.run(sql="SELECT * FROM sirius_read_parquet('/etc/passwd')")
