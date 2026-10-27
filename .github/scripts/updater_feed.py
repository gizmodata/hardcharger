#!/usr/bin/env python3
"""Write latest.json, the desktop app's update feed (gizmodata/hardcharger#7), for a release.

    updater_feed.py --version 2.2.0 --notes release-notes.md --dist dist \\
        --base-url https://github.com/gizmodata/hardcharger/releases/download/v2.2.0 \\
        --config desktop/src-tauri/tauri.conf.json > dist/latest.json

Every platform's update package and its .sig (desktop.yml signs them with `tauri signer sign
--app-version`) must be in --dist. Each signature must verify with the public key in
tauri.conf.json and name this version in its signed comment, which is what the app's updater
checks (`requireSignedVersion`) before installing. A release whose feed the app would reject
fails here instead. Uses the standard library and the minisign command (apt: minisign).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The updater looks for "{os}-{arch}-{installer}", then "{os}-{arch}".
PACKAGES = {
    "darwin-aarch64": "Hardcharger-desktop-macos-arm64.app.tar.gz",
    "windows-x86_64": "Hardcharger-desktop-windows-amd64.msi",
    "windows-aarch64": "Hardcharger-desktop-windows-arm64.msi",
}
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-rc\.[1-9]\d*)?$")


def fail(message: str) -> None:
    raise SystemExit(f"updater_feed.py: {message}")


def tauri_text(value: str, what: str) -> str:
    """Tauri stores minisign keys and signatures as base64 of the minisign file's text."""
    try:
        return base64.b64decode(value.strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        fail(f"{what} is not a base64 minisign file")


def verify(package: Path, signature: str, pubkey: str, version: str) -> None:
    lines = tauri_text(signature, f"{package.name}.sig").splitlines()
    comment = next((line[len("trusted comment: "):] for line in lines if line.startswith("trusted comment: ")), "")
    if f"version:{version}" not in comment.split("\t"):
        fail(f"{package.name}.sig isn't signed for version {version} (trusted comment: {comment!r})")
    key = tauri_text(pubkey, "the updater public key").splitlines()
    if len(key) < 2 or not key[0].startswith("untrusted comment: minisign public key"):
        fail("the updater public key in tauri.conf.json is not a minisign public key")
    with tempfile.TemporaryDirectory() as directory:
        minisig = Path(directory) / "package.minisig"
        minisig.write_text("\n".join(lines) + "\n")
        result = subprocess.run(["minisign", "-V", "-q", "-P", key[1].strip(), "-m", str(package), "-x", str(minisig)],
                                capture_output=True, text=True)
    if result.returncode:
        fail(f"{package.name}: the signature doesn't verify with the updater key: {(result.stderr or result.stdout).strip()}")


def feed(version: str, notes: str, dist: Path, base_url: str, pubkey: str, pub_date: str) -> dict:
    if not VERSION.match(version):
        fail(f'"{version}" is not a release version (X.Y.Z or X.Y.Z-rc.N)')
    if shutil.which("minisign") is None:
        fail("minisign is not installed (apt-get install minisign)")
    platforms = {}
    for target, name in PACKAGES.items():
        package, signature = dist / name, dist / f"{name}.sig"
        if not package.is_file() or not signature.is_file():
            fail(f"{name} and {name}.sig must both be in {dist}")
        text = signature.read_text().strip()
        verify(package=package, signature=text, pubkey=pubkey, version=version)
        platforms[target] = {"signature": text, "url": f"{base_url.rstrip('/')}/{name}"}
    return {"version": version, "notes": notes.strip(), "pub_date": pub_date, "platforms": platforms}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    pubkey = json.loads(args.config.read_text())["plugins"]["updater"]["pubkey"]
    pub_date = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = feed(version=args.version, notes=args.notes.read_text(), dist=args.dist, base_url=args.base_url,
                  pubkey=pubkey, pub_date=pub_date)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
