"""``python -m hardcharger``: download the server, print its path, or run it.

python -m hardcharger download [--version X.Y.Z]   download (if needed); print the path
python -m hardcharger server [ARGS...]             run hardcharger-server with ARGS
"""

from __future__ import annotations

import argparse
import os
import sys

from hardcharger._install import InstallError, ensure_server


def server_main(argv: list[str] | None = None) -> int:
    """The ``hardcharger-server`` console script: run the server binary with ``argv``."""
    args = sys.argv[1:] if argv is None else argv
    try:
        binary = ensure_server()
    except InstallError as error:
        print(f"hardcharger: {error}", file=sys.stderr)
        return 1
    if os.name == "nt":  # no exec on Windows: wait for it instead
        import subprocess

        return subprocess.call(args=[str(binary), *args])
    os.execv(binary, [str(binary), *args])
    return 0  # not reached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hardcharger")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser(name="download", help="download the server; print its path")
    download.add_argument("--version", help="server version (default: the one this package pins)")
    commands.add_parser(name="server", help="run hardcharger-server", add_help=False)
    args, rest = parser.parse_known_args(args=argv)
    if args.command == "server":
        return server_main(argv=rest)
    if rest:
        parser.error(message=f"unrecognized arguments: {' '.join(rest)}")
    try:
        print(ensure_server(version=args.version))
    except InstallError as error:
        print(f"hardcharger: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
