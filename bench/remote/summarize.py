"""Summarizes results-*.jsonl (from client.py) into Markdown tables.

    python3 summarize.py --results results/results-lan.jsonl results/results-wan.jsonl \
        --cpu results/cpu-server.log.gz [--label plaintext|batch122880]

Per configuration: median, p10 and p90 wall time over the measured repetitions; rows/s and
MB/s (Arrow in-memory bytes, 1 MB = 10^6 bytes) at the median; wire MB (both directions,
TLS included) per repetition; client CPU (cores the client process used); and server CPU
(whole-machine busy cores averaged over each repetition's window, from cpu_log.py).
"""

import argparse
import bisect
import gzip
import json
import statistics
from collections import defaultdict


def load_cpu(path: str | None) -> tuple[list[float], list[float]]:
    if not path:
        return [], []
    times, cores = [], []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, mode="rt") as f:
        for line in f:
            t, c = line.split()
            times.append(float(t))
            cores.append(float(c))
    return times, cores


def window_mean(times: list[float], cores: list[float], start: float, end: float) -> float | None:
    # Samples are taken at the END of each interval; include those inside the window.
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end)
    samples = cores[lo:hi]
    return statistics.mean(samples) if samples else None


def name(r: dict) -> str:
    if r["system"] == "gizmosql":
        return "GizmoSQL 1.42.0 (Flight SQL)"
    if r["system"] == "quack":
        order = "" if r["workload"] == "fetch" else (
            ", insertion order kept" if r["preserve_order"] == "true" else ", unordered"
        )
        return f"Quack ({r['quack_mode']}{order})"
    extras = []
    if r["compression"] == "zstd":
        extras.append("zstd")
    if r.get("partitions", 1) > 1:
        extras.append(f"{r['partitions']} partitions" + (", any order" if r.get("order") == "any" else ", in order"))
    if r["workload"] == "ingest" and r.get("ingest_parts", 1) > 1:
        extras.append(f"{r['ingest_parts']} upload parts")
    return f"{HARDCHARGER} (Warp SQL{''.join(', ' + e for e in extras)})"


HARDCHARGER = "Hardcharger"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", required=True)
    parser.add_argument("--cpu")
    parser.add_argument("--label", default="", help="only rows with this label")
    parser.add_argument("--hardcharger", default="Hardcharger", help="how Hardcharger rows are named")
    args = parser.parse_args()
    global HARDCHARGER
    HARDCHARGER = args.hardcharger
    times, cores = load_cpu(path=args.cpu)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for path in args.results:
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                if r["warmup"] or r.get("label", "") != args.label:
                    continue
                key = (r["network"], r["workload"], r["dataset"], name(r))
                groups[key].append(r)
    current = None
    for key in sorted(groups, key=lambda k: (k[0], k[1] == "ingest", ["sf1", "sf10", "narrow"].index(k[2]))):
        network, workload, dataset, label = key
        if (network, workload, dataset) != current:
            current = (network, workload, dataset)
            rows = groups[key][0]["rows"]
            print(f"\n#### {network.upper()} {workload} {dataset} ({rows:,} rows)\n")
            print("| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |")
            print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        runs = groups[key]
        secs = [r["seconds"] for r in runs]
        med = statistics.median(secs)
        q = statistics.quantiles(secs, n=10, method="inclusive")
        nbytes = statistics.median([r["arrow_bytes"] for r in runs])
        wire = statistics.median([r["wire_rx_bytes"] + r["wire_tx_bytes"] for r in runs])
        client = statistics.median([r["client_cpu_cores"] for r in runs])
        server = [window_mean(times=times, cores=cores, start=r["start_epoch"], end=r["end_epoch"]) for r in runs]
        server = [s for s in server if s is not None]
        server_txt = f"{statistics.median(server):.1f}" if server else "n/a"
        print(
            f"| {label} | {med:.3f} | {q[0]:.3f} | {q[-1]:.3f} | {runs[0]['rows'] / med / 1e6:.1f} "
            f"| {nbytes / med / 1e6:,.0f} | {wire / 1e6:,.0f} | {client:.1f} | {server_txt} |"
        )


if __name__ == "__main__":
    main()
