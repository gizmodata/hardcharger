"""Writes the ingest sources on the client: lineitem at SF1 and SF10 as Arrow IPC files.

The rows come from DuckDB's own dbgen (DuckDB 2.0 here; dbgen's output is deterministic,
so it is the same data the server generated), in batches of 122,880 rows. Beside each file
goes the checksum every ingest is verified against: row count, sum(l_extendedprice) and
count(DISTINCT l_orderkey).

    ~/venv/bin/python prepare_data.py --data-dir ~/bench/data
"""

import argparse
import json
import os

import duckdb
import pyarrow as pa

BATCH_ROWS = 122_880


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--sf", type=int, nargs="+", default=[1, 10])
    args = parser.parse_args()
    os.makedirs(args.data_dir, exist_ok=True)
    for sf in args.sf:
        con = duckdb.connect()
        con.execute(query="LOAD tpch")
        con.execute(query=f"CALL dbgen(sf={sf})").fetchall()
        check = con.execute(
            query="SELECT count(*), sum(l_extendedprice)::VARCHAR, count(DISTINCT l_orderkey) "
            "FROM lineitem"
        ).fetchall()[0]
        reader = con.sql(query="SELECT * FROM lineitem").to_arrow_reader(batch_size=BATCH_ROWS)
        path = os.path.join(args.data_dir, f"lineitem_sf{sf}.arrow")
        nbytes = 0
        with pa.OSFile(path=path, mode="wb") as sink:
            with pa.ipc.new_file(sink=sink, schema=reader.schema) as writer:
                for batch in reader:
                    nbytes += batch.nbytes
                    writer.write_batch(batch=batch)
        with open(os.path.join(args.data_dir, f"lineitem_sf{sf}.check.json"), "w") as f:
            json.dump([int(check[0]), str(check[1]), int(check[2])], f)
        print(f"sf{sf}: {check}, {nbytes / 1e9:.2f} GB of Arrow -> {path}", flush=True)
        con.close()


if __name__ == "__main__":
    main()
