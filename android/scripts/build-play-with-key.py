#!/usr/bin/env python3
"""Build locally with credentials kept outside the checkout; never upload anything."""
import argparse
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--signing-directory", type=Path, required=True)
parser.add_argument("--build-number", type=int, required=True)
parser.add_argument("--abi", choices=("arm64-v8a", "x86_64"), default="arm64-v8a")
args = parser.parse_args()
if not 0 < args.build_number <= 2100000000:
    parser.error("build-number must be a positive Play version code, at most 2100000000")
root = Path(__file__).resolve().parents[2]
directory = args.signing_directory.expanduser().resolve()
if directory == root or root in directory.parents:
    parser.error("keep signing material outside the repository")
credentials_file = directory / "credentials.json"
if os.name == "posix" and credentials_file.stat().st_mode & 0o077:
    parser.error("credentials.json must be readable only by its owner (chmod 600)")
credentials = json.loads(credentials_file.read_text())
keystore = (directory / credentials["keystore"]).resolve()
if not keystore.is_file():
    parser.error("upload keystore does not exist")
if os.name == "posix" and keystore.stat().st_mode & 0o077:
    parser.error("upload keystore must be readable only by its owner (chmod 600)")
environment = dict(os.environ)
environment.update({
    "HARDCHARGER_ANDROID_KEYSTORE": str(keystore),
    "HARDCHARGER_ANDROID_STORE_PASSWORD": credentials["store_password"],
    "HARDCHARGER_ANDROID_KEY_ALIAS": credentials["alias"],
    "HARDCHARGER_ANDROID_KEY_PASSWORD": credentials["key_password"],
    "HARDCHARGER_ANDROID_BUILD_NUMBER": str(args.build_number),
})
subprocess.run([str(root / "android/scripts/build-play.sh"), args.abi],
               cwd=root, env=environment, check=True)
