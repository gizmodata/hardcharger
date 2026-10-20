"""The comparison report for one TPC-H-derived run directory (Markdown on stdout).

    python report.py --results-dir results/sf1000 --price-per-hour 7.69 --instance r9gd.24xlarge \
        [--published published-2026-09-04-r9gd.24xlarge.json]

Reads <dir>/stable.json, edge.json (benchmark-flight-sql output) and hardcharger.json
(warp_benchmark.py output), plus each server's <name>-info.json. Per query: every run and the
mean; total = sum of the 22 means; cost per run = total / 3600 x price. Exits non-zero if any
run failed or a query's row count differs between the servers (or between runs).
"""

import argparse
import json
import sys
from pathlib import Path

# TPC fair use: every report says once that this is derived from TPC-H, not a TPC-H result.
DISCLAIMER = ("Derived from the TPC-H benchmark; not an official TPC-H result and not comparable to published T"
              "PC-H results, as these results do not comply with the TPC-H specification. TPC-H™ is a trademark of the Transaction Processing Performance Council (TPC).")

SERVERS = [("stable", "GizmoSQL stable"), ("edge", "GizmoSQL edge"), ("hardcharger", "Hardcharger")]


def load_run(path: Path) -> dict:
    doc = json.loads(path.read_text())
    queries = {}
    for batch in doc["query_run_results"]:
        number = int(batch["query"]["query_id"].split("-")[-1])
        runs = batch["runs"]
        queries[number] = {"runs_s": [r["run_time"] for r in runs],
                           "mean_s": sum(r["run_time"] for r in runs) / len(runs),
                           "rows": [r.get("row_count") for r in runs],
                           "status": [r["status"] for r in runs]}
    return {"version": doc.get("database_version"), "queries": queries}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--price-per-hour", type=float, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--published")
    args = parser.parse_args()
    directory = Path(args.results_dir)

    runs = {key: load_run(path=directory / f"{key}.json") for key, _ in SERVERS
            if (directory / f"{key}.json").exists()}
    info = {key: json.loads((directory / f"{key}-info.json").read_text()) for key in runs
            if (directory / f"{key}-info.json").exists()}
    published = json.loads(Path(args.published).read_text()) if args.published else None
    pub_q = {q["query"]: q for q in published["queries"]} if published else {}
    present = [(k, label) for k, label in SERVERS if k in runs]
    problems = []

    out = [f"*{DISCLAIMER}*", "", f"### Per query (seconds; {args.instance})", ""]
    head = "| Query |"
    rule = "|---|"
    if published:
        head += " Published G5 runs | Published mean |"
        rule += "---|---:|"
    for _, label in present:
        head += f" {label} runs | {label} mean |"
        rule += "---|---:|"
    if "hardcharger" in runs and "stable" in runs:
        head += " HC / stable |"
        rule += "---:|"
    if "hardcharger" in runs and "edge" in runs:
        head += " HC / edge |"
        rule += "---:|"
    out += [head, rule]
    for number in range(1, 23):
        row = f"| Q{number:02d} |"
        if published:
            p = pub_q[number]
            row += f" {' / '.join(f'{r:.2f}' for r in p['runs_s'])} | {p['mean_s']:.3f} |"
        for key, _ in present:
            q = runs[key]["queries"][number]
            row += f" {' / '.join(f'{r:.2f}' for r in q['runs_s'])} | {q['mean_s']:.3f} |"
            if any(s != "SUCCESS" for s in q["status"]):
                problems.append(f"{key} Q{number:02d}: {q['status']}")
            if len(set(q["rows"])) != 1:
                problems.append(f"{key} Q{number:02d}: row counts differ between runs {q['rows']}")
        if "hardcharger" in runs and "stable" in runs:
            row += f" {runs['hardcharger']['queries'][number]['mean_s'] / runs['stable']['queries'][number]['mean_s']:.2f} |"
        if "hardcharger" in runs and "edge" in runs:
            row += f" {runs['hardcharger']['queries'][number]['mean_s'] / runs['edge']['queries'][number]['mean_s']:.2f} |"
        out.append(row)
        counts = {key: runs[key]["queries"][number]["rows"][0] for key, _ in present}
        if len(set(counts.values())) != 1:
            problems.append(f"Q{number:02d}: row counts differ between servers {counts}")

    totals = {key: sum(q["mean_s"] for q in runs[key]["queries"].values()) for key in runs}
    row = "| **Total** |"
    if published:
        row += f" {' / '.join(f'{t:.2f}' for t in published['published_run_totals_s'])} | **{published['total_of_means_s']:.3f}** |"
    for key, _ in present:
        per_run = [sum(q["runs_s"][i] for q in runs[key]["queries"].values()) for i in range(3)]
        row += f" {' / '.join(f'{t:.2f}' for t in per_run)} | **{totals[key]:.3f}** |"
    if "hardcharger" in runs and "stable" in runs:
        row += f" **{totals['hardcharger'] / totals['stable']:.2f}** |"
    if "hardcharger" in runs and "edge" in runs:
        row += f" **{totals['hardcharger'] / totals['edge']:.2f}** |"
    out.append(row)

    out += ["", "### Totals and cost per run", "",
            "| Server | Engine | Total of 22 means (s) | Cost per run (USD) |", "|---|---|---:|---:|"]
    if published:
        out.append(f"| Published GizmoSQL (2026-09-04) | DuckDB {published['duckdb']} | "
                   f"{published['total_of_means_s']:.3f} | "
                   f"{published['total_of_means_s'] / 3600 * published['price_per_hour_usd']:.4f} |")
    for key, label in present:
        engine = runs[key]["version"]
        source = ""
        if key in info:
            pv = info[key].get("pragma_version") or [{}]
            source = f" ({pv[0].get('source_id', '?')})"
        out.append(f"| {label} | DuckDB {engine}{source} | {totals[key]:.3f} | "
                   f"{totals[key] / 3600 * args.price_per_hour:.4f} |")

    out += ["", "### Row counts (run 1; identical across runs and servers unless listed below)", "",
            "| " + " | ".join(f"Q{n:02d}" for n in range(1, 23)) + " |", "|" + "---:|" * 22]
    first = present[0][0]
    out.append("| " + " | ".join(str(runs[first]["queries"][n]["rows"][0]) for n in range(1, 23)) + " |")
    out += ["", "### Checks", ""]
    out += [f"- PROBLEM: {p}" for p in problems] or ["- All runs succeeded; every query's row count matches "
                                                     "across its three runs and across the servers."]
    print("\n".join(out))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
