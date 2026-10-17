"""Serves a DuckDB 2.0 database over Quack (DuckDB's own remote protocol) until killed.

DuckDB 2.0.0.dev2610161340 is DuckDB v2.0.0-alpha48624 (ffe0b8b7a2), the commit
Hardcharger builds against; its core repository serves Quack e7a9be3f8c, the commit that
DuckDB's .github/config/extensions/quack.cmake pins.
"""

import argparse
import os
import signal
import threading

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--listen", default="quack:0.0.0.0:9494")
    parser.add_argument("--plaintext", action="store_true", help="serve plain HTTP, no TLS")
    args = parser.parse_args()
    con = duckdb.connect(database=args.database)
    con.execute(query=f"SET threads = {args.threads}")
    con.execute(query=f"SET memory_limit = '{args.memory_limit}'")
    con.execute(query="LOAD quack")
    if args.plaintext:  # Quack refuses certificate files when it serves plain HTTP
        row = con.execute(
            query="""CALL quack_serve(?, allow_other_hostname => true, disable_ssl => true,
                     token => ?)""",
            parameters=[args.listen, os.environ["BENCH_PASSWORD"]],
        ).fetchall()
    else:
        row = con.execute(
            query="""CALL quack_serve(?, allow_other_hostname => true, disable_ssl => false,
                     ssl_cert_file => ?, ssl_key_file => ?, token => ?)""",
            parameters=[args.listen, args.cert, args.key, os.environ["BENCH_PASSWORD"]],
        ).fetchall()
    print("quack_serve:", [r[:2] for r in row], flush=True)
    print(duckdb.__version__, con.execute(query="PRAGMA version").fetchall(), flush=True)
    print(
        con.execute(
            query="SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'quack'"
        ).fetchall(),
        flush=True,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()


if __name__ == "__main__":
    main()
