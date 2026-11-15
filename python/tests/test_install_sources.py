"""Where the server comes from: the release's direct download URL (no token, no API), the
GitHub API with and without a token, the container image on Linux, and what happens when
one of them fails. Against the fake GitHub and the fake registry: no test here uses the
network, and none skips."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.parse
from pathlib import Path

import pytest
from conftest import (
    IMAGE_REPOSITORY,
    NOTICES,
    PULL_TOKEN,
    TOKEN,
    FakeRegistry,
    fake_server_script,
    image_layers,
    make_layer,
    make_tarball,
)

from hardcharger import SERVER_VERSION, _install
from hardcharger._install import InstallError, ensure_server

RELEASE = f"/gizmodata/hardcharger/releases/download/v{SERVER_VERSION}"
API_RELEASE = f"/repos/gizmodata/hardcharger/releases/tags/v{SERVER_VERSION}"
#: 1794758400 seconds after the epoch, as the launcher prints a rate limit's reset time.
RESET_EPOCH, RESET_TEXT = "1794758400", "2026-11-15 16:00:00 UTC"


# The ``extraction`` fixture runs the launcher's own checks on Pythons that do have the
# extraction filters; 3.12 and 3.13 then warn that extractall() got no filter.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Python 3.14 will, by default, filter extracted tar archives:DeprecationWarning"
)


def installed(platform_name: str, tmp_path: Path, version: str = SERVER_VERSION) -> Path:
    return tmp_path / "cache" / f"v{version}" / platform_name


def runs(binary: Path, version: str = SERVER_VERSION) -> bool:
    result = subprocess.run(args=[binary, "--version"], capture_output=True, text=True)
    return result.stdout == f"Hardcharger {version}\n"


@pytest.fixture(params=["extraction filter", "checked by hand"])
def extraction(request, monkeypatch):
    """Both ways of unpacking safely: tarfile's ``data`` filter, and the launcher's own
    checks on a Python without the filters."""
    if request.param == "checked by hand":
        monkeypatch.setattr(_install, "_HAS_DATA_FILTER", False)
    return request.param


# ---- the direct download: no token, no API ---------------------------------------------


def test_without_a_token_the_release_downloads_from_its_url(
    fake_github, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    binary = ensure_server()
    platform_name = _install.detect_platform()
    assert binary == installed(platform_name=platform_name, tmp_path=tmp_path) / "bin" / (
        "hardcharger-server"
    )
    assert runs(binary=binary)
    assert (binary.parent.parent / "THIRD_PARTY_NOTICES.md").read_bytes() == NOTICES
    # The checksums, then the tarball, from the download URLs; the API is never asked.
    assert [r.path for r in fake_github.web_requests] == [
        f"{RELEASE}/SHA256SUMS",
        f"{RELEASE}/hardcharger-{platform_name}.tar.gz",
    ]
    assert fake_github.api_requests == []
    assert [r.authorization for r in fake_github.storage_requests] == [None, None]
    assert "didn't work" not in capsys.readouterr().err


def test_a_checksum_mismatch_is_refused_and_nothing_else_is_tried(
    linux, fake_github, fake_registry, monkeypatch, tmp_path
):
    # Linux, with a registry that could serve the image: a wrong checksum still stops.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.require_token = False
    actual = hashlib.sha256(fake_github.tarball).hexdigest()
    fake_github.sums = f"{'0' * 64}  hardcharger-linux-arm64.tar.gz\n"
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        "hardcharger-linux-arm64.tar.gz doesn't match its SHA-256 in the SHA256SUMS of "
        f"release v{SERVER_VERSION} (expected {'0' * 64}, got {actual})"
    )
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()
    assert fake_github.api_requests == []
    assert fake_registry.requests == []


def test_an_asset_missing_from_the_checksums_is_refused(
    linux, fake_github, fake_registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.require_token = False
    fake_github.sums = (
        f"{'1' * 64}  hardcharger-linux-amd64.tar.gz\n{'2' * 64} *hardcharger-macos-arm64.tar.gz\n"
    )
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"the SHA256SUMS of release v{SERVER_VERSION} has no line for "
        "hardcharger-linux-arm64.tar.gz (it lists hardcharger-linux-amd64.tar.gz, "
        "hardcharger-macos-arm64.tar.gz): not installing a download that can't be checked"
    )
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()
    # Only the checksums were fetched: not the tarball, not the API, not the image.
    assert [r.path for r in fake_github.web_requests] == [f"{RELEASE}/SHA256SUMS"]
    assert fake_github.api_requests == []
    assert fake_registry.requests == []


def test_checksums_in_binary_mode_spelling_are_read(fake_github, monkeypatch):
    # `sha256sum -b` writes "<digest> *<name>".
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    digest = hashlib.sha256(fake_github.tarball).hexdigest()
    fake_github.sums = f"{digest.upper()} *hardcharger-{_install.detect_platform()}.tar.gz\n"
    assert runs(binary=ensure_server())


def test_an_answer_that_is_no_checksum_list_falls_back_to_the_api(fake_github, monkeypatch, capsys):
    # A proxy's page in place of SHA256SUMS says nothing about the release: the API is
    # asked next, and the digest it records checks the download.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.require_token = False
    fake_github.sums = "<html><body>Blocked by policy</body></html>\n"
    assert runs(binary=ensure_server())
    assert [r.path for r in fake_github.api_requests] == [API_RELEASE, "/assets/1"]
    assert [r.authorization for r in fake_github.api_requests] == [None, None]
    assert (
        f"hardcharger: GitHub release download didn't work: {fake_github.web.url}{RELEASE}/"
        f"SHA256SUMS: the answer isn't a SHA256SUMS file\n"
    ) in capsys.readouterr().err


def test_no_checksum_list_and_no_api_names_both(macos, fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.sums = "<html><body>Blocked by policy</body></html>\n"
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert (
        f"  - GitHub release download: {fake_github.web.url}{RELEASE}/SHA256SUMS: the answer "
        f"isn't a SHA256SUMS file\n"
    ) in message
    assert (
        f"  - GitHub API without a token: {fake_github.api.url}{API_RELEASE}: HTTP 404 Not Found"
    ) in message
    assert not installed(platform_name="macos-arm64", tmp_path=tmp_path).exists()


def test_a_download_that_breaks_off_is_not_a_wrong_checksum(
    linux, fake_github, fake_registry, monkeypatch, capsys
):
    # Half a tarball has another SHA-256, but that is the network's doing: the next
    # source is tried.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.truncate = True
    assert runs(binary=ensure_server())
    err = capsys.readouterr().err
    asset = f"{fake_github.web.url}{RELEASE}/hardcharger-linux-arm64.tar.gz"
    size = len(fake_github.tarball)
    broke_off = f"{asset}: the download broke off after {size // 2} bytes"
    ended = f"{asset}: the download ended after {size // 2} of {size} bytes"
    assert broke_off in err or ended in err
    assert f"hardcharger: trying the container image {fake_registry.name}:{SERVER_VERSION}" in err
    assert fake_registry.storage_requests != []


def test_a_failure_after_a_redirect_names_the_host_it_led_to(macos, fake_github, monkeypatch):
    # github.com answered and the storage host refused: the report says so, without the
    # signed URL the redirect carried.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.require_token = False
    fake_github.storage_error = 403
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert (
        f"  - GitHub release download: {fake_github.web.url}{RELEASE}/SHA256SUMS (redirected "
        f"to {fake_github.storage.url}): HTTP 403 Forbidden\n"
    ) in message
    assert (
        f"  - GitHub API without a token: {fake_github.api.url}/assets/1 (redirected to "
        f"{fake_github.storage.url}): HTTP 403 Forbidden\n"
    ) in message
    assert "sig=abc" not in message


# ---- a token that is refused, a rate limit ---------------------------------------------


@pytest.mark.parametrize(
    argnames=("api_error", "status"),
    argvalues=[
        (None, "HTTP 404 Not Found"),  # the fake's answer to a token it doesn't know
        ((401, {}), "HTTP 401 Unauthorized"),
        ((403, {}), "HTTP 403 Forbidden"),
    ],
)
def test_a_refused_token_is_retried_without_it(fake_github, monkeypatch, capsys, api_error, status):
    monkeypatch.setenv("GH_TOKEN", "expired-token-789")
    fake_github.api_error = api_error
    assert runs(binary=ensure_server())
    # The token went to the API once and was refused; the download itself carried none.
    assert [(r.path, r.authorization) for r in fake_github.api_requests] == [
        (API_RELEASE, "Bearer expired-token-789")
    ]
    assert [r.authorization for r in fake_github.web_requests] == [None, None]
    assert [r.authorization for r in fake_github.storage_requests] == [None, None]
    err = capsys.readouterr().err
    assert (
        f"hardcharger: GitHub API with the configured token didn't work: {fake_github.api.url}"
        f"{API_RELEASE}: {status}; retrying without the token\n"
    ) in err
    assert "expired-token-789" not in err


def test_a_good_token_is_not_retried(fake_github, monkeypatch, capsys):
    # The twin of the refused token: the API serves it, and nothing is said or retried.
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert runs(binary=ensure_server())
    assert fake_github.web_requests == []
    assert "didn't work" not in capsys.readouterr().err


def test_an_api_rate_limit_falls_back_to_the_direct_download(fake_github, monkeypatch, capsys):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.api_error = (
        403,
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": RESET_EPOCH},
    )
    assert runs(binary=ensure_server())
    assert len(fake_github.api_requests) == 1
    assert (
        f"{API_RELEASE}: HTTP 403 Forbidden: the rate limit is used up, it resets at "
        f"{RESET_TEXT}; retrying without the token\n"
    ) in capsys.readouterr().err


@pytest.mark.parametrize(
    argnames=("status", "headers", "expected"),
    argvalues=[
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": RESET_EPOCH},
            f"HTTP 403 Forbidden: the rate limit is used up, it resets at {RESET_TEXT}",
        ),
        (
            429,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"},
            "HTTP 429 Too Many Requests: the rate limit is used up",
        ),
        (
            429,
            {"retry-after": "37"},
            "HTTP 429 Too Many Requests: rate limited, retry after 37 s",
        ),
        # A 403 with requests left is not a rate limit, and isn't called one.
        (
            403,
            {"x-ratelimit-remaining": "41", "x-ratelimit-reset": RESET_EPOCH},
            "HTTP 403 Forbidden",
        ),
    ],
)
def test_a_rate_limit_everywhere_is_reported_with_its_reset_time(
    macos, fake_github, monkeypatch, status, headers, expected
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.api_error = (status, headers)
    fake_github.web_error = (status, headers)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert (
        f"  - GitHub release download: {fake_github.web.url}{RELEASE}/SHA256SUMS: {expected}\n"
    ) in message
    assert (
        f"  - GitHub API without a token: {fake_github.api.url}{API_RELEASE}: {expected}\n"
    ) in message
    assert ("rate limit" in message) == ("rate limit" in expected)


def test_a_token_urllib_refuses_is_never_printed(fake_github, monkeypatch, capsys):
    # urllib's own error for a header with a line break quotes the header: the launcher
    # passes on the kind of error only, and goes on without the token.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "secret-part-one\nsecret-part-two")
    assert runs(binary=ensure_server())
    err = capsys.readouterr().err
    assert (
        f"hardcharger: GitHub API with the configured token didn't work: {fake_github.api.url}"
        f"{API_RELEASE}: the request can't be made (ValueError); retrying without the token\n"
    ) in err
    assert "secret-part" not in err
    assert fake_github.api_requests == []


# ---- the API: its own URL, a release without recorded digests ---------------------------


def test_only_an_api_url_means_no_direct_download(fake_github, monkeypatch):
    # GitHub Enterprise Server by its API URL alone: github.com is not asked for a
    # repository of that name, the API is, without a token when there is none.
    monkeypatch.delenv("HARDCHARGER_RELEASE_DOWNLOAD_URL")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.require_token = False
    assert _install._download_url() is None
    assert runs(binary=ensure_server())
    assert [r.path for r in fake_github.api_requests] == [API_RELEASE, "/assets/1"]
    assert fake_github.web_requests == []


def test_a_private_repository_without_a_token_says_which_variable(macos, fake_github, monkeypatch):
    monkeypatch.delenv("HARDCHARGER_RELEASE_DOWNLOAD_URL")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert (
        f"  - GitHub API without a token: {fake_github.api.url}{API_RELEASE}: HTTP 404 Not "
        "Found (if gizmodata/hardcharger is private there, set HARDCHARGER_INSTALL_TOKEN to "
        "a token that can read it)\n"
    ) in message
    assert "GitHub release download" not in message
    assert fake_github.web_requests == []


def test_the_default_addresses(monkeypatch):
    monkeypatch.delenv("HARDCHARGER_RELEASE_API_URL")
    monkeypatch.delenv("HARDCHARGER_RELEASE_DOWNLOAD_URL")
    monkeypatch.delenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE")
    assert _install._api_url() == "https://api.github.com"
    assert _install._download_url() == "https://github.com"
    assert _install._repo() == "gizmodata/hardcharger"
    assert _install.image_reference() == (
        "https://ghcr.io",
        "gizmodata/hardcharger",
        "ghcr.io/gizmodata/hardcharger",
    )


def test_without_a_recorded_digest_the_api_download_uses_the_checksums(fake_github, monkeypatch):
    # GitHub Enterprise Server before asset digests: SHA256SUMS, through the API too.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.digest = ""
    assert runs(binary=ensure_server())
    assert [(r.path, r.authorization) for r in fake_github.api_requests] == [
        (API_RELEASE, f"Bearer {TOKEN}"),
        ("/assets/2", f"Bearer {TOKEN}"),
        ("/assets/1", f"Bearer {TOKEN}"),
    ]
    assert [r.authorization for r in fake_github.storage_requests] == [None, None]
    assert fake_github.web_requests == []


def test_without_a_recorded_digest_a_wrong_checksum_is_refused(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.digest = ""
    platform_name = _install.detect_platform()
    fake_github.sums = f"{'a' * 64}  hardcharger-{platform_name}.tar.gz\n"
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"hardcharger-{platform_name}.tar.gz doesn't match its SHA-256 in the SHA256SUMS of "
        f"release v{SERVER_VERSION} (expected {'a' * 64}, got "
        f"{hashlib.sha256(fake_github.tarball).hexdigest()})"
    )
    assert not installed(platform_name=platform_name, tmp_path=tmp_path).exists()


def test_a_release_with_nothing_to_check_against_is_not_installed(
    macos, fake_github, monkeypatch, tmp_path
):
    # Neither a digest in the API's answer nor a SHA256SUMS: before, such a download was
    # installed unchecked.
    monkeypatch.delenv("HARDCHARGER_RELEASE_DOWNLOAD_URL")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.digest = ""
    fake_github.api_lists_sums = False
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert (
        f"release v{SERVER_VERSION} records no SHA-256 for hardcharger-macos-arm64.tar.gz (no "
        "digest in the answer, no SHA256SUMS): not installing a download that can't be checked"
    ) in str(error.value)
    assert fake_github.storage_requests == []
    assert not installed(platform_name="macos-arm64", tmp_path=tmp_path).exists()


# ---- the container image ---------------------------------------------------------------


def test_github_unreachable_installs_from_the_image(
    linux, fake_registry, extraction, monkeypatch, tmp_path, capsys
):
    # No fake GitHub: its addresses refuse the connection.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    binary = ensure_server()
    root = installed(platform_name="linux-arm64", tmp_path=tmp_path)
    assert binary == root / "bin" / "hardcharger-server"
    assert runs(binary=binary)
    assert os.access(binary, os.X_OK)
    # The release tree as /opt/hardcharger holds it, and nothing else of the image.
    assert sorted(str(p.relative_to(root)) for p in root.rglob(pattern="*")) == [
        "CHANGELOG.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES-web-ui.txt",
        "THIRD_PARTY_NOTICES.md",
        "bin",
        "bin/hardcharger",
        "bin/hardcharger-server",
    ]
    assert (root / "THIRD_PARTY_NOTICES.md").read_bytes() == NOTICES
    # An anonymous pull: one 401, one token, then the index, the arm64 manifest and
    # every layer with the pull token, which never goes to the blobs' storage host.
    assert fake_registry.requests[0].authorization is None
    assert len(fake_registry.token_requests) == 1
    asked = urllib.parse.urlsplit(fake_registry.token_requests[0].path)
    assert asked.path == "/token"
    assert urllib.parse.parse_qs(qs=asked.query) == {
        "scope": [f"repository:{IMAGE_REPOSITORY}:pull"],
        "service": ["fake-registry"],
    }
    assert fake_registry.token_requests[0].authorization is None
    assert {r.authorization for r in fake_registry.requests[1:]} == {f"Bearer {PULL_TOKEN}"}
    digests = fake_registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")
    assert [r.path for r in fake_registry.storage_requests] == [
        f"/blobs/{digest}?sig=xyz" for digest in reversed(digests)
    ]
    assert {r.authorization for r in fake_registry.storage_requests} == {None}
    err = capsys.readouterr().err
    assert "GitHub release download didn't work: " in err
    assert "couldn't connect: " in err
    assert f"hardcharger: trying the container image {fake_registry.name}:{SERVER_VERSION}" in err
    # Installed once: the next call uses the cache.
    requests = len(fake_registry.requests)
    assert ensure_server() == binary
    assert len(fake_registry.requests) == requests


def test_every_source_failing_names_each_and_why(linux, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    lines = str(error.value).splitlines()
    api = os.environ["HARDCHARGER_RELEASE_API_URL"]
    web = os.environ["HARDCHARGER_RELEASE_DOWNLOAD_URL"]
    registry = os.environ["HARDCHARGER_INSTALL_REGISTRY_IMAGE"]
    assert lines[0] == f"couldn't download Hardcharger {SERVER_VERSION} for linux-arm64. Tried:"
    assert lines[1].startswith(
        f"  - GitHub release download: {web}{RELEASE}/SHA256SUMS: couldn't connect: "
    )
    assert lines[2].startswith(
        f"  - GitHub API without a token: {api}{API_RELEASE}: couldn't connect: "
    )
    assert lines[3].startswith(
        f"  - Container image {registry.removeprefix('http://')}:{SERVER_VERSION}: "
        f"{registry.replace('/test/', '/v2/test/')}/manifests/{SERVER_VERSION}: couldn't connect: "
    )
    assert all("refused" in line.lower() for line in lines[1:4])
    assert lines[4] == (
        "HARDCHARGER_INSTALL_SOURCE (auto, github, ghcr; now auto) picks the source, "
        "HARDCHARGER_INSTALL_REGISTRY_IMAGE another registry's copy of the image, and "
        "HARDCHARGER_SERVER_BINARY a server binary you already have."
    )
    assert len(lines) == 5
    assert "internal" not in str(error.value)
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_the_image_for_amd64(fake_registry, monkeypatch, tmp_path):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    binary = ensure_server()
    assert binary == installed(platform_name="linux-amd64", tmp_path=tmp_path) / "bin" / (
        "hardcharger-server"
    )
    digests = fake_registry.layer_digests(tag=SERVER_VERSION, architecture="amd64")
    assert f"/blobs/{digests[0]}?sig=xyz" in [r.path for r in fake_registry.storage_requests]


def test_a_registry_that_asks_for_no_token(linux, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    fake_registry.require_auth = False
    assert runs(binary=ensure_server())
    assert fake_registry.token_requests == []
    assert {r.authorization for r in fake_registry.requests} == {None}


def test_a_pull_token_that_expires_mid_pull_is_renewed(linux, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    fake_registry.expire_after = 3  # the index, the manifest and one layer
    assert runs(binary=ensure_server())
    assert len(fake_registry.token_requests) == 2
    assert fake_registry.requests[-1].authorization == f"Bearer {PULL_TOKEN}-renewed"


def test_a_registry_that_wants_a_sign_in_is_named(linux, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    fake_registry.challenge = 'Basic realm="Registry"'
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert (
        f"  - Container image {fake_registry.name}:{SERVER_VERSION}: {fake_registry.registry.url}"
        f"/v2/{IMAGE_REPOSITORY}/manifests/{SERVER_VERSION}: HTTP 401, and the registry names "
        "no place to get an anonymous pull token (WWW-Authenticate: Basic): only images that "
        "can be pulled without signing in are supported\n"
    ) in str(error.value)
    assert fake_registry.token_requests == []


def test_the_github_token_never_goes_to_the_registry(linux, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    assert runs(binary=ensure_server())
    seen = fake_registry.requests + fake_registry.token_requests + fake_registry.storage_requests
    assert len(seen) > 5
    assert all(TOKEN not in (r.authorization or "") for r in seen)


def test_a_version_the_registry_does_not_have(linux, fake_registry, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server(version="9.9.9")
    assert str(error.value).splitlines()[:2] == [
        "couldn't download Hardcharger 9.9.9 for linux-arm64. Tried:",
        f"  - Container image {fake_registry.name}:9.9.9: {fake_registry.registry.url}/v2/"
        f"{IMAGE_REPOSITORY}/manifests/9.9.9: HTTP 404 Not Found",
    ]
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path, version="9.9.9").exists()


def test_an_image_without_this_architecture(linux, monkeypatch, tmp_path):
    registry = FakeRegistry()
    try:
        registry.tag_index(tag=SERVER_VERSION, variants={"amd64": image_layers()})
        monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", registry.image)
        monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
        with pytest.raises(expected_exception=InstallError) as error:
            ensure_server()
    finally:
        registry.close()
    assert (
        f"/manifests/{SERVER_VERSION}: the image has no linux/arm64 variant (it has "
        "linux/amd64, unknown/unknown)\n"
    ) in str(error.value)
    assert registry.storage_requests == []


@pytest.mark.parametrize(argnames="built_for", argvalues=["arm64", "amd64"])
def test_a_single_architecture_copy_of_the_image(linux, monkeypatch, built_for):
    # A mirror that copied one architecture has a manifest behind the tag, not an index:
    # the image's configuration says what it was built for.
    registry = FakeRegistry()
    try:
        digest = registry.add_manifest(layers=image_layers(), architecture=built_for)
        registry.tag_manifest(tag=SERVER_VERSION, digest=digest)
        monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", registry.image)
        monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
        if built_for == "arm64":
            assert runs(binary=ensure_server())
        else:
            with pytest.raises(expected_exception=InstallError) as error:
                ensure_server()
            assert (
                f"/manifests/{SERVER_VERSION}: the image is for linux/amd64, not linux/arm64\n"
            ) in str(error.value)
            # Only the configuration was fetched, no layer.
            assert len(registry.storage_requests) == 1
    finally:
        registry.close()


def _same_length_but_different(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 1])


def test_a_layer_that_does_not_match_its_digest_is_refused(
    linux, fake_registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    digest = fake_registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")[2]
    fake_registry.blobs[digest] = _same_length_but_different(data=fake_registry.blobs[digest])
    actual = hashlib.sha256(fake_registry.blobs[digest]).hexdigest()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"layer {digest} of {fake_registry.name}:{SERVER_VERSION} doesn't match its digest "
        f"(got sha256:{actual})"
    )
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_a_layer_longer_than_the_manifest_says_is_not_read_to_its_end(
    linux, fake_registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    digest = fake_registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")[2]
    size = len(fake_registry.blobs[digest])
    fake_registry.blobs[digest] += b"\0" * (4 << 20)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert (f"/blobs/{digest}: the answer is longer than the {size} bytes expected\n") in str(
        error.value
    )
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_a_manifest_that_does_not_match_its_digest_is_refused(
    linux, fake_registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    index = json.loads(fake_registry.manifests[SERVER_VERSION][1])
    digest = next(
        m["digest"] for m in index["manifests"] if m["platform"]["architecture"] == "arm64"
    )
    media_type, manifest = fake_registry.manifests[digest]
    fake_registry.manifests[digest] = (media_type, manifest + b" ")
    actual = hashlib.sha256(manifest + b" ").hexdigest()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"{fake_registry.registry.url}/v2/{IMAGE_REPOSITORY}/manifests/{digest} doesn't match "
        f"its digest (got sha256:{actual})"
    )
    assert fake_registry.storage_requests == []


def _tree_layer(version: str = SERVER_VERSION, extra: dict[str, object] | None = None) -> bytes:
    return make_layer(
        entries={
            "opt": None,
            "opt/hardcharger": None,
            "opt/hardcharger/bin": None,
            "opt/hardcharger/bin/hardcharger-server": fake_server_script(version=version),
            **(extra or {}),
        }
    )


@pytest.fixture
def image_of(linux, monkeypatch):
    """Serve an arm64 image made of the given layers (base first) as the pinned version."""
    registries: list[FakeRegistry] = []

    def serve(layers: list[bytes]) -> FakeRegistry:
        registry = FakeRegistry()
        registries.append(registry)
        registry.tag_index(tag=SERVER_VERSION, variants={"arm64": layers})
        monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", registry.image)
        monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
        return registry

    yield serve
    for registry in registries:
        registry.close()


@pytest.mark.parametrize(
    argnames=("entries", "refusal"),
    argvalues=[
        ({"opt/hardcharger/../../escaped.txt": b"pwned"}, "unsafe member"),
        ({"opt/hardcharger/bin/../../../../escaped.txt": b"pwned"}, "unsafe member"),
        ({"/tmp/hardcharger-layer-absolute-escape.txt": b"pwned"}, "unsafe member"),
        # Not below /opt/hardcharger at all, and still a reason to refuse the layer.
        ({"usr/../../escaped.txt": b"pwned"}, "unsafe member"),
        (
            {
                "opt/hardcharger/out": ("symlink", "../../.."),
                "opt/hardcharger/out/escaped.txt": b"pwned",
            },
            "out",
        ),
        ({"opt/hardcharger/etc": ("symlink", "/etc")}, "etc"),
        ({"opt/hardcharger/null": ("device",)}, "null"),
        ({"opt/hardcharger/bin/alias": ("hardlink", "opt/hardcharger/bin/nowhere")}, "hard link"),
    ],
)
def test_a_layer_entry_outside_the_install_directory_is_refused(
    image_of, extraction, tmp_path, entries, refusal
):
    registry = image_of(layers=[_tree_layer(), make_layer(entries=entries)])
    digest = registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")[1]
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert message.startswith(
        f"refusing to unpack layer {digest} of {registry.name}:{SERVER_VERSION}: "
    )
    assert refusal in message
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()
    assert list(tmp_path.rglob(pattern="escaped.txt")) == []
    assert not Path("/tmp/hardcharger-layer-absolute-escape.txt").exists()


def test_layer_entries_inside_the_install_directory_are_unpacked(image_of, extraction, tmp_path):
    # The twin of the refusals: links that stay inside, and the absolute links the real
    # image has outside /opt/hardcharger, which are none of the launcher's business.
    image_of(
        layers=[
            _tree_layer(),
            make_layer(
                entries={
                    "opt/hardcharger/bin/hc": ("symlink", "hardcharger-server"),
                    "opt/hardcharger/current": ("symlink", "bin"),
                    "usr/local/bin/hardcharger-server": (
                        "symlink",
                        "/opt/hardcharger/bin/hardcharger-server",
                    ),
                    "dev/null": ("device",),
                }
            ),
        ]
    )
    binary = ensure_server()
    root = installed(platform_name="linux-arm64", tmp_path=tmp_path)
    assert runs(binary=binary)
    assert os.readlink(root / "bin" / "hc") == "hardcharger-server"
    assert runs(binary=root / "current" / "hc")
    assert not (root / "usr").exists()


def test_layers_stack_as_in_the_image(image_of, extraction, tmp_path):
    # What `docker run` would show in /opt/hardcharger: upper layers replace and remove
    # what lower layers hold.
    base = make_layer(
        entries={
            "opt": None,
            "opt/hardcharger": None,
            "opt/hardcharger/kept.txt": b"from the base",
            "opt/hardcharger/removed.txt": b"removed above",
            "opt/hardcharger/replaced.txt": b"old",
            "opt/hardcharger/data": None,
            "opt/hardcharger/data/old.bin": b"old data",
            "opt/hardcharger/lib": None,
            "opt/hardcharger/lib/gone.so": b"gone with its directory",
            "opt/hardcharger/became-a-file": None,
            "opt/hardcharger/became-a-file/inner.txt": b"hidden by the file above",
            "opt/hardcharger/became-a-link": None,
            "opt/hardcharger/became-a-link/inner.txt": b"hidden by the link above",
        }
    )
    middle = _tree_layer(
        extra={
            "opt/hardcharger/.wh.removed.txt": b"",
            "opt/hardcharger/replaced.txt": b"new",
            "opt/hardcharger/data": None,
            "opt/hardcharger/data/.wh..wh..opq": b"",
            "opt/hardcharger/data/new.bin": b"new data",
            "opt/hardcharger/.wh.lib": b"",
            "opt/hardcharger/became-a-file": b"a file now",
            "opt/hardcharger/became-a-link": ("symlink", "data"),
            "opt/hardcharger/original": b"ORIGINAL",
            "opt/hardcharger/copy": ("hardlink", "opt/hardcharger/original"),
        }
    )
    top = make_layer(
        entries={
            "opt/hardcharger/original": b"NEWER",
            "opt/hardcharger/bin/alias": ("hardlink", "opt/hardcharger/bin/twin"),
            "opt/hardcharger/bin/twin": b"twin",
        }
    )
    image_of(layers=[base, middle, top])
    binary = ensure_server()
    root = installed(platform_name="linux-arm64", tmp_path=tmp_path)
    assert runs(binary=binary)
    assert sorted(str(p.relative_to(root)) for p in root.rglob(pattern="*")) == [
        "became-a-file",
        "became-a-link",
        "bin",
        "bin/alias",
        "bin/hardcharger-server",
        "bin/twin",
        "copy",
        "data",
        "data/new.bin",
        "kept.txt",
        "original",
        "replaced.txt",
    ]
    assert (root / "kept.txt").read_bytes() == b"from the base"
    assert (root / "replaced.txt").read_bytes() == b"new"
    assert (root / "became-a-file").read_bytes() == b"a file now"
    assert os.readlink(root / "became-a-link") == "data"
    # A hard link is a copy of the file its own layer holds, whatever replaced that file above.
    assert (root / "copy").read_bytes() == b"ORIGINAL"
    assert (root / "original").read_bytes() == b"NEWER"
    assert (root / "bin" / "alias").read_bytes() == b"twin"
    assert not (root / "bin" / "alias").is_symlink()


def test_removing_one_directory_does_not_hide_its_siblings_below(image_of, tmp_path):
    # The top layer replaces bin/ (a whiteout, then the new directory); the licence next
    # to it lives in the layer below and still belongs to the tree.
    base = make_layer(
        entries={
            "opt/hardcharger/LICENSE": b"Apache-2.0",
            "opt/hardcharger/bin/old-tool": b"old",
        }
    )
    registry = image_of(layers=[base, _tree_layer(extra={"opt/hardcharger/.wh.bin": b""})])
    assert runs(binary=ensure_server())
    root = installed(platform_name="linux-arm64", tmp_path=tmp_path)
    assert (root / "LICENSE").read_bytes() == b"Apache-2.0"
    assert not (root / "bin" / "old-tool").exists()
    assert len(registry.storage_requests) == 2


def test_layers_below_a_replaced_tree_are_not_downloaded(image_of, tmp_path):
    base = make_layer(entries={"opt/hardcharger/stale.txt": b"stale", "etc/os-release": b"x"})
    registry = image_of(
        layers=[
            base,
            _tree_layer(extra={"opt/hardcharger/.wh..wh..opq": b""}),
            make_layer(entries={}),
        ]
    )
    assert runs(binary=ensure_server())
    root = installed(platform_name="linux-arm64", tmp_path=tmp_path)
    assert not (root / "stale.txt").exists()
    digests = registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")
    assert [r.path for r in registry.storage_requests] == [
        f"/blobs/{digests[2]}?sig=xyz",
        f"/blobs/{digests[1]}?sig=xyz",
    ]


def test_an_image_without_the_server_is_named(image_of, tmp_path):
    registry = image_of(layers=[make_layer(entries={"etc/os-release": b"NAME=Other\n"})])
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert (
        f"  - Container image {registry.name}:{SERVER_VERSION}: {registry.name}:"
        f"{SERVER_VERSION} has no /opt/hardcharger/bin/hardcharger-server in its layers\n"
    ) in str(error.value)
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_an_image_whose_server_reports_another_version_is_refused(image_of, tmp_path):
    registry = image_of(layers=[_tree_layer(version="0.1.9")])
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == f"{registry.name}:{SERVER_VERSION} reports version 0.1.9"
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_a_layer_that_cannot_be_unpacked_is_named(image_of, tmp_path):
    registry = image_of(layers=[_tree_layer(), b"not a tar archive at all"])
    digest = registry.layer_digests(tag=SERVER_VERSION, architecture="arm64")[1]
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert (
        f"  - Container image {registry.name}:{SERVER_VERSION}: layer {digest} of "
        f"{registry.name}:{SERVER_VERSION} can't be unpacked: "
    ) in str(error.value)
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_the_dockerfile_puts_the_release_tree_where_the_launcher_looks():
    # The image is built from docker/Dockerfile; moving the tree there without moving
    # IMAGE_ROOT would break the fallback for every later release.
    dockerfile = (Path(__file__).resolve().parents[2] / "docker" / "Dockerfile").read_text()
    root = "/" + "/".join(_install.IMAGE_ROOT)
    assert root == "/opt/hardcharger"
    assert f"COPY dist/hardcharger-linux-${{TARGETARCH}}/ {root}/\n" in dockerfile


# ---- HARDCHARGER_INSTALL_SOURCE --------------------------------------------------------


@pytest.mark.parametrize(argnames="value", argvalues=["auto", "", " AUTO "])
def test_source_auto_uses_github_first(linux, fake_github, fake_registry, monkeypatch, value):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", value)
    assert runs(binary=ensure_server())
    assert len(fake_github.web_requests) == 2
    assert fake_registry.requests == []


def test_source_auto_falls_back_to_the_image(linux, fake_github, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "auto")
    fake_github.web_error = (503, {})
    fake_github.api_error = (503, {})
    assert runs(binary=ensure_server())
    assert fake_registry.storage_requests != []


def test_source_github_never_asks_the_registry(
    linux, fake_github, fake_registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "github")
    fake_github.web_error = (503, {})
    fake_github.api_error = (503, {})
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert (
        f"  - GitHub release download: {fake_github.web.url}{RELEASE}/SHA256SUMS: HTTP 503 "
        "Service Unavailable\n"
    ) in message
    assert "Container image" not in message
    assert "HARDCHARGER_INSTALL_SOURCE (auto, github, ghcr; now github) picks the source" in message
    assert fake_registry.requests == []
    assert not installed(platform_name="linux-arm64", tmp_path=tmp_path).exists()


def test_source_github_installs_from_github(linux, fake_github, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "github")
    assert runs(binary=ensure_server())
    assert fake_registry.requests == []


def test_source_ghcr_never_asks_github(linux, fake_github, fake_registry, monkeypatch, capsys):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    assert runs(binary=ensure_server())
    assert fake_github.api_requests == []
    assert fake_github.web_requests == []
    assert fake_registry.storage_requests != []
    assert capsys.readouterr().err.startswith(
        f"hardcharger: downloading Hardcharger {SERVER_VERSION} from the container image "
        f"{fake_registry.name}:{SERVER_VERSION} (5 layers)\n"
    )


@pytest.mark.parametrize(argnames="value", argvalues=["docker", "ghcr.io", "github,ghcr"])
def test_an_unknown_source_is_an_error_naming_the_valid_ones(fake_github, monkeypatch, value):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", value)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"HARDCHARGER_INSTALL_SOURCE={value!r} is not a source: use one of auto, github, ghcr"
    )
    assert fake_github.web_requests == []


def test_macos_never_tries_the_image_and_says_why(macos, fake_registry, monkeypatch, tmp_path):
    # GitHub refuses the connection, and a registry with the image is right there.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert message.startswith(
        f"couldn't download Hardcharger {SERVER_VERSION} for macos-arm64. Tried:\n"
    )
    assert (
        f"  - Container image {fake_registry.name}:{SERVER_VERSION}: not tried, it holds Linux "
        "binaries and this is macos-arm64\n"
    ) in message
    assert fake_registry.requests == []
    assert fake_registry.token_requests == []
    assert not installed(platform_name="macos-arm64", tmp_path=tmp_path).exists()


def test_macos_installs_from_github(macos, fake_github, fake_registry, monkeypatch, tmp_path):
    # The twin: the same machine, with GitHub answering.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    binary = ensure_server()
    assert binary.parent.parent == installed(platform_name="macos-arm64", tmp_path=tmp_path)
    assert runs(binary=binary)
    assert fake_registry.requests == []


def test_source_ghcr_on_macos_is_refused(macos, fake_github, fake_registry, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        "HARDCHARGER_INSTALL_SOURCE=ghcr works on Linux only: the container image holds Linux "
        "binaries, and this is macos-arm64. Use HARDCHARGER_INSTALL_SOURCE=github, or leave "
        "it unset."
    )
    assert fake_registry.requests == []
    assert fake_github.web_requests == []


# ---- HARDCHARGER_INSTALL_REGISTRY_IMAGE ------------------------------------------------


@pytest.mark.parametrize(
    argnames=("value", "expected"),
    argvalues=[
        (
            "ghcr.io/gizmodata/hardcharger",
            ("https://ghcr.io", "gizmodata/hardcharger", "ghcr.io/gizmodata/hardcharger"),
        ),
        (
            "registry.example.com/mirrors/gizmodata/hardcharger",
            (
                "https://registry.example.com",
                "mirrors/gizmodata/hardcharger",
                "registry.example.com/mirrors/gizmodata/hardcharger",
            ),
        ),
        (
            "http://127.0.0.1:5000/mirror/hardcharger",
            ("http://127.0.0.1:5000", "mirror/hardcharger", "127.0.0.1:5000/mirror/hardcharger"),
        ),
        (
            "localhost/hardcharger",
            ("https://localhost", "hardcharger", "localhost/hardcharger"),
        ),
        # Docker Hub, with and without its name in front, at the address its API has.
        (
            "docker.io/gizmodata/hardcharger",
            (
                "https://registry-1.docker.io",
                "gizmodata/hardcharger",
                "docker.io/gizmodata/hardcharger",
            ),
        ),
        (
            "gizmodata/hardcharger",
            (
                "https://registry-1.docker.io",
                "gizmodata/hardcharger",
                "docker.io/gizmodata/hardcharger",
            ),
        ),
        (
            "hardcharger",
            ("https://registry-1.docker.io", "library/hardcharger", "docker.io/hardcharger"),
        ),
    ],
)
def test_the_registry_image(monkeypatch, value, expected):
    monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", value)
    assert _install.image_reference() == expected


@pytest.mark.parametrize(
    argnames="value",
    argvalues=[
        "ghcr.io/gizmodata/hardcharger:2.13.0",
        "ghcr.io/gizmodata/hardcharger@sha256:" + "0" * 64,
        "ghcr.io/Gizmodata/Hardcharger",
        "ghcr.io/",
        "hardcharger:latest",
        "ftp://ghcr.io/gizmodata/hardcharger",
        "ghcr.io/gizmodata/hard charger",
    ],
)
def test_a_registry_image_with_a_tag_or_another_shape_is_an_error(fake_github, monkeypatch, value):
    # Even with GitHub answering: a setting that can't be used is said so at once.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE", value)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"HARDCHARGER_INSTALL_REGISTRY_IMAGE={value!r} is not an image name: give "
        "registry/repository in lower case, without a tag or digest (the server version is "
        "the tag), like ghcr.io/gizmodata/hardcharger"
    )
    assert fake_github.web_requests == []


# ---- the cache directory, a server that hangs -------------------------------------------


def test_a_cache_directory_that_cannot_be_made_is_an_install_error(
    fake_github, monkeypatch, tmp_path
):
    blocker = tmp_path / "a-file"
    blocker.write_text(data="in the way")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_CACHE_DIR", str(blocker / "cache"))
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert message.startswith(
        f"couldn't create the cache directory {blocker / 'cache' / f'v{SERVER_VERSION}'}: "
    )
    assert message.endswith("(set HARDCHARGER_CACHE_DIR to a directory that can be written)")
    assert fake_github.web_requests == []


def test_an_install_another_process_finished_first_is_kept(fake_github, monkeypatch, tmp_path):
    # Two processes on a cold cache (parallel test workers): the one that finishes second
    # must not delete the install the first one may already be running.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    target = installed(platform_name=_install.detect_platform(), tmp_path=tmp_path)
    check_version = _install.server_version_of

    def check_while_the_other_process_installs(binary: Path) -> str:
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "hardcharger-server").write_bytes(
            fake_server_script(version=SERVER_VERSION)
        )
        (target / "bin" / "hardcharger-server").chmod(mode=0o755)
        (target / "installed-by-the-other-process").write_text(data="first")
        return check_version(binary=binary)

    monkeypatch.setattr(_install, "server_version_of", check_while_the_other_process_installs)
    binary = ensure_server()
    assert binary == target / "bin" / "hardcharger-server"
    assert runs(binary=binary)
    assert (target / "installed-by-the-other-process").read_text() == "first"
    # Nothing of this process's own download is left behind.
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_a_broken_install_directory_is_replaced(fake_github, monkeypatch, tmp_path):
    # The twin: a directory without a server in it is not an install, and makes way.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    target = installed(platform_name=_install.detect_platform(), tmp_path=tmp_path)
    (target / "bin").mkdir(parents=True)
    (target / "leftover").write_text(data="broken")
    binary = ensure_server()
    assert runs(binary=binary)
    assert not (target / "leftover").exists()
    assert (target / "LICENSE").read_text() == "Apache-2.0"


def test_a_server_that_hangs_on_version_is_an_install_error(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setattr(_install, "_VERSION_TIMEOUT", 0.5)
    platform_name = _install.detect_platform()
    fake_github.tarball = make_tarball(
        platform_name=platform_name,
        extra={
            f"hardcharger-{platform_name}/bin/hardcharger-server": b"#!/bin/sh\nexec sleep 30\n"
        },
    )
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "bin/hardcharger-server doesn't run: " in str(error.value)
    assert "timed out after 0.5 seconds" in str(error.value)
    assert not installed(platform_name=platform_name, tmp_path=tmp_path).exists()


# ---- the release tarball, unpacked both ways --------------------------------------------


@pytest.mark.parametrize(
    argnames=("member", "refusal"),
    argvalues=[
        ("../escaped.txt", "unsafe member"),
        ("/tmp/hardcharger-absolute-escape.txt", "unsafe member"),
    ],
)
def test_a_tarball_member_outside_the_target_is_refused_both_ways(
    fake_github, extraction, monkeypatch, tmp_path, member, refusal
):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    platform_name = _install.detect_platform()
    fake_github.tarball = make_tarball(platform_name=platform_name, extra={member: b"pwned"})
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value) == (
        f"refusing to unpack hardcharger-{platform_name}.tar.gz: {refusal} {member!r}"
    )
    assert list(tmp_path.rglob(pattern="escaped.txt")) == []
    assert not Path("/tmp/hardcharger-absolute-escape.txt").exists()


def test_a_tarball_unpacks_both_ways(fake_github, extraction, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    assert runs(binary=ensure_server())


def test_a_tarball_that_is_no_archive_is_an_install_error(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.tarball = b"this is not a gzip file"
    platform_name = _install.detect_platform()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert str(error.value).startswith(f"couldn't unpack hardcharger-{platform_name}.tar.gz: ")
    assert not installed(platform_name=platform_name, tmp_path=tmp_path).exists()
