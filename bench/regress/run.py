#!/usr/bin/env python3
"""Performance regression suite: runs the same workloads against several Hardcharger server
versions on one machine and writes raw results (results.json) and a report (report.md).

    python bench/regress/run.py --release 2.4.1 --release 2.13.1 --server 2.14.0-dev=server/build/hardcharger-server \\
        --scale 1 --threads 8 --server-cores 0-7 --client-cores 8-31 --repetitions 5 --out results/run1

Each repetition starts every version once, in a rotated order (round-robin, so drift and order
effects spread evenly), and runs, in this order:

  tpch            one session: the 22 TPC-H queries once just after the start (cold), then
                  --warm-passes more times (warm); per-statement latency and suite elapsed time
  many-pipelines  one session: two statements with ~30 pipelines each, --mp-runs times each
  concurrent      --clients N (default 1, 4, 10): N processes, one session each, each running
                  the 22 queries --concurrent-passes times from its own starting point;
                  throughput, latency distribution and errors
  tpch-10x22      ten sessions, each running the 22 queries once from q01 (the ten-client case
                  of server/tests/test_tpch_benchmark.py); wall time

Every result is checked: TPC-H against DuckDB's published answers (scale factors 0.01, 0.1 and
1; at other scales against the first server's own answer), the many-pipelines statements
against answers computed in Python. A wrong answer is a correctness failure whatever the timing.
The last server listed is the candidate; the report compares it with each of the others.
See docs/benchmarks.md.
"""

import argparse
import datetime
import functools
import hashlib
import json
import multiprocessing
import os
import pathlib
import platform
import queue
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compare
import httpx
import psutil
import pyarrow
import stats
import workers
import workloads

import server as servers

SCHEMA_VERSION = 1
ALL_WORKLOADS = ["tpch", "many-pipelines", "concurrent", "tpch-10x22"]


def log(message: str) -> None:
    print(f"{datetime.datetime.now(tz=datetime.UTC):%H:%M:%S} {message}", flush=True)


