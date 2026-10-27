"""Mount the finished installer and check the metadata Finder consumes: the layout, the
drag-to-Applications background picture, and that the app inside still verifies (dmgbuild
copies it with ditto without checking ditto's exit status)."""

import pathlib
import plistlib
import subprocess
import sys
import tempfile

from ds_store import DSStore
from mac_alias import Alias


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
            # The view style: dmgbuild writes it as icvl, the hand-written layout as vstl.
            # A record reads as b"icnv" when iterated and (b"type", b"icnv") when indexed.
            styles = [record.value[-1] if isinstance(record.value, tuple) else record.value
                      for record in store if record.filename == "." and record.code in (b"icvl", b"vstl")]
            if not styles or any(style != b"icnv" for style in styles):
                sys.exit(f"Installer must open in icon view: {styles}")
            for name in ("Hardcharger.app", "Applications"):
                position = store[name]["Iloc"]
                if not isinstance(position, tuple) or len(position) != 2:
                    sys.exit(f"Installer has no icon position for {name}")
            # The arrow: a picture background (type 2) whose alias names a file on this volume.
            view = store["."]["icvp"]
            if view.get("backgroundType") != 2 or "backgroundImageAlias" not in view:
                sys.exit("Installer has no background picture")
            picture = Alias.from_bytes(view["backgroundImageAlias"])
            if picture.volume.name != "Hardcharger" or not (mount / picture.target.filename).is_file():
                sys.exit(f"Installer background points outside the volume: {picture.volume.name}/{picture.target.filename}")
        # One TIFF holding the 1x and 2x pictures (660x400 and 1320x800).
        sizes = subprocess.check_output(args=["sips", "-g", "pixelWidth", "-g", "pixelHeight",
                                              str(mount / picture.target.filename)], text=True)
        if "pixelWidth: 1320" not in sizes and "pixelWidth: 660" not in sizes:
            sys.exit(f"Installer background has an unexpected size:\n{sizes}")
        subprocess.run(args=["codesign", "--verify", "--strict", "--deep", str(mount / "Hardcharger.app")], check=True)
        print("Mounted installer: Finder layout dictionaries, icon view, background picture, "
              "app signature and Applications shortcut verified")
    finally:
        subprocess.run(args=["hdiutil", "detach", device], check=True)
