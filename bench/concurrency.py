"""Ten clients at once, each with its own session, running a mix of queries derived from TPC-H.

Usage: python bench/concurrency.py hardcharger|gizmosql (after tpch.py has loaded the data).
"""

import concurrent.futures
import statistics
import sys
import time

import tpch

CLIENTS = 10
ROUNDS = 6
MIX = [1, 3, 6, 10, 14, 19]


def worker(kind: str, port: int) -> list[float]:
    client = tpch.HardchargerClient(port) if kind == "hardcharger" else tpch.GizmoSQLClient(port)
    times = []
    for _ in range(ROUNDS):
        for number in MIX:
            start = time.perf_counter()
            client.query(tpch.QUERIES[number])
            times.append(time.perf_counter() - start)
    return times


if __name__ == "__main__":
    kind = sys.argv[1]
    tpch.WORK.mkdir(parents=True, exist_ok=True)
    process, port = tpch.start(kind, tpch.WORK / f"tpch_{kind}.duckdb")
    try:
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENTS) as pool:
            results = list(pool.map(lambda _: worker(kind, port), range(CLIENTS)))
        wall = time.perf_counter() - start
        latencies = sorted(t for r in results for t in r)
        print(f"{kind}: {len(latencies)} queries in {wall:.1f} s = {len(latencies) / wall:.1f} q/s, "
              f"median {statistics.median(latencies) * 1000:.0f} ms, "
              f"p95 {latencies[int(len(latencies) * 0.95)] * 1000:.0f} ms")
    finally:
        process.terminate()
        process.terminate()
        process.wait(timeout=60)
