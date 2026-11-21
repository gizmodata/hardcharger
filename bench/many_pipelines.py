"""Latency and server CPU of a statement whose plan has many pipelines.

The query has the shape of DB Olympics' q311: a 30-way UNION ALL of small aggregates (each
part one or two pipelines of its own) joined to one more aggregate. 2.9.0 to 2.13.1 ran these
150-1350 ms slower than 2.4.1 at times, because DuckDB recomputed the progress estimate after
every task under a lock its worker threads need (Hardcharger Engine progress sampling).

Each server gets its own database (built on first use, in HARDCHARGER_BENCH_WORK), the query
runs 20 times to warm up and then RUNS times, and the script prints the median, p90, p99 and
maximum latency at the client and the server's CPU time over the timed runs. With --no-progress
the session first runs SET enable_progress_bar = false (the control: no estimate at all).

Usage: python bench/many_pipelines.py [--runs N] [--threads N] [--scale N] [--no-progress] [--cli] <label>=<server binary> ...
"""

import argparse
import os
import pathlib
import statistics
import subprocess
import sys
import time


import tpch

PARTS = ["brands", "categories", "products", "product_variants", "collections", "users", "billing_profiles",
         "plans", "price_lists", "discounts", "coupons", "promotions", "campaigns", "email_templates", "segments",
         "landing_pages", "referrals", "loyalty_programs", "ab_tests", "experiments", "cohorts", "funnels",
         "contacts", "accounts", "knowledge_articles", "macros", "files", "utm_links", "suppliers", "warehouses"]
ROWS_PER_PART = 50_000  # times --scale
AUDIT_ROWS = 1_000_000

UNION = "\n  UNION ALL ".join(
    f"SELECT '{name}' AS entity_table, COUNT(*) AS total_soft_deleted, COUNT(DISTINCT tenant_id) AS tenant_count "
    f"FROM {name} WHERE deleted_at IS NOT NULL" for name in PARTS)
QUERY = f"""WITH soft_deleted AS (
  {UNION}
),
audit_counts AS (
  SELECT entity_table, COUNT(*) AS deleted_audit_rows FROM audit_log WHERE action = 'delete' GROUP BY entity_table
)
SELECT sd.entity_table, sd.total_soft_deleted, COALESCE(ac.deleted_audit_rows, 0) AS matching_audit_rows,
       CASE WHEN sd.total_soft_deleted = 0 THEN NULL
            ELSE 100.0 * COALESCE(ac.deleted_audit_rows, 0) / sd.total_soft_deleted END AS coverage_rate_pct,
       sd.tenant_count
FROM soft_deleted sd LEFT JOIN audit_counts ac ON ac.entity_table = sd.entity_table
ORDER BY sd.total_soft_deleted DESC NULLS FIRST, sd.entity_table ASC NULLS LAST"""


def create_sql(scale: int) -> str:
    statements = [
        f"CREATE TABLE {name} AS SELECT i AS id, i % 97 AS tenant_id, "
        f"CASE WHEN i % 11 = {n % 11} THEN TIMESTAMP '2025-01-01' + INTERVAL (i) SECOND END AS deleted_at "
        f"FROM range({ROWS_PER_PART * scale}) t(i)" for n, name in enumerate(PARTS)]
    statements.append(
        f"CREATE TABLE audit_log AS SELECT i AS id, ['{"', '".join(PARTS)}'][1 + i % {len(PARTS)}] AS entity_table, "
        f"CASE WHEN i % 3 = 0 THEN 'delete' ELSE 'update' END AS action FROM range({AUDIT_ROWS * scale}) t(i)")
    return "; ".join(statements)


