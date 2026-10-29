"""Port of GizmoSQL 1.x tests/integration/test_graceful_shutdown.cpp (5 cases).

On SIGTERM a server with graceful_shutdown drains (Chargewire §11.1): running statements
finish, new ones get 503 server_draining ("the server is shutting down"), the health
check reports not serving, and it stops once idle, when shutdown_grace_period_seconds
runs out (cancelling what still runs), or at a second signal. 1.x drove most cases
through an in-process call and one through raise(SIGTERM); here every case sends a real
SIGTERM to the server process. All 5 apply, plus graceful_shutdown=false (stop at once).

A forced stop (the second signal, or the first without graceful_shutdown) also exits 0,
with nothing touching what the stop freed, whatever is in flight: a large result stream
the client has stopped reading, an ingest upload (in one request, or a part of a
partitioned one), a prepared statement running (and another one open), or all at once;
and over TLS with a client certificate (mTLS) too.
"""

import subprocess
import threading
import time

import httpx
import pyarrow as pa
import pytest

from conftest import STOP_SIGNAL, TIMEOUT_SCALE, problem
from chargewire_client import ARROW_STREAM, Client, ipc_bytes

LONG = "SELECT count(*) FROM range(10000000000) a, range(100000) b"
LONG_STREAM = "SELECT a.range * 2 AS v FROM range(100000000000) a"


def run_in_background(session, sql: str) -> dict:
    outcome = {}

    def run():
        try:
            outcome["response"] = session.submit(sql=sql, timeout=120)
        except httpx.HTTPError as error:
            outcome["error"] = error

    outcome["thread"] = threading.Thread(target=run)
    outcome["thread"].start()
    return outcome


def assert_exits_cleanly(server, timeout: float) -> None:
    """The server exits 0 within `timeout`; otherwise the assertion carries the end of its
    log (a sanitizer report or an abort's message is written there, and CI shows nothing else)."""
    try:
        code = server.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"the server did not exit within {timeout} s; its log ends:\n{server.log()[-6000:]}")
    assert code == 0, f"the server exited {code}; its log ends:\n{server.log()[-6000:]}"


def wait_running(session, timeout: float = 10 * TIMEOUT_SCALE) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.scalar(sql="SELECT count(*) FROM hardcharger_sessions() WHERE running AND NOT is_current") > 0:
            return
        time.sleep(0.05)
    raise AssertionError("the slow statement never started")


def test_drains_in_flight_and_rejects_new_work(launcher):
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "true", "HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "30"})
    slow = server.admin()
    probe = server.admin()
    outcome = run_in_background(session=slow, sql="SELECT sleep_ms(4000) AS slept")
    wait_running(session=probe)
    server.process.send_signal(STOP_SIGNAL)
    time.sleep(0.5)
    started = time.monotonic()
    body = problem(response=probe.submit(sql="SELECT 1"), status=503, code="server_draining")
    assert time.monotonic() - started < 3
    assert "shutting down" in body["detail"], body
    assert httpx.get(url=f"{server.base_url}/chargewire/v1/health").status_code == 503
    outcome["thread"].join(timeout=30)
    assert outcome["response"].status_code in (200, 201), outcome  # the running statement finished
    assert_exits_cleanly(server=server, timeout=20)


def test_stops_promptly_when_idle(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"})
    assert server.admin().scalar(sql="SELECT 1") == 1
    started = time.monotonic()
    server.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=server, timeout=10)
    assert time.monotonic() - started < 10


def test_real_signal_drives_drain(launcher):
    server = launcher.start()  # graceful by default
    slow = server.admin()
    probe = server.admin()
    outcome = run_in_background(session=slow, sql="SELECT sleep_ms(3000) AS slept")
    wait_running(session=probe)
    server.process.send_signal(STOP_SIGNAL)
    time.sleep(0.5)
    problem(response=probe.submit(sql="SELECT 1"), status=503, code="server_draining")
    outcome["thread"].join(timeout=30)
    assert outcome["response"].status_code in (200, 201), outcome
    assert_exits_cleanly(server=server, timeout=20)


def test_grace_period_elapsed_forces_stop(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "2"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=server, timeout=15)
    assert time.monotonic() - started < 15
    outcome["thread"].join(timeout=30)
    failed = "error" in outcome or outcome["response"].json().get("state") != "succeeded"
    assert failed, outcome


