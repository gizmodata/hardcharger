"""Times mid-sized results (a few batches) at several partition counts, through the ADBC driver.

    midsize.py --host <server> [--cert ~/bench/tls.crt] [--codec auto]
"""

import argparse
import os
import statistics
import time

from adbc_driver_chargewire import dbapi

PASSWORD = os.environ.get("BENCH_PASSWORD", "bench-password-not-secret")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--cert", default=os.path.expanduser("~/bench/tls.crt"))
    parser.add_argument("--codec", default="none")
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--limits", default="130000,400000,1000000,2000000")
    parser.add_argument("--partitions", default="1,8,32")
    args = parser.parse_args()
    for limit in [int(x) for x in args.limits.split(",")]:
        for p in [int(x) for x in args.partitions.split(",")]:
            conn = dbapi.connect(uri=f"chargewire://{args.host}:31337", username="bench", password=PASSWORD,
                                 tls_root_certs=args.cert,
                                 db_kwargs={"adbc.chargewire.ipc_compression": args.codec,
                                            "adbc.chargewire.statement.result_partitions": str(p)})
            cur = conn.cursor()
            times = []
            for rep in range(args.reps + 1):
                t0 = time.perf_counter()
                cur.execute(operation=f"SELECT * FROM sf1.lineitem LIMIT {limit}")
                rows = sum(b.num_rows for b in cur.fetch_record_batch())
                if rep:
                    times.append(time.perf_counter() - t0)
            assert rows == limit, rows
            print(f"limit {limit:>8} p={p:<3} codec={args.codec}: median {statistics.median(times)*1000:7.1f} ms "
                  f"min {min(times)*1000:7.1f} ms", flush=True)
            cur.close()
            conn.close()


if __name__ == "__main__":
    main()
