"""Starts Hardcharger servers for the tests.

HARDCHARGER_TEST_BINARY names the server binary (default: ../build/hardcharger-server), the
one build that ships; HARDCHARGER_TEST_CLIENT_BINARY the command-line client (default:
../build/hardcharger) and HARDCHARGER_TEST_CLIENT_TESTS_BINARY its ported C++ tests. A missing binary or a server that doesn't come up fails the run;
nothing is skipped. Every feature is on whenever its own option says so: there is no
license.
"""

import dataclasses
import json
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from warp_client import Client, mint_server_token

BUILD = pathlib.Path(__file__).resolve().parent.parent / "build"
EXE = ".exe" if sys.platform == "win32" else ""
# Absolute (a relative name is the caller's, but servers run in their own directories).
BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_BINARY", BUILD / f"hardcharger-server{EXE}")).resolve()
# Sanitizer builds run several times slower: HARDCHARGER_TEST_TIMEOUT_SCALE stretches the
# waits that tests size by hand (CI's TSan job sets it), never what they assert.
TIMEOUT_SCALE = float(os.environ.get("HARDCHARGER_TEST_TIMEOUT_SCALE", "1"))
# Wall-clock performance limits (the TPC-H suite's 30 s) times HARDCHARGER_TEST_PERF_SCALE.
# Only the TSan job sets it: instrumented code runs 5-15x slower, and there the workload is
# run for its races and its results; the build jobs keep 1 and are where the limits count.
PERF_SCALE = float(os.environ.get("HARDCHARGER_TEST_PERF_SCALE", "1"))
UNIT_TEST_BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_UNIT_BINARY",
                                               BUILD / f"hardcharger-unit-tests{EXE}")).resolve()
CLIENT_BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_CLIENT_BINARY", BUILD / f"hardcharger{EXE}")).resolve()
CLIENT_TESTS_BINARY = pathlib.Path(
    os.environ.get("HARDCHARGER_TEST_CLIENT_TESTS_BINARY", BUILD / f"hardcharger-client-tests{EXE}")).resolve()


# How the tests ask a server to stop, as `kill` does with SIGTERM: on Windows a console
# Ctrl+Break event, which reaches only a process started in its own process group
# (POPEN_FLAGS; Ctrl+C can't be sent to one). A hard kill (SIGKILL) is process.kill().
if sys.platform == "win32":
    STOP_SIGNAL = signal.CTRL_BREAK_EVENT
    POPEN_FLAGS = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    STOP_SIGNAL = signal.SIGTERM
    POPEN_FLAGS = {}


# Ports for the servers the tests start. The OS's "any free port" (bind to 0) is free only
# until the socket closes, and the server binds it a moment later: under pytest-xdist
# another worker's bind to 0 can be handed the same port in between. So each worker
# takes ports from a block of its own, below every OS's ephemeral range (Linux starts
# at 32768, macOS at 49152), and never hands out the same port twice while it has others.
_PORTS_FIRST, _PORTS_END = 20000, 32000


def _worker_ports() -> range:
    count = max(1, int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1")))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    index = int(worker.removeprefix("gw")) if worker.removeprefix("gw").isdigit() else 0
    size = (_PORTS_END - _PORTS_FIRST) // count
    first = _PORTS_FIRST + (index % count) * size
    return range(first, first + size)


_PORTS = _worker_ports()
_next_port = _PORTS.start


def free_port() -> int:
    """A port no other worker of this run hands out, that nothing has bound right now."""
    global _next_port
    for _ in range(len(_PORTS)):
        port = _next_port
        _next_port = _PORTS.start + (_next_port + 1 - _PORTS.start) % len(_PORTS)
        with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue  # in use (or lingering): try the next one
            return port
    pytest.fail(reason=f"no free port in {_PORTS.start}-{_PORTS.stop - 1}")


def _test_home() -> pathlib.Path:
    """A HOME of the run's own for every server, so none of them sees this machine's
    ~/.duckdb persistent secrets (a server loads every one of them at startup). Only the
    extension cache is shared, so extensions download once."""
    global _HOME
    if _HOME is None:
        import tempfile
        _HOME = pathlib.Path(tempfile.mkdtemp(prefix="hardcharger-test-home-"))
        real = pathlib.Path.home() / ".duckdb" / "extensions"
        real.mkdir(parents=True, exist_ok=True)
        (_HOME / ".duckdb").mkdir()
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(real), str(_HOME / ".duckdb" / "extensions"))  # no privilege needed
        else:
            (_HOME / ".duckdb" / "extensions").symlink_to(real)
    return _HOME


