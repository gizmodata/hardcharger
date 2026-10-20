"""The access-control store when it fails: stopped (connections refused) or paused (a
server that accepts and never answers), on a PostgreSQL container of each test's own.

- No error, client-facing or logged, carries the store's password.
- Every store round trip is bounded (rbac_store_timeout_seconds), and what it was part of
  is rolled back: a CREATE USER a client was told failed never appears later.
- DDL whose owner can't be recorded fails, and creates nothing.
- While the store is down the server keeps serving from its last snapshot, says so
  (a warning, hardcharger_rbac_store_up, hardcharger_rbac_snapshot_age_seconds), and past
  rbac_max_snapshot_age_seconds refuses every principal but the administrator.

Needs Docker (the `services` marker: the Linux CI jobs); fails, never skips, without it.
"""

import re
import secrets
import shutil
import subprocess
import threading
import time

import httpx
import pytest

from conftest import free_port
from rbac import WarpError, start_rbac_server

pytestmark = pytest.mark.services

UNAVAILABLE = "access control store unavailable"


class Postgres:
    """A throwaway PostgreSQL 17 on 127.0.0.1, with a random password, removed afterwards."""

    def __init__(self):
        if shutil.which("docker") is None:
            pytest.fail(reason="these tests need Docker (they stop and pause a PostgreSQL container)")
        self.name = "hc-test-store-" + secrets.token_hex(nbytes=4)
        self.password = "pw-" + secrets.token_hex(nbytes=10)
        self.port = free_port()
        subprocess.run(args=["docker", "run", "-d", "--name", self.name, "-e", "POSTGRES_USER=hardcharger",
                             "-e", f"POSTGRES_PASSWORD={self.password}", "-e", "POSTGRES_DB=hardcharger",
                             "-p", f"127.0.0.1:{self.port}:5432", "postgres:17-alpine"],
                       check=True, capture_output=True, timeout=300)
        self.wait_ready()
        self.uri = f"postgresql://hardcharger:{self.password}@127.0.0.1:{self.port}/hardcharger"

    def docker(self, *args: str) -> None:
        subprocess.run(args=["docker", *args, self.name], check=True, capture_output=True, timeout=120)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            # pg_isready over TCP: the image's init server listens on the socket only.
            done = subprocess.run(args=["docker", "exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U",
                                        "hardcharger"], capture_output=True, timeout=30, check=False)
            if done.returncode == 0:
                return
            time.sleep(0.5)
        pytest.fail(reason=f"PostgreSQL container {self.name} did not come up")

    def stop(self) -> None:
        self.docker("stop", "-t", "1")

    def start(self) -> None:
        self.docker("start")
        self.wait_ready()

    def pause(self) -> None:
        self.docker("pause")

    def unpause(self) -> None:
        self.docker("unpause")

    def remove(self) -> None:
        subprocess.run(args=["docker", "unpause", self.name], capture_output=True, timeout=60, check=False)
        subprocess.run(args=["docker", "rm", "-f", self.name], capture_output=True, timeout=120, check=False)


@pytest.fixture
def postgres():
    database = Postgres()
    yield database
    database.remove()


def refused(call) -> WarpError:
    with pytest.raises(WarpError) as caught:
        call()
    return caught.value


def bounded(call, seconds: float) -> tuple[WarpError | None, float]:
    """Runs `call` (expected to fail with WarpError) on a thread; fails the test when it is
    still waiting after `seconds` (the store holding the statement), else returns its error
    and how long it took."""
    outcome: list = []

    def work() -> None:
        started = time.monotonic()
        try:
            call()
            outcome.append((None, time.monotonic() - started))
        except WarpError as error:
            outcome.append((error, time.monotonic() - started))
        except Exception as error:  # noqa: BLE001 - reported below
            outcome.append((error, time.monotonic() - started))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        pytest.fail(reason=f"the statement was still waiting for the store after {seconds} s")
    error, took = outcome[0]
    if error is not None and not isinstance(error, WarpError):
        raise error
    return error, took


def users(admin) -> set[str]:
    return set(admin.run(sql="SELECT name FROM hardcharger_users()").column("name").to_pylist())


def metrics(port: int) -> dict[str, float]:
    text = httpx.get(url=f"http://127.0.0.1:{port}/metrics", timeout=10).text
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"^(hardcharger_rbac_\w+) (\S+)$", text, re.MULTILINE)}


