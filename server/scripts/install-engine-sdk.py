#!/usr/bin/env python3
"""Install a verified Hardcharger Engine release SDK into the dependency prefix."""

import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

REPO = "https://github.com/gizmodata/hardcharger-engine"


def download(url, dest):
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
                    "--output", str(dest), url], check=True)


def install(archive, checksums, commit, platform, destination, stage):
    name = archive.name
    entries = re.findall(r"^([a-f0-9]{64})  (?:\./)?" + re.escape(name) + "$", checksums, re.MULTILINE)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if entries != [digest.hexdigest()]:
        raise ValueError(f"Checksum mismatch or missing checksum for {name}")
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(stage, filter="data")
    metadata = stage / "share/hardcharger-engine"
    if (metadata / "commit").read_text().strip() != commit or (metadata / "platform").read_text().strip() != platform:
        raise ValueError(f"SDK provenance differs from requested {platform} at {commit}")
    if not (stage / "include/duckdb_v2.h").is_file():
        raise ValueError("SDK is missing its embedded API header")
    previous = stage.parent / "previous"
    if destination.exists():
        destination.rename(previous)
    try:
        stage.rename(destination)
    except OSError:
        if previous.exists():
            previous.rename(destination)
        raise


def main(platform, deps):
    pin = Path(__file__).with_name("duckdb-source.sh").read_text()
    commit = re.search(r"^DUCKDB_COMMIT=([a-f0-9]{40})$", pin, re.MULTILINE).group(1)
    tag = re.search(r"^HARDCHARGER_ENGINE_TAG=(v[0-9.]+)$", pin, re.MULTILINE).group(1)
    ref = "refs/tags/" + tag
    output = subprocess.check_output(["git", "ls-remote", REPO + ".git", ref, ref + "^{}"], text=True)
    refs = {item: sha for sha, item in (line.split() for line in output.splitlines())}
    if refs.get(ref + "^{}", refs.get(ref)) != commit:
        raise ValueError(f"Published engine tag {tag} differs from the pinned commit {commit}")
    deps.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="engine-sdk-", dir=deps) as temp:
        work = Path(temp)
        filename = f"hardcharger-engine-sdk-{platform}.tar.gz"
        base = f"{REPO}/releases/download/{tag}/"
        download(base + "SHA256SUMS", work / "SHA256SUMS")
        download(base + filename, work / filename)
        install(work / filename, (work / "SHA256SUMS").read_text(), commit, platform,
                deps / "duckdb-install", work / "new")
    print(f"Installed {platform} engine SDK at {commit}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]).resolve())