def instance_type() -> str | None:
    """The EC2 instance type, from the instance metadata service (IMDSv2), or None."""
    try:
        token_request = urllib.request.Request(url="http://169.254.169.254/latest/api/token", method="PUT",
                                               headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(token_request, timeout=0.5) as response:
            token = response.read().decode()
        request = urllib.request.Request(url="http://169.254.169.254/latest/meta-data/instance-type",
                                         headers={"X-aws-ec2-metadata-token": token})
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return response.read().decode()
    except OSError:
        return None


def cpu_model() -> str:
    if platform.system() == "Linux":
        for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if platform.system() == "Darwin":
        done = subprocess.run(args=["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                              check=False)
        return done.stdout.strip()
    return platform.processor()


def host_info(args) -> dict:
    ec2 = instance_type()
    host_class = args.host_class or (f"aws-{ec2}" if ec2 else f"{platform.system().lower()}-{platform.machine()}-local")
    return {"host_class": host_class, "instance_type": ec2, "system": platform.system(),
            "release": platform.release(), "machine": platform.machine(), "cpu_model": cpu_model(),
            "logical_cpus": os.cpu_count(), "memory_bytes": psutil.virtual_memory().total,
            "load_average_at_start": list(os.getloadavg())}


def suite_fingerprint() -> str:
    """sha256 over the suite's own files (a copy on a benchmark host has no git checkout)."""
    digest = hashlib.sha256()
    for path in sorted(HERE.glob("*.py")) + [HERE / "requirements.txt", HERE / "thresholds.json"]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def git_sha() -> str | None:
    done = subprocess.run(args=["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=False)
    return done.stdout.strip() or None


def resolve_servers(args, work: pathlib.Path) -> list[dict]:
    specs = []
    for version in args.release:
        specs.append({"label": version, "binary": servers.install_release(version=version, cache=work / "installs"),
                      "source": f"release {version} (official installer)"})
    for item in args.server:
        label, _, path = item.partition("=")
        if not path:
            raise SystemExit(f"--server takes LABEL=PATH, got {item!r}")
        specs.append({"label": label, "binary": pathlib.Path(path).resolve(), "source": f"binary {path}"})
    if args.order:
        by_label = {s["label"]: s for s in specs}
        specs = [by_label[label] for label in args.order.split(",")]
    labels = [s["label"] for s in specs]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"labels must differ: {labels}")
    for spec in specs:
        spec.update(servers.describe_binary(binary=spec["binary"]))
        spec["binary"] = str(spec["binary"])
    return specs


def make_server(spec: dict, args, work: pathlib.Path) -> servers.Server:
    label = spec["label"]
    return servers.Server(label=label, binary=pathlib.Path(spec["binary"]),
                          database=work / "db" / f"{label}-{workloads.scale_name(scale=args.scale)}.duckdb",
                          log=work / "logs" / f"server-{label}.log", threads=args.threads,
                          memory_limit=args.memory_limit, cores=servers.parse_cores(spec=args.server_cores))


def prepare(specs: list[dict], args, work: pathlib.Path, results: dict) -> None:
    """TPC-H Parquet (once, with the first server), then each version's own database file."""
    data_root = work / "data"
    (work / "db").mkdir(parents=True, exist_ok=True)
    (work / "logs").mkdir(parents=True, exist_ok=True)
    first = specs[0]
    manifest_path = workloads.tpch_dir(data_root=data_root, scale=args.scale) / "manifest.json"
    if not manifest_path.exists():
        log(f"generating TPC-H sf {args.scale:g} with {first['label']}")
        scratch = work / "db" / "generate.duckdb"
        for leftover in work.glob("db/generate.duckdb*"):
            leftover.unlink()
        gen = servers.Server(label=first["label"], binary=pathlib.Path(first["binary"]), database=scratch,
                             log=work / "logs" / "generate.log", threads=args.threads,
                             memory_limit=args.memory_limit, cores=None)
        gen.start()
        try:
            session = gen.session()
            workloads.generate_tpch(session=session, data_root=data_root, scale=args.scale)
            session.close()
        finally:
            gen.stop()
            for leftover in work.glob("db/generate.duckdb*"):
                leftover.unlink()
    results["data"] = json.loads(manifest_path.read_text())
    for spec in specs:
        server = make_server(spec=spec, args=args, work=work)
        marker = server.database.with_suffix(".loaded.json")
        if marker.exists():
            results["load"][spec["label"]] = json.loads(marker.read_text())
            continue
        for leftover in server.database.parent.glob(server.database.name + "*"):
            leftover.unlink()
        server.start()
        try:
            session = server.session()
            began = time.perf_counter()
            for sql in workloads.tpch_load_sql(data_root=data_root, scale=args.scale):
                session.query(sql=sql)
            tpch_s = time.perf_counter() - began
            began = time.perf_counter()
            for sql in workloads.mp_load_sql(scale=args.scale):
                session.query(sql=sql)
            mp_s = time.perf_counter() - began
            session.query(sql="CHECKPOINT")
            rows = session.scalar(sql="SELECT count(*) FROM lineitem")
            session.close()
        finally:
            server.stop()
        if rows != results["data"]["rows"]["lineitem"]:
            raise RuntimeError(f"{spec['label']} loaded {rows} lineitem rows, expected {results['data']['rows']['lineitem']}")
        loaded = {"tpch_parquet_load_s": tpch_s, "many_pipelines_tables_s": mp_s}
        marker.write_text(json.dumps(loaded))
        results["load"][spec["label"]] = loaded
        log(f"{spec['label']}: loaded TPC-H in {tpch_s:.2f} s, many-pipelines tables in {mp_s:.2f} s")


def stringify(table) -> list[list]:
    """A result as text cells, the form of the published answers (a reference answer)."""
    rows = []
    for row in table.to_pylist():
        rows.append([None if v is None else v.isoformat() if hasattr(v, "isoformat") else str(v) for v in row.values()])
    return rows


class Recorder:
    def __init__(self, results: dict):
        self.results = results

    def checked(self, label: str, count: int = 1) -> None:
        self.results["correctness"]["checks"] += count
        by_label = self.results["correctness"]["checks_by_label"]
        by_label[label] = by_label.get(label, 0) + count

    def correctness(self, label: str, rep: int, workload: str, name: str, problem: str | None) -> None:
        self.checked(label=label)
        if problem:
            self.results["correctness"]["failures"].append(
                {"label": label, "rep": rep, "workload": workload, "name": name, "detail": problem})
            log(f"  WRONG ANSWER {label} {workload} {name}: {problem}")


def run_tpch(server, label: str, rep: int, args, queries, expected, recorder, record: dict) -> None:
    session = server.session()
    try:
        passes = []
        for number in range(1 + args.warm_passes):
            state = "cold" if number == 0 else "warm"
            times = {}
            began = time.perf_counter()
            for name, sql in queries:
                started = time.perf_counter()
                try:
                    table = session.query(sql=sql)
                except Exception as error:  # noqa: BLE001
                    recorder.correctness(label=label, rep=rep, workload="tpch", name=name, problem=f"error: {error}")
                    continue
                times[name] = time.perf_counter() - started
                if name not in expected and table is not None:
                    expected[name] = stringify(table=table)  # the first server's answer is the reference
                    recorder.results["reference_answers"][name] = label
                recorder.correctness(label=label, rep=rep, workload="tpch", name=name,
                                     problem=workloads.check_answer(table=table, expected=expected.get(name)))
            passes.append({"state": state, "suite_s": time.perf_counter() - began, "statements_s": times})
        record["tpch"] = passes
        warm = [p["suite_s"] for p in passes if p["state"] == "warm"]
        log(f"  {label} tpch: cold {passes[0]['suite_s']:.3f} s, warm median {stats.median(samples_by_rep=[warm]):.3f} s")
    finally:
        session.close()


def run_many_pipelines(server, label: str, rep: int, args, recorder, record: dict) -> None:
    expected = workloads.mp_expected(scale=args.scale)
    session = server.session()
    try:
        out = {}
        for name, sql in workloads.MP_QUERIES.items():
            recorder.correctness(label=label, rep=rep, workload="many-pipelines", name=name,
                                 problem=workloads.check_answer(table=session.query(sql=sql), expected=expected[name]))
            for _ in range(args.mp_warmup):
                session.query(sql=sql)
            times = []
            for _ in range(args.mp_runs):
                started = time.perf_counter()
                session.query(sql=sql)
                times.append(time.perf_counter() - started)
            out[name] = times
            log(f"  {label} {name}: p50 {1000 * stats.percentile(values=times, fraction=0.5):.1f} ms, "
                f"p99 {1000 * stats.percentile(values=times, fraction=0.99):.1f} ms, max {1000 * max(times):.1f} ms")
        record["many-pipelines"] = out
    finally:
        session.close()


def run_clients(server, label: str, rep: int, clients: int, passes: int, spread: bool, queries, expected, args,
                recorder, workload: str) -> dict:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(clients)
    results = context.Queue()
    cores = servers.parse_cores(spec=args.client_cores)
    processes = []
    for index in range(clients):
        offset = (index * len(queries)) // clients if spread else 0
        process = context.Process(target=workers.client_main, kwargs={
            "base_url": server.base_url, "username": servers.USERNAME, "password": servers.PASSWORD,
            "statements": queries, "expected": expected, "passes": passes, "offset": offset, "cores": cores,
            "barrier": barrier, "results": results})
        process.start()
        processes.append(process)
    records = []
    for _ in processes:
        try:
            records.append(results.get(timeout=args.client_timeout))
        except queue.Empty:
            break
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.kill()
    errors = [e for r in records for e in r["errors"]]
    if len(records) < clients:
        errors.append(f"{clients - len(records)} client(s) never reported")
    for r in records:
        for wrong in r["wrong"]:
            name, _, detail = wrong.partition(": ")
            recorder.correctness(label=label, rep=rep, workload=workload, name=name, problem=detail)
    recorder.checked(label=label, count=sum(len(r["latencies"]) for r in records) - sum(len(r["wrong"]) for r in records))
    latencies = [seconds for r in records for _, seconds in r["latencies"]]
    started = [r["started"] for r in records if r["started"]]
    finished = [r["finished"] for r in records if r["finished"]]
    wall = (max(finished) - min(started)) if started and finished else 0.0
    out = {"clients": clients, "passes": passes, "statements": len(latencies), "errors": errors,
           "wall_s": wall, "throughput_qps": len(latencies) / wall if wall else 0.0, "latencies_s": latencies}
    p = (lambda f: 1000 * stats.percentile(values=latencies, fraction=f)) if latencies else (lambda f: float("nan"))
    log(f"  {label} {workload} c={clients}: {len(latencies)} statements in {wall:.2f} s = {out['throughput_qps']:.1f} q/s, "
        f"p50 {p(0.5):.0f} ms p90 {p(0.9):.0f} ms p99 {p(0.99):.0f} ms, {len(errors)} errors")
    return out


def measured(server, record: dict, workload: str, run):
    """Runs one workload, recording the server's CPU seconds and wall time around it; returns
    what the workload returns."""
    cpu = server.sampler.cpu_seconds()
    began = time.perf_counter()
    result = run()
    record.setdefault("resources", {})[workload] = {"server_cpu_s": server.sampler.cpu_seconds() - cpu,
                                                    "wall_s": time.perf_counter() - began}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", action="append", default=[], help="a published version (installed with the official installer)")
    parser.add_argument("--server", action="append", default=[], help="LABEL=PATH of a hardcharger-server binary")
    parser.add_argument("--order", help="comma-separated labels: the order (last = candidate); default: releases, then servers")
    parser.add_argument("--scale", type=float, default=1.0, help="TPC-H scale factor (default 1)")
    parser.add_argument("--threads", type=int, default=8, help="DuckDB threads per server (default 8)")
    parser.add_argument("--memory-limit", default="16GiB", help="DuckDB memory_limit per server (default 16GiB)")
    parser.add_argument("--server-cores", help="Linux: cores the server is pinned to, e.g. 0-7 (taskset)")
    parser.add_argument("--client-cores", help="Linux: cores the client processes are pinned to, e.g. 8-31")
    parser.add_argument("--repetitions", type=int, default=5, help="server starts per version (default 5; 0: only install, generate and load)")
    parser.add_argument("--warm-passes", type=int, default=3, help="warm TPC-H passes per repetition (default 3)")
    parser.add_argument("--mp-runs", type=int, default=50, help="timed runs of each many-pipelines statement (default 50)")
    parser.add_argument("--mp-warmup", type=int, default=5, help="untimed runs before them (default 5)")
    parser.add_argument("--clients", default="1,4,10", help="concurrent client counts (default 1,4,10)")
    parser.add_argument("--concurrent-passes", type=int, default=2, help="passes over the 22 queries per client (default 2)")
    parser.add_argument("--client-timeout", type=float, default=1800, help="seconds to wait for a concurrent client")
    parser.add_argument("--workloads", default=",".join(ALL_WORKLOADS), help=f"subset of {','.join(ALL_WORKLOADS)}")
    parser.add_argument("--drop-caches", action="store_true", help="Linux + sudo: empty the page cache before each start")
    parser.add_argument("--host-class", help="baseline directory name (default: aws-<instance type> or <os>-<arch>-local)")
    parser.add_argument("--work", default=str(HERE / "work"), help="data, databases, installs and logs (kept between runs)")
    parser.add_argument("--out", required=True, help="directory for results.json and report.md")
    parser.add_argument("--thresholds", default=str(HERE / "thresholds.json"), help="regression thresholds (JSON)")
    args = parser.parse_args()

    selected = args.workloads.split(",")
    unknown = set(selected) - set(ALL_WORKLOADS)
    if unknown:
        raise SystemExit(f"unknown workloads: {sorted(unknown)}")
    work = pathlib.Path(args.work).resolve()
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (cores := servers.parse_cores(spec=args.client_cores)) and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cores)

    specs = resolve_servers(args=args, work=work)
    if len(specs) < 1:
        raise SystemExit("name at least one server (--release or --server)")
    results = {
        "suite": "hardcharger-perf-regress", "schema_version": SCHEMA_VERSION,
        "started_at": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        "host": host_info(args=args),
        "config": {"scale": args.scale, "threads": args.threads, "memory_limit": args.memory_limit,
                   "server_cores": args.server_cores, "client_cores": args.client_cores,
                   "repetitions": args.repetitions, "warm_passes": args.warm_passes, "mp_runs": args.mp_runs,
                   "mp_warmup": args.mp_warmup, "clients": [int(c) for c in args.clients.split(",") if c],
                   "concurrent_passes": args.concurrent_passes, "workloads": selected,
                   "drop_caches": args.drop_caches, "transport": "Chargewire HTTP/1.1, plaintext, loopback"},
        "client": {"python": platform.python_version(), "httpx": httpx.__version__, "pyarrow": pyarrow.__version__,
                   "bench_git_sha": git_sha(), "suite_sha256": suite_fingerprint()},
        "servers": {s["label"]: {k: v for k, v in s.items() if k != "label"} for s in specs},
        "order": [s["label"] for s in specs], "candidate": specs[-1]["label"],
        "load": {}, "reference_answers": {}, "repetitions": [],
        "correctness": {"checks": 0, "checks_by_label": {}, "failures": []},
    }
    prepare(specs=specs, args=args, work=work, results=results)
    if args.repetitions == 0:
        log("prepared (installs, data, databases); no repetitions asked for")
        return 0
    data_root = work / "data"
    queries = workloads.tpch_queries(data_root=data_root, scale=args.scale)
    expected = workloads.tpch_expected(data_root=data_root, scale=args.scale)
    # Without published answers at this scale, the first server's answers (repetition 0 runs it
    # first) are the reference every other run must match.
    results["answers"] = "published (tpch_answers)" if expected else f"reference: {specs[0]['label']}'s own answers"
    recorder = Recorder(results=results)

    for rep in range(args.repetitions):
        rotation = rep % len(specs)
        order = specs[rotation:] + specs[:rotation]
        log(f"repetition {rep + 1}/{args.repetitions}: {', '.join(s['label'] for s in order)}")
        for position, spec in enumerate(order):
            label = spec["label"]
            cache_dropped = servers.drop_os_caches() if args.drop_caches else False
            server = make_server(spec=spec, args=args, work=work)
            server.start()
            record = {"rep": rep, "label": label, "position": position, "os_cache_dropped": cache_dropped,
                      "server_start_s": server.start_seconds,
                      "load_average": list(os.getloadavg())}
            try:
                session = server.session()
                record["settings"] = {
                    "threads": session.scalar(sql="SELECT current_setting('threads')"),
                    "memory_limit": session.scalar(sql="SELECT current_setting('memory_limit')"),
                    "duckdb": session.scalar(sql="SELECT version()"),
                    "enable_progress_bar": session.scalar(sql="SELECT current_setting('enable_progress_bar')"),
                }
                session.close()
                if int(record["settings"]["threads"]) != args.threads:
                    raise RuntimeError(f"{label} runs {record['settings']['threads']} threads, not {args.threads}")
                common = {"server": server, "label": label, "rep": rep, "args": args, "recorder": recorder}
                if "tpch" in selected:
                    measured(server=server, record=record, workload="tpch", run=functools.partial(
                        run_tpch, queries=queries, expected=expected, record=record, **common))
                if "many-pipelines" in selected:
                    measured(server=server, record=record, workload="many-pipelines", run=functools.partial(
                        run_many_pipelines, record=record, **common))
                if "concurrent" in selected:
                    record["concurrent"] = {}
                    for clients in results["config"]["clients"]:
                        record["concurrent"][str(clients)] = measured(
                            server=server, record=record, workload=f"concurrent-{clients}", run=functools.partial(
                                run_clients, clients=clients, passes=args.concurrent_passes, spread=True,
                                queries=queries, expected=expected, workload="concurrent", **common))
                if "tpch-10x22" in selected:
                    record["tpch-10x22"] = measured(
                        server=server, record=record, workload="tpch-10x22", run=functools.partial(
                            run_clients, clients=10, passes=1, spread=False, queries=queries, expected=expected,
                            workload="tpch-10x22", **common))
                record["peak_rss_bytes"] = server.peak_rss_linux() or server.sampler.peak_rss
            finally:
                server.stop()
            results["repetitions"].append(record)
            (out / "results.json").write_text(json.dumps(results, indent=1))  # a partial run is still a record

    results["finished_at"] = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    (out / "results.json").write_text(json.dumps(results, indent=1))
    log(f"wrote {out / 'results.json'}")
    report, verdict = compare.build_report(runs=[results], thresholds=compare.load_thresholds(path=pathlib.Path(args.thresholds)))
    (out / "report.md").write_text(report)
    (out / "verdict.json").write_text(json.dumps(verdict, indent=1))
    log(f"wrote {out / 'report.md'}: {verdict['summary']}")
    return 1 if verdict["correctness_failures"] or verdict["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
