"""TPC-H SF10: Hardcharger (Warp SQL over HTTP) vs GizmoSQL 1.x (Flight SQL over gRPC).

Each server loads the same Parquet files into its own database file, then every
query runs once to warm up and five times timed; times include fetching every row.
See docs/benchmarks.md. Usage: python bench/tpch.py [hardcharger] [gizmosql]
"""

import io
import json
import os
import pathlib
import signal
import socket
import statistics
import subprocess
import sys
import time

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc

HERE = pathlib.Path(__file__).resolve().parent
# Parquet files from DuckDB's tpch extension (CALL dbgen(sf => 10), then COPY each table).
DATA = pathlib.Path(os.environ.get("HARDCHARGER_BENCH_DATA", HERE / "data"))
WORK = pathlib.Path(os.environ.get("HARDCHARGER_BENCH_WORK", HERE / "work"))
QUERIES = {int(k): v for k, v in json.loads((HERE / "queries.json").read_text()).items()}
TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]
RUNS = 5
PASSWORD = "bench-pw"
HARDCHARGER = pathlib.Path(os.environ.get("HARDCHARGER_BENCH_HARDCHARGER", HERE.parent / "server/build/hardcharger"))
GIZMOSQL = pathlib.Path(os.environ.get("HARDCHARGER_BENCH_GIZMOSQL", "gizmosql_server"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_sql(db: pathlib.Path) -> str:
    if db.exists():
        return ""
    return "; ".join(f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{DATA / (t + '.parquet')}')"
                     for t in TABLES)


def wait_port(port: int, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise RuntimeError(f"port {port} never opened")


class HardchargerClient:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.http = httpx.Client(base_url=self.base, timeout=600)
        token = self.http.post(url="/warp/v1/auth/token", json={"grant_type": "password",
                                                                 "username": "bench", "password": PASSWORD}).json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session = self.http.post(url="/warp/v1/sessions", headers=self.headers, json={}).json()["session_id"]

    def query(self, sql: str, compression: str | None = None) -> pa.Table:
        headers = {**self.headers, "Accept": "application/vnd.apache.arrow.stream"}
        if compression:
            headers["Warp-IPC-Compression"] = compression
        response = self.http.post(url=f"/warp/v1/sessions/{self.session}/statements", headers=headers,
                                  json={"sql": sql})
        response.raise_for_status()
        return ipc.open_stream(source=io.BytesIO(initial_bytes=response.content)).read_all()


class GizmoSQLClient:
    def __init__(self, port: int):
        from adbc_driver_gizmosql import dbapi
        self.conn = dbapi.connect(f"grpc://127.0.0.1:{port}", username="bench", password=PASSWORD,
                                  tls_skip_verify=True)

    def query(self, sql: str, compression: str | None = None) -> pa.Table:
        with self.conn.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetch_arrow_table()


def start(kind: str, db: pathlib.Path) -> tuple[subprocess.Popen, int]:
    port = free_port()
    log = open(WORK / f"{kind}.log", "w")
    if kind == "hardcharger":
        env = {"PATH": os.environ["PATH"], "HARDCHARGER_PASSWORD": PASSWORD, "HARDCHARGER_USERNAME": "bench",
               "HARDCHARGER_PRINT_QUERIES": "false", "HARDCHARGER_DATABASE": str(db),
               "HARDCHARGER_INIT_SQL_COMMANDS": load_sql(db), "HARDCHARGER_HOST": "127.0.0.1",
               "HARDCHARGER_PORT": str(port)}
        process = subprocess.Popen(args=[str(HARDCHARGER)], env=env, stdout=log, stderr=log)
    else:
        env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "GIZMOSQL_PASSWORD": PASSWORD, "PRINT_QUERIES": "0"}
        args = [str(GIZMOSQL), "--database-filename", str(db), "--username", "bench",
                "--port", str(port), "--hostname", "127.0.0.1"]
        init = load_sql(db)
        if init:
            args += ["--init-sql-commands", init]
        process = subprocess.Popen(args=args, env=env, stdout=log, stderr=log)
    wait_port(port)
    return process, port


def bench(kind: str) -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    db = WORK / f"tpch_{kind}.duckdb"
    process, port = start(kind, db)
    try:
        client = HardchargerClient(port) if kind == "hardcharger" else GizmoSQLClient(port)
        results = {}
        for number, sql in sorted(QUERIES.items()):
            client.query(sql)  # warm-up
            times = []
            rows = 0
            for _ in range(RUNS):
                start_time = time.perf_counter()
                rows = client.query(sql).num_rows
                times.append(time.perf_counter() - start_time)
            results[f"q{number:02d}"] = {"median_s": statistics.median(times), "rows": rows}
            print(f"{kind} q{number:02d} {statistics.median(times) * 1000:8.1f} ms  rows={rows}", flush=True)
        # Bulk transfer: 10 million lineitem rows to the client.
        transfer = "SELECT * FROM lineitem LIMIT 10000000"
        for compression in ([None, "zstd"] if kind == "hardcharger" else [None]):
            client.query(transfer, compression=compression)
            times = []
            for _ in range(3):
                start_time = time.perf_counter()
                table = client.query(transfer, compression=compression)
                times.append(time.perf_counter() - start_time)
            name = "transfer_10m" + ("_zstd" if compression else "")
            results[name] = {"median_s": statistics.median(times), "rows": table.num_rows,
                             "mb": table.nbytes / 1e6}
            print(f"{kind} {name} {statistics.median(times):6.2f} s  {table.nbytes / 1e6:.0f} MB", flush=True)
        return results
    finally:
        process.send_signal(signal.SIGTERM)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)


if __name__ == "__main__":
    kinds = sys.argv[1:] or ["hardcharger", "gizmosql"]
    all_results = {kind: bench(kind) for kind in kinds}
    (WORK / f"results_{"_".join(kinds)}.json").write_text(json.dumps(all_results, indent=2))
