"""Starts Hardcharger servers for the tests.

HARDCHARGER_TEST_BINARY names the server binary (default: ../build/hardcharger). A
missing binary or a server that doesn't come up fails the run; nothing is skipped.

Enterprise features need a license. With HARDCHARGER_TEST_LICENSE_KEY_FILE set (a real
GizmoData key with every feature), licensed tests use it. Without it they need a
test-license build (-DHARDCHARGER_TRUST_TEST_LICENSES=ON), which also trusts the key in
server/tests/license; this run signs licenses with it, each with the features its test
names. That build is never installed or shipped. HARDCHARGER_TEST_RELEASE_BINARY names
the build that ships (default: ../build/hardcharger), for the tests that check it refuses
test-signed keys.
"""

import dataclasses
import datetime
import json
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from warp_client import Client, mint_server_token

BUILD = pathlib.Path(__file__).resolve().parent.parent / "build"
# Absolute (a relative name is the caller's, but servers run in their own directories).
BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_BINARY", BUILD / "hardcharger")).resolve()
RELEASE_BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_RELEASE_BINARY", BUILD / "hardcharger")).resolve()
LICENSE_DIR = pathlib.Path(__file__).resolve().parent / "license"
UNIT_TEST_BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_UNIT_BINARY", BUILD / "hardcharger-unit-tests")).resolve()
REAL_LICENSE_FILE = os.environ.get("HARDCHARGER_TEST_LICENSE_KEY_FILE", "")

ALL_FEATURES = ["instrumentation", "kill_session", "catalog_permissions", "external_auth", "statement_queue",
                "metrics", "rbac"]

# GizmoSQL 1.x's unprefixed environment names, which Hardcharger reads too: a value this
# shell exports (for a real server) must never reach a test server.
LEGACY_ENV = {"SECRET_KEY", "INIT_SQL_COMMANDS", "INIT_SQL_COMMANDS_FILE", "TOKEN_ALLOWED_ISSUER",
              "TOKEN_ALLOWED_AUDIENCE", "TOKEN_SIGNATURE_VERIFY_CERT_PATH", "DATABASE_FILENAME", "DATABASE_BACKEND",
              "TLS_ENABLED", "TLS_CERT", "TLS_KEY", "READONLY", "QUERY_TIMEOUT", "PRINT_QUERIES",
              "MTLS_CA_CERT_FILENAME", "HEALTH_PORT"}


def free_port() -> int:
    with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        (_HOME / ".duckdb" / "extensions").symlink_to(real)
    return _HOME


_HOME = None


def clean_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HARDCHARGER_", "GIZMOSQL_")) and k not in LEGACY_ENV}
    env["HOME"] = str(_test_home())
    return env


def is_test_license_build(binary: pathlib.Path) -> bool:
    if binary not in _BUILD_KIND:
        if not binary.exists():
            pytest.fail(reason=f"{binary} is missing: build the server first")
        version = subprocess.run(args=[str(binary), "--version"], capture_output=True, text=True, check=True).stdout
        _BUILD_KIND[binary] = "test-license build" in version
    return _BUILD_KIND[binary]


_BUILD_KIND: dict[pathlib.Path, bool] = {}


def require_test_license_build(binary: pathlib.Path) -> None:
    if not is_test_license_build(binary=binary):
        pytest.fail(reason=f"{binary} is not a test-license build: build with -DHARDCHARGER_TRUST_TEST_LICENSES=ON "
                           f"(or set HARDCHARGER_TEST_LICENSE_KEY_FILE to a real license)")


# What GIZMOSQL_EDITION() and server-info say for a licensed server of this run.
ENTERPRISE = "Enterprise" if REAL_LICENSE_FILE else "Enterprise (test license)"


class LicenseSigner:
    """Signs license keys in GizmoData's format with the tests' key (server/tests/license),
    which only a test-license build trusts."""

    def __init__(self):
        self.key = serialization.load_pem_private_key(
            data=(LICENSE_DIR / "test-license-private.pem").read_bytes(), password=None)

    def sign(self, features: list[str], lifetime: datetime.timedelta = datetime.timedelta(days=1), **overrides) -> str:
        now = datetime.datetime.now(tz=datetime.UTC)
        payload = {"jti": str(uuid.uuid4()), "iss": "GizmoData LLC", "sub": "tests@gizmodata.com",
                   "aud": "Hardcharger tests", "iat": now, "nbf": now - datetime.timedelta(minutes=1),
                   "exp": now + lifetime, "features": features, **overrides}
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload=payload, key=self.key, algorithm="RS256")


@pytest.fixture(scope="session")
def license_signer() -> LicenseSigner:
    return LicenseSigner()


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
                                    "password": self.password if password is None else password})
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def session(self, token: str) -> str:
        response = httpx.post(url=f"{self.base_url}/warp/v1/sessions",
                              headers={"Authorization": f"Bearer {token}"}, json={})
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
            self.process.send_signal(signal.SIGTERM)
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Launcher:
    def __init__(self, tmp_path: pathlib.Path, signer_factory):
        self.tmp_path = tmp_path
        self.started: list[Running] = []
        self._signer_factory = signer_factory

    def start(self, args: list[str] | None = None, env: dict[str, str] | None = None,
              password: str | None = None, expect_up: bool = True, features: list[str] | None = None,
              license_key: str | None = None, username: str = "admin", secret_key: str | None = None,
              binary: pathlib.Path | None = None, wait_for: str = "/warp", startup_seconds: float = 30) -> Running:
        """Starts a server. `features`: run it licensed with those Enterprise features
        (see the module docstring); `license_key`: a key this run signed itself."""
        binary = binary or BINARY
        env = dict(env or {})
        if features is not None or license_key is not None:
            if REAL_LICENSE_FILE and license_key is None:
                env.setdefault("HARDCHARGER_LICENSE_KEY_FILE", REAL_LICENSE_FILE)
            else:
                require_test_license_build(binary=binary)
                env.setdefault("HARDCHARGER_LICENSE_KEY",
                               license_key or self._signer_factory().sign(
                                   features=features if features is not None else ALL_FEATURES))
        if not binary.exists():
            pytest.fail(reason=f"{binary} is missing: build the server first")
        port = free_port()
        password = password or secrets.token_urlsafe(nbytes=12) + ":p@ss"
        given = next((env[name] for name in ("HARDCHARGER_SECRET_KEY", "SECRET_KEY", "GIZMOSQL_SECRET_KEY")
                      if name in env), None)
        secret_key = secret_key or given or secrets.token_hex(nbytes=32)
        if given is None:
            env["HARDCHARGER_SECRET_KEY"] = secret_key
        log_path = self.tmp_path / f"server-{port}.log"
        process = subprocess.Popen(
            args=[str(binary), "--host", "127.0.0.1", "--port", str(port), *(args or [])],
            env={**clean_environment(), "HARDCHARGER_PASSWORD": password, "HARDCHARGER_USERNAME": username, **env},
            stdout=subprocess.DEVNULL, stderr=log_path.open("w"), cwd=self.tmp_path)
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
def launcher(tmp_path, request):
    launch = Launcher(tmp_path=tmp_path, signer_factory=lambda: request.getfixturevalue("license_signer"))
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


@pytest.fixture(scope="module")
def module_launcher(tmp_path_factory, request):
    """A launcher whose servers live for the whole test module."""
    launch = Launcher(tmp_path=tmp_path_factory.mktemp("module"),
                      signer_factory=lambda: request.getfixturevalue("license_signer"))
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
