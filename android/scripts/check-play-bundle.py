#!/usr/bin/env python3
"""Check the actual AAB's native library hashes against verified Play build artifacts."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
bundle = Path(sys.argv[1])
with zipfile.ZipFile(bundle) as archive:
    libraries = [name for name in archive.namelist() if name.startswith("base/lib/") and name.endswith("/libhardcharger_android.so")]
    if not libraries:
        raise SystemExit("No embedded server in the bundle")
    for name in libraries:
        abi = name.split("/")[2]
        metadata = json.loads((root / f"app/src/play/jniLibs/{abi}.json").read_text())
        if metadata["distribution"] != "play" or hashlib.sha256(archive.read(name)).hexdigest() != metadata["sha256"]:
            raise SystemExit(f"Bundle does not contain the verified Play native artifact: {abi}")
        print(f"Verified bundled-only native library: {abi}")
    signature = any(name.startswith("META-INF/") and name.endswith((".RSA", ".EC", ".DSA")) for name in archive.namelist())
    if "--require-signed" in sys.argv[2:] and not signature:
        raise SystemExit("Signed release requested, but bundle has no signature")
    print("Bundle has a signature; verify it with jarsigner." if signature else "Bundle is UNSIGNED: local review only, not upload-ready.")
