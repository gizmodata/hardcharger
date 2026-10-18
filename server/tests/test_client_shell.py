"""Port of GizmoSQL 1.x tests/test_client_shell.sh (64 assertions; the battery once
counted 67, but v1.42.0's script runs 64, as running it shows).

The script is ported as a script (test_client_shell.sh, the same 64 assertions with
Hardcharger's names) and run here: one case per assertion, so a failure names it. All 64
apply. The two URI cases give the user with --username and plain HTTP with ?tls=false:
Hardcharger's URIs follow the ADBC driver's (HTTPS unless ?tls=false, tls the only parameter),
where 1.x's carried ?username= and were plain HTTP by default.
"""

import pathlib
import re
import subprocess

import pytest

from conftest import BASH, BINARY, CLIENT_BINARY, clean_environment

SCRIPT = pathlib.Path(__file__).resolve().parent / "test_client_shell.sh"
NAMES = re.findall(pattern=r'^\s*run_test(?:_not_contains|_exit_code)? "([^"]+)"', string=SCRIPT.read_text(),
                   flags=re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def outcome():
    for binary in (BINARY, CLIENT_BINARY):
        if not binary.exists():
            pytest.fail(reason=f"{binary} is missing: build the server first")
    env = {**clean_environment(), "HARDCHARGER_TEST_BINARY": str(BINARY),
           "HARDCHARGER_TEST_CLIENT_BINARY": str(CLIENT_BINARY)}
    result = subprocess.run(args=[BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=600)
    lines = ANSI.sub("", result.stdout).splitlines()
    passed = {line.split("PASS: ", 1)[1] for line in lines if line.strip().startswith("PASS: ")}
    failed = {line.split("FAIL: ", 1)[1] for line in lines if line.strip().startswith("FAIL: ")}
    return result, passed, failed


def test_every_assertion_is_ported():
    assert len(NAMES) == 64, NAMES
    assert len(set(NAMES)) == 64


def test_the_script_passes(outcome):
    result, passed, failed = outcome
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-2000:]
    assert f"Test Results: 64 passed, 0 failed (out of 64)" in ANSI.sub("", result.stdout), result.stdout[-2000:]


@pytest.mark.parametrize("name", NAMES)
def test_assertion(outcome, name):
    result, passed, failed = outcome
    assert name in passed and name not in failed, result.stdout[-5000:]
