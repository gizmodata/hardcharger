"""Download-on-first-use of the ``hardcharger-server`` binary.

The release tarball for this platform is fetched from the GitHub Releases of
``gizmodata/hardcharger`` through the GitHub API, checked against the SHA-256
digest GitHub records for the asset, unpacked into a per-version cache
directory and checked once more by running ``hardcharger-server --version``::

    ~/.cache/hardcharger/v<version>/<platform>/bin/hardcharger-server

While the repository is internal, the download needs a GitHub token that can
read it: ``HARDCHARGER_INSTALL_TOKEN``, else ``GH_TOKEN``, else the token of a
signed-in ``gh`` CLI (``gh auth token``). ``HARDCHARGER_INSTALL_TOKEN=none``
downloads anonymously. The token is sent only to the GitHub API, never to the
host the asset download redirects to, and is never printed.

Environment variables:

``HARDCHARGER_SERVER_BINARY``
    Use this server binary instead of downloading one.
``HARDCHARGER_VERSION``
    Server version to download (default: :data:`SERVER_VERSION`).
``HARDCHARGER_CACHE_DIR``
    Cache root (default ``$XDG_CACHE_HOME/hardcharger`` or ``~/.cache/hardcharger``).
``HARDCHARGER_RELEASE_API_URL``
    GitHub API base URL (default ``https://api.github.com``), e.g. for GitHub
    Enterprise Server. The token is sent to it.
``HARDCHARGER_RELEASE_REPO``
    Repository the releases come from (default ``gizmodata/hardcharger``).

Standard library only, so the package keeps zero dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

#: The hardcharger-server release this package runs by default.
SERVER_VERSION = "2.1.5"

BINARY_ENV = "HARDCHARGER_SERVER_BINARY"
VERSION_ENV = "HARDCHARGER_VERSION"
CACHE_ENV = "HARDCHARGER_CACHE_DIR"
API_URL_ENV = "HARDCHARGER_RELEASE_API_URL"
REPO_ENV = "HARDCHARGER_RELEASE_REPO"
TOKEN_ENV = "HARDCHARGER_INSTALL_TOKEN"

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_REPO = "gizmodata/hardcharger"

#: Platforms hardcharger-server is released for.
PLATFORMS = ("linux-amd64", "linux-arm64", "macos-arm64")

_USER_AGENT = "hardcharger-py"


class InstallError(RuntimeError):
    """The server binary couldn't be found, downloaded, verified or unpacked."""


