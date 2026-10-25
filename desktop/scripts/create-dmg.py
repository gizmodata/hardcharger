"""Make a drag-to-Applications disk image without sending Apple Events to Finder.

Run with Python 3 and ds-store (uv run --with ds-store ...). The app must already be
signed/notarized for a signed build. CI signs and notarizes the enclosing DMG too.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import secrets
import tempfile
from ds_store import DSStore

parser = argparse.ArgumentParser()
parser.add_argument("app", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="hardcharger-dmg-") as directory:
    stage = Path(directory)
    subprocess.run(["ditto", str(args.app), str(stage / "Hardcharger.app")], check=True)
    (stage / "Applications").symlink_to("/Applications")
    shutil.copyfile(args.app / "Contents/Resources/icon.icns", stage / ".VolumeIcon.icns")
    with DSStore.open(str(stage / ".DS_Store"), "w+") as store:
        store["Hardcharger.app"]["Iloc"] = (180, 180)
        store["Applications"]["Iloc"] = (480, 180)
        store["."]["bwsp"] = ("blob", plistlib.dumps({
            "ShowStatusBar": False, "ShowSidebar": False, "ShowToolbar": False,
            "ContainerShowSidebar": False, "WindowBounds": "{{200, 200}, {660, 400}}",
        }))
        store["."]["icvp"] = ("blob", plistlib.dumps({
            "viewOptionsVersion": 1, "iconSize": 104.0, "gridSpacing": 100.0,
            "textSize": 13.0, "labelOnBottom": True, "arrangeBy": "none",
            "showIconPreview": True, "backgroundType": 1,
            "backgroundColorRed": 0.96, "backgroundColorGreen": 0.98, "backgroundColorBlue": 1.0,
        }))
        store["."]["vSrn"] = ("long", 1)
    subprocess.run(["hdiutil", "create", "-ov", "-volname", "Hardcharger", "-fs", "HFS+", "-format", "UDZO", "-srcfolder", str(stage), str(args.destination)], check=True)
if os.getenv("DESKTOP_SIGNED") == "true":
    def run_private(command, capture=False):
        # Never put credential-bearing command arguments into an exception traceback.
        result = subprocess.run(command, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, text=True)
        if result.returncode:
            raise SystemExit(f"{Path(command[0]).name} failed with exit code {result.returncode}")
        return result.stdout

    # Tauri deletes its imported temporary keychain when bundling ends. Import the same
    # existing Developer ID into a short-lived keychain for the enclosing disk image.
    with tempfile.TemporaryDirectory(prefix="hardcharger-dmg-sign-") as directory:
        keychain = str(Path(directory) / "signing.keychain-db")
        certificate = Path(directory) / "developer-id.p12"
        certificate.write_bytes(base64.b64decode(os.environ["APPLE_CERTIFICATE"]))
        certificate.chmod(0o600)
        password = secrets.token_hex(32)
        run_private(["security", "create-keychain", "-p", password, keychain])
        try:
            run_private(["security", "unlock-keychain", "-p", password, keychain])
            run_private(["security", "set-keychain-settings", "-lut", "3600", keychain])
            run_private(["security", "import", str(certificate), "-k", keychain, "-P", os.environ["APPLE_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign"])
            run_private(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, keychain])
            run_private(["codesign", "--force", "--timestamp", "--keychain", keychain, "--sign", os.environ["APPLE_SIGNING_IDENTITY"], str(args.destination)])
        finally:
            run_private(["security", "delete-keychain", keychain])
    output = run_private(["xcrun", "notarytool", "submit", str(args.destination), "--apple-id", os.environ["APPLE_ID"],
                          "--password", os.environ["APPLE_PASSWORD"], "--team-id", os.environ["APPLE_TEAM_ID"],
                          "--wait", "--timeout", "15m", "--output-format", "json"], capture=True)
    receipt = json.loads(output)
    if receipt.get("status") != "Accepted":
        raise SystemExit(f"Disk image notarization was not accepted: {receipt.get('status')} ({receipt.get('id')})")
    print(f"Disk image notarization: {receipt['status']} ({receipt['id']})")
    subprocess.run(["xcrun", "stapler", "staple", str(args.destination)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(args.destination)], check=True)
