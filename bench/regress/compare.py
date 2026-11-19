"""Turns raw results into a report: correctness first, then flagged regressions, then every metric.

    python bench/regress/compare.py results.json [--with-baseline baselines/<host>/<v>.json ...]
        [--candidate LABEL] [--out report.md] [--save-baselines bench/regress/baselines] [--fail-on-regression]

The candidate (default: the run's own, the last server it lists) is compared with every other
label. For each metric the statistic is the median, with a two-level bootstrap interval of the
ratio candidate/baseline (stats.py); thresholds come from thresholds.json. A stored baseline
(--with-baseline) adds a version measured in another run on the same host class and settings;
the report marks such comparisons, as they are not interleaved with the candidate's runs.

Exit status: 1 when any answer was wrong or any statement failed; with --fail-on-regression,
also when a metric is flagged as a regression.
"""

import argparse
import json
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stats  # noqa: E402

# kind -> (threshold key, higher is better)
KINDS = {"latency": ("latency", False), "tail": ("tail", False), "suite": ("suite", False),
         "throughput": ("throughput", True), "cpu": ("cpu", False), "rss": ("rss", False)}
COMPARABLE_CONFIG = ("scale", "threads", "memory_limit", "server_cores", "client_cores", "warm_passes", "mp_runs",
                     "clients", "concurrent_passes")