def server_cpu_seconds(pid: int) -> float:
    """User plus system CPU time of a process, from ps (macOS and Linux: [[dd-]hh:]mm:ss.ss)."""
    text = subprocess.run(args=["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True,
                          check=True).stdout.strip()
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (int(days) * 86400 if days else 0)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def run(client: tpch.HardchargerClient, sql: str) -> None:
    """A statement whose result is not read (DDL, SET)."""
    response = client.http.post(url=f"/chargewire/v1/sessions/{client.session}/statements", headers=client.headers,
                                json={"sql": sql})
    response.raise_for_status()


def start(binary: pathlib.Path, db: pathlib.Path, threads: int) -> tuple[subprocess.Popen, int]:
    port = tpch.free_port()
    log = open(tpch.WORK / f"{db.stem}.log", "w")
    env = {"PATH": os.environ["PATH"], "HARDCHARGER_ADMIN_PASSWORD": tpch.PASSWORD,
           "HARDCHARGER_ADMIN_USERNAME": "bench", "HARDCHARGER_PRINT_QUERIES": "false",
           "HARDCHARGER_DATABASE": str(db), "HARDCHARGER_INIT_SQL_COMMANDS": f"SET threads = {threads}",
           "HARDCHARGER_HOST": "127.0.0.1", "HARDCHARGER_PORT": str(port)}
    process = subprocess.Popen(args=[str(binary)], env=env, stdout=log, stderr=log)
    tpch.wait_port(port=port)
    return process, port


class CliClient:
    """The hardcharger CLI next to the server binary, as DB Olympics drives it: one process, one
    session, each query's rows written as CSV to a file (.once), .echo marking the end."""

    def __init__(self, binary: pathlib.Path, port: int):
        self.out = tpch.WORK / "many_pipelines.csv"
        env = {"PATH": os.environ["PATH"], "HARDCHARGER_PASSWORD": tpch.PASSWORD, "NO_COLOR": "1"}
        self.process = subprocess.Popen(
            args=[str(binary.parent / "hardcharger"), "--host", "127.0.0.1", "--port", str(port), "--tls=false",
                  "--username", "bench", "--quiet", "--csv", "--no-init", "--result-partitions", "1"],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def query(self, sql: str) -> int:
        """Runs sql; returns the number of CSV lines it wrote (header included)."""
        self.process.stdin.write(f".once {self.out}\n{sql}\n;\n.echo\n".encode())
        self.process.stdin.flush()
        noise = b""
        while (line := self.process.stdout.readline()) != b"echo: off\n":
            assert line, noise.decode(errors="replace")
            noise += line
        assert not noise, noise.decode(errors="replace")
        return len(self.out.read_bytes().splitlines())

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=60)


def measure(label: str, binary: pathlib.Path, runs: int, threads: int, progress: bool, cli: bool,
            scale: int) -> dict:
    db = tpch.WORK / f"many_pipelines_{label}_x{scale}.duckdb"
    fresh = not db.exists()
    process, port = start(binary=binary, db=db, threads=threads)
    try:
        http = tpch.HardchargerClient(port=port)
        if fresh:
            run(client=http, sql=create_sql(scale=scale))
        client = CliClient(binary=binary, port=port) if cli else http
        if not progress:
            client.query(sql="SET enable_progress_bar = false") if cli else run(client=http, sql="SET enable_progress_bar = false")

        def rows() -> int:
            result = client.query(sql=QUERY)
            return result - 1 if cli else result.num_rows

        assert rows() == len(PARTS)
        for _ in range(20):
            rows()
        cpu_before = server_cpu_seconds(pid=process.pid)
        times = []
        for _ in range(runs):
            began = time.perf_counter()
            count = rows()
            times.append((time.perf_counter() - began) * 1000)
            assert count == len(PARTS)
        cpu = server_cpu_seconds(pid=process.pid) - cpu_before
        if cli:
            client.close()
    finally:
        process.terminate()
        process.wait(timeout=60)
    return {"label": label + (" cli" if cli else "") + ("" if progress else " (progress off)"), "runs": runs,
            "median_ms": statistics.median(times), "p90_ms": percentile(values=times, fraction=0.9),
            "p99_ms": percentile(values=times, fraction=0.99), "max_ms": max(times),
            "server_cpu_ms_per_run": cpu * 1000 / runs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--scale", type=int, default=1, help="rows per table: 50,000 times this (audit_log 1M times)")
    parser.add_argument("--no-progress", action="store_true", help="SET enable_progress_bar = false first")
    parser.add_argument("--cli", action="store_true",
                        help="query through the hardcharger CLI next to each server binary (as DB Olympics does)")
    parser.add_argument("servers", nargs="+", metavar="label=binary")
    args = parser.parse_args()
    tpch.WORK.mkdir(parents=True, exist_ok=True)
    print(f"{'server':28s} {'median':>8s} {'p90':>8s} {'p99':>8s} {'max':>8s} {'CPU/run':>8s}  (ms, {args.runs} runs)")
    for spec in args.servers:
        label, _, binary = spec.partition("=")
        result = measure(label=label, binary=pathlib.Path(binary), runs=args.runs, threads=args.threads,
                         progress=not args.no_progress, cli=args.cli, scale=args.scale)
        print(f"{result['label']:28s} {result['median_ms']:8.1f} {result['p90_ms']:8.1f} {result['p99_ms']:8.1f} "
              f"{result['max_ms']:8.1f} {result['server_cpu_ms_per_run']:8.1f}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
