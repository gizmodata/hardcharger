"""The download: token choice, the redirect that must not carry the token, digest and
version checks, unsafe tarballs, unsupported platforms. Against a fake GitHub."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import TOKEN, fake_server_script, make_tarball

import hardcharger
from hardcharger import SERVER_VERSION, _install
from hardcharger._install import InstallError, detect_platform, ensure_server, github_token


def test_version_matches_metadata():
    assert hardcharger.__version__ == importlib.metadata.version(distribution_name="hardcharger")
    # A Python packaging patch can ship without a new server binary release.
    assert SERVER_VERSION == "2.0.0"


# ---- platforms ------------------------------------------------------------------------


@pytest.mark.parametrize(
    argnames=("system", "machine", "expected"),
    argvalues=[
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "macos-arm64"),
    ],
)
def test_released_platforms(system, machine, expected):
    assert detect_platform(system=system, machine=machine) == expected


@pytest.mark.parametrize(
    argnames=("system", "machine", "name"),
    argvalues=[("Windows", "AMD64", "windows-amd64"), ("Darwin", "x86_64", "macos-amd64")],
)
def test_unreleased_platform_is_refused(system, machine, name):
    with pytest.raises(expected_exception=InstallError) as error:
        detect_platform(system=system, machine=machine)
    assert f"hardcharger-server is not released for {name}" in str(error.value)
    assert "HARDCHARGER_SERVER_BINARY" in str(error.value)


# ---- the token ------------------------------------------------------------------------


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A `gh` on PATH whose `auth token` prints gh-token."""
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\n[ "$1 $2" = "auth token" ] && echo gh-token && exit 0\nexit 1\n')
    gh.chmod(mode=0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_install_token_wins(fake_gh, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "install-token")
    monkeypatch.setenv("GH_TOKEN", "env-gh-token")
    assert github_token() == "install-token"


def test_gh_token_comes_next(fake_gh, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "env-gh-token")
    assert github_token() == "env-gh-token"


def test_gh_cli_comes_last(fake_gh):
    assert github_token() == "gh-token"


def test_none_means_anonymous(fake_gh, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("GH_TOKEN", "env-gh-token")
    assert github_token() is None


def test_no_gh_and_no_variables_is_anonymous(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no gh here
    assert github_token() is None


# ---- the download ---------------------------------------------------------------------


def test_download_installs_the_release(fake_github, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    binary = ensure_server()
    platform_name = detect_platform()
    assert (
        binary
        == tmp_path / "cache" / f"v{SERVER_VERSION}" / platform_name / "bin" / "hardcharger-server"
    )
    assert binary.stat().st_mode & stat.S_IXUSR
    assert (binary.parent.parent / "LICENSE").read_text() == "Apache-2.0"
    # The notices of the third-party software in the binary come with it.
    notices = (binary.parent.parent / "THIRD_PARTY_NOTICES.md").read_text()
    assert notices == "# Third-party software in Hardcharger\n"
    assert (binary.parent.parent / "THIRD_PARTY_NOTICES-web-ui.txt").is_file()
    assert subprocess.run(args=[binary, "--version"], capture_output=True, text=True).stdout == (
        f"Hardcharger {SERVER_VERSION}\n"
    )
    # The token went to the API and nowhere else, and is never printed.
    assert [r.authorization for r in fake_github.api_requests] == [f"Bearer {TOKEN}"] * 2
    assert [r.path for r in fake_github.storage_requests] == ["/blob/1?sig=abc"]
    assert TOKEN not in capsys.readouterr().err
    # Installed once: the next call uses the cache.
    assert ensure_server() == binary
    assert len(fake_github.api_requests) == 2


def test_token_is_not_sent_to_the_storage_host(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    ensure_server()
    assert fake_github.api_requests[-1].path == "/assets/1"
    assert fake_github.api_requests[-1].authorization == f"Bearer {TOKEN}"
    assert fake_github.storage_requests[0].authorization is None


def test_version_argument_and_variable(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.version = "0.9.0"  # any version but the pin
    fake_github.tarball = make_tarball(platform_name=detect_platform(), version="0.9.0")
    fake_github.digest = "sha256:" + hashlib.sha256(fake_github.tarball).hexdigest()
    monkeypatch.setenv("HARDCHARGER_VERSION", "v0.9.0")
    assert ensure_server().parts[-4] == "v0.9.0"
    assert ensure_server(version="0.9.0").parts[-4] == "v0.9.0"


@pytest.mark.parametrize(
    argnames="spelling",
    argvalues=["2.0.0-rc.1", "v2.0.0-rc.1", "2.0.0rc1", "v2.0.0rc1"],
)
def test_a_release_candidate_downloads_by_its_tag(fake_github, monkeypatch, spelling):
    # The tag and the server's --version say 2.0.0-rc.1; a PEP 440 spelling (this package's
    # own version, 2.0.0rc1) means the same release.
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.version = "2.0.0-rc.1"
    fake_github.tarball = make_tarball(platform_name=detect_platform(), version="2.0.0-rc.1")
    fake_github.digest = "sha256:" + hashlib.sha256(fake_github.tarball).hexdigest()
    binary = ensure_server(version=spelling)
    assert binary.parts[-4] == "v2.0.0-rc.1"
    tag_path = "/repos/gizmodata/hardcharger/releases/tags/v2.0.0-rc.1"
    assert fake_github.api_requests[0].path == tag_path


@pytest.mark.parametrize(
    argnames=("given", "expected"),
    argvalues=[
        ("0.2.0", "0.2.0"),
        ("v0.2.0", "0.2.0"),
        (" 2.0.0-rc.1 ", "2.0.0-rc.1"),
        ("2.0.0rc1", "2.0.0-rc.1"),
        ("2.0.0rc10", "2.0.0-rc.10"),
        ("2.0.0", "2.0.0"),
    ],
)
def test_normalize_version(given, expected):
    assert _install.normalize_version(version=given) == expected


def test_without_a_token_the_error_says_how_to_get_access(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    message = str(error.value)
    assert f"couldn't find Hardcharger release v{SERVER_VERSION} on GitHub (HTTP 404)" in message
    assert (
        "set HARDCHARGER_INSTALL_TOKEN to a GitHub token that can read gizmodata/hardcharger"
        in (message)
    )
    assert fake_github.api_requests[0].authorization is None


def test_with_a_wrong_token_the_error_says_it_cannot_read(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "wrong-token")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "The token in use can't read gizmodata/hardcharger's releases" in str(error.value)
    assert "wrong-token" not in str(error.value)


def test_a_missing_release_is_a_404(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server(version="9.9.9")
    assert "couldn't find Hardcharger release v9.9.9 on GitHub (HTTP 404)" in str(error.value)


def test_a_digest_mismatch_is_refused(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.digest = "sha256:" + "0" * 64
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "doesn't match the SHA-256 GitHub recorded for it" in str(error.value)
    assert not (tmp_path / "cache" / f"v{SERVER_VERSION}" / detect_platform()).exists()


def test_a_binary_reporting_another_version_is_refused(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.tarball = make_tarball(platform_name=detect_platform(), version="0.1.9")
    fake_github.digest = "sha256:" + hashlib.sha256(fake_github.tarball).hexdigest()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert f"of release v{SERVER_VERSION} reports version 0.1.9" in str(error.value)
    assert not (tmp_path / "cache" / f"v{SERVER_VERSION}" / detect_platform()).exists()


@pytest.mark.parametrize(
    argnames="member",
    argvalues=["../escaped.txt", "/tmp/hardcharger-absolute-escape.txt"],
)
def test_a_tarball_member_outside_the_target_is_refused(fake_github, monkeypatch, tmp_path, member):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.tarball = make_tarball(platform_name=detect_platform(), extra={member: b"pwned"})
    fake_github.digest = "sha256:" + hashlib.sha256(fake_github.tarball).hexdigest()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "refusing to unpack" in str(error.value)
    assert not (tmp_path / "cache" / "escaped.txt").exists()
    assert not (tmp_path / "cache" / f"v{SERVER_VERSION}" / "escaped.txt").exists()
    assert not Path("/tmp/hardcharger-absolute-escape.txt").exists()


def test_a_tarball_without_the_server_is_refused(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    fake_github.tarball = _tar_without_server()
    fake_github.digest = "sha256:" + hashlib.sha256(fake_github.tarball).hexdigest()
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "has no bin/hardcharger-server" in str(error.value)


def _tar_without_server() -> bytes:
    import io
    import tarfile

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="hardcharger-x/LICENSE")
        info.size = 3
        tar.addfile(tarinfo=info, fileobj=io.BytesIO(initial_bytes=b"abc"))
    return out.getvalue()


# ---- HARDCHARGER_SERVER_BINARY --------------------------------------------------------


def test_server_binary_variable_skips_the_download(fake_github, monkeypatch, tmp_path):
    local = tmp_path / "hardcharger-server"
    local.write_bytes(fake_server_script(version="0.2.0"))
    monkeypatch.setenv("HARDCHARGER_SERVER_BINARY", str(local))
    assert ensure_server() == local
    assert fake_github.api_requests == []


def test_server_binary_variable_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_SERVER_BINARY", str(tmp_path / "missing"))
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "HARDCHARGER_SERVER_BINARY names" in str(error.value)
    assert "which doesn't exist" in str(error.value)


# ---- the command line -----------------------------------------------------------------


def _python_m(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args=[sys.executable, "-m", "hardcharger", *args], capture_output=True, text=True
    )


def test_cli_download_prints_the_path(fake_github, monkeypatch, tmp_path):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    result = _python_m("download")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == (
        tmp_path / "cache" / f"v{SERVER_VERSION}" / detect_platform() / "bin" / "hardcharger-server"
    )
    assert TOKEN not in result.stdout + result.stderr


def test_cli_download_failure_exits_1(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    result = _python_m("download")
    assert result.returncode == 1
    assert "set HARDCHARGER_INSTALL_TOKEN" in result.stderr


def test_cli_server_runs_the_binary_with_its_arguments(monkeypatch, tmp_path):
    local = tmp_path / "hardcharger-server"
    local.write_bytes(fake_server_script(version="0.2.0"))
    local.chmod(mode=0o755)
    monkeypatch.setenv("HARDCHARGER_SERVER_BINARY", str(local))
    assert _python_m("server", "--version").stdout == "Hardcharger 0.2.0\n"
    # The fake exits 3 for anything but --version: the exit status comes through.
    assert _python_m("server", "--port", "1").returncode == 3


def test_cache_root_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("HARDCHARGER_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert _install.cache_root() == tmp_path / "xdg" / "hardcharger"