def wait_for(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    pytest.fail(reason=f"timed out waiting for {what}")


def test_store_errors_never_carry_its_password(launcher, postgres):
    server = start_rbac_server(launcher=launcher, store_uri=postgres.uri)
    admin = server.admin()
    admin.run(sql="CREATE USER ann WITH PASSWORD 'ann-pw-12345'")  # the twin: the store works
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql="CREATE TABLE t AS SELECT 1 AS a")
    postgres.stop()
    for sql in ("CREATE USER bob WITH PASSWORD 'bob-pw-12345'", "GRANT SELECT ON t TO readers",
                "REVOKE SELECT ON t FROM readers", "DROP USER ann", "GRANT readers TO ann"):
        error = refused(call=lambda sql=sql: admin.run(sql=sql))
        assert postgres.password not in error.detail, sql
        assert UNAVAILABLE in error.detail, (sql, error.detail)
        assert "Connection refused" in error.detail or "connection" in error.detail, error.detail  # the reason survives
    log = server.log()
    assert postgres.password not in log
    assert UNAVAILABLE in log


def test_a_hung_store_fails_in_time_and_rolls_back(launcher, postgres):
    server = start_rbac_server(launcher=launcher, store_uri=postgres.uri,
                               env={"HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS": "3"})
    admin = server.admin()
    admin.run(sql="CREATE USER ann WITH PASSWORD 'ann-pw-12345'")
    postgres.pause()
    try:
        # Bounded (rc.1: held until the store answered, 152 s in the report).
        error, took = bounded(call=lambda: admin.run(sql="CREATE USER bob WITH PASSWORD 'bob-pw-12345'"), seconds=20)
        assert error is not None, "CREATE USER succeeded against a paused store"
        assert f"{UNAVAILABLE}: timed out after 3 s" in error.detail, error.detail
        assert 2.5 < took < 15, took
        # The store is known to hang now: DDL that needs it fails without another full wait.
        error, took = bounded(call=lambda: admin.run(sql="CREATE TABLE t AS SELECT 1 AS a"), seconds=20)
        assert error is not None, "CREATE TABLE succeeded against a paused store"
        assert UNAVAILABLE in error.detail and "owner of what it creates could not be recorded" in error.detail, \
            error.detail
        assert took < 10, took
        # Reads go on from the snapshot.
        assert admin.run(sql="SELECT 42 AS x").column("x").to_pylist() == [42]
    finally:
        postgres.unpause()
    # Once the store answers, the abandoned CREATE USER was rolled back, not committed.
    wait_for(condition=lambda: _runs(admin, "CREATE ROLE probe_after_unpause"), timeout=30,
             what="the store to answer again")
    assert "bob" not in users(admin=admin)
    assert "ann" in users(admin=admin)
    assert not admin.run(sql="SELECT table_name FROM duckdb_tables() WHERE table_name = 't'").num_rows
    admin.run(sql="CREATE USER bob WITH PASSWORD 'bob-pw-12345'")
    assert "bob" in users(admin=admin)
    assert postgres.password not in server.log()


def _runs(client, sql: str) -> bool:
    try:
        client.run(sql=sql)
        return True
    except WarpError:
        return False


def test_ddl_whose_owner_cannot_be_recorded_creates_nothing(launcher, postgres):
    server = start_rbac_server(launcher=launcher, store_uri=postgres.uri)
    admin = server.admin()
    admin.run(sql="CREATE USER ann WITH PASSWORD 'ann-pw-12345'")
    admin.run(sql="CREATE ROLE makers")
    admin.run(sql="GRANT CREATE TABLE ON SCHEMA memory.main TO makers")
    admin.run(sql="GRANT makers TO ann")
    ann = server.user(name="ann", password="ann-pw-12345")
    prepared = admin.prepare(sql="CREATE TABLE p AS SELECT 1 AS a")
    postgres.stop()
    for client, sql in ((admin, "CREATE TABLE a1 AS SELECT 1 AS a"), (admin, "CREATE VIEW v1 AS SELECT 1 AS a"),
                        (ann, "CREATE TABLE u1 (a INTEGER)")):
        error = refused(call=lambda client=client, sql=sql: client.run(sql=sql))
        assert "owner of what it creates could not be recorded" in error.detail, (sql, error.detail)
        assert UNAVAILABLE in error.detail, error.detail
    error = refused(call=lambda: admin.execute_prepared(prepared_id=prepared))
    assert "owner of what it creates could not be recorded" in error.detail, error.detail
    names = admin.run(sql="SELECT table_name FROM duckdb_tables() UNION ALL SELECT view_name FROM duckdb_views() "
                          "WHERE NOT internal").column(0).to_pylist()
    assert not {"a1", "v1", "u1", "p"} & set(names), names
    postgres.start()
    # The twin: with the store back, the same statements create their objects, owned.
    wait_for(condition=lambda: _runs(admin, "CREATE TABLE a1 AS SELECT 1 AS a"), timeout=30,
             what="the store to answer again")
    ann.run(sql="CREATE TABLE u1 (a INTEGER)")
    ann.run(sql="INSERT INTO u1 VALUES (1)")  # its owner may write it: the owner was recorded
    assert ann.run(sql="SELECT count(*) AS n FROM u1").column("n").to_pylist() == [1]
    admin.execute_prepared(prepared_id=prepared)
    assert admin.run(sql="SELECT count(*) AS n FROM p").column("n").to_pylist() == [1]


