"""Remote-throughput harness: Flight SQL (GizmoSQL) vs Quack vs Warp SQL (Hardcharger).

Runs on the client VM. One invocation measures one system on one workload and appends a
JSON line per measured repetition to --out.

    client.py --system hardcharger --host "$SERVER_IP" --workload fetch --dataset sf10
    client.py --system quack --host "$SERVER_IP" --workload ingest --dataset sf1

SERVER_IP is the server VM's address as the client VM reaches it (its private IP in the VPC).

fetch:  SELECT * FROM <table>; the client drains the whole result as Arrow record batches
        (counting rows and Arrow bytes, dropping each batch), or with --collect table builds
        one pyarrow.Table.
ingest: the client holds lineitem in memory as a pyarrow.Table (from prepare_data.py) and
        loads it into a new server table: adbc_ingest(mode="create") for Flight SQL and
        Warp SQL, CREATE TABLE remote.t AS SELECT * FROM <arrow table> for Quack. Each
        repetition is verified afterwards (row count, sum(l_extendedprice),
        count(DISTINCT l_orderkey)) through the same protocol, outside the timing.

Connecting is not timed. Every repetition is timed from the call that submits the SQL (or
starts the ingest) to the last batch received (or the ingest committed).
"""

import argparse
import hashlib
import json
import os
import resource
import ssl
import statistics
import sys
import time

import pyarrow as pa

PASSWORD = os.environ.get("BENCH_PASSWORD", "bench-password-not-secret")
TABLES = {"sf1": "sf1.lineitem", "sf10": "sf10.lineitem", "narrow": "main.narrow"}
# What a fetch is checked against: computed by the server through the same protocol, and
# by the client over the batches it received (on the warm-up and on one extra, untimed
# repetition at the end, so the measured repetitions only count rows).
FETCH_CHECK_SQL = {
    "lineitem": "SELECT count(*), sum(l_orderkey)::VARCHAR, sum(l_extendedprice)::VARCHAR, "
    "sum(length(l_comment))::VARCHAR FROM {table}",
    "narrow": "SELECT count(*), sum(id)::VARCHAR, sum(bucket)::VARCHAR, sum(x)::VARCHAR FROM {table}",
}
FETCH_CHECK_COLUMNS = {
    "lineitem": ("l_orderkey", "l_extendedprice", "l_comment"),
    "narrow": ("id", "bucket", "x"),
}


class FetchChecksum:
    """Row count and three column sums over the batches of a fetch (order-independent)."""

    def __init__(self, kind: str):
        self.columns = FETCH_CHECK_COLUMNS[kind]
        self.rows = 0
        self.sums = [0, 0, 0]

    def add(self, batch: pa.RecordBatch) -> None:
        import pyarrow.compute as pc

        self.rows += batch.num_rows
        for i, name in enumerate(self.columns):
            column = batch.column(batch.schema.get_field_index(name))
            if pa.types.is_string(column.type) or pa.types.is_large_string(column.type) or pa.types.is_string_view(column.type):
                column = pc.utf8_length(column)
            value = pc.sum(column).as_py()
            self.sums[i] += value if value is not None else 0

    def matches(self, expected: tuple) -> bool:
        from decimal import Decimal

        if self.rows != int(expected[0]):
            return False
        for got, want in zip(self.sums, expected[1:]):
            if isinstance(got, float):
                if abs(got - float(want)) > 1e-9 * max(1.0, abs(float(want))):
                    return False
            elif Decimal(str(got)) != Decimal(str(want)):
                return False
        return True

    def __repr__(self) -> str:
        return f"rows={self.rows} sums={self.sums}"


CHECK_SQL = (
    "SELECT count(*), sum(l_extendedprice)::VARCHAR, count(DISTINCT l_orderkey) FROM {table}"
)


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def net_bytes() -> tuple[int, int]:
    """Bytes received and sent on every non-loopback interface (the wire, TLS included).
    Zero where /proc/net/dev doesn't exist (a laptop run on loopback)."""
    rx = tx = 0
    if not os.path.exists("/proc/net/dev"):
        return 0, 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            fields = data.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def tcp_connections(port: int) -> int:
    """Established TCP connections to the server port right now (from /proc/net/tcp)."""
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if fields[3] == "01" and int(fields[2].split(":")[1], 16) == port:
                    count += 1
    return count