def detect_platform(system: str | None = None, machine: str | None = None) -> str:
    """Return this machine's release platform name, e.g. ``"linux-amd64"``."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"linux": "linux", "darwin": "macos"}.get(system, system)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )
    name = f"{os_name}-{arch}"
    if name not in PLATFORMS:
        raise InstallError(
            f"hardcharger-server is not released for {name}; it is released for "
            f"{', '.join(PLATFORMS)}. On other platforms run the Docker image "
            "(ghcr.io/gizmodata/hardcharger) and pass its address to the client, "
            f"or set {BINARY_ENV} to a server binary you built."
        )
    return name


#: A PEP 440 release candidate (``2.0.0rc1``, how this package's own version spells one).
_PEP440_RC = re.compile(r"(\d+\.\d+\.\d+)rc(\d+)")


def normalize_version(version: str) -> str:
    """The server's version as its release tag has it, without the ``v``: ``"v0.2.0"`` or
    ``"0.2.0"`` -> ``"0.2.0"``; a release candidate ``"v2.0.0-rc.1"`` or ``"2.0.0-rc.1"``
    stays ``"2.0.0-rc.1"``, and its PEP 440 spelling ``"2.0.0rc1"`` becomes that too."""
    version = version.strip()
    version = version[1:] if version.startswith("v") else version
    if match := _PEP440_RC.fullmatch(version):
        return f"{match.group(1)}-rc.{match.group(2)}"
    return version


def resolve_version(version: str | None = None) -> str:
    """The server version to use: the argument, else ``HARDCHARGER_VERSION``,
    else :data:`SERVER_VERSION`."""
    return normalize_version(version or os.environ.get(VERSION_ENV) or SERVER_VERSION)


def cache_root() -> Path:
    if override := os.environ.get(CACHE_ENV):
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "hardcharger"


def install_dir(version: str, platform_name: str) -> Path:
    return cache_root() / f"v{normalize_version(version)}" / platform_name


def github_token() -> str | None:
    """The token for the release download, or None for an anonymous one.

    ``HARDCHARGER_INSTALL_TOKEN`` (``"none"``: anonymous), else ``GH_TOKEN``, else
    ``gh auth token`` when the gh CLI is installed and signed in.
    """
    explicit = os.environ.get(TOKEN_ENV)
    if explicit is not None and explicit.strip():
        return None if explicit.strip().lower() == "none" else explicit.strip()
    if gh_token := os.environ.get("GH_TOKEN", "").strip():
        return gh_token
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        result = subprocess.run(
            args=[gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _api_url() -> str:
    return (os.environ.get(API_URL_ENV) or DEFAULT_API_URL).rstrip("/")


def _repo() -> str:
    return os.environ.get(REPO_ENV) or DEFAULT_REPO


def _missing_token_hint(token: str | None) -> str:
    if token is not None:
        return (
            f" The token in use can't read {_repo()}'s releases: use one with read access "
            f"to its contents ({TOKEN_ENV})."
        )
    return (
        f" While the releases are internal, set {TOKEN_ENV} to a GitHub token that can "
        f"read {_repo()} (or sign in with `gh auth login`)."
    )


def _request(url: str, token: str | None, accept: str) -> urllib.request.Request:
    request = urllib.request.Request(url=url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    if token is not None:
        # An unredirected header is not sent again when urllib follows a redirect, so
        # the token never reaches the storage host the asset download redirects to.
        request.add_unredirected_header(key="Authorization", val=f"Bearer {token}")
    return request


def _get_json(url: str, token: str | None, what: str) -> dict:
    request = _request(url=url, token=token, accept="application/vnd.github+json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(fp=response)
    except HTTPError as error:
        hint = _missing_token_hint(token=token) if error.code in (401, 403, 404) else ""
        raise InstallError(f"couldn't find {what} on GitHub (HTTP {error.code}).{hint}") from error
    except URLError as error:
        raise InstallError(f"couldn't reach {url}: {error.reason}") from error


def _download(url: str, token: str | None, destination: Path) -> str:
    """Stream the asset into ``destination``; return its SHA-256 (hex)."""
    request = _request(url=url, token=token, accept="application/octet-stream")
    digest = hashlib.sha256()
    try:
        with (
            urllib.request.urlopen(request, timeout=300) as response,
            destination.open(mode="wb") as out,
        ):
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except HTTPError as error:
        hint = _missing_token_hint(token=token) if error.code in (401, 403, 404) else ""
        raise InstallError(f"downloading {url} failed (HTTP {error.code}).{hint}") from error
    except URLError as error:
        raise InstallError(f"downloading {url} failed: {error.reason}") from error
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    """Unpack a tarball, refusing members that would land outside ``destination``
    (absolute paths, ``..``, links pointing out, devices)."""
    with tarfile.open(name=archive, mode="r:gz") as tar:
        # A release never has these; refuse rather than let a filter rewrite them.
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if member.name.startswith(("/", "\\")) or ".." in parts:
                raise InstallError(
                    f"refusing to unpack {archive.name}: unsafe member {member.name!r}"
                )
        if hasattr(tarfile, "data_filter"):
            try:
                tar.extractall(path=destination, filter="data")
            except tarfile.FilterError as error:
                raise InstallError(f"refusing to unpack {archive.name}: {error}") from error
            return
        # Python versions before the extraction filters: check every member by hand.
        root = destination.resolve()
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if not (member.isfile() or member.isdir() or member.issym()) or (
                target != root and root not in target.parents
            ):
                raise InstallError(
                    f"refusing to unpack {archive.name}: unsafe member {member.name!r}"
                )
            if member.issym():
                link = (target.parent / member.linkname).resolve()
                if root not in link.parents:
                    raise InstallError(
                        f"refusing to unpack {archive.name}: link {member.name!r} points outside"
                    )
        tar.extractall(path=destination)  # noqa: S202 (checked above)


def server_version_of(binary: Path) -> str:
    """Run ``binary --version`` and return the version it reports (``"0.2.0"``)."""
    try:
        result = subprocess.run(
            args=[str(binary), "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError as error:
        raise InstallError(f"{binary} doesn't run: {error}") from error
    words = result.stdout.split()
    if result.returncode != 0 or len(words) < 2 or words[0] != "Hardcharger":
        output = (result.stdout + result.stderr).strip()[-500:]
        raise InstallError(
            f"{binary} --version didn't report a Hardcharger version "
            f"(exit {result.returncode}): {output}"
        )
    return normalize_version(words[1])


def ensure_server(version: str | None = None, *, progress: bool = True) -> Path:
    """Return the path of a ``hardcharger-server`` binary, downloading it first if needed.

    ``HARDCHARGER_SERVER_BINARY`` wins; otherwise the release for ``version`` (default:
    ``HARDCHARGER_VERSION``, then :data:`SERVER_VERSION`) is used from the cache, or
    downloaded into it.
    """
    if override := os.environ.get(BINARY_ENV):
        binary = Path(override).expanduser()
        if not binary.is_file():
            raise InstallError(f"{BINARY_ENV} names {binary}, which doesn't exist")
        return binary

    version = resolve_version(version=version)
    platform_name = detect_platform()
    target = install_dir(version=version, platform_name=platform_name)
    binary = target / "bin" / "hardcharger-server"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary

    token = github_token()
    asset_name = f"hardcharger-{platform_name}.tar.gz"
    release = _get_json(
        url=f"{_api_url()}/repos/{_repo()}/releases/tags/v{version}",
        token=token,
        what=f"Hardcharger release v{version}",
    )
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if asset is None:
        names = sorted(a.get("name", "") for a in release.get("assets", []))
        raise InstallError(f"Hardcharger release v{version} has no {asset_name} (it has {names})")
    expected = (asset.get("digest") or "").removeprefix("sha256:").lower()

    if progress:
        print(
            f"hardcharger: downloading {asset_name} (Hardcharger {version})",
            file=sys.stderr,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as work:
        work_dir = Path(work)
        archive = work_dir / asset_name
        actual = _download(url=asset["url"], token=token, destination=archive)
        if expected and actual != expected:
            raise InstallError(
                f"{asset_name} doesn't match the SHA-256 GitHub recorded for it "
                f"(expected {expected}, got {actual})"
            )
        unpacked = work_dir / "unpacked"
        unpacked.mkdir()
        _safe_extract(archive=archive, destination=unpacked)
        tops = [p for p in unpacked.iterdir() if p.is_dir()]
        root = tops[0] if len(tops) == 1 else unpacked
        new_binary = root / "bin" / "hardcharger-server"
        if not new_binary.is_file():
            raise InstallError(f"{asset_name} has no bin/hardcharger-server")
        new_binary.chmod(mode=new_binary.stat().st_mode | 0o111)
        if sys.platform == "darwin":
            subprocess.run(
                args=["xattr", "-dr", "com.apple.quarantine", str(root)],
                capture_output=True,
                check=False,
            )
        reported = server_version_of(binary=new_binary)
        if reported != version:
            raise InstallError(f"{asset_name} of release v{version} reports version {reported}")
        if target.exists():
            shutil.rmtree(path=target)
        root.rename(target=target)
    if progress:
        print(f"hardcharger: installed Hardcharger {version} in {target}", file=sys.stderr)
    return binary
