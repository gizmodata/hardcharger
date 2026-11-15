"""Real downloads of the pinned release (marker ``download``): from GitHub through the
API with a token, from GitHub's download URL without one, and on Linux from the container
image on ghcr.io.

Token for the API test: HARDCHARGER_PY_TEST_TOKEN, else the package's own lookup (gh auth
token). These tests fail, never skip, when GitHub or ghcr.io can't be read.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from conftest import TEST_TOKEN_ENV

from hardcharger import SERVER_VERSION, InstallError, Server, ensure_server
from hardcharger._install import detect_platform

pytestmark = pytest.mark.download


@pytest.fixture(autouse=True)
def real_addresses(clean_env, monkeypatch):
    """The package's own defaults: github.com, api.github.com and ghcr.io."""
    monkeypatch.delenv("HARDCHARGER_RELEASE_API_URL")
    monkeypatch.delenv("HARDCHARGER_RELEASE_DOWNLOAD_URL")
    monkeypatch.delenv("HARDCHARGER_INSTALL_REGISTRY_IMAGE")


def _is_the_pinned_server(binary, tmp_path) -> None:
    assert binary == (
        tmp_path / "cache" / f"v{SERVER_VERSION}" / detect_platform() / "bin" / "hardcharger-server"
    )
    version = subprocess.run(args=[binary, "--version"], capture_output=True, text=True).stdout
    assert version.strip() == f"Hardcharger {SERVER_VERSION}"
    # The cached binary is what Server runs when nothing else is named.
    with Server() as server:
        assert server.is_running()


def test_download_the_pinned_release_and_run_it(monkeypatch, tmp_path):
    if token := os.environ.get(TEST_TOKEN_ENV):
        monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", token)
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "github")
    _is_the_pinned_server(binary=ensure_server(), tmp_path=tmp_path)


def test_download_without_a_token_or_the_api(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "github")
    _is_the_pinned_server(binary=ensure_server(), tmp_path=tmp_path)
    # The direct download worked at once: nothing was retried another way.
    assert "didn't work" not in capsys.readouterr().err


def test_download_from_the_container_image(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", "none")
    monkeypatch.setenv("HARDCHARGER_INSTALL_SOURCE", "ghcr")
    if not detect_platform().startswith("linux-"):
        # The image holds Linux binaries: here the launcher must say so, not download it.
        with pytest.raises(expected_exception=InstallError) as error:
            ensure_server()
        assert "HARDCHARGER_INSTALL_SOURCE=ghcr works on Linux only" in str(error.value)
        return
    _is_the_pinned_server(binary=ensure_server(), tmp_path=tmp_path)
    assert (
        f"from the container image ghcr.io/gizmodata/hardcharger:{SERVER_VERSION}"
        in capsys.readouterr().err
    )
