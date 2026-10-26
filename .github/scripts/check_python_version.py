#!/usr/bin/env python3
"""Check the Python server package tracks its server, with optional Python-only .postN.

Use --server-tag vX.Y.Z for a server release: that requires the exact base package,
without a post suffix. Python tags use --python-tag python-vX.Y.Z[.postN].
Standard library only; also runs in tests on Python 3.10.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def validate(server: str, package: str, runtime: str, pin: str,
             server_tag: str | None = None, python_tag: str | None = None) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?", server):
        raise ValueError(f"invalid server release version: {server}")
    expected = server.replace("-rc.", "rc")
    if not re.fullmatch(re.escape(expected) + r"(?:\.post[1-9]\d*)?", package):
        raise ValueError(f"Python package {package} must be {expected} or {expected}.postN for server {server}")
    if runtime != package:
        raise ValueError(f"hardcharger.__version__ {runtime} != Python metadata {package}")
    if pin != server:
        raise ValueError(f"SERVER_VERSION {pin} != server {server}; a .postN package still pins the base server")
    if server_tag is not None:
        if server_tag != f"v{server}":
            raise ValueError(f"server tag {server_tag} != v{server}")
        if package != expected:
            raise ValueError(f"a server release requires Python package {expected}, not post-release {package}")
    if python_tag is not None and python_tag != f"python-v{package}":
        raise ValueError(f"Python tag {python_tag} != python-v{package}")


def one(path: Path, pattern: str) -> str:
    matches = re.findall(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"{path}: expected exactly one version declaration, found {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--server-tag")
    parser.add_argument("--python-tag")
    args = parser.parse_args()
    try:
        root = args.root
        cmake = root / "server/CMakeLists.txt"
        server = one(cmake, r"^project\(hardcharger VERSION ([\d.]+) ") + one(
            cmake, r'^set\(HARDCHARGER_VERSION_SUFFIX "([^"]*)"\)')
        package = one(root / "python/pyproject.toml", r'^version = "([^"]+)"$')
        runtime = one(root / "python/src/hardcharger/__init__.py", r'^__version__ = "([^"]+)"$')
        pin = one(root / "python/src/hardcharger/_install.py", r'^SERVER_VERSION = "([^"]+)"$')
        validate(server, package, runtime, pin, args.server_tag, args.python_tag)
    except (ValueError, OSError) as error:
        print(f"Python/server version mismatch: {error}", file=sys.stderr)
        return 1
    print(f"Python {package} pins Hardcharger server {server}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
