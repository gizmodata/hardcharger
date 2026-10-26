"""Release gates reject independently bumped server-package versions and invalid pins."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/check_python_version.py"
SPEC = importlib.util.spec_from_file_location("check_python_version", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


@pytest.mark.parametrize(
    "server,package",
    [
        ("2.1.0", "2.1.0"),
        ("2.1.0", "2.1.0.post1"),
        ("2.1.0", "2.1.0.post12"),
        ("2.1.0-rc.2", "2.1.0rc2"),
    ],
)
def test_aligned_release_or_python_post_release(server, package):
    POLICY.validate(
        server=server, package=package, runtime=package, pin=server, python_tag=f"python-v{package}"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"package": "2.1.1", "runtime": "2.1.1"},
        {"runtime": "2.0.0"},
        {"pin": "2.0.0"},
        {"package": "2.1.0.post1", "runtime": "2.1.0.post1", "pin": "2.1.0.post1"},
        {"package": "2.1.0.post1", "runtime": "2.1.0.post1", "server_tag": "v2.1.0"},
        {"server_tag": "v2.2.0"},
        {"python_tag": "python-v2.1.1"},
        {"package": "2.1.0.dev1"},
        {"package": "2.1.0.post0"},
    ],
)
def test_version_drift_or_wrong_release_tag_is_refused(overrides):
    versions = {"server": "2.1.0", "package": "2.1.0", "runtime": "2.1.0", "pin": "2.1.0"}
    with pytest.raises(ValueError):
        POLICY.validate(**{**versions, **overrides})


def test_repository_versions_match():
    subprocess.run(args=[sys.executable, str(SCRIPT)], check=True, capture_output=True, text=True)
