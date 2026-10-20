# TPC-H SF1000 on one VM: GizmoSQL stable, GizmoSQL edge, Hardcharger

Reproduces the method of "TPC-H 1 TB in 80 Seconds" (gizmodata.com, 2026-09-04) and adds the two
DuckDB 2.0 servers. Internal; results never go anywhere public.

- **VM:** `r9gd.24xlarge` on demand, us-east-2 (the SF10 dry run: `r8gd.2xlarge` Spot). Local NVMe
  in RAID 0 (`mdadm --create /dev/md0 --level=0 --raid-devices=<n>`, `mkfs.ext4 -F`, `/data`).
- **Data:** tpch-datagen Parquet from `s3://sidewindr-benchmark-data-us-east-2/tpch/sf=<N>/`, fetched
  with presigned GET URLs (the VM never gets AWS credentials) to `/data/tpch/sf<N>/<table>/`.
- **Load (untimed):** file A by GizmoSQL stable; file B by GizmoSQL edge, which Hardcharger also
  uses if it opens it (else Hardcharger loads file C). The published eight `CREATE TABLE ... AS
  SELECT * FROM read_parquet(...)` statements, through each server.
- **Runs, in order:** stable on A, edge on B, Hardcharger on B. OS page cache dropped and the server
  started fresh before each (soft fd limit raised to the hard limit for every server: edge's SF1000
  load hit 1024); 22 queries in order, 3 runs each, one connection, client on the VM.
  GizmoSQL through `benchmark-flight-sql --num-query-runs 3` (0.0.6); Hardcharger through
  `warp_benchmark.py`, the same loop over `adbc-driver-hardcharger`. TLS on all three; no tuning.
- **Total** = sum of the 22 per-query means; **cost per run** = total / 3600 x hourly price.

```bash
cd bench/remote/tpch1tb
set -a; . <aws .env>; set +a                      # never printed
TYPE=r9gd.24xlarge MARKET=on-demand AZ=us-east-2c LIFETIME_MIN=240 bash provision.sh up
bash run.sh push 1000 hardcharger-linux-arm64.tar.gz adbc_driver_hardcharger-*-linux_aarch64.whl
bash run.sh start 1000                            # fetch load stable edge hardcharger probe
bash run.sh wait 1000
bash run.sh pull 1000 <dir>
python3 report.py --results-dir <dir>/results/sf1000 --price-per-hour 7.69 \
  --instance r9gd.24xlarge --published published-2026-09-04-r9gd.24xlarge.json
bash provision.sh down                            # ends with `check`: the teardown proof
```

`nodelay.c` and `SERVER_LD_PRELOAD` are diagnostics only (the SF10 dry run used them to show that
Hardcharger's accepted sockets lack TCP_NODELAY); no reported run preloads anything.
