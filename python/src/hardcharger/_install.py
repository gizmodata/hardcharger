"""Download-on-first-use of the ``hardcharger-server`` binary.

The server for this platform is downloaded from a public source, checked, unpacked into a
per-version cache directory and checked once more by running ``hardcharger-server
--version``::

    ~/.cache/hardcharger/v<version>/<platform>/bin/hardcharger-server

No account, token or Docker is needed. Two sources are tried, in this order:

1. **GitHub Releases** of ``gizmodata/hardcharger``. The release tarball is fetched from
   its direct download URL
   (``https://github.com/<repo>/releases/download/v<version>/hardcharger-<platform>.tar.gz``),
   which uses neither the GitHub API nor its rate limit, and must match its SHA-256 in
   that release's ``SHA256SUMS``. When a GitHub token is configured, the GitHub API is
   asked first, with the token (GitHub Enterprise Server, a private mirror); if the API
   refuses the token or can't be used, the download is tried again without it. The API is
   also asked without a token when the direct download fails.
2. **The container image** ``ghcr.io/gizmodata/hardcharger:<version>``, on Linux, when
   GitHub can't be reached or refuses. The image is read through the registry's HTTP API
   (an anonymous pull token, the manifest for this architecture, every layer checked
   against its digest); the server's files (``/opt/hardcharger`` in the image, the same
   tree as the release tarball) are taken out of the layers. The image holds Linux
   binaries, so macOS installs come from GitHub only.

A download that doesn't match its checksum or digest, or an archive entry that would land
outside the install directory, stops the install: no other source is tried.

The token: ``HARDCHARGER_INSTALL_TOKEN``, else ``GH_TOKEN``, else the token of a signed-in
``gh`` CLI (``gh auth token``). ``HARDCHARGER_INSTALL_TOKEN=none`` never sends one. The
token is sent only to the GitHub API, never to the host a download redirects to nor to
the container registry, and is never printed.

Environment variables:

``HARDCHARGER_SERVER_BINARY``
    Use this server binary instead of downloading one.
``HARDCHARGER_VERSION``
    Server version to download (default: :data:`SERVER_VERSION`).
``HARDCHARGER_CACHE_DIR``
    Cache root (default ``$XDG_CACHE_HOME/hardcharger`` or ``~/.cache/hardcharger``).
``HARDCHARGER_INSTALL_SOURCE``
    ``auto`` (default: GitHub, then on Linux the container image), ``github`` or ``ghcr``
    (the container image only; Linux).
``HARDCHARGER_INSTALL_REGISTRY_IMAGE``
    The container image, without a tag (default ``ghcr.io/gizmodata/hardcharger``), e.g. a
    mirror in an air-gapped network: ``registry.example.com/mirror/hardcharger``. The
    version is the tag. ``http://`` in front names a registry served without TLS.
``HARDCHARGER_INSTALL_TOKEN``
    The GitHub token for the GitHub API (``none``: never send one).
``HARDCHARGER_RELEASE_API_URL``
    GitHub API base URL (default ``https://api.github.com``), e.g. for GitHub
    Enterprise Server. The token is sent to it.
``HARDCHARGER_RELEASE_DOWNLOAD_URL``
    Base of the direct download URLs (default ``https://github.com``; on GitHub
    Enterprise Server, the server's own address). When only the API URL is set, the
    direct download is not used: the API answers for that server.
``HARDCHARGER_RELEASE_REPO``
    Repository the releases come from (default ``gizmodata/hardcharger``).
``HARDCHARGER_CPU_TARGET``
    Which Linux x86_64 build to install: ``auto`` (default: the optimized build, or the
    baseline build when this CPU lacks an instruction the optimized one needs), ``optimized``
    (built for x86-64-v3: AVX2) or ``baseline`` (any x86-64 CPU, such as a VM with QEMU's
    ``kvm64`` CPU model; slower). The baseline build is released from 2.13.2 on, as
    ``hardcharger-linux-amd64-baseline.tar.gz``, and comes from GitHub Releases only: the
    container image holds the optimized build. Other platforms have one build.

Standard library only, so the package keeps zero dependencies.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import http.client
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
import zlib
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError

#: The hardcharger-server release this package runs by default.
SERVER_VERSION = "2.14.0"

BINARY_ENV = "HARDCHARGER_SERVER_BINARY"
VERSION_ENV = "HARDCHARGER_VERSION"
CACHE_ENV = "HARDCHARGER_CACHE_DIR"
SOURCE_ENV = "HARDCHARGER_INSTALL_SOURCE"
IMAGE_ENV = "HARDCHARGER_INSTALL_REGISTRY_IMAGE"
API_URL_ENV = "HARDCHARGER_RELEASE_API_URL"
DOWNLOAD_URL_ENV = "HARDCHARGER_RELEASE_DOWNLOAD_URL"
REPO_ENV = "HARDCHARGER_RELEASE_REPO"
TOKEN_ENV = "HARDCHARGER_INSTALL_TOKEN"
CPU_TARGET_ENV = "HARDCHARGER_CPU_TARGET"

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_DOWNLOAD_URL = "https://github.com"
DEFAULT_REPO = "gizmodata/hardcharger"
DEFAULT_IMAGE = "ghcr.io/gizmodata/hardcharger"

#: The values of ``HARDCHARGER_INSTALL_SOURCE``.
SOURCES = ("auto", "github", "ghcr")

#: Platforms hardcharger-server is released for.
PLATFORMS = ("linux-amd64", "linux-arm64", "macos-arm64")

#: The values of ``HARDCHARGER_CPU_TARGET``.
CPU_TARGETS = ("auto", "optimized", "baseline")

#: The Linux x86_64 build for any x86-64 CPU (released from 2.13.2): its release asset is
#: ``hardcharger-<this>.tar.gz``, and it is cached under this name.
BASELINE_PLATFORM = "linux-amd64-baseline"

#: The CPU features (``/proc/cpuinfo`` flags) that tell whether this CPU runs the optimized
#: linux-amd64 build (x86-64-v3, which its DuckDB is compiled for, and x86-64-v2 below it).
#: ``pni`` is SSE3. Two v3 features are left out: the kernel lists ``avx`` only when it saves
#: the AVX registers (so ``xsave`` adds nothing), every CPU with AVX2 and BMI2 has LZCNT
#: (``abm``), and Rosetta's x86_64 Linux lists neither although the optimized build runs there.
#: The binaries check every feature themselves (CPUID) when they start.
X86_64_V3_FLAGS = (
    "cx16",
    "lahf_lm",
    "popcnt",
    "pni",
    "sse4_1",
    "sse4_2",
    "ssse3",
    "avx",
    "avx2",
    "bmi1",
    "bmi2",
    "f16c",
    "fma",
    "movbe",
)

#: Where the image keeps the release tarball's tree (docker/Dockerfile:
#: ``COPY dist/hardcharger-linux-<arch>/ /opt/hardcharger/``).
IMAGE_ROOT = ("opt", "hardcharger")

_USER_AGENT = "hardcharger-py"
#: Seconds one connect or one read may take.
_TIMEOUT = 60
#: Seconds `hardcharger-server --version` may take.
_VERSION_TIMEOUT = 60
#: The most a release answer, checksum list or image manifest may weigh.
_MAX_DOCUMENT = 8 << 20
#: ``tarfile``'s extraction filters (Python 3.12, and the 3.10.12 and 3.11.4 security fixes).
_HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


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


def _cpuinfo_text() -> str | None:
    try:
        return Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def missing_cpu_features(cpuinfo: str | None = None) -> list[str] | None:
    """The x86-64-v3 features this CPU lacks (``[]``: none), from ``/proc/cpuinfo``'s
    first ``flags`` line; None when that can't be read."""
    if cpuinfo is None:
        cpuinfo = _cpuinfo_text()
        if cpuinfo is None:
            return None
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "flags":
            flags = set(value.split())
            return [flag for flag in X86_64_V3_FLAGS if flag not in flags]
    return None


