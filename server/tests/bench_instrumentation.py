"""Instrumentation writer benchmark: how fast a server records executions, and how long
its writer takes to catch up once the clients stop.

    .venv/bin/python bench_instrumentation.py --backend file --sessions 16 --statements 200000
    .venv/bin/python bench_instrumentation.py --backend file --prefill 5000000
    .venv/bin/python bench_instrumentation.py --backend postgres --prefill 2000000
    .venv/bin/python bench_instrumentation.py --backend ducklake
    .venv/bin/python bench_instrumentation.py --backend none      # the clients' own ceiling

`--sessions` client processes (one Warp SQL session each) run `SELECT <n>` back to back
until `--statements` have run. The backlog is drained when every execution of the run is
recorded as finished (cursor_close_time set); "drain" is how long that took after the
last client finished, "recorded/s" is the run's executions over the time from the first
statement to the last record. `--prefill` first writes that many statements and
executions (and a session per thousand) into the tables, so the run measures a server
whose tables are already large. postgres and ducklake need server/tests/docker-compose.yml
(HARDCHARGER_TEST_PG_* / HARDCHARGER_TEST_S3_PORT, services.py). Not a test: pytest
doesn't collect it.
"""

import argparse
import http.client
import json
import multiprocessing
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from conftest import Launcher
from services import PG_DATABASE, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, S3_PORT, pg_conninfo

ARROW_STREAM = "application/vnd.apache.arrow.stream"


def init_sql(backend: str, schema: str, inlining: int) -> str:
    if backend == "postgres":
        return f"INSTALL postgres; LOAD postgres; ATTACH '{pg_conninfo()}' AS bench_instr (TYPE postgres);"
    return f"""
INSTALL ducklake; INSTALL postgres; INSTALL httpfs; LOAD ducklake; LOAD postgres; LOAD httpfs;
CREATE OR REPLACE SECRET s3_secret (TYPE s3, KEY_ID 'minioadmin', SECRET 'minioadmin',
  ENDPOINT '127.0.0.1:{S3_PORT}', USE_SSL false, URL_STYLE 'path', REGION 'us-east-1');
CREATE OR REPLACE SECRET pg_bench_secret (TYPE postgres, HOST '{PG_HOST}', PORT {PG_PORT},
  DATABASE '{PG_DATABASE}', USER '{PG_USER}', PASSWORD '{PG_PASSWORD}');
CREATE OR REPLACE SECRET ducklake_bench_secret (TYPE DUCKLAKE, METADATA_PATH '',
  DATA_PATH 's3://ducklake/{schema}/', METADATA_SCHEMA '{schema}',
  METADATA_PARAMETERS MAP {{'TYPE': 'postgres', 'SECRET': 'pg_bench_secret'}});
ATTACH 'ducklake:ducklake_bench_secret' AS bench_instr (DATA_INLINING_ROW_LIMIT {inlining});
"""