def cert_fingerprint(path: str) -> str:
    with open(path) as f:
        der = ssl.PEM_cert_to_DER_cert(pem_cert_string=f.read())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class AdbcSystem:
    """GizmoSQL over Flight SQL, or Hardcharger over Warp SQL: both are ADBC DB-API drivers."""

    def __init__(
        self,
        system: str,
        host: str,
        port: int,
        cert: str,
        compression: str,
        tls: bool,
        partitions: int = 1,
        order: str = "preserve",
        ingest_parts: int = 1,
    ):
        if system == "gizmosql":
            from adbc_driver_gizmosql import dbapi

            with open(cert) as f:
                pem = f.read()
            self.conn = dbapi.connect(
                uri=f"gizmosql://{host}:{port}" + ("" if tls else "?transport=tcp"),
                username="bench",
                password=PASSWORD,
                db_kwargs={
                    "adbc.flight.sql.client_option.tls_root_certs": pem,
                    # The Go Flight SQL client sends at most 16 MiB per gRPC message by
                    # default; one 122,880-row lineitem batch is ~21 MB.
                    "adbc.flight.sql.client_option.with_max_msg_size": str(1 << 30),
                },
            )
        else:
            from adbc_driver_warpsql import dbapi

            # Set both explicitly (the driver's defaults are 8 each): result partitions, which
            # the driver fetches at once over separate connections (Warp SQL §6.10), and
            # ingest parts, uploaded at once and committed as one (§8.3). 1: one stream.
            db_kwargs = {
                "adbc.warp.ipc_compression": compression,
                "adbc.warp.statement.result_partitions": str(partitions),
                "adbc.warp.statement.result_order": order,
                "adbc.warp.ingest.parts": str(ingest_parts),
            }
            # The driver refuses TLS options on a plaintext URI.
            tls_kwargs = {"tls_root_certs": cert} if tls else {}
            self.conn = dbapi.connect(
                uri=f"warp://{host}:{port}" + ("" if tls else "?tls=false"),
                username="bench",
                password=PASSWORD,
                db_kwargs=db_kwargs,
                **tls_kwargs,
            )
        self.cur = self.conn.cursor()

    def fetch(self, sql: str, collect: str, check: "FetchChecksum | None") -> tuple[int, int, int]:
        self.cur.execute(operation=sql)
        if collect == "table":
            return drain(batches=self.cur.fetch_arrow_table().to_batches(), check=check)
        return drain(batches=self.cur.fetch_record_batch(), check=check)

    def execute(self, sql: str) -> None:
        self.cur.execute(operation=sql)

    def query_one(self, sql: str) -> tuple:
        self.cur.execute(operation=sql)
        return tuple(self.cur.fetchone())

    def ingest(self, table_name: str, data: pa.Table) -> None:
        self.cur.adbc_ingest(table_name=table_name, data=data, mode="create")

    def qualify(self, table: str) -> str:
        return table


class QuackSystem:
    """DuckDB 2.0 attached to the server's database over Quack."""

    def __init__(
        self, host: str, port: int, cert: str, quack_mode: str, preserve_order: bool, tls: bool
    ):
        import duckdb

        self.duckdb = duckdb
        self.con = duckdb.connect()
        self.con.execute(query="LOAD quack")
        self.con.execute(query=f"SET preserve_insertion_order = {str(preserve_order).lower()}")
        self.uri = f"quack:{host}:{port}"
        self.fingerprint = cert_fingerprint(path=cert)
        # TLS pins the server's self-signed certificate; plaintext must say so (Quack
        # defaults to HTTPS for any host but localhost).
        self.tls_options = (
            f"ssl_fingerprint => '{self.fingerprint}'" if tls else "disable_ssl => true"
        )
        attach_tls = f"ssl_fingerprint '{self.fingerprint}'" if tls else "disable_ssl true"
        self.con.execute(
            query=f"ATTACH '{self.uri}' AS remote (TYPE quack, token '{PASSWORD}', {attach_tls})"
        )
        self.quack_mode = quack_mode

    def remote_sql(self, sql: str) -> str:
        """SQL that runs `sql` on the server, as this client is set up to send it."""
        if self.quack_mode == "query":
            quoted = sql.replace("'", "''")
            return (
                f"FROM quack_query('{self.uri}', '{quoted}', token => '{PASSWORD}', "
                f"{self.tls_options})"
            )
        return sql  # attach: the SQL names remote.<schema>.<table>

    def qualify(self, table: str) -> str:
        return table if self.quack_mode == "query" else f"remote.{table}"

    def fetch(self, sql: str, collect: str, check: "FetchChecksum | None") -> tuple[int, int, int]:
        rel = self.con.sql(query=self.remote_sql(sql=sql))
        if collect == "table":
            return drain(batches=rel.to_arrow_table().to_batches(), check=check)
        return drain(batches=rel.to_arrow_reader(), check=check)

    def execute(self, sql: str) -> None:
        self.con.execute(query=sql)

    def query_one(self, sql: str) -> tuple:
        # DuckDB 2.0 refuses new work while a result is unfinished: drain it.
        return tuple(self.con.execute(query=sql).fetchall()[0])

    def ingest(self, table_name: str, data: pa.Table) -> None:
        self.con.register(view_name="bench_source", python_object=data)
        try:
            self.con.execute(query=f"CREATE TABLE {table_name} AS SELECT * FROM bench_source")
        finally:
            self.con.unregister(view_name="bench_source")


