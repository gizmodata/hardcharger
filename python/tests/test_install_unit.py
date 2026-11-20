"""The download through the GitHub API with a token: token choice, the redirect that must
not carry the token, digest and version checks, unsafe tarballs, unsupported platforms.
Against a fake GitHub. The sources and their fallbacks are in test_install_sources.py."""

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
    # Python-only changes may use .postN; the downloaded server stays at the base release.
    expected = SERVER_VERSION.replace("-rc.", "rc")
    assert hardcharger.__version__.split(".post", 1)[0] == expected


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


def test_a_missing_release_names_every_github_attempt(fake_github, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", TOKEN)
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server(version="9.9.9")
    message = str(error.value)
    assert f"couldn't download Hardcharger 9.9.9 for {detect_platform()}. Tried:" in message
    api = f"{fake_github.api.url}/repos/gizmodata/hardcharger/releases/tags/v9.9.9"
    web = f"{fake_github.web.url}/gizmodata/hardcharger/releases/download/v9.9.9/SHA256SUMS"
    assert f"  - GitHub API with the configured token: {api}: HTTP 404 Not Found\n" in message
    assert f"  - GitHub release download: {web}: HTTP 404 Not Found\n" in message
    assert f"  - GitHub API without a token: {api}: HTTP 404 Not Found" in message
    assert TOKEN not in message


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
    result = _python_m("download", "--version", "9.9.9")
    assert result.returncode == 1
    assert "hardcharger: couldn't download Hardcharger 9.9.9" in result.stderr
    assert "/releases/download/v9.9.9/SHA256SUMS: HTTP 404 Not Found" in result.stderr
    assert "Traceback" not in result.stderr


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


# ---- the CPU target (linux-amd64: optimized or baseline, issue #62) ----------------------

#: /proc/cpuinfo's flags on QEMU's kvm64 CPU model ("Common KVM processor", x86-64-v1), as a
#: CentOS 8 guest shows them.
KVM64_CPUINFO = (
    "model name\t: Common KVM processor\n"
    "flags\t\t: fpu de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx "
    "fxsr sse sse2 ht syscall nx lm constant_tsc nopl xtopology cpuid pni cx16 hypervisor pti\n"
)
#: An Ivy Bridge (AVX, no AVX2): x86-64-v2 and AVX, not v3.
IVY_BRIDGE_CPUINFO = (
    "flags\t\t: fpu sse sse2 ssse3 cx16 sse4_1 sse4_2 popcnt pni lahf_lm xsave avx f16c\n"
)


#: Rosetta's x86_64 Linux (Docker Desktop on Apple silicon): AVX2 and BMI2, but no abm or xsave
#: in /proc/cpuinfo.
ROSETTA_CPUINFO = (
    "flags\t\t: fpu tsc de cx8 apic sep cmov pat pse36 clflush mmx fxsr sse sse2 syscall nx lm "
    "rep_good nopl pni pclmulqdq ssse3 cx16 sse4_1 popcnt sse4_2 aes lahf_lm movbe fma avx f16c "
    "rdrand bmi1 avx2 bmi2\n"
)


@pytest.fixture
def linux_amd64(monkeypatch):
    """This machine is linux-amd64, whatever it really is."""
    monkeypatch.setattr(target=_install.platform, name="system", value=lambda: "Linux")
    monkeypatch.setattr(target=_install.platform, name="machine", value=lambda: "x86_64")


def test_missing_cpu_features():
    assert (
        _install.missing_cpu_features(cpuinfo=f"flags\t: {' '.join(_install.X86_64_V3_FLAGS)}\n")
        == []
    )
    missing = _install.missing_cpu_features(cpuinfo=KVM64_CPUINFO)
    assert "avx2" in missing and "sse4_2" in missing and "popcnt" in missing
    assert "pni" not in missing and "cx16" not in missing
    assert _install.missing_cpu_features(cpuinfo=IVY_BRIDGE_CPUINFO) == [
        "avx2",
        "bmi1",
        "bmi2",
        "fma",
        "movbe",
    ]
    # Rosetta (Docker on Apple silicon) lists neither abm nor xsave, and runs the optimized build.
    assert _install.missing_cpu_features(cpuinfo=ROSETTA_CPUINFO) == []
    assert _install.missing_cpu_features(cpuinfo="processor\t: 0\n") is None


@pytest.mark.parametrize(
    argnames=("target", "cpuinfo", "expected"),
    argvalues=[
        ("auto", None, "linux-amd64"),  # the conftest's x86-64-v3 CPU
        ("auto", KVM64_CPUINFO, "linux-amd64-baseline"),
        ("auto", IVY_BRIDGE_CPUINFO, "linux-amd64-baseline"),
        ("auto", ROSETTA_CPUINFO, "linux-amd64"),
        ("auto", "no flags here\n", "linux-amd64"),  # unreadable: the default build
        ("optimized", KVM64_CPUINFO, "linux-amd64"),
        ("baseline", None, "linux-amd64-baseline"),
    ],
)
def test_select_build_on_linux_amd64(target, cpuinfo, expected):
    assert (
        _install.select_build(platform_name="linux-amd64", target=target, cpuinfo=cpuinfo)
        == expected
    )


@pytest.mark.parametrize(argnames="platform_name", argvalues=["linux-arm64", "macos-arm64"])
def test_other_platforms_have_one_build(platform_name):
    assert (
        _install.select_build(platform_name=platform_name, target="auto", cpuinfo=KVM64_CPUINFO)
        == platform_name
    )
    with pytest.raises(expected_exception=InstallError) as error:
        _install.select_build(platform_name=platform_name, target="baseline")
    assert "the baseline build is for linux-amd64 only" in str(error.value)


def test_a_wrong_cpu_target_is_refused(monkeypatch):
    monkeypatch.setenv("HARDCHARGER_CPU_TARGET", "fastest")
    with pytest.raises(expected_exception=InstallError) as error:
        _install.cpu_target()
    assert "HARDCHARGER_CPU_TARGET must be one of auto, optimized, baseline, not 'fastest'" in str(
        error.value
    )


def test_auto_installs_the_baseline_build_on_an_old_cpu(
    linux_amd64, fake_github, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(target=_install, name="_cpuinfo_text", value=lambda: KVM64_CPUINFO)
    fake_github.platform_name = "linux-amd64-baseline"
    fake_github.tarball = make_tarball(platform_name="linux-amd64-baseline")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    binary = ensure_server()
    assert binary == (
        tmp_path
        / "cache"
        / f"v{SERVER_VERSION}"
        / "linux-amd64-baseline"
        / "bin"
        / "hardcharger-server"
    )
    err = capsys.readouterr().err
    assert "this CPU lacks" in err and "avx2" in err and "installing the baseline build" in err
    assert "downloading hardcharger-linux-amd64-baseline.tar.gz" in err
    # From the release download; the API had nothing to do with it.
    assert any(
        r.path.endswith("/hardcharger-linux-amd64-baseline.tar.gz")
        for r in fake_github.web_requests
    )


def test_optimized_on_request_installs_the_optimized_build(
    linux_amd64, fake_github, monkeypatch, capsys
):
    monkeypatch.setattr(target=_install, name="_cpuinfo_text", value=lambda: KVM64_CPUINFO)
    monkeypatch.setenv("HARDCHARGER_CPU_TARGET", "optimized")
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    fake_github.platform_name = "linux-amd64"
    fake_github.tarball = make_tarball(platform_name="linux-amd64")
    assert ensure_server().parent.parent.name == "linux-amd64"
    assert "this CPU lacks" not in capsys.readouterr().err


def test_the_baseline_build_is_not_taken_from_the_image(linux_amd64, monkeypatch):
    monkeypatch.setenv("HARDCHARGER_CPU_TARGET", "baseline")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server()
    assert "the container image holds the optimized build" in str(error.value)
    # auto: GitHub is tried, and the failure says why the image was not.
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "auto")
    with pytest.raises(expected_exception=InstallError) as error:
        ensure_server(progress=False)
    assert "not tried, it holds the optimized build only" in str(error.value)
