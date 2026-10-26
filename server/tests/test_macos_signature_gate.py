"""Ticket propagation may retry; identity, signature and Gatekeeper still gate release."""

import os
import pathlib
import subprocess

import pytest

from conftest import BASH

VERIFIER = pathlib.Path(__file__).resolve().parents[2] / ".github/scripts/verify-macos-signature.sh"


@pytest.mark.parametrize("mode,available_after,expected,lookups", [
    ("valid", 1, 0, 1),
    ("valid", 3, 0, 3),
    ("valid", 99, 1, 12),
    ("broken", 1, 1, 0),
    ("wrong-team", 1, 1, 0),
    ("gatekeeper-rejects", 1, 1, 1),
    ("no-runtime", 1, 1, 1),
    ("no-timestamp", 1, 1, 1),
])
def test_notarization_ticket_retry_remains_a_strict_gate(tmp_path, mode, available_after, expected, lookups):
    tools = tmp_path / "tools"
    tools.mkdir()
    mock = tools / "codesign"
    mock.write_text('''#!/bin/bash
case " $* " in
  *" --check-notarization "*)
    calls=0
    if [ -f "$SIGNATURE_TEST_COUNTER" ]; then calls=$(cat "$SIGNATURE_TEST_COUNTER"); fi
    calls=$((calls + 1))
    echo "$calls" > "$SIGNATURE_TEST_COUNTER"
    [ "$calls" -ge "$SIGNATURE_TEST_AVAILABLE_AFTER" ]
    exit $? ;;
  *" --entitlements "*) exit 0 ;;
  *" --verify "*)
    [ "$SIGNATURE_TEST_MODE" != broken ] || exit 1
    case " $* " in
      *" -R "*) [ "$SIGNATURE_TEST_MODE" != wrong-team ] || exit 1 ;;
    esac
    exit 0 ;;
esac
if [ "$SIGNATURE_TEST_MODE" = no-runtime ]; then
  echo "CodeDirectory flags=0"
else
  echo "CodeDirectory flags=0x10000(runtime)"
fi
[ "$SIGNATURE_TEST_MODE" = no-timestamp ] || echo "Timestamp=Oct 26 2026"
exit 0
''')
    mock.chmod(0o755)
    spctl = tools / "spctl"
    spctl.write_text('#!/bin/sh\n[ "$SIGNATURE_TEST_MODE" != gatekeeper-rejects ]\n')
    spctl.chmod(0o755)
    sleep = tools / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    binary = tmp_path / "signed-fixture"
    binary.touch()
    counter = tmp_path / "lookups"
    result = subprocess.run(args=[BASH, str(VERIFIER), str(binary)], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                                 "APPLE_TEAM_ID": "test-team", "SIGNATURE_TEST_MODE": mode,
                                 "SIGNATURE_TEST_COUNTER": str(counter),
                                 "SIGNATURE_TEST_AVAILABLE_AFTER": str(available_after)})
    assert result.returncode == expected, result.stdout + result.stderr
    assert (int(counter.read_text()) if counter.exists() else 0) == lookups