def load_thresholds(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def extract(run: dict) -> tuple[dict, dict]:
    """(values[label][metric] = samples by repetition, meta[metric] = description)."""
    values: dict[str, dict[str, list[list[float]]]] = {}
    meta: dict[str, dict] = {}

    def add(label: str, metric: str, samples: list[float], group: str, name: str, kind: str, unit: str) -> None:
        meta.setdefault(metric, {"group": group, "name": name, "kind": kind, "unit": unit})
        values.setdefault(label, {}).setdefault(metric, []).append([s for s in samples if s is not None])

    for record in run["repetitions"]:
        label = record["label"]
        passes = record.get("tpch", [])
        if passes:
            warm = [p for p in passes if p["state"] == "warm"]
            cold = [p for p in passes if p["state"] == "cold"]
            for name in sorted({n for p in warm for n in p["statements_s"]}):
                add(label, f"tpch/{name}", [p["statements_s"].get(name) for p in warm], "TPC-H, one session (warm)",
                    name, "latency", "ms")
            add(label, "tpch/suite-warm", [p["suite_s"] for p in warm], "TPC-H, one session (warm)",
                "all 22 (suite)", "suite", "s")
            add(label, "tpch/suite-cold", [p["suite_s"] for p in cold], "TPC-H, one session (cold)",
                "all 22, first pass after start", "suite", "s")
        for name, times in record.get("many-pipelines", {}).items():
            group = "Many pipelines, one session"
            add(label, f"mp/{name}/p50", times, group, f"{name} p50", "latency", "ms")
            add(label, f"mp/{name}/p90", [stats.percentile(values=times, fraction=0.9)], group, f"{name} p90", "tail", "ms")
            add(label, f"mp/{name}/p99", [stats.percentile(values=times, fraction=0.99)], group, f"{name} p99", "tail", "ms")
            add(label, f"mp/{name}/max", [max(times)], group, f"{name} max", "info", "ms")
        for clients, result in sorted(record.get("concurrent", {}).items(), key=lambda kv: int(kv[0])):
            group = f"Concurrent, {clients} client{'s' if clients != '1' else ''}"
            latencies = result["latencies_s"]
            add(label, f"concurrent/{clients}/throughput", [result["throughput_qps"]], group, "throughput",
                "throughput", "q/s")
            if latencies:
                for fraction, tag, kind in ((0.5, "p50", "latency"), (0.9, "p90", "tail"), (0.99, "p99", "tail")):
                    add(label, f"concurrent/{clients}/{tag}", [stats.percentile(values=latencies, fraction=fraction)],
                        group, f"latency {tag}", kind, "ms")
            add(label, f"concurrent/{clients}/errors", [len(result["errors"])], group, "errors", "info", "count")
        if "tpch-10x22" in record:
            result = record["tpch-10x22"]
            add(label, "tpch-10x22/wall", [result["wall_s"]], "Ten clients x 22 queries (test_tpch_benchmark)",
                "wall time", "suite", "s")
            add(label, "tpch-10x22/errors", [len(result["errors"])], "Ten clients x 22 queries (test_tpch_benchmark)",
                "errors", "info", "count")
        for workload, resource in record.get("resources", {}).items():
            add(label, f"cpu/{workload}", [resource["server_cpu_s"]], "Server CPU time", workload, "cpu", "s")
        if record.get("peak_rss_bytes"):
            add(label, "rss/peak", [record["peak_rss_bytes"]], "Server memory", "peak resident set", "rss", "MiB")
    return values, meta


def merge_runs(runs: list[dict]) -> dict:
    """The first run, plus each later run's servers (stored baselines) under their own labels."""
    merged = json.loads(json.dumps(runs[0]))
    merged["stored"] = {}
    for other in runs[1:]:
        for key in COMPARABLE_CONFIG:
            if other["config"].get(key) != merged["config"].get(key):
                raise SystemExit(f"stored baseline differs in {key}: {other['config'].get(key)!r} vs "
                                 f"{merged['config'].get(key)!r}; compare like with like")
        if other["host"]["host_class"] != merged["host"]["host_class"]:
            raise SystemExit(f"stored baseline is from host class {other['host']['host_class']}, "
                             f"this run from {merged['host']['host_class']}")
        for label, server in other["servers"].items():
            new_label = label if label not in merged["servers"] else f"{label} (stored)"
            merged["servers"][new_label] = server
            merged["stored"][new_label] = other.get("started_at")
            merged["load"][new_label] = other.get("load", {}).get(label, {})
            for record in other["repetitions"]:
                if record["label"] == label:
                    merged["repetitions"].append({**record, "label": new_label})
            checks = other["correctness"]["checks_by_label"].get(label, 0)
            merged["correctness"]["checks"] += checks
            merged["correctness"]["checks_by_label"][new_label] = checks
            merged["correctness"]["failures"] += [{**f, "label": new_label} for f in other["correctness"]["failures"]
                                                  if f["label"] == label]
            merged["order"].insert(0, new_label)
    return merged


def fmt(value: float, unit: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if unit == "ms":
        ms = value * 1000
        return f"{ms:.2f}" if ms < 10 else f"{ms:.1f}" if ms < 1000 else f"{ms:.0f}"
    if unit == "MiB":
        return f"{value / 2**20:.0f}"
    if unit == "count":
        return f"{value:.0f}"
    return f"{value:.3f}" if value < 10 else f"{value:.2f}" if value < 100 else f"{value:.1f}"


def compare_metric(candidate: list[list[float]], baseline: list[list[float]], kind: str, unit: str,
                   thresholds: dict) -> dict:
    point, low, high = stats.ratio_interval(candidate=candidate, baseline=baseline,
                                            iterations=thresholds["bootstrap_iterations"],
                                            confidence=thresholds["confidence"])
    status = "info"
    if kind in KINDS:
        key, higher_is_better = KINDS[kind]
        change = stats.median(samples_by_rep=candidate) - stats.median(samples_by_rep=baseline)
        minimum = {"ms": thresholds["min_change_ms"] / 1000, "s": thresholds["min_change_ms"] / 1000,
                   "MiB": thresholds["min_change_mib"] * 2**20}.get(unit, 0.0)
        if kind == "cpu":
            minimum = thresholds["min_change_cpu_s"]
        status = stats.judge(point=point, low=low, high=high, threshold=thresholds[key],
                             higher_is_better=higher_is_better, absolute_change=change, minimum_change=minimum)
    return {"ratio": point, "low": low, "high": high, "status": status}


MARK = {"regression": "**REGRESSION**", "possible regression": "possible", "improvement": "improved", "ok": "",
        "info": ""}


def build_report(runs: list[dict], thresholds: dict, candidate: str | None = None) -> tuple[str, dict]:
    run = merge_runs(runs=runs)
    values, meta = extract(run=run)
    labels = [label for label in run["order"] if label in values]
    candidate = candidate or run["candidate"]
    baselines = [label for label in labels if label != candidate]
    comparisons: dict[tuple[str, str], dict] = {}
    for metric, info in meta.items():
        if candidate not in values or metric not in values[candidate]:
            continue
        for base in baselines:
            if metric in values.get(base, {}):
                comparisons[(metric, base)] = compare_metric(candidate=values[candidate][metric],
                                                             baseline=values[base][metric], kind=info["kind"],
                                                             unit=info["unit"], thresholds=thresholds)
    failures = run["correctness"]["failures"]
    errors = []
    for record in run["repetitions"]:
        for workload in ("tpch-10x22",):
            errors += [(record["label"], record["rep"], workload, e) for e in record.get(workload, {}).get("errors", [])]
        for clients, result in record.get("concurrent", {}).items():
            errors += [(record["label"], record["rep"], f"concurrent c={clients}", e) for e in result["errors"]]
    flagged = [(metric, base, c) for (metric, base), c in comparisons.items() if c["status"] != "ok" and c["status"] != "info"]
    regressions = [f for f in flagged if f[2]["status"] == "regression"]
    possible = [f for f in flagged if f[2]["status"] == "possible regression"]
    improvements = [f for f in flagged if f[2]["status"] == "improvement"]

    host, config = run["host"], run["config"]
    lines = [f"# Hardcharger performance regression report", ""]
    lines += [f"Run {run['started_at']} to {run.get('finished_at', '(unfinished)')} on **{host['host_class']}** "
              f"({host['cpu_model']}, {host['logical_cpus']} logical CPUs, {host['memory_bytes'] / 2**30:.0f} GiB, "
              f"{host['system']} {host['release']}).", ""]
    lines += [f"Settings: TPC-H scale factor {config['scale']:g}, {config['threads']} DuckDB threads, memory limit "
              f"{config['memory_limit']}, server cores {config['server_cores'] or 'not pinned'}, client cores "
              f"{config['client_cores'] or 'not pinned'}; {config['repetitions']} repetitions (server starts) per "
              f"version, round-robin; {config['warm_passes']} warm TPC-H passes each; many-pipelines statements "
              f"{config['mp_runs']} times each; concurrent clients {', '.join(str(c) for c in config['clients'])} "
              f"x {config['concurrent_passes']} passes; transport {config['transport']}; client Python "
              f"{run['client']['python']}, httpx {run['client']['httpx']}, pyarrow {run['client']['pyarrow']}; "
              f"answers {run.get('answers', '?')}; OS page cache "
              f"{'dropped before each start' if config['drop_caches'] else 'kept'}.", ""]
    lines += ["| Version | Server | DuckDB | Binary sha256 | Source |", "|---|---|---|---|---|"]
    for label in labels:
        server = run["servers"][label]
        stored = f" (stored baseline from {run['stored'][label]}, not interleaved)" if label in run.get("stored", {}) else ""
        role = " (candidate)" if label == candidate else ""
        lines.append(f"| {label}{role} | {server['version']} | {server['duckdb_version']} | `{server['sha256'][:12]}` | "
                     f"{server['source']}{stored} |")
    lines.append("")
    verdict_line = (f"{len(regressions)} regression(s), {len(possible)} possible, {len(improvements)} improvement(s); "
                    f"{run['correctness']['checks']} answers checked, {len(failures)} wrong; {len(errors)} statement error(s)")
    lines += [f"**Verdict for {candidate}:** {verdict_line}.", ""]

    lines += ["## Correctness", ""]
    if not failures and not errors:
        lines += [f"All {run['correctness']['checks']} checked answers were right; no statement failed.", ""]
    else:
        lines += ["| Version | Rep | Workload | Statement | Problem |", "|---|---|---|---|---|"]
        for f in failures[:200]:
            lines.append(f"| {f['label']} | {f['rep'] + 1} | {f['workload']} | {f['name']} | {f['detail'][:200]} |")
        for label, rep, workload, error in errors[:200]:
            lines.append(f"| {label} | {rep + 1} | {workload} | (error) | {error[:200]} |")
        lines.append("")

    lines += ["## Flagged changes", ""]
    t = thresholds
    lines += [f"Thresholds: latency {t['latency']:.0%}, suite elapsed {t['suite']:.0%}, tail latency {t['tail']:.0%}, "
              f"throughput {t['throughput']:.0%}, server CPU {t['cpu']:.0%}, memory {t['rss']:.0%}; changes under "
              f"{t['min_change_ms']:g} ms (CPU: {t['min_change_cpu_s']:g} s, memory: {t['min_change_mib']:g} MiB) are "
              f"never flagged. **REGRESSION**: the whole {t['confidence']:.0%} interval of the ratio is past the "
              f"threshold; *possible*: the median ratio is past it and the whole interval is slower than no change, "
              f"but not all of it past the threshold.", ""]
    if flagged:
        lines += ["| Status | Metric | Against | Baseline | Candidate | Change | 95% interval |", "|---|---|---|---|---|---|---|"]
        order = {"regression": 0, "possible regression": 1, "improvement": 2}
        for metric, base, c in sorted(flagged, key=lambda f: (order[f[2]["status"]], f[0])):
            info = meta[metric]
            lines.append(f"| {MARK[c['status']]} | {info['group']}: {info['name']} | {base} | "
                         f"{fmt(stats.median(samples_by_rep=values[base][metric]), info['unit'])} {info['unit']} | "
                         f"{fmt(stats.median(samples_by_rep=values[candidate][metric]), info['unit'])} {info['unit']} | "
                         f"{(c['ratio'] - 1):+.1%} | {(c['low'] - 1):+.1%} to {(c['high'] - 1):+.1%} |")
        lines.append("")
    else:
        lines += ["Nothing flagged.", ""]

    lines += ["## All metrics", "",
              "Medians over all repetitions; the change columns are the candidate against each baseline, "
              "with the 95% bootstrap interval.", ""]
    groups: dict[str, list[str]] = {}
    for metric, info in meta.items():
        groups.setdefault(info["group"], []).append(metric)
    for group, metrics in groups.items():
        unit = meta[metrics[0]]["unit"]
        lines += [f"### {group}", ""]
        header = ["Metric"] + [f"{label} ({meta[metrics[0]]['unit']})" if len({meta[m]['unit'] for m in metrics}) == 1
                               else label for label in labels] + [f"{candidate} vs {base}" for base in baselines]
        lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for metric in metrics:
            info = meta[metric]
            unit = info["unit"]
            cells = [info["name"] + ("" if len({meta[m]['unit'] for m in metrics}) == 1 else f" ({unit})")]
            for label in labels:
                samples = values.get(label, {}).get(metric)
                cells.append(fmt(stats.median(samples_by_rep=samples), unit) if samples and stats.pooled(samples) else "-")
            for base in baselines:
                c = comparisons.get((metric, base))
                if not c or info["kind"] == "info":
                    cells.append("")
                    continue
                cells.append(f"{(c['ratio'] - 1):+.1%} [{(c['low'] - 1):+.0%}, {(c['high'] - 1):+.0%}] "
                             f"{MARK[c['status']]}".rstrip())
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    lines += ["### Loading (once per version, not interleaved; informational)", "",
              "| Version | TPC-H from Parquet (s) | Many-pipelines tables (s) |", "|---|---|---|"]
    for label in labels:
        load = run["load"].get(label) or {}
        lines.append(f"| {label} | {fmt(load.get('tpch_parquet_load_s', float('nan')), 's')} | "
                     f"{fmt(load.get('many_pipelines_tables_s', float('nan')), 's')} |")
    lines.append("")
    lines += ["### Per repetition (server start, order, load average)", "",
              "| Rep | Order | Start (s) | Load average (1 min) |", "|---|---|---|---|"]
    by_rep: dict[int, list[dict]] = {}
    for record in run["repetitions"]:
        if record["label"] in run.get("stored", {}):
            continue
        by_rep.setdefault(record["rep"], []).append(record)
    for rep, records in sorted(by_rep.items()):
        records.sort(key=lambda r: r["position"])
        lines.append(f"| {rep + 1} | {' > '.join(r['label'] for r in records)} | "
                     f"{', '.join(f'{r['server_start_s']:.2f}' for r in records)} | "
                     f"{', '.join(f'{r['load_average'][0]:.1f}' for r in records)} |")
    lines.append("")

    verdict = {"candidate": candidate, "baselines": baselines, "summary": verdict_line,
               "regressions": [{"metric": m, "baseline": b, **c} for m, b, c in regressions],
               "possible": [{"metric": m, "baseline": b, **c} for m, b, c in possible],
               "improvements": [{"metric": m, "baseline": b, **c} for m, b, c in improvements],
               "correctness_failures": len(failures), "errors": len(errors)}
    return "\n".join(lines), verdict


def save_baselines(run: dict, directory: pathlib.Path) -> list[pathlib.Path]:
    """One file per version: <directory>/<host class>/<label>.json, the run with only that version."""
    written = []
    target = directory / run["host"]["host_class"]
    target.mkdir(parents=True, exist_ok=True)
    for label in run["servers"]:
        subset = {**run, "servers": {label: run["servers"][label]}, "order": [label], "candidate": label,
                  "load": {label: run["load"].get(label, {})},
                  "repetitions": [r for r in run["repetitions"] if r["label"] == label],
                  "correctness": {"checks": run["correctness"]["checks_by_label"].get(label, 0),
                                  "checks_by_label": {label: run["correctness"]["checks_by_label"].get(label, 0)},
                                  "failures": [f for f in run["correctness"]["failures"] if f["label"] == label]}}
        path = target / f"{label}.json"
        path.write_text(json.dumps(subset, indent=1))
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", help="results.json of a run")
    parser.add_argument("--with-baseline", action="append", default=[], help="a stored baseline file to compare with")
    parser.add_argument("--candidate", help="label to judge (default: the run's candidate)")
    parser.add_argument("--thresholds", default=str(HERE / "thresholds.json"))
    parser.add_argument("--out", help="write the report here (default: print it)")
    parser.add_argument("--save-baselines", help="also store each version of this run under DIR/<host class>/")
    parser.add_argument("--fail-on-regression", action="store_true", help="exit 1 when a regression is flagged")
    args = parser.parse_args()
    runs = [json.loads(pathlib.Path(args.results).read_text())]
    runs += [json.loads(pathlib.Path(path).read_text()) for path in args.with_baseline]
    report, verdict = build_report(runs=runs, thresholds=load_thresholds(path=pathlib.Path(args.thresholds)),
                                   candidate=args.candidate)
    if args.out:
        pathlib.Path(args.out).write_text(report)
        pathlib.Path(args.out).with_suffix(".verdict.json").write_text(json.dumps(verdict, indent=1))
    else:
        print(report)
    print(verdict["summary"], file=sys.stderr)
    if args.save_baselines:
        for path in save_baselines(run=runs[0], directory=pathlib.Path(args.save_baselines)):
            print(f"stored {path}", file=sys.stderr)
    failed = verdict["correctness_failures"] or verdict["errors"]
    return 1 if failed or (args.fail_on_regression and verdict["regressions"]) else 0


if __name__ == "__main__":
    sys.exit(main())
