#!/usr/bin/env python3
"""The app's advertised macOS minimum must cover every bundled executable."""

import pathlib
import plistlib
import re
import subprocess
import sys


def version(value):
    parts = [int(part) for part in value.split(".")]
    return tuple((parts + [0, 0, 0])[:3])


app = pathlib.Path(sys.argv[1])
with (app / "Contents/Info.plist").open("rb") as file:
    info = plistlib.load(fp=file)
minimum = info["LSMinimumSystemVersion"]
for name in (info["CFBundleExecutable"], "hardcharger-server", "hardcharger"):
    binary = app / "Contents/MacOS" / name
    output = subprocess.check_output(args=["xcrun", "vtool", "-show-build", str(binary)], text=True)
    requirements = re.findall(r"^\s*minos\s+([\d.]+)\s*$", output, re.MULTILINE)
    if not requirements:
        sys.exit(f"Cannot determine the macOS minimum of {binary}")
    for required in requirements:
        if version(value=required) > version(value=minimum):
            sys.exit(f"{name} requires macOS {required}, but the app advertises {minimum}")
print(f"macOS {minimum}+ covers the desktop app, server and CLI")