def test_a_stale_snapshot_is_reported_and_bounded(launcher, postgres):
    limited_metrics, open_metrics = free_port(), free_port()
    limited = start_rbac_server(launcher=launcher, store_uri=postgres.uri, args=["--metrics-port", str(limited_metrics)],
                                env={"HARDCHARGER_RBAC_MAX_SNAPSHOT_AGE_SECONDS": "4",
                                     "HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS": "2"})
    unlimited = start_rbac_server(launcher=launcher, store_uri=postgres.uri, args=["--metrics-port", str(open_metrics)],
                                  env={"HARDCHARGER_RBAC_STORE_TIMEOUT_SECONDS": "2"})
    admin = limited.admin()
    admin.run(sql="CREATE USER ann WITH PASSWORD 'ann-pw-12345'")
    admin.run(sql="CREATE TABLE t AS SELECT 1 AS a")
    unlimited.admin().run(sql="CREATE TABLE t AS SELECT 1 AS a")  # each server's own memory database
    admin.run(sql="CREATE ROLE readers")
    admin.run(sql="GRANT SELECT ON t TO readers")
    admin.run(sql="GRANT readers TO ann")
    def reached() -> bool:
        try:
            return _runs(unlimited.user(name="ann", password="ann-pw-12345"), "SELECT a FROM t")
        except WarpError:
            return False
    wait_for(condition=reached, timeout=10, what="the grant to reach the second server")
    ann = limited.user(name="ann", password="ann-pw-12345")
    ann_elsewhere = unlimited.user(name="ann", password="ann-pw-12345")
    before = metrics(port=limited_metrics)
    assert before["hardcharger_rbac_store_up"] == 1, before
    assert 0 <= before["hardcharger_rbac_snapshot_age_seconds"] < 3, before

    postgres.stop()
    stopped = time.monotonic()
    assert ann.run(sql="SELECT a FROM t").column("a").to_pylist() == [1]  # still served, within the limit
    wait_for(condition=lambda: metrics(port=limited_metrics)["hardcharger_rbac_store_up"] == 0, timeout=15,
             what="hardcharger_rbac_store_up to drop")
    wait_for(condition=lambda: time.monotonic() - stopped > 6, timeout=10, what="the snapshot to age")
    age = metrics(port=limited_metrics)["hardcharger_rbac_snapshot_age_seconds"]
    assert age > 4, age
    error = refused(call=lambda: ann.run(sql="SELECT a FROM t"))
    assert "has not answered for" in error.detail and "rbac_max_snapshot_age_seconds (4)" in error.detail, error.detail
    with pytest.raises(WarpError) as sign_in:
        limited.user(name="ann", password="ann-pw-12345")
    assert "rbac_max_snapshot_age_seconds" in sign_in.value.detail, sign_in.value.detail
    assert admin.run(sql="SELECT a FROM t").column("a").to_pylist() == [1]  # the administrator is not refused
    # Without a limit the other server stays available, on its last snapshot.
    assert ann_elsewhere.run(sql="SELECT a FROM t").column("a").to_pylist() == [1]
    assert metrics(port=open_metrics)["hardcharger_rbac_store_up"] == 0
    for server in (limited, unlimited):
        log = server.log()
        assert "the access control store does not answer" in log, log
        assert "snapshot_age_seconds=" in log, log
        assert postgres.password not in log

    postgres.start()
    wait_for(condition=lambda: metrics(port=limited_metrics)["hardcharger_rbac_store_up"] == 1, timeout=30,
             what="hardcharger_rbac_store_up to come back")
    assert ann.run(sql="SELECT a FROM t").column("a").to_pylist() == [1]
    assert "the access control store answers again" in limited.log()
