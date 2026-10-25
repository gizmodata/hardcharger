"""Extract a trusted native archive and stage it for Tauri (no downloads here)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("archive", type=Path)
parser.add_argument("--checksums", type=Path)
parser.add_argument("--target", required=True)
args = parser.parse_args()
if args.checksums:
    expected = dict(line.split(None, 1)[::-1] for line in args.checksums.read_text().splitlines() if line.strip())
    expected = {name.strip().lstrip("*"): value for name, value in expected.items()}
    if hashlib.sha256(args.archive.read_bytes()).hexdigest() != expected.get(args.archive.name):
        raise SystemExit("Native archive checksum verification failed")
stage = args.archive.parent / "unpacked"
stage.mkdir(exist_ok=True)
if args.archive.suffix == ".zip":
    with zipfile.ZipFile(args.archive) as archive:
        for name in archive.namelist():
            if not (stage / name).resolve().is_relative_to(stage.resolve()):
                raise SystemExit("Unsafe archive member")
        archive.extractall(stage)
else:
    with tarfile.open(args.archive) as archive:
        archive.extractall(stage, filter="data")
suffix = ".exe" if "windows" in args.target else ""
servers = list(stage.glob(f"*/bin/hardcharger-server{suffix}"))
if len(servers) != 1:
    raise SystemExit("Expected exactly one server in the native archive")
server = servers[0].resolve()
environment = {**os.environ, "HARDCHARGER_DESKTOP_SERVER": str(server), "HARDCHARGER_DESKTOP_TARGET": args.target}
subprocess.run(["node", "scripts/prepare.mjs"], cwd=Path(__file__).resolve().parents[1], env=environment, check=True)
if os.getenv("GITHUB_ENV"):
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"HARDCHARGER_DESKTOP_TEST_SERVER={server}\n")