def test_second_signal_forces_stop(launcher):
    server = launcher.start(env={"HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(STOP_SIGNAL)
    time.sleep(1)
    assert server.process.poll() is None, "the first signal must only drain"
    server.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=server, timeout=15)
    assert time.monotonic() - started < 15
    outcome["thread"].join(timeout=30)


def test_without_graceful_shutdown_the_first_signal_stops(launcher):
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "false"})
    slow = server.admin()
    outcome = run_in_background(session=slow, sql=LONG)
    wait_running(session=server.admin())
    started = time.monotonic()
    server.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=server, timeout=15)
    assert time.monotonic() - started < 10
    outcome["thread"].join(timeout=30)


def test_a_drain_ends_in_a_clean_exit_every_time(launcher):
    # The drain's last step stops the server on a thread of its own, while main() returns
    # from Wait() and destroys it: that thread must be finished (joined) before the server
    # is torn down. Under ThreadSanitizer the race made the process exit 66, not 0.
    for _ in range(5):
        server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "true"})
        assert server.admin().scalar(sql="SELECT 1") == 1
        server.process.send_signal(STOP_SIGNAL)
        assert_exits_cleanly(server=server, timeout=60)


class InFlight:
    """Work a forced stop finds under way, started on threads of its own. Each piece ends
    when the server goes away (with an error or a reply; neither is asserted, only that
    none of them hangs)."""

    def __init__(self, server, new_http=lambda: httpx.Client(timeout=30)):
        self.server = server  # base_url and admin() (a session of the administrator)
        self.new_http = new_http  # an HTTP client for the server (over TLS, one that trusts it)
        self.threads: list[threading.Thread] = []
        self.clients: list[httpx.Client] = []

    def _client(self) -> httpx.Client:
        client = self.new_http()
        self.clients.append(client)
        return client

    def _start(self, target) -> None:
        def run():
            try:
                target()
            except (httpx.HTTPError, StopIteration):
                pass  # the server stopped under it

        thread = threading.Thread(target=run)
        thread.start()
        self.threads.append(thread)

    def stalled_stream(self) -> None:
        """A result far larger than the server buffers, read once, then not at all."""
        session = self.server.admin()
        began = threading.Event()

        def read_a_little():
            with self._client().stream(method="POST", url=f"{self.server.base_url}{session.url}/statements",
                                       headers=session.headers(**{"Accept": ARROW_STREAM}),
                                       json={"sql": LONG_STREAM}) as stream:
                assert stream.status_code == 200
                next(stream.iter_bytes())
                began.set()
                time.sleep(20)  # the stop comes while the server waits on this reader

        self._start(target=read_a_little)
        assert began.wait(timeout=30), "the stream never began"

    def _slow_upload(self, session, url: str, params: dict | None = None) -> None:
        """POSTs an Arrow stream of about 20 MB to `url`, sent over ~15 s; returns once
        the server has its first chunks."""
        body = ipc_bytes(table=pa.table(data={"v": pa.array(obj=range(2_500_000), type=pa.int64())}))
        sending = threading.Event()

        def chunks():
            for offset in range(0, len(body), 64 * 1024):
                if offset >= 4 * 64 * 1024:
                    sending.set()
                yield body[offset:offset + 64 * 1024]
                time.sleep(0.05)

        def upload():
            self._client().post(url=f"{self.server.base_url}{url}", params=params or {},
                                headers=session.headers(**{"Content-Type": ARROW_STREAM}), content=chunks())

        self._start(target=upload)
        assert sending.wait(timeout=30), "the upload never began"

    def ingest_upload(self) -> None:
        """A one-request bulk ingest whose body is still arriving."""
        session = self.server.admin()
        self._slow_upload(session=session, url=f"{session.url}/ingest",
                          params={"table": "uploading", "mode": "create_append"})

    def partitioned_upload(self) -> None:
        """A partitioned ingest (Chargewire §8.3): one part uploaded, another arriving."""
        session = self.server.admin()
        opened = session.http.post(url=f"{session.url}/ingests", headers=session.headers(),
                                   json={"table": "uploading_parts", "mode": "create_append"})
        assert opened.status_code == 201, opened.text
        ingest_url = f"{session.url}/ingests/{opened.json()['ingest_id']}"
        rows = pa.table(data={"v": pa.array(obj=[1, 2, 3], type=pa.int64())})
        part = session.http.post(url=f"{ingest_url}/parts/1", content=ipc_bytes(table=rows),
                                 headers=session.headers(**{"Content-Type": ARROW_STREAM}))
        assert part.status_code == 200, part.text
        self._slow_upload(session=session, url=f"{ingest_url}/parts/0")

    def prepared(self) -> None:
        """A prepared statement executing, and another one prepared and left open."""
        session = self.server.admin()
        session.prepare(sql="SELECT ? AS left_open")
        # Preparing LONG runs nothing (chargewire's prepare describes it by binding), so
        # only the execution is long.
        running = session.prepare(sql=LONG)
        self._start(target=lambda: session.execute_prepared(prepared_id=running["prepared_id"]))
        wait_running(session=self.server.admin())

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "a client of the stopped server hung"
        for client in self.clients:
            client.close()