def cpu_target() -> str:
    """``HARDCHARGER_CPU_TARGET``: auto (the default), optimized or baseline."""
    value = os.environ.get(CPU_TARGET_ENV, "").strip().lower() or "auto"
    if value not in CPU_TARGETS:
        raise InstallError(
            f"{CPU_TARGET_ENV} must be one of {', '.join(CPU_TARGETS)}, not {value!r}"
        )
    return value


def select_build(
    platform_name: str,
    *,
    target: str | None = None,
    cpuinfo: str | None = None,
    progress: bool = False,
) -> str:
    """The release build to install on ``platform_name``: the platform itself, or on
    linux-amd64 the baseline build when asked for, or (``auto``) when this CPU lacks a
    feature the optimized build needs."""
    target = target or cpu_target()
    if platform_name != "linux-amd64":
        if target == "baseline":
            raise InstallError(
                f"{CPU_TARGET_ENV}=baseline: the baseline build is for linux-amd64 only, and "
                f"this is {platform_name}, which has one build for every CPU"
            )
        return platform_name
    if target == "optimized":
        return platform_name
    if target == "baseline":
        return BASELINE_PLATFORM
    missing = missing_cpu_features(cpuinfo=cpuinfo)
    if not missing:
        return platform_name
    if progress:
        print(
            f"hardcharger: this CPU lacks {', '.join(missing)}, which the optimized linux-amd64 "
            f"build needs (x86-64-v3); installing the baseline build, which runs on any x86-64 "
            f"CPU but slower. (A virtual machine may hide its host's CPU features: see its CPU "
            f"model setting. {CPU_TARGET_ENV}=optimized or =baseline chooses the build.)",
            file=sys.stderr,
        )
    return BASELINE_PLATFORM


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


def _download_url() -> str | None:
    """Base of the direct download URLs, or None when only the API is known to serve the
    repository (``HARDCHARGER_RELEASE_API_URL`` is set and the download URL isn't: the
    same repository name on github.com could be somebody else's)."""
    if explicit := os.environ.get(DOWNLOAD_URL_ENV):
        return explicit.rstrip("/")
    return DEFAULT_DOWNLOAD_URL if _api_url() == DEFAULT_API_URL else None


