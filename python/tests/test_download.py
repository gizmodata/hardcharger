"""A real download of the pinned release from GitHub (marker ``download``).

Token: HARDCHARGER_PY_TEST_TOKEN, else the package's own lookup (gh auth token). Fails,
never skips, when GitHub can't be read.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from conftest import TEST_TOKEN_ENV

from hardcharger import SERVER_VERSION, Server, ensure_server
from hardcharger._install import detect_platform

pytestmark = pytest.mark.download


def test_download_the_pinned_release_and_run_it(monkeypatch, tmp_path):
    if token := os.environ.get(TEST_TOKEN_ENV):
        monkeypatch.setenv("HARDCHARGER_INSTALL_TOKEN", token)
    binary = ensure_server()
    assert binary == (
        tmp_path / "cache" / f"v{SERVER_VERSION}" / detect_platform() / "bin" / "hardcharger-server"
    )
    version = subprocess.run(args=[binary, "--version"], capture_output=True, text=True).stdout
    assert version.strip() == f"Hardcharger {SERVER_VERSION}"
    # The cached binary is what Server runs when nothing else is named.
    with Server() as server:
        assert server.is_running()
