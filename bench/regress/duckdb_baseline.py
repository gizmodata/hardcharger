"""Measure the candidate database with the stock DuckDB wheel in a fresh process."""

import argparse
import json
import math
import os
import pathlib
import sys

import stats
import workloads


class EmbeddedSession:
    def __init__(self, connection):
        self.connection = connection

    def query(self, sql):
        return self.connection.sql(sql).to_arrow_table()

    def close(self):
        self.connection.close()


class EmbeddedDatabase:
    def __init__(self, path, threads, memory_limit):
        self.path = path
        self.config = {"threads": str(threads), "memory_limit": memory_limit}

    def session(self):
        import duckdb

        return EmbeddedSession(duckdb.connect(str(self.path), config=self.config))


def samples(record, warm_passes, mp_runs):
    expected_queries = {f"q{i:02}" for i in range(1, 23)}
    warm = [p["statements_s"] for p in record["tpch"] if p["state"] == "warm"]
    if len(warm) != warm_passes or not all(set(p) == expected_queries for p in warm):
        raise ValueError("incomplete warm TPC-H measurements")
    # Exclude Python answer checking from both totals. Each statement includes fetching all rows.
    metrics = {"TPC-H warm 22-query total": [sum(p.values()) for p in warm]}
    for name in workloads.MP_QUERIES:
        times = record["many-pipelines"][name]
        if len(times) != mp_runs:
            raise ValueError(f"incomplete many-pipelines measurements for {name}")
        metrics[name] = times
    if not all(math.isfinite(v) and v > 0 for values in metrics.values() for v in values):
        raise ValueError("invalid benchmark timing")
    return metrics


def compare_overhead(results, thresholds):
    config = results["config"]
    records = [r for r in results["repetitions"] if r["label"] == results["candidate"]]
    if (sorted(r["rep"] for r in records) != list(range(config["repetitions"]))
            or len(records) < thresholds["min_repetitions"]):
        raise ValueError("incomplete candidate / upstream DuckDB comparison")
    sides = [[samples(r if side == "candidate" else r["upstream_duckdb"],
                      config["warm_passes"], config["mp_runs"]) for r in records]
             for side in ("candidate", "upstream")]
    version = results["servers"][results["candidate"]]["version"].partition("-rc.")[0]
    budget = thresholds.get("duckdb_overhead_by_release", {}).get(version, thresholds["duckdb_overhead"])
    metrics = {}
    for name in sides[0][0]:
        candidate, baseline = [[rep[name] for rep in side] for side in sides]
        ratio, low, high = stats.paired_ratio_interval(candidate, baseline, thresholds["bootstrap_iterations"],
                                               thresholds["confidence"])
        metrics[name] = {"ratio": ratio, "low": low, "high": high, "over_budget": low > 1 + budget}
    return {"budget": budget, "confidence": thresholds["confidence"], "metrics": metrics}


def main():
    # Reuse the server suite's timers, SQL, result materialization boundary and answer checks.
    import run

    options = json.load(sys.stdin)
    args = argparse.Namespace(**options["config"])
    if args.server_cores and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, run.servers.parse_cores(args.server_cores))
    import duckdb

    if "v" + duckdb.__version__ != options["version"]:
        raise ValueError(f"DuckDB wheel {duckdb.__version__} differs from {options['version']}")
    database = EmbeddedDatabase(options["database"], args.threads, args.memory_limit)
    root = pathlib.Path(options["data"])
    queries = workloads.tpch_queries(root, args.scale)
    expected = workloads.tpch_expected(root, args.scale)
    if len(expected) != 22 or set(expected) != {name for name, _ in queries}:
        raise ValueError("published answers required for all 22 queries")
    correctness = {"checks": 0, "checks_by_label": {}, "failures": []}
    record = {"version": duckdb.__version__, "correctness": correctness}
    recorder = run.Recorder({"correctness": correctness})
    common = dict(server=database, label="upstream DuckDB", rep=options["rep"], args=args,
                  recorder=recorder, record=record)
    run.run_tpch(queries=queries, expected=expected, **common)
    run.run_many_pipelines(**common)
    pathlib.Path(options["output"]).write_text(json.dumps(record))


if __name__ == "__main__":
    main()
