#!/usr/bin/env python3
"""Stage pinned native drivers, manifests and notices for one or both Linux arches.

Requires gh (already authenticated for private pre-release testing). GitHub asset
digests are checked before unpacking. No credentials are passed into Docker.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


# Match GizmoSQL's driver set, adding Hardcharger. dbc supplies the other ten.
DRIVERS = {
    "gizmosql": ("gizmosql-adbc", "v2.0.15", "AdbcDriverGizmoSQLInit"),
    "oracle": ("adbc-driver-oracle", "v1.0.1", "OracleDriverInit"),
    "db2": ("adbc-driver-db2", "v0.2.1", "Db2DriverInit"),
    "quack": ("adbc-driver-quack", "v0.1.0-alpha.11", "QuackDriverInit"),
    "hardcharger": ("hardcharger-adbc", "v2.0.0", "AdbcDriverHardchargerInit"),
}


def gh(*args):
    return subprocess.check_output(args=["gh", *args])


def prepare(*, output, arch, hardcharger_tag):
    root = output / f"linux_{arch}"
    for directory in ("lib", "manifests", "notices"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    inventory = {}
    for name, (repo, version, entrypoint) in DRIVERS.items():
        if name == "hardcharger":
            version = hardcharger_tag
        release = json.loads(gh("release", "view", version, "--repo", f"gizmodata/{repo}",
                                "--json", "assets"))
        suffix = f"-linux_{arch}.tar.gz" if name in ("gizmosql", "hardcharger") else f"-linux-{arch}.so"
        assets = [asset for asset in release["assets"]
                  if asset["name"].startswith(f"libadbc_driver_{name}-") and asset["name"].endswith(suffix)]
        if len(assets) != 1:
            raise RuntimeError(f"Expected one {name} {arch} library, found {len(assets)}")
        asset = assets[0]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
            raise RuntimeError(f"Missing SHA-256 digest: {asset['name']}")
        with tempfile.TemporaryDirectory() as temporary:
            gh("release", "download", version, "--repo", f"gizmodata/{repo}",
               "--pattern", asset["name"], "--dir", temporary)
            payload = Path(temporary, asset["name"]).read_bytes()
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            if digest != asset["digest"]:
                raise RuntimeError(f"Digest mismatch: {asset['name']}")
            notices = root / "notices" / name
            notices.mkdir(exist_ok=True)
            library = root / "lib" / f"libadbc_driver_{name}.so"
            if asset["name"].endswith(".tar.gz"):
                with tarfile.open(name=Path(temporary, asset["name"]), mode="r:gz") as archive:
                    members = [member for member in archive.getmembers() if member.isfile()]
                    libraries = [member for member in members if Path(member.name).name == library.name]
                    if len(libraries) != 1:
                        raise RuntimeError(f"Expected one library inside {asset['name']}")
                    library.write_bytes(archive.extractfile(member=libraries[0]).read())
                    for member in members:
                        basename = Path(member.name).name
                        if basename.startswith(("LICENSE", "NOTICE", "THIRD_PARTY")):
                            (notices / basename).write_bytes(archive.extractfile(member=member).read())
                    templates = [member for member in members if Path(member.name).name == f"{name}.toml.in"]
                    if len(templates) != 1:
                        raise RuntimeError(f"Missing {name} manifest template")
                    manifest = archive.extractfile(member=templates[0]).read().decode()
                    manifest = manifest.replace("@PREFIX@", "/usr/local/lib").replace("@VERSION@", version.removeprefix("v"))
            else:
                library.write_bytes(payload)
                # These releases ship bare libraries; retain the release's license.
                (notices / "LICENSE").write_bytes(gh("api", "-H", "Accept: application/vnd.github.raw+json",
                    f"repos/gizmodata/{repo}/contents/LICENSE?ref={version}"))
                manifest = f"""manifest_version = 1
name = '{name}'
version = '{version.removeprefix('v')}'
publisher = 'GizmoData'
license = 'Apache-2.0'
url = 'https://github.com/gizmodata/{repo}'
[ADBC]
version = '1.1.0'
[Driver]
entrypoint = '{entrypoint}'
[Driver.shared]
linux_amd64 = '/usr/local/lib/{library.name}'
linux_arm64 = '/usr/local/lib/{library.name}'
"""
            library.chmod(mode=0o755)
            (root / "manifests" / f"{name}.toml").write_text(data=manifest)
            inventory[name] = {"repository": f"gizmodata/{repo}", "tag": version,
                               "asset": asset["name"], "digest": digest}
            print(f"Verified {name} {version} linux/{arch}: {digest}", flush=True)
    (root / "notices" / "native-drivers.json").write_text(data=json.dumps(obj=inventory, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("drivers"))
    parser.add_argument("--arch", choices=("amd64", "arm64"), action="append")
    parser.add_argument("--hardcharger-tag", default="v2.0.0")
    options = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", options.hardcharger_tag):
        parser.error(message="Invalid driver release tag")
    for architecture in options.arch or ("amd64", "arm64"):
        prepare(output=options.output, arch=architecture, hardcharger_tag=options.hardcharger_tag)