SHAPES = {
    "stalled_stream": ["stalled_stream"],
    "ingest_upload": ["ingest_upload"],
    "partitioned_upload": ["partitioned_upload"],
    "prepared": ["prepared"],
    "all_at_once": ["stalled_stream", "ingest_upload", "partitioned_upload", "prepared"],
}


@pytest.mark.parametrize("graceful", [True, False], ids=["second_signal", "graceful_off"])
@pytest.mark.parametrize("shape", list(SHAPES))
def test_forced_stop_with_work_in_flight_exits_cleanly(launcher, shape, graceful):
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "true" if graceful else "false",
                                 "HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"})
    in_flight = InFlight(server=server)
    if graceful:
        # A statement that never finishes on its own holds the drain open whatever the
        # shape (an upload isn't a statement), so only the second signal can stop it.
        outcome = run_in_background(session=server.admin(), sql=LONG)
        wait_running(session=server.admin())
    for start in SHAPES[shape]:
        getattr(in_flight, start)()
    started = time.monotonic()
    server.process.send_signal(STOP_SIGNAL)
    if graceful:
        time.sleep(1)
        assert server.process.poll() is None, "the first signal must only drain"
        server.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=server, timeout=15)
    assert time.monotonic() - started < 15
    in_flight.join()
    if graceful:
        outcome["thread"].join(timeout=30)


class TlsTarget:
    """A server with TLS and mTLS, as InFlight and assert_exits_cleanly use one: every
    connection presents the client certificate `identity` and verifies the server."""

    def __init__(self, tls_server, identity):
        self.tls = tls_server
        self.identity = identity
        self.base_url = tls_server.base_url
        self.process = tls_server.running.process
        self.log = tls_server.running.log
        running = tls_server.running
        self.token = tls_server.password_token(identity=identity, user=running.username, password=running.password)

    def http(self) -> httpx.Client:
        return httpx.Client(verify=self.tls.pki.context(identity=self.identity), timeout=30)

    def admin(self):
        client = Client(base_url=self.base_url, token=self.token)
        client.http.close()
        client.http = httpx.Client(base_url=self.base_url, verify=self.tls.pki.context(identity=self.identity),
                                   timeout=120)
        return client.new_session()


@pytest.mark.parametrize("graceful", [True, False], ids=["second_signal", "graceful_off"])
def test_forced_stop_over_mtls_exits_cleanly(launcher, tmp_path, graceful):
    # TLS connections end differently (close_notify, the TLS stream's own writer): a stop
    # with a stalled stream, an upload and a prepared statement on them exits 0 too.
    from test_mtls_identity import Pki, start

    pki = Pki(directory=tmp_path)
    ops = pki.issue(name="ops@example.com")
    target = TlsTarget(tls_server=start(launcher=launcher, pki=pki, probe=ops, env={
        "HARDCHARGER_GRACEFUL_SHUTDOWN": "true" if graceful else "false",
        "HARDCHARGER_SHUTDOWN_GRACE_PERIOD_SECONDS": "300"}), identity=ops)
    in_flight = InFlight(server=target, new_http=target.http)
    for start_shape in SHAPES["all_at_once"]:
        getattr(in_flight, start_shape)()
    if graceful:
        outcome = run_in_background(session=target.admin(), sql=LONG)
        wait_running(session=target.admin())
    started = time.monotonic()
    target.process.send_signal(STOP_SIGNAL)
    if graceful:
        time.sleep(1)
        assert target.process.poll() is None, "the first signal must only drain"
        target.process.send_signal(STOP_SIGNAL)
    assert_exits_cleanly(server=target, timeout=15)
    assert time.monotonic() - started < 15
    in_flight.join()
    if graceful:
        outcome["thread"].join(timeout=30)
