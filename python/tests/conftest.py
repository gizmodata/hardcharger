"""Fixtures: a fake GitHub (the API host, the host of the direct download URLs, and a
separate storage host both redirect downloads to), a fake container registry (token
endpoint, index, manifests, blobs redirected to their own storage host), fake release
tarballs and image layers, and the real server binary for the integration tests.

No unit test reaches the real GitHub or a real registry: every test starts with the
package's addresses pointing at a closed local port, and the fakes replace them.

Test settings use HARDCHARGER_PY_TEST_* names only:

HARDCHARGER_PY_TEST_SERVER_BINARY
    A real hardcharger-server for the ``integration`` tests (they fail without it).
HARDCHARGER_PY_TEST_TOKEN
    A GitHub token for the ``download`` test that goes through the GitHub API (else
    ``gh auth token``; ``none`` for no token).
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.server
import io
import json
import os
import platform
import socket
import tarfile
import threading
import urllib.parse
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
    "HARDCHARGER_RELEASE_DOWNLOAD_URL",
    "HARDCHARGER_RELEASE_REPO",
    "HARDCHARGER_INSTALL_TOKEN",
    "HARDCHARGER_INSTALL_SOURCE",
    "HARDCHARGER_INSTALL_REGISTRY_IMAGE",
    "GH_TOKEN",
    "XDG_CACHE_HOME",
)


def closed_address() -> str:
    """``127.0.0.1:<port>`` with nothing listening: a connection is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in PACKAGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARDCHARGER_CACHE_DIR", str(tmp_path / "cache"))
    # Nothing a test forgets to fake may reach the real GitHub or ghcr.io.
    nowhere = closed_address()
    monkeypatch.setenv("HARDCHARGER_RELEASE_API_URL", f"http://{nowhere}/api")
    monkeypatch.setenv("HARDCHARGER_RELEASE_DOWNLOAD_URL", f"http://{nowhere}/web")
    monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", f"http://{nowhere}/test/hardcharger")


@pytest.fixture
def linux(monkeypatch):
    """This machine is linux-arm64, whatever it really is. Ask for it before the fakes."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")


@pytest.fixture
def macos(monkeypatch):
    """This machine is macos-arm64, whatever it really is. Ask for it before the fakes."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")


