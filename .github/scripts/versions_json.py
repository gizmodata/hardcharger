#!/usr/bin/env python3
"""versions.json: what a Hardcharger release is made of, for machines (GizmoData Cloud).

    .github/scripts/versions_json.py [--root <checkout>] [--binary <hardcharger-server>] [--tag <tag>]

Prints one JSON document, read from the checkout (never from the network):

    hardcharger  version (.github/scripts/release.sh version) and the checkout's commit
    warp_sql     the submodule's commit (the one the checkout records, which must be the one
                 checked out), its SDK version (cpp/CMakeLists.txt) and its specification
                 version (spec/openapi.yaml info.version)
    duckdb       the version name the build reports (PRAGMA version; the extension
                 repository's path) and the commit, from warp-sql's build-duckdb.sh
    extensions   the extensions built into the server: DuckDB's own and in-tree ones by
                 name, the out-of-tree ones with the repository and commit
                 (server/scripts/duckdb-extensions.cmake), and the repository INSTALL
                 fetches the others from

--binary also runs that server's --version and --print-duckdb-version and fails unless they
say the same versions, so the document describes the binaries it ships with. --tag fails
unless the tag is v<hardcharger version>. Any value it can't read exactly is an error
(exit 1, the reason on stderr), never a guess or an empty field.

server.yml's build job writes it next to the Linux amd64 tarball (artifact
hardcharger-versions) on every run; the release job publishes it as the GitHub Release's
versions.json. Its tests: .github/scripts/test_versions_json.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUCKDB_CORE_EXTENSIONS = ["core_functions", "parquet", "json", "icu"]  # DuckDB always builds these in
SHA = re.compile(r"^[0-9a-f]{40}$")


class Failure(Exception):
    pass


def read(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        raise Failure(f"no {relative}")
    return path.read_text(encoding="utf-8")


def one(pattern: str, text: str, what: str, flags: int = re.M) -> str:
    found = re.findall(pattern, text, flags)
    if len(found) != 1:
        raise Failure(f"{what}: expected exactly one match, found {len(found)}")
    return found[0]


def git(root: Path, *args: str) -> str:
    out = subprocess.run(args=["git", "-C", str(root), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise Failure(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def hardcharger_version(root: Path) -> str:
    out = subprocess.run(args=["bash", str(HERE / "release.sh"), "version", str(root / "server" / "CMakeLists.txt")],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise Failure(out.stderr.strip())
    return out.stdout.strip()


def warp_sql(root: Path) -> dict:
    entry = git(root, "ls-tree", "HEAD", "third_party/warp-sql").split()
    if len(entry) != 4 or entry[0] != "160000" or not SHA.match(entry[2]):
        raise Failure("third_party/warp-sql is not a submodule in HEAD")
    commit = entry[2]
    checked_out = git(root / "third_party" / "warp-sql", "rev-parse", "HEAD")
    if checked_out != commit:
        raise Failure(f"third_party/warp-sql has {checked_out} checked out, HEAD records {commit}")
    sdk = one(r"^project\(warpsql VERSION ([0-9]+\.[0-9]+\.[0-9]+)\b", read(root, "third_party/warp-sql/cpp/CMakeLists.txt"),
              "warp-sql's project(warpsql VERSION ...)")
    spec = one(r"^info:\n(?:  .*\n)*?  version: ([0-9]+\.[0-9]+\.[0-9]+)$", read(root, "third_party/warp-sql/spec/openapi.yaml"),
               "warp-sql's spec/openapi.yaml info.version")
    return {"version": sdk, "spec_version": spec, "commit": commit}


def duckdb(root: Path) -> dict:
    script = read(root, "third_party/warp-sql/cpp/scripts/build-duckdb.sh")
    commit = one(r"^DUCKDB_COMMIT=([0-9a-f]{40})$", script, "build-duckdb.sh's DUCKDB_COMMIT")
    name = one(r"^DUCKDB_VERSION_NAME=(v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|rc|dev)[0-9]+)?)$", script,
               "build-duckdb.sh's DUCKDB_VERSION_NAME")
    return {"version": name, "commit": commit}


def extensions(root: Path, duckdb_version: str) -> dict:
    text = read(root, "server/scripts/duckdb-extensions.cmake")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    loaded = re.findall(r"duckdb_extension_load\(\s*([a-z0-9_]+)([^)]*)\)", code)
    if not loaded:
        raise Failure("no duckdb_extension_load() in server/scripts/duckdb-extensions.cmake")
    linked = one(r"duckdb_extension_statically_link\(([^)]*)\)", code, "duckdb_extension_statically_link()", flags=0).split()
    in_tree, pinned = [], {}
    for name, body in loaded:
        url = re.findall(r"\bGIT_URL\s+(\S+)", body)
        tag = re.findall(r"\bGIT_TAG\s+(\S+)", body)
        if not url and not tag:
            in_tree.append(name)
            continue
        if len(url) != 1 or len(tag) != 1 or not SHA.match(tag[0]):
            raise Failure(f"{name}: expected one GIT_URL and one GIT_TAG commit in duckdb-extensions.cmake")
        pinned[name] = {"repository": url[0], "commit": tag[0]}
    names = in_tree + list(pinned)
    if sorted(linked) != sorted(names):
        raise Failure(f"duckdb_extension_statically_link({' '.join(linked)}) is not the loaded set ({' '.join(names)})")
    return {
        "built_in": list(dict.fromkeys(DUCKDB_CORE_EXTENSIONS + names)),
        "pinned": pinned,
        "repository": f"https://extensions.duckdb.org/{duckdb_version}",
    }


def check_binary(binary: Path, versions: dict) -> None:
    def run(*args: str) -> str:
        out = subprocess.run(args=[str(binary), *args], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise Failure(f"{binary} {' '.join(args)} exited {out.returncode}: {out.stderr.strip()}")
        return out.stdout.strip()

    said = run("--version")
    if said != f"Hardcharger {versions['hardcharger']['version']}":
        raise Failure(f"{binary} --version says {said!r}, the checkout {versions['hardcharger']['version']}")
    said = run("--print-duckdb-version")
    if said != versions["duckdb"]["version"]:
        raise Failure(f"{binary} --print-duckdb-version says {said!r}, build-duckdb.sh {versions['duckdb']['version']}")


def build(root: Path, binary: Path | None, tag: str | None) -> dict:
    version = hardcharger_version(root)
    if tag is not None and tag != f"v{version}":
        raise Failure(f"tag {tag} != v{version} (server/CMakeLists.txt)")
    commit = git(root, "rev-parse", "HEAD")
    database = duckdb(root)
    versions = {
        "hardcharger": {"version": version, "commit": commit},
        "warp_sql": warp_sql(root),
        "duckdb": database,
        "extensions": extensions(root, database["version"]),
    }
    if binary is not None:
        check_binary(binary, versions)
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=HERE.parents[1])
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    try:
        versions = build(root=args.root.resolve(), binary=args.binary, tag=args.tag)
    except (Failure, OSError, subprocess.TimeoutExpired) as e:
        print(f"versions_json.py: {e}", file=sys.stderr)
        return 1
    print(json.dumps(versions, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
