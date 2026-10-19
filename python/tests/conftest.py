"""Fixtures: a fake GitHub (API host plus a separate storage host the asset download
redirects to), fake release tarballs, and the real server binary for the integration
tests.

Test settings use HARDCHARGER_PY_TEST_* names only:

HARDCHARGER_PY_TEST_SERVER_BINARY
    A real hardcharger-server for the ``integration`` tests (they fail without it).
HARDCHARGER_PY_TEST_TOKEN
    A GitHub token for the ``download`` test (else ``gh auth token``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.server
import io
import json
import os
import tarfile
import threading
from pathlib import Path

import pytest

from hardcharger import SERVER_VERSION
from hardcharger._install import detect_platform

TEST_BINARY_ENV = "HARDCHARGER_PY_TEST_SERVER_BINARY"
TEST_TOKEN_ENV = "HARDCHARGER_PY_TEST_TOKEN"
TOKEN = "test-token-123"

# Every variable the package reads: tests start without them (this laptop's shell may
# export real ones).
PACKAGE_ENV = (
    "HARDCHARGER_SERVER_BINARY",
    "HARDCHARGER_VERSION",
    "HARDCHARGER_CACHE_DIR",
    "HARDCHARGER_RELEASE_API_URL",
    "HARDCHARGER_RELEASE_REPO",
    "HARDCHARGER_INSTALL_TOKEN",
    "GH_TOKEN",
    "XDG_CACHE_HOME",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in PACKAGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARDCHARGER_CACHE_DIR", str(tmp_path / "cache"))


def fake_server_script(version: str) -> bytes:
    script = (
        "#!/bin/sh\n"
        f'if [ "$1" = --version ]; then echo "Hardcharger {version}"; exit 0; fi\n'
        "exit 3\n"
    )
    return script.encode()


def make_tarball(
    platform_name: str, version: str = SERVER_VERSION, extra: dict[str, bytes] | None = None
) -> bytes:
    """A release tarball shaped like the real ones: hardcharger-<platform>/bin/...,
    lib/, LICENSE."""
    out = io.BytesIO()
    top = f"hardcharger-{platform_name}"
    files = {
        f"{top}/bin/hardcharger-server": fake_server_script(version=version),
        f"{top}/lib/libduckdb.so": b"not really a library",
        f"{top}/LICENSE": b"Apache-2.0",
        **(extra or {}),
    }
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755 if "/bin/" in name else 0o644
            tar.addfile(tarinfo=info, fileobj=io.BytesIO(initial_bytes=data))
    return out.getvalue()


@dataclasses.dataclass
class Recorded:
    path: str
    authorization: str | None


class _Server:
    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeGitHub:
    """``/repos/<repo>/releases/tags/v<version>`` and ``/assets/<id>`` on the API host;
    the asset answer is a 302 to the storage host, as GitHub's is. ``require_token``
    makes the API answer 404 without the right token, as GitHub does for a private
    repository."""

    def __init__(self, tarball: bytes, version: str = SERVER_VERSION, digest: str | None = None):
        self.tarball = tarball
        self.version = version
        self.digest = (
            digest if digest is not None else "sha256:" + hashlib.sha256(tarball).hexdigest()
        )
        self.require_token = True
        self.api_requests: list[Recorded] = []
        self.storage_requests: list[Recorded] = []
        github = self

        class Storage(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                github.storage_requests.append(
                    Recorded(path=self.path, authorization=self.headers.get("Authorization"))
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(github.tarball)))
                self.end_headers()
                self.wfile.write(github.tarball)

        class Api(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def reply(self, status: int, body: dict):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                authorization = self.headers.get("Authorization")
                github.api_requests.append(Recorded(path=self.path, authorization=authorization))
                if github.require_token and authorization != f"Bearer {TOKEN}":
                    return self.reply(status=404, body={"message": "Not Found"})
                platform_name = detect_platform()
                if self.path == f"/repos/gizmodata/hardcharger/releases/tags/v{github.version}":
                    return self.reply(
                        status=200,
                        body={
                            "tag_name": f"v{github.version}",
                            "assets": [
                                {
                                    "name": f"hardcharger-{platform_name}.tar.gz",
                                    "url": f"{github.api.url}/assets/1",
                                    "digest": github.digest,
                                }
                            ],
                        },
                    )
                if self.path == "/assets/1":
                    self.send_response(302)
                    self.send_header("Location", f"{github.storage.url}/blob/1?sig=abc")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self.reply(status=404, body={"message": "Not Found"})

        self.api = _Server(handler=Api)
        self.storage = _Server(handler=Storage)

    def close(self):
        self.api.close()
        self.storage.close()


@pytest.fixture
def fake_github(monkeypatch):
    github = FakeGitHub(tarball=make_tarball(platform_name=detect_platform()))
    monkeypatch.setenv("HARDCHARGER_RELEASE_API_URL", github.api.url)
    yield github
    github.close()


@pytest.fixture
def real_binary() -> Path:
    """The real hardcharger-server; the integration tests fail (never skip) without it."""
    value = os.environ.get(TEST_BINARY_ENV)
    if not value:
        pytest.fail(reason=f"set {TEST_BINARY_ENV} to a hardcharger-server binary")
    path = Path(value)
    if not path.is_file():
        pytest.fail(reason=f"{TEST_BINARY_ENV}={path} doesn't exist")
    return path
