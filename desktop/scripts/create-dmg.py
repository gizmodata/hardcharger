"""Make a drag-to-Applications disk image without sending Apple Events to Finder.

Run with Python 3 and dmgbuild (uv run --with dmgbuild ...), which writes Finder's layout and
the background picture (an arrow from the app to Applications, desktop/src-tauri/installer/
dmg-background.png and @2x, from create-dmg-background.py) into the mounted image. The app
must already be signed/notarized for a signed build. CI signs and notarizes the enclosing DMG
too.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import secrets
import shlex
import re
import tempfile
import time
import dmgbuild

parser = argparse.ArgumentParser()
parser.add_argument("app", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.parent.mkdir(parents=True, exist_ok=True)

def staple_and_validate(path):
    # Apple's ticket-delivery service can drop a connection just after acceptance.
    # A retry still requires a real ticket and successful validation; it never skips it.
    for attempt in range(3):
        if subprocess.run(["xcrun", "stapler", "staple", str(path)]).returncode == 0:
            if subprocess.run(["xcrun", "stapler", "validate", str(path)]).returncode == 0:
                return
        if attempt < 2:
            time.sleep(10)
    raise SystemExit(f"Could not staple and validate {path.name} after three attempts")

if os.getenv("DESKTOP_SIGNED") == "true":
    # Do not rely on a bundler's successful notarization log as proof of a local ticket.
    # Staple before copying the app into the disk image or the standalone app archive.
    staple_and_validate(args.app)

# A background picture is an alias to a file on the mounted volume itself, so the layout
# is written into the mounted image (dmgbuild), not into a staging folder. The window,
# icon positions and size must match create-dmg-background.py's drawing.
installer = Path(__file__).resolve().parents[1] / "src-tauri/installer"
args.destination.unlink(missing_ok=True)
dmgbuild.build_dmg(filename=str(args.destination), volume_name="Hardcharger", settings={
    "format": "UDZO", "filesystem": "HFS+",
    # dmgbuild copies with ditto, which keeps the signature and the stapled ticket.
    "files": [(str(args.app), "Hardcharger.app")],
    "symlinks": {"Applications": "/Applications"},
    "icon": str(args.app / "Contents/Resources/icon.icns"),
    # dmg-background@2x.png beside it makes this a Retina TIFF (tiffutil -cathidpicheck).
    "background": str(installer / "dmg-background.png"),
    "window_rect": ((200, 200), (660, 400)),
    "default_view": "icon-view", "show_status_bar": False, "show_tab_view": False,
    "show_toolbar": False, "show_pathbar": False, "show_sidebar": False,
    "icon_size": 104, "text_size": 13, "grid_spacing": 100, "label_pos": "bottom",
    "arrange_by": None, "show_icon_preview": True,
    "icon_locations": {"Hardcharger.app": (180, 180), "Applications": (480, 180)},
})
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
        previous_keychains = shlex.split(run_private(["security", "list-keychains", "-d", "user"], capture=True))
        run_private(["security", "create-keychain", "-p", password, keychain])
        try:
            run_private(["security", "unlock-keychain", "-p", password, keychain])
            run_private(["security", "set-keychain-settings", "-lut", "3600", keychain])
            run_private(["security", "import", str(certificate), "-k", keychain, "-P", os.environ["APPLE_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign"])
            run_private(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, keychain])
            # codesign also looks up the private key through the user's search list,
            # even when --keychain restricts its certificate lookup.
            run_private(["security", "list-keychains", "-d", "user", "-s", keychain, *previous_keychains])
            identities = run_private(["security", "find-identity", "-v", "-p", "codesigning", keychain], capture=True)
            identity = next((match.group(1) for line in identities.splitlines()
                             if os.environ["APPLE_SIGNING_IDENTITY"] in line
                             and (match := re.search(r"\b([0-9A-Fa-f]{40})\b", line))), None)
            if not identity:
                raise SystemExit("No valid Developer ID identity in the temporary signing keychain")
            run_private(["codesign", "--force", "--timestamp", "--keychain", keychain, "--sign", identity, str(args.destination)])
        finally:
            run_private(["security", "list-keychains", "-d", "user", "-s", *previous_keychains])
            run_private(["security", "delete-keychain", keychain])
    output = run_private(["xcrun", "notarytool", "submit", str(args.destination), "--apple-id", os.environ["APPLE_ID"],
                          "--password", os.environ["APPLE_PASSWORD"], "--team-id", os.environ["APPLE_TEAM_ID"],
                          "--wait", "--timeout", "15m", "--output-format", "json"], capture=True)
    receipt = json.loads(output)
    if receipt.get("status") != "Accepted":
        raise SystemExit(f"Disk image notarization was not accepted: {receipt.get('status')} ({receipt.get('id')})")
    print(f"Disk image notarization: {receipt['status']} ({receipt['id']})")
    staple_and_validate(args.destination)
