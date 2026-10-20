#!/usr/bin/env python3
"""Compares ClickBench result files for one machine: totals, and ClickBench's own rating.

    compare.py ours.json ../ClickBench/gizmosql/results/20260511/c6a.4xlarge.json ...

Cold = the first run of each query; hot = the faster of runs 2 and 3. The rating is ClickBench's
(README, "Results Usage"): per query, (10 ms + time) / (10 ms + best time among the files given),
a missing result counted as 2 x max(300 s, the entry's slowest query), then the geometric mean over
the 43 queries. It is relative to the files given here, not to the whole ClickBench board.
"""

import json
import math
import sys


def load(path):
    with open(file=path, encoding="utf-8") as handle:
        data = json.load(fp=handle)
    rows = data["result"]
    cold = [r[0] for r in rows]
    hot = [min(r[1], r[2]) if r[1] is not None and r[2] is not None else None for r in rows]
    return {"name": f"{data['system']} ({data['date']})", "data": data, "cold": cold, "hot": hot}


def rating(entries, key):
    n = len(entries[0][key])
    best = []
    for q in range(n):
        values = [e[key][q] for e in entries if e[key][q] is not None]
        best.append(min(values) if values else None)
    out = []
    for e in entries:
        present = [v for v in e[key] if v is not None]
        penalty = 2 * max(300.0, max(present) if present else 0.0)
        logs = []
        for q, v in enumerate(e[key]):
            if best[q] is None:
                continue
            logs.append(math.log((0.01 + (penalty if v is None else v)) / (0.01 + best[q])))
        out.append(math.exp(sum(logs) / len(logs)))
    return out


def main():
    entries = [load(path=p) for p in sys.argv[1:]]
    cold_r = rating(entries=entries, key="cold")
    hot_r = rating(entries=entries, key="hot")
    print(f"{'entry':40} {'load s':>7} {'size GB':>8} {'cold sum':>9} {'hot sum':>8} {'cold x':>7} {'hot x':>6} {'QPS':>7} {'err':>6} nulls")
    for e, c, h in zip(entries, cold_r, hot_r):
        d = e["data"]
        nulls = sum(v is None for r in d["result"] for v in r)
        cs = sum(v for v in e["cold"] if v is not None)
        hs = sum(v for v in e["hot"] if v is not None)
        qps = d.get("concurrent_qps")
        err = d.get("concurrent_error_ratio")
        print(f"{e['name']:40} {d['load_time']:>7} {d['data_size'] / 1e9:>8.2f} {cs:>9.2f} {hs:>8.2f} {c:>7.2f} {h:>6.2f} "
              f"{'-' if qps is None else qps:>7} {'-' if err is None else err:>6} {nulls}")
    if len(entries) >= 2:
        a, b = entries[0], entries[1]
        print(f"\nper query, {a['name']} / {b['name']} (hot; > 1.5x or < 0.67x marked)")
        for q in range(len(a["hot"])):
            x, y = a["hot"][q], b["hot"][q]
            if x is None or y is None:
                print(f"  Q{q + 1:<3} {x} vs {y}")
                continue
            r = (0.01 + x) / (0.01 + y)
            mark = " <<" if r < 0.67 else (" >>" if r > 1.5 else "")
            print(f"  Q{q + 1:<3} cold {a['cold'][q]:>7.3f} vs {b['cold'][q]:>7.3f}   hot {x:>7.3f} vs {y:>7.3f}   x{r:5.2f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
