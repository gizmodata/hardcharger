"""Mount the finished installer and check the metadata Finder consumes."""

import pathlib
import plistlib
import subprocess
import sys
import tempfile

from ds_store import DSStore


image = pathlib.Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix="hardcharger-dmg-check-") as directory:
    mount = pathlib.Path(directory) / "volume"
    receipt = plistlib.loads(subprocess.check_output(args=[
        "hdiutil", "attach", "-readonly", "-nobrowse", "-plist", "-mountpoint", str(mount), str(image),
    ]))
    device = receipt["system-entities"][0]["dev-entry"]
    try:
        if not (mount / "Hardcharger.app").is_dir():
            sys.exit("Installer has no Hardcharger.app")
        shortcut = mount / "Applications"
        if not shortcut.is_symlink() or shortcut.readlink() != pathlib.Path("/Applications"):
            sys.exit("Installer has no drag-to-Applications shortcut")
        with DSStore.open(str(mount / ".DS_Store"), "r") as store:
            for key in ("bwsp", "icvp"):
                # Finder sends allKeys to these values and crashes on an array.
                if not isinstance(store["."][key], dict):
                    sys.exit(f"Finder's {key} layout must be a dictionary")
            if store["."]["vstl"] != (b"type", b"icnv"):
                sys.exit("Installer must open in icon view")
            for name in ("Hardcharger.app", "Applications"):
                position = store[name]["Iloc"]
                if not isinstance(position, tuple) or len(position) != 2:
                    sys.exit(f"Installer has no icon position for {name}")
        print("Mounted installer: Finder layout dictionaries, icon view, app and Applications shortcut verified")
    finally:
        subprocess.run(args=["hdiutil", "detach", device], check=True)
