"""Generates the benchmark database on the server, with DuckDB 1.5.6 (~/venv15).

DuckDB 1.5.6 writes storage format v1.0.0+, which DuckDB 2.0 reads and writes without
upgrading it, so GizmoSQL (DuckDB 1.5.6), Quack and Hardcharger (DuckDB 2.0) all serve a
copy of the same file in the same format.

    ~/venv15/bin/python gen_data.py --path ~/bench/base.duckdb

Tables: sf1.lineitem (6,001,215 rows), sf10.lineitem (59,986,052 rows), both from
CALL dbgen(); and main.narrow, 100,000,000 rows of two BIGINT and two DOUBLE columns.
"""

import argparse
import os
import time

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--narrow-rows", type=int, default=100_000_000)
    args = parser.parse_args()
    if os.path.exists(args.path):
        os.remove(args.path)
    con = duckdb.connect(database=args.path)
    con.execute(query="LOAD tpch")
    for sf in (1, 10):
        start = time.time()
        con.execute(query=f"CREATE SCHEMA sf{sf}")
        con.execute(query=f"CALL dbgen(sf={sf}, schema='sf{sf}')")
        # Only lineitem is benchmarked; the other TPC-H tables would just take space.
        for table in ("orders", "customer", "part", "partsupp", "supplier", "nation", "region"):
            con.execute(query=f"DROP TABLE sf{sf}.{table}")
        print(f"sf{sf} generated in {time.time() - start:.1f}s", flush=True)
    con.execute(
        query=f"""CREATE TABLE main.narrow AS
        SELECT range AS id, range % 1000003 AS bucket,
               range * 0.5 AS x, sqrt(range::DOUBLE) AS y
        FROM range({args.narrow_rows})"""
    )
    con.execute(query="CHECKPOINT")
    for table in ("sf1.lineitem", "sf10.lineitem", "main.narrow"):
        print(table, con.execute(query=f"SELECT count(*) FROM {table}").fetchone()[0])
    print(con.execute(query="SELECT tags FROM duckdb_databases() WHERE NOT internal").fetchall())
    con.close()
    print(f"{args.path}: {os.path.getsize(args.path) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