def drain(batches, check: "FetchChecksum | None") -> tuple[int, int, int]:
    rows = nbytes = count = 0
    for batch in batches:
        rows += batch.num_rows
        nbytes += batch.nbytes
        count += 1
        if check is not None:
            check.add(batch=batch)
    return rows, nbytes, count


def load_table(path: str) -> pa.Table:
    # OSFile reads into memory (no memory map), so the table is resident before timing.
    with pa.OSFile(path=path) as f:
        table = pa.ipc.open_file(source=f).read_all()
    return table


def summarize(seconds: list[float]) -> dict:
    qs = statistics.quantiles(seconds, n=10, method="inclusive") if len(seconds) > 1 else seconds * 9
    return {"median": statistics.median(seconds), "p10": qs[0], "p90": qs[-1]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--system", required=True, choices=["gizmosql", "quack", "hardcharger"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--workload", required=True, choices=["fetch", "ingest"])
    parser.add_argument("--dataset", required=True, choices=list(TABLES))
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--compression", default="none", choices=["none", "zstd"])
    parser.add_argument("--quack-mode", default="attach", choices=["attach", "query"])
    parser.add_argument("--collect", default="stream", choices=["stream", "table"])
    parser.add_argument("--preserve-order", default="true", choices=["true", "false"])
    parser.add_argument("--tls", default="true", choices=["true", "false"])
    parser.add_argument("--partitions", type=int, default=1, help="Hardcharger: result partitions")
    parser.add_argument("--order", default="preserve", choices=["preserve", "any"])
    parser.add_argument("--ingest-parts", type=int, default=1, help="Hardcharger: ingest parts")
    parser.add_argument("--cert", default=os.path.expanduser("~/bench/tls.crt"))
    parser.add_argument("--data-dir", default=os.path.expanduser("~/bench/data"))
    parser.add_argument("--network", default="lan")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default=os.path.expanduser("~/bench/results.jsonl"))
    args = parser.parse_args()

    port = args.port or (9494 if args.system == "quack" else 31337)
    if args.system == "quack":
        sut = QuackSystem(
            host=args.host,
            port=port,
            cert=args.cert,
            quack_mode=args.quack_mode,
            preserve_order=args.preserve_order == "true",
            tls=args.tls == "true",
        )
    else:
        sut = AdbcSystem(
            system=args.system,
            host=args.host,
            port=port,
            cert=args.cert,
            compression=args.compression,
            partitions=args.partitions,
            order=args.order,
            ingest_parts=args.ingest_parts,
            tls=args.tls == "true",
        )

    config = {
        "system": args.system,
        "network": args.network,
        "workload": args.workload,
        "dataset": args.dataset,
        "compression": args.compression if args.system == "hardcharger" else "none",
        "quack_mode": args.quack_mode if args.system == "quack" else None,
        "collect": args.collect if args.workload == "fetch" else None,
        "preserve_order": args.preserve_order if args.system == "quack" else None,
        "label": args.label,
        "tls": args.tls == "true",
        "partitions": args.partitions if args.system == "hardcharger" else 1,
        "order": args.order if args.system == "hardcharger" and args.partitions > 1 else None,
        "ingest_parts": args.ingest_parts if args.system == "hardcharger" else 1,
    }

    if args.workload == "fetch":
        sql = f"SELECT * FROM {sut.qualify(table=TABLES[args.dataset])}"
        expected_rows = None
        kind = "narrow" if args.dataset == "narrow" else "lineitem"
        expected_check = sut.query_one(
            sql=FETCH_CHECK_SQL[kind].format(table=sut.qualify(table=TABLES[args.dataset]))
        )
    else:
        data = load_table(path=os.path.join(args.data_dir, f"lineitem_{args.dataset}.arrow"))
        with open(os.path.join(args.data_dir, f"lineitem_{args.dataset}.check.json")) as f:
            expected = tuple(json.load(f))
        target = sut.qualify(table="main.bench_ingest")
        if args.system != "quack":
            target = "bench_ingest"

    times: list[float] = []
    for rep in range(args.warmup + args.reps):
        measured = rep >= args.warmup
        if args.workload == "fetch":
            (rx0, tx0), cpu0, t0 = net_bytes(), cpu_seconds(), time.perf_counter()
            start_epoch = time.time()
            check = FetchChecksum(kind=kind) if not measured else None
            rows, nbytes, batches = sut.fetch(sql=sql, collect=args.collect, check=check)
            elapsed = time.perf_counter() - t0
            cpu = cpu_seconds() - cpu0
            rx1, tx1 = net_bytes()
            if expected_rows is None:
                expected_rows = rows
            assert rows == expected_rows, (rows, expected_rows)
            if check is not None and not check.matches(expected=expected_check):
                print(f"VERIFY FAILED: fetched {check}, server says {expected_check}", file=sys.stderr)
                sys.exit(1)
            extra = {"batches": batches}
        else:
            sut.execute(sql=f"DROP TABLE IF EXISTS {target}")
            (rx0, tx0), cpu0, t0 = net_bytes(), cpu_seconds(), time.perf_counter()
            start_epoch = time.time()
            sut.ingest(table_name=target, data=data)
            elapsed = time.perf_counter() - t0
            cpu = cpu_seconds() - cpu0
            rx1, tx1 = net_bytes()
            got = sut.query_one(sql=CHECK_SQL.format(table=target))
            got = (int(got[0]), str(got[1]), int(got[2]))
            if got != expected:
                print(f"VERIFY FAILED: got {got}, expected {expected}", file=sys.stderr)
                sys.exit(1)
            rows, nbytes = data.num_rows, data.nbytes
            extra = {"verified": list(got)}
        record = {
            **config,
            "rep": rep - args.warmup,
            "warmup": not measured,
            "seconds": elapsed,
            "rows": rows,
            "arrow_bytes": nbytes,
            "client_cpu_cores": cpu / elapsed,
            "wire_rx_bytes": rx1 - rx0,
            "wire_tx_bytes": tx1 - tx0,
            "tcp_connections_after": tcp_connections(port=port),
            "start_epoch": start_epoch,
            "end_epoch": start_epoch + elapsed,
            **extra,
        }
        with open(args.out, "a") as f:
            f.write(json.dumps(record) + "\n")
        print(
            f"{args.system:11} {args.workload} {args.dataset:6} rep {record['rep']:>2}"
            f"{' (warm-up)' if not measured else ''}: {elapsed:7.3f}s "
            f"{rows / elapsed / 1e6:8.2f} Mrows/s {nbytes / elapsed / 1e6:8.1f} MB/s "
            f"client {cpu / elapsed:5.2f} cores wire rx {(rx1 - rx0) / 1e6:8.1f} MB"
            f" tx {(tx1 - tx0) / 1e6:8.1f} MB conns {record['tcp_connections_after']}",
            flush=True,
        )
        if measured:
            times.append(elapsed)
    if args.workload == "fetch":
        # One more, untimed repetition that checks every row's contribution again, so a
        # measured configuration is never one whose data went wrong.
        check = FetchChecksum(kind=kind)
        rows, _, _ = sut.fetch(sql=sql, collect=args.collect, check=check)
        if not check.matches(expected=expected_check):
            print(f"VERIFY FAILED: fetched {check}, server says {expected_check}", file=sys.stderr)
            sys.exit(1)
        print(f"verified: {check} matches the server's {expected_check}", flush=True)
    s = summarize(seconds=times)
    print(
        f"SUMMARY {json.dumps(config)} median {s['median']:.3f}s p10 {s['p10']:.3f}s "
        f"p90 {s['p90']:.3f}s {rows / s['median'] / 1e6:.2f} Mrows/s "
        f"{nbytes / s['median'] / 1e6:.1f} MB/s",
        flush=True,
    )


if __name__ == "__main__":
    main()
