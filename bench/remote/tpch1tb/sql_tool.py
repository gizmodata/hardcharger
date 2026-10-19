"""Load the TPC-H tables through a running server, or read its engine details.

    python sql_tool.py --server stable|edge|hardcharger load --scale-factor 1000 --output load.json
    python sql_tool.py --server stable|edge|hardcharger info --output info.json

`load` runs the published eight statements (not timed for the benchmark; the times are kept
for the record), one connection, in the published order:
    CREATE TABLE <t> AS SELECT * FROM read_parquet('/data/tpch/sf<N>/<t>/*.parquet');
`info` records the engine's version and source id (pragma_version), the database file's size
and each table's row count. GizmoSQL goes through adbc-driver-gizmosql (Flight SQL),
Hardcharger through adbc-driver-hardcharger (Warp SQL); both over TLS on 127.0.0.1:31337.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

TABLES = ["lineitem", "orders", "customer", "part", "partsupp", "supplier", "nation", "region"]
PASSWORD = os.environ.get("BENCH_PASSWORD", "bench-password-not-secret")


def connect(server: str):
    if server == "hardcharger":
        from adbc_driver_hardcharger import dbapi
        return dbapi.connect(uri="hardcharger://127.0.0.1:31337", username="bench", password=PASSWORD,
                             tls_skip_verify=True)
    from adbc_driver_gizmosql import dbapi
    return dbapi.connect(uri="grpc+tls://127.0.0.1:31337",
                         db_kwargs={"username": "bench", "password": PASSWORD,
                                    "adbc.flight.sql.client_option.tls_skip_verify": "true"})


def query(conn, sql: str) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(operation=sql)
        return cursor.fetch_arrow_table().to_pylist()


def execute(conn, sql: str) -> None:
    """A statement with no result set (CREATE TABLE AS, CHECKPOINT)."""
    with conn.cursor() as cursor:
        cursor.execute(operation=sql)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, choices=["stable", "edge", "hardcharger"])
    sub = parser.add_subparsers(dest="action", required=True)
    load = sub.add_parser("load")
    load.add_argument("--scale-factor", required=True)
    load.add_argument("--output", required=True)
    info = sub.add_parser("info")
    info.add_argument("--output", required=True)
    args = parser.parse_args()

    conn = connect(server=args.server)
    try:
        if args.action == "load":
            record = {"server": args.server, "scale_factor": args.scale_factor,
                      "start": datetime.now(tz=timezone.utc).isoformat(), "tables": []}
            for table in TABLES:
                sql = (f"CREATE TABLE {table} AS SELECT * FROM "
                       f"read_parquet('/data/tpch/sf{args.scale_factor}/{table}/*.parquet');")
                start = time.time()
                execute(conn=conn, sql=sql)
                seconds = time.time() - start
                rows = query(conn=conn, sql=f"SELECT count(*) AS n FROM {table}")[0]["n"]
                record["tables"].append({"table": table, "sql": sql, "seconds": seconds, "rows": rows})
                print(f"{args.server}: {table} {rows:,} rows in {seconds:.1f} s", flush=True)
            # Persist everything before the server is stopped.
            execute(conn=conn, sql="CHECKPOINT")
            record["end"] = datetime.now(tz=timezone.utc).isoformat()
        else:
            record = {"server": args.server,
                      "version": query(conn=conn, sql="SELECT version() AS v")[0]["v"],
                      "pragma_version": query(conn=conn, sql="SELECT * FROM pragma_version()"),
                      "database_size": query(conn=conn, sql="SELECT * FROM pragma_database_size()"),
                      "databases": query(conn=conn, sql="SELECT database_name, path, type, readonly "
                                                        "FROM duckdb_databases() WHERE NOT internal"),
                      "settings": query(conn=conn, sql="SELECT name, value FROM duckdb_settings() WHERE name "
                                                       "IN ('threads', 'memory_limit', 'temp_directory', "
                                                       "'max_temp_directory_size')"),
                      "row_counts": {t: query(conn=conn, sql=f"SELECT count(*) AS n FROM {t}")[0]["n"]
                                     for t in TABLES}}
            print(json.dumps(record, default=str, indent=2))
    finally:
        conn.close()
    with open(args.output, "w") as handle:
        json.dump(record, handle, default=str, indent=2)


if __name__ == "__main__":
    main()
