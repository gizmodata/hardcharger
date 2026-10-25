"""Record exact preview/release provenance and checksums, without publishing anything."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

platform = sys.argv[1]
desktop = Path(__file__).resolve().parents[1]
artifacts = desktop / "artifacts"
assets = sorted(path for path in artifacts.iterdir() if path.suffix in {".dmg", ".msi", ".zip"})
if not assets:
    raise SystemExit("No desktop installers were built")
checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets}
(artifacts / f"SHA256SUMS-desktop-{platform}").write_text("".join(f"{digest}  {name}\n" for name, digest in checksums.items()))
manifest = {
    "platform": platform,
    "desktop_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "version": json.loads((desktop / "src-tauri/tauri.conf.json").read_text())["version"],
    "signed": os.getenv("DESKTOP_SIGNED") == "true",
    "native_binaries": "server workflow artifacts" if os.getenv("DESKTOP_NATIVE_RUN") else "matching published release (SHA256SUMS verified)",
    "native_run": os.getenv("DESKTOP_NATIVE_RUN") or None,
    "workflow_run": os.getenv("GITHUB_RUN_ID"),
    "assets": checksums,
}
(artifacts / f"desktop-{platform}.json").write_text(json.dumps(manifest, indent=2) + "\n")
