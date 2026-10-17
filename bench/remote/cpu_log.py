"""Logs whole-machine CPU use every --interval seconds as "epoch busy_cores" lines.

Runs on the server during a benchmark (nothing else runs there); summarize.py averages the
samples inside each repetition's start/end window to get the server's CPU use.

    nohup python3 cpu_log.py --out ~/bench/cpu-server.log &
"""

import argparse
import os
import time


def read_jiffies() -> tuple[int, int]:
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    return sum(fields[:8]) - idle, sum(fields[:8])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()
    cores = os.cpu_count()
    busy0, total0 = read_jiffies()
    with open(args.out, "a", buffering=1) as out:
        while True:
            time.sleep(args.interval)
            busy, total = read_jiffies()
            if total > total0:
                out.write(f"{time.time():.3f} {cores * (busy - busy0) / (total - total0):.3f}\n")
            busy0, total0 = busy, total


if __name__ == "__main__":
    main()
