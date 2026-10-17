"""Starts Hardcharger servers for the tests.

HARDCHARGER_TEST_BINARY names the server binary (default: ../build/hardcharger). A
missing binary or a server that doesn't come up fails the run; nothing is skipped.
"""

import dataclasses
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import time

import httpx
import pytest

BINARY = pathlib.Path(os.environ.get("HARDCHARGER_TEST_BINARY",
                                     pathlib.Path(__file__).resolve().parent.parent / "build" / "hardcharger"))


def free_port() -> int:
    with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclasses.dataclass
class Running:
    base_url: str
    username: str
    password: str
    process: subprocess.Popen
    log_path: pathlib.Path

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

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

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Launcher:
    def __init__(self, tmp_path: pathlib.Path):
        self.tmp_path = tmp_path
        self.started: list[Running] = []

    def start(self, args: list[str] | None = None, env: dict[str, str] | None = None,
              password: str | None = None, expect_up: bool = True) -> Running:
        if not BINARY.exists():
            pytest.fail(reason=f"{BINARY} is missing: build the server first")
        port = free_port()
        password = password or secrets.token_urlsafe(nbytes=12) + ":p@ss"
        log_path = self.tmp_path / f"server-{port}.log"
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("HARDCHARGER_", "GIZMOSQL_"))}
        process = subprocess.Popen(
            args=[str(BINARY), "--host", "127.0.0.1", "--port", str(port), *(args or [])],
            env={**clean_env, "HARDCHARGER_PASSWORD": password, "HARDCHARGER_USERNAME": "admin",
                 **(env or {})},
            stdout=subprocess.DEVNULL, stderr=log_path.open("w"))
        running = Running(base_url=f"http://127.0.0.1:{port}", username="admin", password=password,
                          process=process, log_path=log_path)
        self.started.append(running)
        if expect_up:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(reason=f"server exited: {running.log()}")
                try:
                    if httpx.get(url=f"{running.base_url}/warp", timeout=1).status_code == 200:
                        return running
                except httpx.HTTPError:
                    time.sleep(0.1)
            pytest.fail(reason=f"server did not come up: {running.log()}")
        return running

    def stop_all(self) -> None:
        for running in self.started:
            running.stop()


@pytest.fixture
def launcher(tmp_path):
    launch = Launcher(tmp_path=tmp_path)
    yield launch
    launch.stop_all()


@pytest.fixture(scope="module")
def module_launcher(tmp_path_factory):
    """Servers shared by the tests of one module (seeded once, as GizmoSQL 1.x's fixtures)."""
    launch = Launcher(tmp_path=tmp_path_factory.mktemp("servers"))
    yield launch
    launch.stop_all()


@pytest.fixture(scope="session")
def store():
    """The PostgreSQL the access-control tests keep their grant stores on."""
    from rbac import Store
    databases = Store()
    yield databases
    databases.drop_all()