class Setup:
    """Where the instrumentation tables live, and how to start servers that use them."""

    def __init__(self, launcher: Launcher, work: pathlib.Path, backend: str, binary: pathlib.Path, inlining: int):
        self.launcher, self.work, self.backend, self.binary = launcher, work, backend, binary
        self.schema = f"bench_{uuid.uuid4().hex[:8]}"
        self.inlining = inlining
        self.database = work / "db.duckdb"
        self.instr_file = work / "hardcharger_instrumentation.db"

    @property
    def prefix(self) -> str:
        if self.backend == "file":
            return "_hardcharger_instr.main"
        return f"bench_instr.{self.schema}" if self.backend == "postgres" else "bench_instr.main"

    def server_env(self, instrumented: bool) -> dict[str, str]:
        env = {"HARDCHARGER_PRINT_QUERIES": "false"}
        if self.backend in ("postgres", "ducklake"):
            env["HARDCHARGER_INIT_SQL_COMMANDS"] = init_sql(backend=self.backend, schema=self.schema,
                                                            inlining=self.inlining)
            if instrumented:
                env["HARDCHARGER_INSTRUMENTATION_CATALOG"] = "bench_instr"
                if self.backend == "postgres":
                    env["HARDCHARGER_INSTRUMENTATION_SCHEMA"] = self.schema
        return env

    def start(self, instrumented: bool):
        args = ["--database", str(self.database)]
        if instrumented and self.backend != "none":
            args.append("--enable-instrumentation")
        return self.launcher.start(binary=self.binary, args=args, env=self.server_env(instrumented=instrumented),
                                   startup_seconds=120)

    def drop_schema(self) -> None:
        if self.backend not in ("postgres", "ducklake"):
            return
        admin = self.launcher.start(binary=self.binary).admin()
        admin.execute(sql="INSTALL postgres; LOAD postgres;")
        admin.execute(sql=f"ATTACH '{pg_conninfo()}' AS pg_reset (TYPE postgres)")
        admin.execute(sql=f"CALL postgres_execute('pg_reset', 'DROP SCHEMA IF EXISTS {self.schema} CASCADE')")

    def prefill(self, rows: int) -> float:
        """Creates the tables (a server starts and stops once), then writes `rows`
        statements and executions into them from a plain server. Returns its seconds."""
        started = time.monotonic()
        first = self.start(instrumented=True)
        first.stop()
        if self.backend == "file":
            writer = self.launcher.start(binary=self.binary, args=["--database", str(self.work / "fill.duckdb")])
            admin = writer.admin()
            admin.execute(sql=f"ATTACH '{self.instr_file}' AS instr_fill")
            p = "instr_fill.main"
        else:
            writer = self.launcher.start(binary=self.binary, env=self.server_env(instrumented=False),
                                         startup_seconds=120)
            admin = writer.admin()
            p = self.prefix
        instance = str(uuid.uuid4())
        sessions = max(1, rows // 1000)

        # Ids as random as a server's (q_ and 32 characters), so no min/max statistics on
        # the id columns can skip the prefilled rows.
        def qid(i: str) -> str:
            return f"'q_' || md5('q' || ({i})::VARCHAR)"

        def sid(i: str) -> str:
            return f"'s_' || md5('s' || ({i})::VARCHAR)"

        # Times in the past, in id order, a millisecond apart (as a busy server writes them).
        base = f"(now() - INTERVAL {rows // 1000 + 3600} SECOND)"
        admin.execute(sql=f"INSERT INTO {p}.instances (instance_id, hardcharger_version, duckdb_version, arrow_version, "
                          f"tls_enabled, mtls_required, readonly, start_time, stop_time, status, stop_reason) VALUES "
                          f"('{instance}', 'prefill', 'prefill', 'prefill', false, false, false, {base}, {base}, "
                          "'stopped', 'graceful')")
        admin.execute(sql=f"INSERT INTO {p}.sessions (session_id, instance_id, username, role, auth_method, peer, "
                          "connection_protocol, start_time, stop_time, status, stop_reason) "
                          f"SELECT {sid('i')}, '{instance}', 'admin', 'admin', 'password', '', 'http', "
                          f"{base} + to_milliseconds(i * 1000), {base} + to_milliseconds(i * 1000 + 999), 'closed', "
                          f"'closed' FROM range({sessions}) t(i)")
        chunk = 1_000_000
        for first_row in range(0, rows, chunk):
            last = min(rows, first_row + chunk)
            admin.execute(sql=f"INSERT INTO {p}.sql_statements (statement_id, session_id, sql_text, statement_kind, "
                              "is_internal, prepare_success, created_time) "
                              f"SELECT {qid('i')}, {sid('i // 1000')}, 'SELECT ' || i, 'statement', "
                              f"false, true, {base} + to_milliseconds(i) FROM range({first_row}, {last}) t(i)")
            admin.execute(sql=f"INSERT INTO {p}.sql_executions (execution_id, statement_id, execution_start_time, "
                              "execution_end_time, fetch_start_time, fetch_end_time, cursor_close_time, rows_fetched, "
                              "status, duration_ms, total_duration_ms) "
                              f"SELECT {qid('i')}, {qid('i')}, {base} + to_milliseconds(i), "
                              f"{base} + to_milliseconds(i), {base} + to_milliseconds(i), {base} + to_milliseconds(i), "
                              f"{base} + to_milliseconds(i), 1, 'success', 0, 0 FROM range({first_row}, {last}) t(i)")
        writer.stop()
        return time.monotonic() - started


def client(args: tuple) -> tuple[int, int]:
    """One session running `count` statements; returns (succeeded, failed)."""
    port, token, count = args
    connection = http.client.HTTPConnection(host="127.0.0.1", port=port, timeout=120)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    connection.request(method="POST", url="/warp/v1/sessions", body=json.dumps({"options": {}}), headers=headers)
    response = connection.getresponse()
    session = json.loads(response.read())["session_id"]
    url = f"/warp/v1/sessions/{session}/statements"
    headers["Accept"] = ARROW_STREAM
    ok = failed = 0
    for i in range(count):
        connection.request(method="POST", url=url, body=json.dumps({"sql": f"SELECT {i} AS v"}), headers=headers)
        response = connection.getresponse()
        response.read()
        if response.status == 200:
            ok += 1
        else:
            failed += 1
    connection.request(method="DELETE", url=f"/warp/v1/sessions/{session}", headers=headers)
    connection.getresponse().read()
    connection.close()
    return ok, failed


def rss_sampler(pid: int, peak: list[int], stop: threading.Event) -> None:
    while not stop.is_set():
        out = subprocess.run(args=["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
        if out:
            peak[0] = max(peak[0], int(out) * 1024)
        stop.wait(timeout=0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent / "build" / "hardcharger-server")
    parser.add_argument("--backend", choices=["file", "postgres", "ducklake", "none"], default="file")
    parser.add_argument("--sessions", type=int, default=16)
    parser.add_argument("--statements", type=int, default=200_000)
    parser.add_argument("--prefill", type=int, default=0)
    parser.add_argument("--inlining", type=int, default=0, help="DuckLake DATA_INLINING_ROW_LIMIT")
    parser.add_argument("--drain-timeout", type=float, default=600)
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    options = parser.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="hc-instr-bench-"))
    launcher = Launcher(tmp_path=work)
    setup = Setup(launcher=launcher, work=work, backend=options.backend, binary=options.binary.resolve(),
                  inlining=options.inlining)
    result = {"backend": options.backend, "sessions": options.sessions, "statements": options.statements,
              "prefill": options.prefill}
    try:
        if options.prefill and options.backend != "none":
            result["prefill_s"] = round(setup.prefill(rows=options.prefill), 1)
        server = setup.start(instrumented=True)
        peak = [0]
        stop_sampling = threading.Event()
        sampler = threading.Thread(target=rss_sampler, args=(server.process.pid, peak, stop_sampling), daemon=True)
        sampler.start()
        token = server.token()
        per_client = options.statements // options.sessions
        total = per_client * options.sessions
        reader = server.admin()
        t0_sql = reader.scalar(sql="SELECT now()::VARCHAR")
        started = time.monotonic()
        with multiprocessing.get_context("spawn").Pool(processes=options.sessions) as pool:
            outcomes = pool.map(client, [(server.port, token, per_client)] * options.sessions)
        clients_done = time.monotonic()
        t1_sql = reader.scalar(sql="SELECT now()::VARCHAR")
        ok = sum(o[0] for o in outcomes)
        result["failed"] = sum(o[1] for o in outcomes)
        result["client_s"] = round(clients_done - started, 2)
        result["client_per_s"] = round(total / (clients_done - started))
        if options.backend != "none":
            # This run's finished executions, by time (the reader's own polls start later).
            sql = (f"SELECT count(*) FROM {setup.prefix}.sql_executions WHERE execution_start_time >= '{t0_sql}' "
                   f"AND execution_start_time <= '{t1_sql}' AND cursor_close_time IS NOT NULL")
            deadline = time.monotonic() + options.drain_timeout
            recorded = 0
            while time.monotonic() < deadline:
                recorded = reader.scalar(sql=sql)
                if recorded >= ok:
                    break
                time.sleep(0.05)
            drained = time.monotonic()
            result["recorded"] = recorded
            result["drain_s"] = round(drained - clients_done, 2)
            result["recorded_per_s"] = round(recorded / (drained - started))
        result["peak_rss_mb"] = round(peak[0] / 1e6)
        stop_started = time.monotonic()
        server.stop()
        result["stop_s"] = round(time.monotonic() - stop_started, 2)
        stop_sampling.set()
        if options.backend == "file":
            result["file_mb"] = round(setup.instr_file.stat().st_size / 1e6, 1)
        # The writer's own account: written, dropped and failed records.
        stopped = [line for line in server.log().splitlines() if "instrumentation stopped" in line]
        if stopped:
            result["writer"] = stopped[-1].split("instrumentation stopped", 1)[1].strip()
        setup.drop_schema()
    finally:
        launcher.stop_all()
        if not options.keep:
            subprocess.run(args=["rm", "-rf", str(work)], check=False)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
