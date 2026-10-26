#!/usr/bin/env python3
"""Fail if the signed, bundled macOS server cannot load DuckDB extension libraries."""

import plistlib
import subprocess
import sys

KEY = "com.apple.security.cs.disable-library-validation"

for binary in sys.argv[1:]:
    result = subprocess.run(args=["codesign", "-d", "--entitlements", "-", "--xml", binary],
                            capture_output=True, check=True)
    entitlements = plistlib.loads(result.stdout) if result.stdout.strip() else {}
    if entitlements.get(KEY) is not True:
        raise SystemExit(f"{binary}: missing {KEY}=true; DuckDB extension loading would fail")
    print(f"{binary}: DuckDB extension loading entitlement verified")
if len(sys.argv) < 2:
    raise SystemExit("usage: check-extension-entitlement.py BUNDLED-SERVER [SERVER...]")
