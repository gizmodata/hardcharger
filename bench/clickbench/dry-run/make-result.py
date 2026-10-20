#!/usr/bin/env python3
"""Turns a ClickBench run's log into a result JSON shaped like ClickBench's own.

ClickBench's sink parses the same lines (`Load time:`, `Data size:`, the 43 `[t1,t2,t3],` rows,
`Concurrent QPS:`, `Concurrent error ratio:`, `Machine:`) into `<system>/results/<YYYYMMDD>/<machine>.json`.
This does that locally, for a run that must not reach the sink.

    make-result.py --log log --template ../hardcharger/template.json --date 2026-10-20 > out.json
"""

import argparse
import json
import re
import sys


def parse_log(text):
    load_times = [float(m) for m in re.findall(r"^Load time: ([0-9.]+)$", text, flags=re.M)]
    size = re.search(r"^Data size: ([0-9]+)$", text, flags=re.M)
    machine = re.search(r"^Machine: (\S+)$", text, flags=re.M)
    rows = [json.loads(s=m.rstrip(",")) for m in re.findall(r"^\[[-0-9.null, ]+\],$", text, flags=re.M)]

    def number_or_null(pattern):
        m = re.search(pattern, text, flags=re.M)
        return None if m is None or m.group(1) == "null" else float(m.group(1))

    return {
        "load_time": round(sum(load_times)) if load_times else None,
        "data_size": int(size.group(1)) if size else None,
        "machine": machine.group(1) if machine else None,
        "concurrent_qps": number_or_null(r"^Concurrent QPS: (\S+)$"),
        "concurrent_error_ratio": number_or_null(r"^Concurrent error ratio: (\S+)$"),
        "result": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--date", required=True, help="UTC date of the run, YYYY-MM-DD")
    args = parser.parse_args()

    with open(file=args.template, encoding="utf-8") as handle:
        template = json.load(fp=handle)
    with open(file=args.log, encoding="utf-8", errors="replace") as handle:
        parsed = parse_log(text=handle.read())
    if len(parsed["result"]) != 43:
        print(f"expected 43 result rows, found {len(parsed['result'])}", file=sys.stderr)
        return 1

    out = {
        "system": template["system"],
        "date": args.date,
        "machine": parsed["machine"],
        "cluster_size": 1,
        "proprietary": template["proprietary"],
        "hardware": template["hardware"],
        "tuned": template["tuned"],
        "tags": template["tags"],
        "load_time": parsed["load_time"],
        "data_size": parsed["data_size"],
        "concurrent_qps": parsed["concurrent_qps"],
        "concurrent_error_ratio": parsed["concurrent_error_ratio"],
        "result": parsed["result"],
    }
    # ClickBench's layout: four-space indent, one query row per line.
    head = json.dumps(obj={k: v for k, v in out.items() if k != "result"}, indent=4)[:-2]
    head = re.sub(r'"tags": \[\s*([^\]]*?)\s*\]', lambda m: '"tags": [' + re.sub(r",\s+", ",", m.group(1)) + "]", head)
    rows = ",\n".join("        " + json.dumps(obj=row) for row in out["result"])
    print(head + ',\n    "result": [\n' + rows + "\n]\n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