def install_source() -> str:
    """``HARDCHARGER_INSTALL_SOURCE``: ``"auto"`` (the default), ``"github"`` or ``"ghcr"``."""
    raw = os.environ.get(SOURCE_ENV) or ""
    value = raw.strip().lower() or "auto"
    if value not in SOURCES:
        raise InstallError(f"{SOURCE_ENV}={raw!r} is not a source: use one of {', '.join(SOURCES)}")
    return value


def image_reference() -> tuple[str, str, str]:
    """``HARDCHARGER_INSTALL_REGISTRY_IMAGE`` as (the registry's base URL, the repository
    in it, the image's name for messages)."""
    raw = (os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE).strip()
    scheme, rest = "https", raw
    if match := re.match(pattern=r"(https?)://", string=raw, flags=re.IGNORECASE):
        scheme, rest = match.group(1).lower(), raw[match.end() :]
    host, _, repository = rest.partition("/")
    if not repository or not ("." in host or ":" in host or host == "localhost"):
        # Docker's rule: a name without a registry in front is on Docker Hub.
        host, repository = "docker.io", rest
    component = r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*"
    if not re.fullmatch(
        pattern=r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d+)?", string=host
    ) or not re.fullmatch(pattern=rf"{component}(?:/{component})*", string=repository):
        raise InstallError(
            f"{IMAGE_ENV}={raw!r} is not an image name: give registry/repository in lower "
            f"case, without a tag or digest (the server version is the tag), like "
            f"{DEFAULT_IMAGE}"
        )
    name = f"{host}/{repository}"
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
        repository = repository if "/" in repository else f"library/{repository}"
    return f"{scheme}://{host}", repository, name


# ---- HTTP -----------------------------------------------------------------------------


class _Unavailable(Exception):
    """One way of getting the server didn't work, and another one may. ``status`` is the
    HTTP status when the far side answered, ``challenge`` its ``WWW-Authenticate``."""

    def __init__(self, reason: str, *, status: int | None = None, challenge: str | None = None):
        super().__init__(reason)
        self.status = status
        self.challenge = challenge


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects as urllib does, remembering where the last one led: a download
    that fails after a redirect failed at that host, not at the one in its URL."""

    def __init__(self) -> None:
        self.target: str | None = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.target = newurl
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url: str, token: str | None, accept: str) -> urllib.request.Request:
    request = urllib.request.Request(url=url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    if token is not None:
        # An unredirected header is not sent again when urllib follows a redirect, so
        # the token never reaches the storage host the download redirects to.
        request.add_unredirected_header(key="Authorization", val=f"Bearer {token}")
    return request


def _http_failure(error: HTTPError) -> str:
    """``HTTP 403 Forbidden``, with the rate limit's reset time when that is the reason."""
    text = f"HTTP {error.code} {error.reason or ''}".strip()
    headers = error.headers
    if headers is None or error.code not in (403, 429):
        return text
    reset = (headers.get("x-ratelimit-reset") or "").strip()
    retry_after = (headers.get("retry-after") or "").strip()
    if (headers.get("x-ratelimit-remaining") or "").strip() == "0":
        if reset.isdigit() and len(reset) <= 11:
            when = datetime.datetime.fromtimestamp(int(reset), tz=datetime.timezone.utc)
            return f"{text}: the rate limit is used up, it resets at {when:%Y-%m-%d %H:%M:%S} UTC"
        return f"{text}: the rate limit is used up"
    if retry_after.isdigit():
        return f"{text}: rate limited, retry after {retry_after} s"
    return text


