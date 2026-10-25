"""Make a drag-to-Applications disk image without sending Apple Events to Finder.

Run with Python 3 and ds-store (uv run --with ds-store ...). The app must already be
signed/notarized for a signed build. CI signs and notarizes the enclosing DMG too.
"""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
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
    subprocess.run(["codesign", "--force", "--timestamp", "--sign", os.environ["APPLE_SIGNING_IDENTITY"], str(args.destination)], check=True)
    subprocess.run(["xcrun", "notarytool", "submit", str(args.destination), "--apple-id", os.environ["APPLE_ID"],
                    "--password", os.environ["APPLE_PASSWORD"], "--team-id", os.environ["APPLE_TEAM_ID"], "--wait", "--timeout", "15m"], check=True)
    subprocess.run(["xcrun", "stapler", "staple", str(args.destination)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(args.destination)], check=True)
