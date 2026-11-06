#!/usr/bin/env python3
"""PATH shim: retry only transient Apple timestamp failures while signing.

All other codesign operations and errors retain their original exit status. The real
tool is addressed absolutely so this shim cannot invoke itself. Never log arguments:
callers may pass keychain paths or identities that should not be repeated in CI logs.
"""
import subprocess
import sys
import time


DELAYS = (10, 20, 40, 60, 60)
TIMESTAMP_UNAVAILABLE = b"the timestamp service is not available"
PERMANENT_ERRORS = (
    b"no identity found", b"not found in keychain", b"errsec", b"cssmerr",
    b"user interaction is not allowed", b"invalid entitlements",
)


def codesign(args, *, run=subprocess.run, sleep=time.sleep, stdout=None, stderr=None):
    stdout = sys.stdout.buffer if stdout is None else stdout
    stderr = sys.stderr.buffer if stderr is None else stderr
    signing = any(arg in ("--sign", "-s") or arg.startswith("--sign=")
                  or (arg.startswith("-s") and len(arg) > 2) for arg in args)
    # Verification is always one attempt, even if an unusual invocation mixes flags.
    signing = signing and not any(arg in ("--verify", "-v") for arg in args)
    for attempt in range(len(DELAYS) + 1):
        result = run(args=["/usr/bin/codesign", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout.write(result.stdout)
        stdout.flush()
        stderr.write(result.stderr)
        stderr.flush()
        diagnostic = (result.stdout + result.stderr).lower()
        retry = (signing and result.returncode != 0 and TIMESTAMP_UNAVAILABLE in diagnostic
                 and not any(error in diagnostic for error in PERMANENT_ERRORS))
        if not retry or attempt == len(DELAYS):
            return result.returncode
        delay = DELAYS[attempt]
        stderr.write(f"Apple timestamp service unavailable; retrying codesign in {delay}s "
                     f"(attempt {attempt + 2}/{len(DELAYS) + 1}).\n".encode())
        stderr.flush()
        sleep(delay)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(codesign(args=sys.argv[1:]))