_HOME = None


def clean_environment() -> dict[str, str]:
    # A value this shell exports (for a real server) must never reach a test server.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARDCHARGER_")}
    env["HOME"] = str(_test_home())
    if sys.platform == "win32":
        env["USERPROFILE"] = str(_test_home())  # DuckDB's (and the server's) home there
    return env


@dataclasses.dataclass
class Running:
    base_url: str
    username: str
    password: str
    secret_key: str
    process: subprocess.Popen
    log_path: pathlib.Path
    port: int

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def log_records(self) -> list[dict]:
        """The JSON log lines (servers started with log_format json)."""
        records = []
        for line in self.log().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def token(self, username: str | None = None, password: str | None = None) -> str:
        response = httpx.post(url=f"{self.base_url}/warp/v1/auth/token",
                              json={"grant_type": "password",
                                    "username": self.username if username is None else username,
                                    "password": self.password if password is None else password},
                              timeout=120)  # as the clients' (httpx's 5 s is short for sanitizer builds)
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def session(self, token: str) -> str:
        response = httpx.post(url=f"{self.base_url}/warp/v1/sessions",
                              headers={"Authorization": f"Bearer {token}"}, json={}, timeout=120)
        assert response.status_code == 201, response.text
        return response.json()["session_id"]

    def run(self, token: str, session_id: str, sql: str) -> httpx.Response:
        return httpx.post(url=f"{self.base_url}/warp/v1/sessions/{session_id}/statements",
                          headers={"Authorization": f"Bearer {token}"}, json={"sql": sql},
                          timeout=60)

    # ---- Clients ----

    def client(self, token: str | None = None) -> Client:
        """A client for `token` (default: the admin, signed in with the password)."""
        return Client(base_url=self.base_url, token=self.token() if token is None else token)

    def admin(self):
        """A session of the admin."""
        return self.client().new_session()

    def as_role(self, role: str, subject: str = "tester", **claims):
        """A session of a principal with `role`, from a token in the server's own format."""
        token = mint_server_token(secret_key=self.secret_key, subject=subject, role=role, **claims)
        return self.client(token=token).new_session()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(STOP_SIGNAL)
            self.process.send_signal(STOP_SIGNAL)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Launcher:
    def __init__(self, tmp_path: pathlib.Path):
        self.tmp_path = tmp_path
        self.started: list[Running] = []

    def start(self, args: list[str] | None = None, env: dict[str, str] | None = None,
              password: str | None = None, expect_up: bool = True, username: str = "admin",
              secret_key: str | None = None, binary: pathlib.Path | None = None, wait_for: str = "/warp",
              startup_seconds: float = 30) -> Running:
        """Starts a server (the administrator `username` with `password`, by default a
        random one)."""
        binary = binary or BINARY
        env = dict(env or {})
        if not binary.exists():
            pytest.fail(reason=f"{binary} is missing: build the server first")
        port = free_port()
        password = password or secrets.token_urlsafe(nbytes=12) + ":p@ss"
        given = env.get("HARDCHARGER_SECRET_KEY")
        secret_key = secret_key or given or secrets.token_hex(nbytes=32)
        if given is None:
            env["HARDCHARGER_SECRET_KEY"] = secret_key
        log_path = self.tmp_path / f"server-{port}.log"
        process = subprocess.Popen(
            args=[str(binary), "--host", "127.0.0.1", "--port", str(port), *(args or [])],
            env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": password, "HARDCHARGER_ADMIN_USERNAME": username,
                 **env},
            stdout=subprocess.DEVNULL, stderr=log_path.open("w"), cwd=self.tmp_path, **POPEN_FLAGS)
        running = Running(base_url=f"http://127.0.0.1:{port}", username=username, password=password,
                          secret_key=secret_key, process=process, log_path=log_path, port=port)
        self.started.append(running)
        if expect_up:
            deadline = time.monotonic() + startup_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(reason=f"server exited: {running.log()}")
                try:
                    if httpx.get(url=f"{running.base_url}{wait_for}", timeout=1).status_code == 200:
                        return running
                except httpx.HTTPError:
                    time.sleep(0.1)
            pytest.fail(reason=f"server did not come up: {running.log()}")
        return running

    @staticmethod
    def wait_healthy(running: "Running", timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if httpx.get(url=f"{running.base_url}/warp/v1/health", timeout=2).status_code == 200:
                return
            time.sleep(0.05)
        pytest.fail(reason=f"server never became healthy: {running.log()}")

    def stop_all(self) -> None:
        for running in self.started:
            running.stop()


@pytest.fixture
def launcher(tmp_path):
    launch = Launcher(tmp_path=tmp_path)
    yield launch
    launch.stop_all()


def run_unit_tests(gtest_filter: str) -> dict[str, bool]:
    """Runs the C++ unit tests matching `gtest_filter`; returns pass/fail by test name.
    Fails (never skips) when the binary is missing or no test matched."""
    if not UNIT_TEST_BINARY.exists():
        pytest.fail(reason=f"{UNIT_TEST_BINARY} is missing: build the server first")
    out = subprocess.run(args=[str(UNIT_TEST_BINARY), f"--gtest_filter={gtest_filter}", "--gtest_brief=0"],
                         capture_output=True, text=True, timeout=300)
    results = {}
    for line in out.stdout.splitlines():
        for marker, passed in (("[       OK ] ", True), ("[  FAILED  ] ", False)):
            if line.startswith(marker) and "(" in line:
                results[line[len(marker):].split(" (")[0]] = passed
    assert results, f"no unit test matched {gtest_filter}:\n{out.stdout}\n{out.stderr}"
    assert out.returncode == 0 or not all(results.values()), out.stdout + out.stderr
    return results


def run_client_tests(gtest_filter: str, env: dict[str, str] | None = None, timeout: float = 600) -> dict[str, bool]:
    """Runs the client's ported C++ tests matching `gtest_filter` (with `env` added to a
    clean environment); returns pass/fail by test name. Fails (never skips) when the
    binary is missing or no test matched."""
    if not CLIENT_TESTS_BINARY.exists():
        pytest.fail(reason=f"{CLIENT_TESTS_BINARY} is missing: build the server first")
    out = subprocess.run(args=[str(CLIENT_TESTS_BINARY), f"--gtest_filter={gtest_filter}", "--gtest_brief=0"],
                         capture_output=True, text=True, timeout=timeout, env={**clean_environment(), **(env or {})})
    results = {}
    for line in out.stdout.splitlines():
        for marker, passed in (("[       OK ] ", True), ("[  FAILED  ] ", False)):
            if line.startswith(marker) and "(" in line:
                results[line[len(marker):].split(" (")[0]] = passed
    assert results, f"no client test matched {gtest_filter}:\n{out.stdout}\n{out.stderr}"
    if not all(results.values()):
        print(out.stdout[-20000:], out.stderr[-5000:])
    assert out.returncode == 0 or not all(results.values()), out.stdout + out.stderr
    return results


def gtest_cases(source: pathlib.Path) -> list[str]:
    """The Suite.Name of every TEST/TEST_F in a C++ test file, in order."""
    import re
    return [f"{suite}.{name}" for suite, name in
            re.findall(pattern=r"^TEST(?:_F)?\((\w+),\s*(\w+)\)", string=source.read_text(), flags=re.M)]


@pytest.fixture(scope="module")
def module_launcher(tmp_path_factory):
    """A launcher whose servers live for the whole test module."""
    launch = Launcher(tmp_path=tmp_path_factory.mktemp("module"))
    yield launch
    launch.stop_all()


def problem(response: httpx.Response, status: int, code: str) -> dict:
    """Asserts `response` is the problem document with `status` and `code`."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


@pytest.fixture(scope="session")
def store():
    """The PostgreSQL the access-control tests keep their grant stores on."""
    from rbac import Store
    databases = Store()
    yield databases
    databases.drop_all()