NOTICES = b"# Third-party software in Hardcharger\n"


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
    lib/, LICENSE and the third-party notices."""
    out = io.BytesIO()
    top = f"hardcharger-{platform_name}"
    files = {
        f"{top}/bin/hardcharger-server": fake_server_script(version=version),
        f"{top}/lib/libduckdb.so": b"not really a library",
        f"{top}/LICENSE": b"Apache-2.0",
        f"{top}/THIRD_PARTY_NOTICES.md": NOTICES,
        f"{top}/THIRD_PARTY_NOTICES-web-ui.txt": b"Hardcharger web UI: third-party notices\n",
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
        # A short poll: close() waits for one, and every test closes several servers.
        threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeGitHub:
    """GitHub's three hosts.

    The API: ``/repos/<repo>/releases/tags/v<version>`` and ``/assets/<id>``; an asset
    answer is a 302 to the storage host, as GitHub's is. ``require_token`` makes the API
    answer 404 without the right token, as GitHub does for a private repository.

    The web host: ``/<repo>/releases/download/v<version>/<name>`` for the tarball and
    ``SHA256SUMS``, each a 302 to the storage host, with no token involved.

    ``api_error`` / ``web_error`` (a status and headers) make a host refuse everything,
    ``storage_error`` (a status) the storage host; ``truncate`` makes the storage host
    close the tarball's download halfway."""

    def __init__(self, tarball: bytes, version: str = SERVER_VERSION, digest: str | None = None):
        self.tarball = tarball
        self.version = version
        #: The digest the API records for the tarball; None: the tarball's own, "": none.
        self.digest = digest
        #: The SHA256SUMS text; None: one that lists the tarball correctly.
        self.sums: str | None = None
        #: Whether the release has a SHA256SUMS asset in the API's answer.
        self.api_lists_sums = True
        self.require_token = True
        self.api_error: tuple[int, dict[str, str]] | None = None
        self.web_error: tuple[int, dict[str, str]] | None = None
        self.storage_error: int | None = None
        self.truncate = False
        self.api_requests: list[Recorded] = []
        self.web_requests: list[Recorded] = []
        self.storage_requests: list[Recorded] = []
        github = self

        def asset_name() -> str:
            return f"hardcharger-{detect_platform()}.tar.gz"

        def sums_bytes() -> bytes:
            if github.sums is not None:
                return github.sums.encode()
            other = hashlib.sha256(b"another asset").hexdigest()
            return (
                f"{other}  hardcharger-windows-amd64.zip\n"
                f"{hashlib.sha256(github.tarball).hexdigest()}  {asset_name()}\n"
            ).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def reply(self, status: int, body: dict, headers: dict[str, str] | None = None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def redirect(self, location: str):
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class Storage(Handler):
            def do_GET(self):
                github.storage_requests.append(
                    Recorded(path=self.path, authorization=self.headers.get("Authorization"))
                )
                if github.storage_error is not None:
                    return self.reply(status=github.storage_error, body={"message": "no"})
                data = sums_bytes() if self.path.startswith("/blob/sums") else github.tarball
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if github.truncate and data is github.tarball:
                    self.wfile.write(data[: len(data) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(data)

        class Web(Handler):
            def do_GET(self):
                github.web_requests.append(
                    Recorded(path=self.path, authorization=self.headers.get("Authorization"))
                )
                if github.web_error is not None:
                    status, headers = github.web_error
                    return self.reply(status=status, body={"message": "no"}, headers=headers)
                release = f"/gizmodata/hardcharger/releases/download/v{github.version}"
                if self.path == f"{release}/SHA256SUMS":
                    return self.redirect(location=f"{github.storage.url}/blob/sums?sig=abc")
                if self.path == f"{release}/{asset_name()}":
                    return self.redirect(location=f"{github.storage.url}/blob/1?sig=abc")
                return self.reply(status=404, body={"message": "Not Found"})

        class Api(Handler):
            def do_GET(self):
                authorization = self.headers.get("Authorization")
                github.api_requests.append(Recorded(path=self.path, authorization=authorization))
                if github.api_error is not None:
                    status, headers = github.api_error
                    return self.reply(status=status, body={"message": "no"}, headers=headers)
                if github.require_token and authorization != f"Bearer {TOKEN}":
                    return self.reply(status=404, body={"message": "Not Found"})
                if self.path == f"/repos/gizmodata/hardcharger/releases/tags/v{github.version}":
                    digest = github.digest
                    if digest is None:
                        digest = "sha256:" + hashlib.sha256(github.tarball).hexdigest()
                    asset = {"name": asset_name(), "url": f"{github.api.url}/assets/1"}
                    if digest:
                        asset["digest"] = digest
                    assets = [asset]
                    if github.api_lists_sums:
                        assets.append({"name": "SHA256SUMS", "url": f"{github.api.url}/assets/2"})
                    return self.reply(
                        status=200, body={"tag_name": f"v{github.version}", "assets": assets}
                    )
                if self.path == "/assets/1":
                    return self.redirect(location=f"{github.storage.url}/blob/1?sig=abc")
                if self.path == "/assets/2":
                    return self.redirect(location=f"{github.storage.url}/blob/sums?sig=abc")
                return self.reply(status=404, body={"message": "Not Found"})

        self.api = _Server(handler=Api)
        self.web = _Server(handler=Web)
        self.storage = _Server(handler=Storage)

    def close(self):
        self.api.close()
        self.web.close()
        self.storage.close()


@pytest.fixture
def fake_github(monkeypatch):
    github = FakeGitHub(tarball=make_tarball(platform_name=detect_platform()))
    monkeypatch.setenv("HARDCHARGER_RELEASE_API_URL", github.api.url)
    monkeypatch.setenv("HARDCHARGER_RELEASE_DOWNLOAD_URL", github.web.url)
    yield github
    github.close()


# ---- the container image ---------------------------------------------------------------

PULL_TOKEN = "pull-token-456"
IMAGE_REPOSITORY = "test/hardcharger"


def make_layer(entries: dict[str, object]) -> bytes:
    """An image layer (tar+gzip). Each name maps to bytes (a file), None (a directory),
    ``("symlink", target)``, ``("hardlink", name)`` or ``("device",)``. A name ending in
    ``.wh.<name>`` or ``.wh..wh..opq`` with ``b""`` is a whiteout, as in a real layer."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, value in entries.items():
            info = tarfile.TarInfo(name=name)
            if value is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(tarinfo=info)
            elif isinstance(value, bytes):
                info.size = len(value)
                info.mode = 0o755 if "/bin/" in name else 0o644
                tar.addfile(tarinfo=info, fileobj=io.BytesIO(initial_bytes=value))
            elif value[0] == "symlink":
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, value[1], 0o777
                tar.addfile(tarinfo=info)
            elif value[0] == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, value[1]
                tar.addfile(tarinfo=info)
            else:
                info.type, info.devmajor, info.devminor = tarfile.CHRTYPE, 1, 3
                tar.addfile(tarinfo=info)
    return out.getvalue()


def image_layers(version: str = SERVER_VERSION) -> list[bytes]:
    """The layers of an image shaped like the real one (docker/Dockerfile), base first:
    the distribution, the packages, the release tree in /opt/hardcharger, the links to
    it (absolute, outside the tree), and an empty layer."""
    return [
        make_layer(
            entries={
                "etc": None,
                "etc/os-release": b"NAME=Fake\n",
                "usr/bin/perl": b"perl",
                "usr/bin/perl5": ("hardlink", "usr/bin/perl"),
            }
        ),
        make_layer(
            entries={"etc": None, "etc/passwd": b"hardcharger:x:999:999::/var/lib:/bin/sh\n"}
        ),
        make_layer(
            entries={
                "opt": None,
                "opt/hardcharger": None,
                "opt/hardcharger/CHANGELOG.md": b"# Changelog\n",
                "opt/hardcharger/LICENSE": b"Apache-2.0",
                "opt/hardcharger/THIRD_PARTY_NOTICES-web-ui.txt": (
                    b"Hardcharger web UI: third-party notices\n"
                ),
                "opt/hardcharger/THIRD_PARTY_NOTICES.md": NOTICES,
                "opt/hardcharger/bin": None,
                "opt/hardcharger/bin/hardcharger": b"#!/bin/sh\necho client\n",
                "opt/hardcharger/bin/hardcharger-server": fake_server_script(version=version),
            }
        ),
        make_layer(
            entries={
                "usr": None,
                "usr/local": None,
                "usr/local/bin": None,
                "usr/local/bin/hardcharger-server": (
                    "symlink",
                    "/opt/hardcharger/bin/hardcharger-server",
                ),
            }
        ),
        make_layer(entries={}),
    ]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    """A container registry: ``/v2/<repository>/manifests/<tag or digest>`` and
    ``/v2/<repository>/blobs/<digest>``, the latter a 307 to a storage host, as
    ghcr.io's is. With ``require_auth`` every ``/v2/`` request needs the pull token that
    ``/token`` hands out to anybody, and gets a 401 naming that endpoint without it.

    ``blobs`` and ``manifests`` map a digest (or tag) to what is served for it, so a test
    can serve bytes that don't match their digest."""

    def __init__(self):
        self.require_auth = True
        #: The 401's WWW-Authenticate; None: a Bearer challenge naming /token.
        self.challenge: str | None = None
        #: The pull token /token hands out and /v2/ accepts; a test may change it mid-pull.
        self.pull_token = PULL_TOKEN
        #: After this many accepted /v2/ requests the pull token expires (a new one is issued).
        self.expire_after: int | None = None
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, tuple[str, bytes]] = {}
        self.requests: list[Recorded] = []
        self.token_requests: list[Recorded] = []
        self.storage_requests: list[Recorded] = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send(self, status: int, data: bytes, headers: dict[str, str]):
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    # The launcher stopped reading (a blob longer than its manifest says).
                    self.close_connection = True

        class Storage(Handler):
            def do_GET(self):
                registry.storage_requests.append(
                    Recorded(path=self.path, authorization=self.headers.get("Authorization"))
                )
                digest = urllib.parse.urlsplit(self.path).path.removeprefix("/blobs/")
                self.send(status=200, data=registry.blobs[digest], headers={})

        class Registry(Handler):
            def do_GET(self):
                authorization = self.headers.get("Authorization")
                record = Recorded(path=self.path, authorization=authorization)
                if self.path.startswith("/token?"):
                    registry.token_requests.append(record)
                    body = json.dumps({"token": registry.pull_token}).encode()
                    return self.send(status=200, data=body, headers={})
                registry.requests.append(record)
                if registry.expire_after == 0:
                    registry.pull_token, registry.expire_after = f"{PULL_TOKEN}-renewed", None
                if registry.require_auth and authorization != f"Bearer {registry.pull_token}":
                    challenge = registry.challenge or (
                        f'Bearer realm="{registry.registry.url}/token",service="fake-registry",'
                        f'scope="repository:{IMAGE_REPOSITORY}:pull"'
                    )
                    return self.send(
                        status=401, data=b"{}", headers={"WWW-Authenticate": challenge}
                    )
                if registry.expire_after is not None:
                    registry.expire_after -= 1
                kind, _, reference = self.path.removeprefix(f"/v2/{IMAGE_REPOSITORY}/").partition(
                    "/"
                )
                if kind == "manifests" and reference in registry.manifests:
                    media_type, data = registry.manifests[reference]
                    return self.send(status=200, data=data, headers={"Content-Type": media_type})
                if kind == "blobs" and reference in registry.blobs:
                    location = f"{registry.storage.url}/blobs/{reference}?sig=xyz"
                    return self.send(status=307, data=b"", headers={"Location": location})
                return self.send(
                    status=404, data=b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', headers={}
                )

        self.registry = _Server(handler=Registry)
        self.storage = _Server(handler=Storage)
        #: What HARDCHARGER_INSTALL_REGISTRY_IMAGE is set to.
        self.image = f"{self.registry.url}/{IMAGE_REPOSITORY}"
        #: The image's name in messages.
        self.name = f"{self.registry.url.removeprefix('http://')}/{IMAGE_REPOSITORY}"

    def add_blob(self, data: bytes) -> str:
        digest = _sha256(data)
        self.blobs[digest] = data
        return digest

    def add_manifest(self, layers: list[bytes], architecture: str, os_name: str = "linux") -> str:
        """One image's manifest (with its configuration and layers); returns its digest."""
        config = json.dumps({"architecture": architecture, "os": os_name}).encode()
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": self.add_blob(data=config),
                    "size": len(config),
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": self.add_blob(data=layer),
                        "size": len(layer),
                    }
                    for layer in layers
                ],
            }
        ).encode()
        digest = _sha256(manifest)
        self.manifests[digest] = ("application/vnd.oci.image.manifest.v1+json", manifest)
        return digest

    def tag_index(self, tag: str, variants: dict[str, list[bytes]]) -> None:
        """Tag a multi-architecture image: an index of one manifest per architecture, plus
        an attestation entry (platform unknown/unknown), as buildx pushes them."""
        entries = [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": self.add_manifest(layers=layers, architecture=architecture),
                "platform": {"architecture": architecture, "os": "linux"},
            }
            for architecture, layers in variants.items()
        ]
        entries.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": _sha256(b"attestation"),
                "platform": {"architecture": "unknown", "os": "unknown"},
            }
        )
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": entries,
        }
        self.manifests[tag] = (
            "application/vnd.oci.image.index.v1+json",
            json.dumps(index).encode(),
        )

    def tag_manifest(self, tag: str, digest: str) -> None:
        """Tag one image without an index, as a single-architecture copy has it."""
        self.manifests[tag] = self.manifests[digest]

    def layer_digests(self, tag: str, architecture: str) -> list[str]:
        index = json.loads(self.manifests[tag][1])
        digest = next(
            m["digest"] for m in index["manifests"] if m["platform"]["architecture"] == architecture
        )
        return [layer["digest"] for layer in json.loads(self.manifests[digest][1])["layers"]]

    def close(self):
        self.registry.close()
        self.storage.close()


@pytest.fixture
def fake_registry(monkeypatch):
    """A registry holding the image of the pinned version for both architectures."""
    registry = FakeRegistry()
    registry.tag_index(
        tag=SERVER_VERSION, variants={"amd64": image_layers(), "arm64": image_layers()}
    )
    monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", registry.image)
    yield registry
    registry.close()


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