def _open(*, url: str, token: str | None, accept: str):
    """The response to a GET of ``url``. A failure names the URL, the host a redirect led
    to (never the redirect's URL: it carries a signature), and never the token."""
    redirects = _Redirects()
    opener = urllib.request.build_opener(redirects)

    def where() -> str:
        if redirects.target is None:
            return url
        target = urllib.parse.urlsplit(redirects.target)
        host = target.netloc.rpartition("@")[2]
        return f"{url} (redirected to {target.scheme}://{host})"

    try:
        return opener.open(fullurl=_request(url=url, token=token, accept=accept), timeout=_TIMEOUT)
    except HTTPError as error:
        reason = _http_failure(error=error)
        challenge = error.headers.get("WWW-Authenticate") if error.headers is not None else None
        if error.fp is not None:
            error.close()
        raise _Unavailable(
            f"{where()}: {reason}", status=error.code, challenge=challenge
        ) from error
    except ValueError as error:
        # A URL or header urllib refuses. Its message quotes the header, so with a token
        # in play only the kind of error is passed on.
        detail = str(error) if token is None else type(error).__name__
        raise _Unavailable(f"{where()}: the request can't be made ({detail})") from error
    except (OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", None) or error
        raise _Unavailable(f"{where()}: couldn't connect: {reason}") from error


def _copy_body(*, response, url: str, write, limit: int | None) -> None:
    """Hand the response's body to ``write``. A body that breaks off, or is shorter than
    its Content-Length, is a failed download, not a wrong checksum."""
    declared = (response.headers.get("Content-Length") or "").strip()
    received = 0
    while True:
        try:
            chunk = response.read(1 << 20)
        except (OSError, http.client.HTTPException) as error:
            raise _Unavailable(
                f"{url}: the download broke off after {received} bytes: {error}"
            ) from error
        if not chunk:
            break
        received += len(chunk)
        if limit is not None and received > limit:
            raise _Unavailable(f"{url}: the answer is longer than the {limit} bytes expected")
        write(chunk)
    if declared.isdigit() and received != int(declared):
        raise _Unavailable(f"{url}: the download ended after {received} of {declared} bytes")


def _document(*, response, url: str) -> bytes:
    """The body of a small answer (a release, a checksum list, a manifest)."""
    chunks: list[bytes] = []
    with response:
        _copy_body(response=response, url=url, write=chunks.append, limit=_MAX_DOCUMENT)
    return b"".join(chunks)


def _json(*, data: bytes, url: str) -> dict:
    try:
        document = json.loads(data)
    except ValueError as error:
        raise _Unavailable(f"{url}: the answer isn't JSON ({error})") from error
    if not isinstance(document, dict):
        raise _Unavailable(f"{url}: the answer isn't a JSON object")
    return document


def _save(
    *, response, url: str, destination: Path, algorithm: str = "sha256", limit: int | None = None
) -> str:
    """Stream the response's body into ``destination``; return its digest (hex)."""
    digest = hashlib.new(algorithm)
    try:
        with response, destination.open(mode="wb") as out:

            def write(chunk: bytes) -> None:
                digest.update(chunk)
                out.write(chunk)

            _copy_body(response=response, url=url, write=write, limit=limit)
    except OSError as error:
        raise InstallError(f"couldn't write {destination}: {error}") from error
    return digest.hexdigest()


# ---- unpacking ------------------------------------------------------------------------


def _unsafe_name(name: str) -> bool:
    """An archive member that names a place outside the directory it is unpacked into."""
    return name.startswith(("/", "\\")) or ".." in PurePosixPath(name.replace("\\", "/")).parts


def _extract_members(
    *, tar: tarfile.TarFile, members: list[tarfile.TarInfo], destination: Path, label: str
) -> None:
    """Unpack ``members``, refusing any that would land outside ``destination``
    (absolute paths, ``..``, links pointing out, devices)."""
    # A release never has these; refuse rather than let a filter rewrite them.
    for member in members:
        if _unsafe_name(name=member.name):
            raise InstallError(f"refusing to unpack {label}: unsafe member {member.name!r}")
    if _HAS_DATA_FILTER:
        try:
            tar.extractall(path=destination, members=members, filter="data")
        except tarfile.FilterError as error:
            raise InstallError(f"refusing to unpack {label}: {error}") from error
        return
    # Python versions before the extraction filters: check every member by hand.
    root = destination.resolve()
    for member in members:
        target = (root / member.name).resolve()
        if not (member.isfile() or member.isdir() or member.issym()) or (
            target != root and root not in target.parents
        ):
            raise InstallError(f"refusing to unpack {label}: unsafe member {member.name!r}")
        if member.issym():
            link = (target.parent / member.linkname).resolve()
            if root not in link.parents:
                raise InstallError(
                    f"refusing to unpack {label}: link {member.name!r} points outside"
                )
    tar.extractall(path=destination, members=members)  # noqa: S202 (checked above)


def _safe_extract(archive: Path, destination: Path) -> None:
    """Unpack a release tarball, refusing members that would land outside ``destination``."""
    try:
        with tarfile.open(name=archive, mode="r:gz") as tar:
            _extract_members(
                tar=tar, members=tar.getmembers(), destination=destination, label=archive.name
            )
    except (tarfile.TarError, EOFError, zlib.error, OSError) as error:
        raise InstallError(f"couldn't unpack {archive.name}: {error}") from error


class _ImageTree:
    """The files below ``/opt/hardcharger`` of an image, assembled from its layers, the
    top layer first: a path an upper layer has decided (written, or removed with a
    whiteout) is skipped in the layers below it, which is what stacking them gives."""

    def __init__(self, *, destination: Path):
        self.destination = destination
        #: Every path (relative to the root) an upper layer wrote: is it a directory?
        self.kinds: dict[str, bool] = {}
        #: Paths (from the image's /) upper layers removed: ``.wh.<name>``.
        self.removed: set[tuple[str, ...]] = set()
        #: Directories whose lower-layer contents upper layers hid: ``.wh..wh..opq``.
        self.opaque: set[tuple[str, ...]] = set()

    def _hidden(self, parts: tuple[str, ...]) -> bool:
        """Whether upper layers removed this path, or wrote something that isn't a
        directory where one of its parents would be."""
        for length in range(len(parts) + 1):
            parent = parts[:length]
            if parent in self.removed or (parent in self.opaque and length < len(parts)):
                return True
        relative = parts[len(IMAGE_ROOT) :]
        return any(
            self.kinds.get("/".join(relative[:length])) is False for length in range(len(relative))
        )

    def complete(self) -> bool:
        """Whether nothing in a lower layer can show through any more: the root or a
        directory above it was removed or emptied, or the root is no directory."""
        lengths = range(len(IMAGE_ROOT) + 1)
        return (
            any(IMAGE_ROOT[:length] in self.removed for length in lengths)
            or any(IMAGE_ROOT[:length] in self.opaque for length in lengths)
            or self.kinds.get("") is False
        )

    def apply(self, *, archive: Path, label: str) -> None:
        """Take from one layer what the layers above it left undecided."""
        removed: set[tuple[str, ...]] = set()
        opaque: set[tuple[str, ...]] = set()
        chosen: dict[str, tarfile.TarInfo] = {}

        def parts_of(name: str) -> tuple[str, ...]:
            if _unsafe_name(name=name):
                raise InstallError(f"refusing to unpack {label}: unsafe member {name!r}")
            return tuple(part for part in PurePosixPath(name).parts if part != ".")

        try:
            with tarfile.open(name=archive, mode="r:*") as tar:
                members = tar.getmembers()
                files = {parts_of(name=m.name): m for m in members if m.isreg()}
                for member in members:
                    parts = parts_of(name=member.name)
                    if parts and parts[-1] == ".wh..wh..opq":
                        opaque.add(parts[:-1])
                        continue
                    if parts and parts[-1].startswith(".wh."):
                        removed.add((*parts[:-1], parts[-1][len(".wh.") :]))
                        continue
                    if parts[: len(IMAGE_ROOT)] != IMAGE_ROOT:
                        continue
                    name = "/".join(parts[len(IMAGE_ROOT) :])
                    if name in self.kinds or self._hidden(parts=parts):
                        continue
                    if member.islnk():
                        # A hard link is unpacked as a copy of the file it names, which
                        # lives in this layer whatever the layers above replaced.
                        target = files.get(parts_of(name=member.linkname))
                        if target is None:
                            raise InstallError(
                                f"refusing to unpack {label}: hard link {member.name!r} "
                                f"names no file in its layer"
                            )
                        member = target
                    entry = copy.copy(member)
                    entry.name = name
                    chosen[name] = entry
                # The root itself is made by the caller; only its kind matters here.
                root = chosen.pop("", None)
                if root is None or root.isdir():
                    _extract_members(
                        tar=tar,
                        members=list(chosen.values()),
                        destination=self.destination,
                        label=label,
                    )
        except (tarfile.TarError, EOFError, zlib.error, OSError) as error:
            raise _Unavailable(f"{label} can't be unpacked: {error}") from error
        if root is not None:
            self.kinds[""] = root.isdir()
        if root is None or root.isdir():
            for name, entry in chosen.items():
                self.kinds[name] = entry.isdir()
                for parent in PurePosixPath(name).parents:
                    self.kinds.setdefault("" if str(parent) == "." else str(parent), True)
        self.removed |= removed
        self.opaque |= opaque


# ---- GitHub ---------------------------------------------------------------------------

_OCTETS = "application/octet-stream"


def _checksum_for(*, data: bytes, asset_name: str, version: str, url: str) -> str:
    """The asset's SHA-256 from a ``SHA256SUMS`` (``sha256sum`` format)."""
    sums: dict[str, str] = {}
    for line in data.decode(encoding="utf-8", errors="replace").splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and re.fullmatch(pattern=r"[0-9a-fA-F]{64}", string=fields[0]):
            sums[fields[1].strip().lstrip("*")] = fields[0].lower()
    if not sums:
        # Not a checksum list at all: a proxy's page, an error document.
        raise _Unavailable(f"{url}: the answer isn't a SHA256SUMS file")
    if asset_name not in sums:
        raise InstallError(
            f"the SHA256SUMS of release v{version} has no line for {asset_name} (it lists "
            f"{', '.join(sorted(sums))}): not installing a download that can't be checked"
        )
    return sums[asset_name]


def _download_direct(*, base: str, version: str, asset_name: str, archive: Path) -> None:
    """The release asset from its download URL, without the API or a token, checked
    against the release's ``SHA256SUMS``."""
    release = f"{base}/{_repo()}/releases/download/v{version}"
    sums_url = f"{release}/SHA256SUMS"
    sums = _document(response=_open(url=sums_url, token=None, accept=_OCTETS), url=sums_url)
    expected = _checksum_for(data=sums, asset_name=asset_name, version=version, url=sums_url)
    asset_url = f"{release}/{asset_name}"
    actual = _save(
        response=_open(url=asset_url, token=None, accept=_OCTETS),
        url=asset_url,
        destination=archive,
    )
    if actual != expected:
        raise InstallError(
            f"{asset_name} doesn't match its SHA-256 in the SHA256SUMS of release "
            f"v{version} (expected {expected}, got {actual})"
        )


def _download_via_api(*, token: str | None, version: str, asset_name: str, archive: Path) -> None:
    """The release asset through the GitHub API, checked against the SHA-256 the API
    records for it, or the release's ``SHA256SUMS`` where the API records none."""
    release_url = f"{_api_url()}/repos/{_repo()}/releases/tags/v{version}"
    release = _json(
        data=_document(
            response=_open(url=release_url, token=token, accept="application/vnd.github+json"),
            url=release_url,
        ),
        url=release_url,
    )
    assets = release.get("assets")
    by_name = {
        str(a.get("name")): a
        for a in (assets if isinstance(assets, list) else [])
        if isinstance(a, dict) and isinstance(a.get("url"), str)
    }
    asset = by_name.get(asset_name)
    if asset is None:
        raise _Unavailable(
            f"{release_url}: release v{version} has no {asset_name} (it has "
            f"{', '.join(sorted(by_name)) or 'no assets'})"
        )
    digest = str(asset.get("digest") or "").lower()
    if re.fullmatch(pattern=r"sha256:[0-9a-f]{64}", string=digest):
        expected, recorded = digest.removeprefix("sha256:"), "the SHA-256 GitHub recorded for it"
    elif "SHA256SUMS" in by_name:
        sums_url = by_name["SHA256SUMS"]["url"]
        sums = _document(response=_open(url=sums_url, token=token, accept=_OCTETS), url=sums_url)
        expected = _checksum_for(data=sums, asset_name=asset_name, version=version, url=sums_url)
        recorded = f"its SHA-256 in the SHA256SUMS of release v{version}"
    else:
        raise _Unavailable(
            f"{release_url}: release v{version} records no SHA-256 for {asset_name} (no "
            f"digest in the answer, no SHA256SUMS): not installing a download that can't "
            f"be checked"
        )
    actual = _save(
        response=_open(url=asset["url"], token=token, accept=_OCTETS),
        url=asset["url"],
        destination=archive,
    )
    if actual != expected:
        raise InstallError(
            f"{asset_name} doesn't match {recorded} (expected {expected}, got {actual})"
        )


def _from_github(
    *, version: str, platform_name: str, work_dir: Path, failures: list[str], progress: bool
) -> Path | None:
    """The unpacked release from GitHub, or None with the reasons added to ``failures``."""
    asset_name = f"hardcharger-{platform_name}.tar.gz"
    archive = work_dir / asset_name
    token = github_token()
    base = _download_url()
    attempts: list[tuple[str, str | None, bool]] = []  # (what, token, through the API)
    if token is not None:
        attempts.append(("GitHub API with the configured token", token, True))
    if base is not None:
        attempts.append(("GitHub release download", None, False))
    attempts.append(("GitHub API without a token", None, True))
    if progress:
        print(
            f"hardcharger: downloading {asset_name} (Hardcharger {version})",
            file=sys.stderr,
        )
    api_reached = True
    for what, attempt_token, through_api in attempts:
        if through_api and not api_reached:
            continue  # the API host didn't answer a moment ago
        try:
            if through_api:
                _download_via_api(
                    token=attempt_token, version=version, asset_name=asset_name, archive=archive
                )
            else:
                _download_direct(
                    base=str(base), version=version, asset_name=asset_name, archive=archive
                )
        except _Unavailable as error:
            reason = str(error)
            if through_api:
                api_reached = error.status is not None
            private = _api_url() != DEFAULT_API_URL or _repo() != DEFAULT_REPO
            if through_api and attempt_token is None and private and error.status in (401, 404):
                reason += (
                    f" (if {_repo()} is private there, set {TOKEN_ENV} to a token that can read it)"
                )
            failures.append(f"{what}: {reason}")
            if progress:
                retry = "; retrying without the token" if attempt_token is not None else ""
                print(f"hardcharger: {what} didn't work: {reason}{retry}", file=sys.stderr)
            continue
        break
    else:
        return None
    unpacked = work_dir / "unpacked"
    unpacked.mkdir()
    _safe_extract(archive=archive, destination=unpacked)
    tops = [p for p in unpacked.iterdir() if p.is_dir()]
    root = tops[0] if len(tops) == 1 else unpacked
    if not (root / "bin" / "hardcharger-server").is_file():
        raise InstallError(f"{asset_name} has no bin/hardcharger-server")
    return root


# ---- the container image --------------------------------------------------------------

_MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _digest(value: object, *, url: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        pattern=r"sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}", string=value
    ):
        raise _Unavailable(f"{url}: it names a digest this launcher can't check ({value!r})")
    return value


class _Registry:
    """A container registry's HTTP API (OCI distribution), pulling anonymously."""

    def __init__(self, *, base_url: str, repository: str):
        self.base_url = base_url
        self.repository = repository
        self._pull_token: str | None = None

    def url(self, kind: str, reference: str) -> str:
        return f"{self.base_url}/v2/{self.repository}/{kind}/{reference}"

    def open(self, *, url: str, accept: str):
        """GET with the pull token; a 401 gets a (new) token from the realm it names,
        once. The token goes to the registry only, never to where a blob redirects."""
        try:
            return _open(url=url, token=self._pull_token, accept=accept)
        except _Unavailable as error:
            if error.status != 401:
                raise
            self._pull_token = self._anonymous_token(url=url, challenge=error.challenge or "")
        return _open(url=url, token=self._pull_token, accept=accept)

    def _anonymous_token(self, *, url: str, challenge: str) -> str:
        scheme, _, parameters = challenge.strip().partition(" ")
        fields = dict(re.findall(pattern=r'(\w+)="([^"]*)"', string=parameters))
        realm = fields.get("realm", "")
        if scheme.lower() != "bearer" or urllib.parse.urlsplit(realm).scheme not in (
            "http",
            "https",
        ):
            raise _Unavailable(
                f"{url}: HTTP 401, and the registry names no place to get an anonymous "
                f"pull token (WWW-Authenticate: {scheme or 'none'}): only images that can "
                f"be pulled without signing in are supported",
                status=401,
            )
        query = {"scope": fields.get("scope") or f"repository:{self.repository}:pull"}
        if fields.get("service"):
            query["service"] = fields["service"]
        token_url = f"{realm}{'&' if '?' in realm else '?'}{urllib.parse.urlencode(query=query)}"
        answer = _json(
            data=_document(
                response=_open(url=token_url, token=None, accept="application/json"),
                url=token_url,
            ),
            url=token_url,
        )
        token = answer.get("token") or answer.get("access_token")
        if not isinstance(token, str) or not token:
            raise _Unavailable(f"{token_url}: the answer holds no pull token")
        return token

    def document(self, *, url: str, accept: str, digest: str | None = None) -> dict:
        data = _document(response=self.open(url=url, accept=accept), url=url)
        if digest is not None:
            algorithm, _, expected = digest.partition(":")
            actual = hashlib.new(algorithm, data).hexdigest()
            if actual != expected:
                raise InstallError(f"{url} doesn't match its digest (got {algorithm}:{actual})")
        return _json(data=data, url=url)

    def layers(self, *, tag: str, architecture: str) -> list[dict]:
        """The layers of the ``linux/<architecture>`` image behind ``tag``, base first."""
        url = self.url(kind="manifests", reference=tag)
        manifest = self.document(url=url, accept=_MANIFEST_TYPES)
        variants = manifest.get("manifests")
        if isinstance(variants, list):
            platforms = [
                (v.get("platform") if isinstance(v.get("platform"), dict) else {}, v)
                for v in variants
                if isinstance(v, dict)
            ]
            wanted = [
                v
                for p, v in platforms
                if p.get("os") == "linux" and p.get("architecture") == architecture
            ]
            if not wanted:
                has = sorted({f"{p.get('os')}/{p.get('architecture')}" for p, _ in platforms})
                raise _Unavailable(
                    f"{url}: the image has no linux/{architecture} variant (it has "
                    f"{', '.join(has) or 'none'})"
                )
            digest = _digest(value=wanted[0].get("digest"), url=url)
            url = self.url(kind="manifests", reference=digest)
            manifest = self.document(url=url, accept=_MANIFEST_TYPES, digest=digest)
        layers = manifest.get("layers")
        if not isinstance(layers, list) or not all(isinstance(layer, dict) for layer in layers):
            raise _Unavailable(
                f"{url}: not an image manifest this launcher reads ({manifest.get('mediaType')})"
            )
        if not isinstance(variants, list):
            # One image, no index: its configuration says what it was built for.
            config = manifest.get("config")
            digest = _digest(
                value=config.get("digest") if isinstance(config, dict) else None, url=url
            )
            built = self.document(
                url=self.url(kind="blobs", reference=digest), accept=_OCTETS, digest=digest
            )
            if (built.get("os"), built.get("architecture")) != ("linux", architecture):
                raise _Unavailable(
                    f"{url}: the image is for {built.get('os')}/{built.get('architecture')}, "
                    f"not linux/{architecture}"
                )
        return layers


def _from_image(
    *, version: str, platform_name: str, work_dir: Path, failures: list[str], progress: bool
) -> Path | None:
    """The server's tree out of the container image's layers, or None with the reason
    added to ``failures``."""
    base_url, repository, name = image_reference()
    image = f"{name}:{version}"
    registry = _Registry(base_url=base_url, repository=repository)
    root = work_dir / "image"
    root.mkdir()
    blob = work_dir / "layer"
    try:
        layers = registry.layers(tag=version, architecture=platform_name.partition("-")[2])
        if progress:
            print(
                f"hardcharger: downloading Hardcharger {version} from the container image "
                f"{image} ({len(layers)} layers)",
                file=sys.stderr,
            )
        tree = _ImageTree(destination=root)
        for layer in reversed(layers):
            url = registry.url(kind="blobs", reference=str(layer.get("digest")))
            digest = _digest(value=layer.get("digest"), url=url)
            algorithm, _, expected = digest.partition(":")
            size = layer.get("size")
            actual = _save(
                response=registry.open(url=url, accept=_OCTETS),
                url=url,
                destination=blob,
                algorithm=algorithm,
                limit=size if isinstance(size, int) and not isinstance(size, bool) else None,
            )
            if actual != expected:
                raise InstallError(
                    f"layer {digest} of {image} doesn't match its digest (got {algorithm}:{actual})"
                )
            tree.apply(archive=blob, label=f"layer {digest} of {image}")
            if tree.complete():
                break
        if not (root / "bin" / "hardcharger-server").is_file():
            raise _Unavailable(
                f"{image} has no /{'/'.join(IMAGE_ROOT)}/bin/hardcharger-server in its layers"
            )
    except _Unavailable as error:
        failures.append(f"Container image {image}: {error}")
        return None
    return root


# ---- the install ----------------------------------------------------------------------


def server_version_of(binary: Path) -> str:
    """Run ``binary --version`` and return the version it reports (``"0.2.0"``)."""
    try:
        result = subprocess.run(
            args=[str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"{binary} doesn't run: {error}") from error
    words = result.stdout.split()
    if result.returncode != 0 or len(words) < 2 or words[0] != "Hardcharger":
        output = (result.stdout + result.stderr).strip()[-500:]
        hint = ""
        if "baseline build" in output or result.returncode == 132:
            # The optimized linux-amd64 build on a CPU it can't run (issue #62), where
            # /proc/cpuinfo didn't tell (an emulator).
            hint = f" ({CPU_TARGET_ENV}=baseline installs the build for any x86-64 CPU)"
        raise InstallError(
            f"{binary} --version didn't report a Hardcharger version "
            f"(exit {result.returncode}): {output}{hint}"
        )
    return normalize_version(words[1])


def ensure_server(version: str | None = None, *, progress: bool = True) -> Path:
    """Return the path of a ``hardcharger-server`` binary, downloading it first if needed.

    ``HARDCHARGER_SERVER_BINARY`` wins; otherwise the release for ``version`` (default:
    ``HARDCHARGER_VERSION``, then :data:`SERVER_VERSION`) is used from the cache, or
    downloaded into it: from GitHub Releases, else (on Linux) out of the container image.
    ``HARDCHARGER_INSTALL_SOURCE`` picks one of the two.
    """
    if override := os.environ.get(BINARY_ENV):
        binary = Path(override).expanduser()
        if not binary.is_file():
            raise InstallError(f"{BINARY_ENV} names {binary}, which doesn't exist")
        return binary

    source = install_source()
    version = resolve_version(version=version)
    platform_name = select_build(platform_name=detect_platform(), progress=progress)
    target = install_dir(version=version, platform_name=platform_name)
    binary = target / "bin" / "hardcharger-server"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary

    on_linux = platform_name.startswith("linux-")
    baseline = platform_name == BASELINE_PLATFORM
    if source == "ghcr" and not on_linux:
        raise InstallError(
            f"{SOURCE_ENV}=ghcr works on Linux only: the container image holds Linux "
            f"binaries, and this is {platform_name}. Use {SOURCE_ENV}=github, or leave it unset."
        )
    if source == "ghcr" and baseline:
        raise InstallError(
            f"{SOURCE_ENV}=ghcr: the container image holds the optimized build, and this "
            f"install needs the baseline build ({CPU_TARGET_ENV}), which comes from GitHub "
            f"Releases only. Use {SOURCE_ENV}=github, or leave it unset."
        )
    use_github = source != "ghcr"
    use_image = not baseline and (source == "ghcr" or (source == "auto" and on_linux))
    image = f"{image_reference()[2]}:{version}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        work = tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent)
    except OSError as error:
        raise InstallError(
            f"couldn't create the cache directory {target.parent}: {error} "
            f"(set {CACHE_ENV} to a directory that can be written)"
        ) from error
    with work:
        work_dir = Path(work.name)
        failures: list[str] = []
        root, origin = None, ""
        if use_github:
            origin = f"hardcharger-{platform_name}.tar.gz of release v{version}"
            root = _from_github(
                version=version,
                platform_name=platform_name,
                work_dir=work_dir,
                failures=failures,
                progress=progress,
            )
        if root is None and use_image:
            if use_github and progress:
                print(f"hardcharger: trying the container image {image}", file=sys.stderr)
            origin = image
            root = _from_image(
                version=version,
                platform_name=platform_name,
                work_dir=work_dir,
                failures=failures,
                progress=progress,
            )
        if root is None:
            if source == "auto" and not on_linux:
                failures.append(
                    f"Container image {image}: not tried, it holds Linux binaries and "
                    f"this is {platform_name}"
                )
            if baseline:
                failures.append(
                    f"Container image {image}: not tried, it holds the optimized build only "
                    f"(the baseline build is released from 2.13.2 on; {CPU_TARGET_ENV}=optimized "
                    f"installs the optimized one)"
                )
            raise InstallError(
                f"couldn't download Hardcharger {version} for {platform_name}. Tried:\n"
                + "".join(f"  - {failure}\n" for failure in failures)
                + f"{SOURCE_ENV} ({', '.join(SOURCES)}; now {source}) picks the source, "
                f"{IMAGE_ENV} another registry's copy of the image, and {BINARY_ENV} a "
                f"server binary you already have."
            )
        new_binary = root / "bin" / "hardcharger-server"
        new_binary.chmod(mode=new_binary.stat().st_mode | 0o111)
        if sys.platform == "darwin":
            subprocess.run(
                args=["xattr", "-dr", "com.apple.quarantine", str(root)],
                capture_output=True,
                check=False,
            )
        reported = server_version_of(binary=new_binary)
        if reported != version:
            raise InstallError(f"{origin} reports version {reported}")
        # Another process may have installed the same release meanwhile (parallel test
        # workers on a cold cache): its install is as good, and may be running already.
        if not (binary.is_file() and os.access(binary, os.X_OK)):
            try:
                if target.exists():
                    shutil.rmtree(path=target)
                root.rename(target=target)
            except OSError as error:
                if not (binary.is_file() and os.access(binary, os.X_OK)):
                    raise InstallError(
                        f"couldn't move the server into {target}: {error}"
                    ) from error
    if progress:
        print(f"hardcharger: installed Hardcharger {version} in {target}", file=sys.stderr)
    return binary
