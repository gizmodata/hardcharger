#!/usr/bin/env python3
"""Tests of .github/scripts/updater_feed.py with a throwaway minisign key, in Tauri's format
(base64 of the minisign files). server.yml's `changes` job runs it on every run; the release
job runs the script itself on the real, signed packages.

    python3 .github/scripts/test_updater_feed.py     # needs minisign (apt-get install minisign)
"""

from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import updater_feed  # noqa: E402

BASE = "https://github.com/gizmodata/hardcharger/releases/download/v2.2.0"


def wrap(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


class FeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="updater-feed-"))
        self.dist = self.directory / "dist"
        self.dist.mkdir()
        self.pubkey = self.key("release")
        for name in updater_feed.PACKAGES.values():
            (self.dist / name).write_bytes(f"package {name}".encode())
            self.sign(name=name, key="release", version="2.2.0")

    def key(self, name: str) -> str:
        subprocess.run(["minisign", "-G", "-W", "-f", "-p", str(self.directory / f"{name}.pub"),
                        "-s", str(self.directory / f"{name}.key")], check=True, capture_output=True)
        return wrap(self.directory / f"{name}.pub")

    def sign(self, name: str, key: str, version: str) -> None:
        # What `tauri signer sign --app-version` writes: the version in the signed comment.
        minisig = self.directory / f"{name}.minisig"
        subprocess.run(["minisign", "-S", "-s", str(self.directory / f"{key}.key"), "-m", str(self.dist / name),
                        "-x", str(minisig), "-t", f"timestamp:1798000000\tfile:{name}\tversion:{version}"],
                       check=True, capture_output=True)
        (self.dist / f"{name}.sig").write_text(wrap(minisig))

    def feed(self, version: str = "2.2.0") -> dict:
        return updater_feed.feed(version=version, notes="\n### Fixed\n\n- Something\n", dist=self.dist,
                                 base_url=BASE + "/", pubkey=self.pubkey, pub_date="2026-11-02T00:00:00Z")

    def test_a_signed_release_has_every_platform(self) -> None:
        result = self.feed()
        self.assertEqual(result["version"], "2.2.0")
        self.assertEqual(result["notes"], "### Fixed\n\n- Something")
        self.assertEqual(set(result["platforms"]), {"darwin-aarch64", "windows-x86_64", "windows-aarch64"})
        mac = result["platforms"]["darwin-aarch64"]
        self.assertEqual(mac["url"], f"{BASE}/Hardcharger-desktop-macos-arm64.app.tar.gz")
        self.assertEqual(mac["signature"], (self.dist / "Hardcharger-desktop-macos-arm64.app.tar.gz.sig").read_text())

    def test_a_changed_package_fails(self) -> None:
        (self.dist / "Hardcharger-desktop-windows-arm64.msi").write_bytes(b"something else")
        with self.assertRaisesRegex(SystemExit, "windows-arm64.msi: the signature doesn't verify"):
            self.feed()

    def test_another_key_fails(self) -> None:
        self.key("other")
        self.sign(name="Hardcharger-desktop-macos-arm64.app.tar.gz", key="other", version="2.2.0")
        with self.assertRaisesRegex(SystemExit, "macos-arm64.app.tar.gz: the signature doesn't verify"):
            self.feed()

    def test_a_signature_for_another_version_fails(self) -> None:
        # An older package's genuine signature can't be republished as a newer version.
        self.sign(name="Hardcharger-desktop-windows-amd64.msi", key="release", version="2.1.9")
        with self.assertRaisesRegex(SystemExit, "isn't signed for version 2.2.0"):
            self.feed()

    def test_a_missing_package_or_signature_fails(self) -> None:
        (self.dist / "Hardcharger-desktop-macos-arm64.app.tar.gz.sig").unlink()
        with self.assertRaisesRegex(SystemExit, "must both be in"):
            self.feed()

    def test_only_release_versions(self) -> None:
        for bad in ("v2.2.0", "2.2", "2.2.0-beta.1", "python-v2.2.0"):
            with self.assertRaisesRegex(SystemExit, "is not a release version"):
                self.feed(version=bad)

    def test_a_placeholder_key_fails(self) -> None:
        self.pubkey = "REPLACE_WITH_THE_UPDATER_PUBLIC_KEY"
        with self.assertRaisesRegex(SystemExit, "not a base64 minisign file|not a minisign public key"):
            self.feed()


if __name__ == "__main__":
    unittest.main()
