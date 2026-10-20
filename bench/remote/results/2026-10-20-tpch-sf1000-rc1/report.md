# TPC-H SF1000: Hardcharger 2.0.0-rc.1 vs GizmoSQL v1.42.0 stable and edge (2026-10-20)

Internal. Method as in `../../tpch1tb/README.md` and the SF10 dry run (`../2026-10-19-tpch-sf10-dryrun/`):
one r9gd.24xlarge on demand (us-east-2c, 96 vCPU, 742 GiB, 3 x 1.9 TB NVMe in RAID 0), SF1000 Parquet
from tpch-datagen loaded into DuckDB files (untimed), then per server: page cache dropped, server
started fresh with defaults plus TLS, 22 queries x 3 runs over one connection, client on the VM.
Total = sum of the 22 per-query means. Stable ran on file A (loaded by stable); edge and Hardcharger
both ran on file B (loaded by edge; Hardcharger opened it). Every server had 96 threads and a
593.6 GiB memory limit (the DuckDB defaults).

## Summary

| Server | Engine | Pass 1 total (s) | Pass 2 total (s) | vs published 80.475 s | Cost per run (pass 1) |
|---|---|---:|---:|---:|---:|
| Published GizmoSQL (2026-09-04) | DuckDB v1.5.5 | 80.475 | | 1.00 | $0.1719 |
| GizmoSQL v1.42.0 stable | DuckDB v1.5.6 (069cc9f9b) | 76.957 | 76.196 | 0.96 | $0.1644 |
| GizmoSQL v1.42.0 edge | DuckDB v2.0.0-alpha46057 (b15358c371) | 49.455 | 49.166 | 0.61 | $0.1056 |
| Hardcharger 2.0.0-rc.1 | DuckDB v2.0.0-alpha48624 (ffe0b8b7a2) | **42.722** | **42.658** | **0.53** | **$0.0913** |

- Hardcharger / edge = 0.86 (pass 1 and pass 2 alike); Hardcharger / stable = 0.56.
- Pass 2 repeated the three timed runs on the same VM and files right after pass 1 (`pass2/`);
  every total is within 1% of pass 1. All 396 query runs succeeded; row counts match across runs
  and servers. (Q11 returns 0 rows on every server: the benchmark's query text uses a fixed
  0.0001 fraction, not 0.0001/SF.)
- Biggest Hardcharger gains over edge (pass 1 means): Q09 5.97 vs 8.57 s (0.70), Q22 0.78 vs 1.15
  (0.67), Q20 0.90 vs 1.21 (0.74), Q14 1.00 vs 1.34 (0.75), Q21 2.41 vs 3.14 (0.77), Q07 1.21 vs
  1.56 (0.78), Q03 1.75 vs 2.14 (0.82). Slightly slower than edge: Q17 (1.08), Q19 (1.05), Q06 (1.04);
  in pass 2 those three are 1.01, 0.99 and 1.00, so they are within noise (a tie with edge).
- Both DuckDB 2.0 servers vs stable: Q21 0.22, Q19 0.29, Q14 0.39, Q09 0.45 (Hardcharger / stable).
- The first (cold) run of each query is the same on all three (Q01 run 1 is ~6 s everywhere: reading
  the 265-282 GB file from NVMe). The gains are in the warm runs.

## Versions

- GizmoSQL v1.42.0 public release zips `gizmosql_cli_linux_arm64.zip` (stable, DuckDB v1.5.6,
  069cc9f9b) and `gizmosql_cli_linux_arm64_edge.zip` (edge, DuckDB v2.0.0-alpha46057, b15358c371);
  client benchmark-flight-sql 0.0.6 (28bb41e), adbc-driver-gizmosql 2.0.15, pyarrow 25.0.1.
- Hardcharger v2.0.0-rc.1 release asset `hardcharger-linux-arm64.tar.gz` (tag feaa5d39, sha256
  b7b95bf1..., checked against SHA256SUMS); DuckDB pin from warp-sql 0e8dc876: ffe0b8b7a2,
  v2.0.0-alpha48624. Client `warp_benchmark.py` over adbc-driver-hardcharger 0.1.0 built from
  hardcharger-adbc `fix/adbc-error-status` 3d1cbc6a with warp-sql-adbc 15ff0499 (Warp SQL 1.0;
  Go 1.27.2; wheel sha256 cf20e19d...), pyarrow 26.0.0. Full list: `versions.txt`.
- Ubuntu 24.04.5, kernel 7.0.0-1014-aws.

### Edge vs Hardcharger DuckDB builds

- **Engine commits:** b15358c371 (2026-10-09) vs ffe0b8b7a2 (2026-10-16): Hardcharger's pin is
  1,371 commits newer, a descendant of edge's. This is the most likely source of the per-query
  differences; this run doesn't separate engine from server.
- **Same build recipe otherwise:** both Release, gcc-toolset-14 in manylinux_2_28_aarch64 (GizmoSQL's
  image unpinned, Hardcharger's pinned by digest), DuckDB's default jemalloc, static libduckdb, no
  LTO, no native-arch flags (DuckDB's default `-march=armv8-a` on both).
- **Built-in extensions:** both have icu and tpch built in; Hardcharger also builds in httpfs, ducklake,
  postgres_scanner and autocomplete (not used by TPC-H on local files). Hardcharger applies two DuckDB
  patches (a view-binder hook for access control; HUGEINT Arrow export range check).
- **Path:** GizmoSQL uses DuckDB's C++ API and Arrow Flight SQL over gRPC; Hardcharger uses the v2 C
  API Arrow results over Warp SQL (HTTP, Arrow IPC). Both over TLS on 127.0.0.1.

## Notes

- **File-descriptor limit.** The first load attempt failed in GizmoSQL edge's `CREATE TABLE lineitem`
  with "Too many open files" (lineitem is 755 Parquet files; the default soft limit is 1024; stable
  loaded fine). `servers.sh` now raises the soft limit (`ulimit -n 1048576`, the hard limit) for
  every server; the load was rerun from scratch for both files, and every reported run used it. The
  failed attempt is at the top of `pipeline.log`.
- **Probe:** edge and Hardcharger both open the DuckDB 1.5 file A; Hardcharger opens edge's file B.
- **Cost and time:** the VM ran 09:30:06-~10:08:30Z (~39 min on demand at $7.69/h, about $5.0, plus
  a 100 GB gp3 root for the same time). `provision.sh down` then `check`: no instances, volumes,
  security groups, key pairs or Spot requests tagged Purpose=hardcharger-benchmark in us-east-2 or
  us-west-2.

## Pass 1 detail (generated by report.py)

### Per query (seconds; r9gd.24xlarge)

| Query | Published G5 runs | Published mean | GizmoSQL stable runs | GizmoSQL stable mean | GizmoSQL edge runs | GizmoSQL edge mean | Hardcharger runs | Hardcharger mean | HC / stable | HC / edge |
|---|---|---:|---|---:|---|---:|---|---:|---:|---:|
| Q01 | 5.35 / 1.69 / 1.68 | 2.905 | 6.04 / 1.28 / 1.28 | 2.868 | 6.01 / 0.85 / 0.85 | 2.571 | 5.80 / 0.95 / 0.95 | 2.565 | 0.89 | 1.00 |
| Q02 | 0.85 / 0.47 / 0.55 | 0.624 | 0.73 / 0.35 / 0.29 | 0.457 | 0.68 / 0.18 / 0.17 | 0.342 | 0.64 / 0.17 / 0.17 | 0.325 | 0.71 | 0.95 |
| Q03 | 2.69 / 2.50 / 2.50 | 2.561 | 3.02 / 2.37 / 2.40 | 2.597 | 2.83 / 1.79 / 1.79 | 2.136 | 2.45 / 1.40 / 1.41 | 1.754 | 0.68 | 0.82 |
| Q04 | 3.09 / 2.23 / 2.37 | 2.563 | 3.87 / 2.09 / 2.28 | 2.744 | 2.76 / 1.13 / 1.12 | 1.668 | 2.55 / 1.01 / 1.00 | 1.521 | 0.55 | 0.91 |
| Q05 | 2.31 / 2.06 / 2.34 | 2.237 | 2.75 / 2.21 / 1.92 | 2.293 | 2.34 / 1.71 / 1.79 | 1.950 | 2.05 / 1.43 / 1.42 | 1.632 | 0.71 | 0.84 |
| Q06 | 0.60 / 0.62 / 0.58 | 0.599 | 0.49 / 0.35 / 0.43 | 0.422 | 0.25 / 0.25 / 0.25 | 0.250 | 0.26 / 0.26 / 0.26 | 0.259 | 0.61 | 1.04 |
| Q07 | 1.82 / 2.00 / 2.31 | 2.042 | 1.69 / 2.13 / 1.78 | 1.866 | 1.58 / 1.56 / 1.55 | 1.564 | 1.28 / 1.13 / 1.23 | 1.214 | 0.65 | 0.78 |
| Q08 | 2.02 / 1.82 / 2.50 | 2.115 | 2.65 / 1.89 / 1.83 | 2.126 | 2.36 / 1.33 / 1.36 | 1.682 | 2.13 / 1.25 / 1.26 | 1.546 | 0.73 | 0.92 |
| Q09 | 14.16 / 13.57 / 13.86 | 13.865 | 13.53 / 13.36 / 13.13 | 13.342 | 9.01 / 8.16 / 8.55 | 8.570 | 6.32 / 5.98 / 5.62 | 5.974 | 0.45 | 0.70 |
| Q10 | 6.05 / 4.30 / 4.48 | 4.947 | 6.16 / 4.19 / 4.49 | 4.943 | 4.43 / 4.36 / 4.55 | 4.447 | 4.12 / 4.05 / 4.22 | 4.131 | 0.84 | 0.93 |
| Q11 | 1.20 / 0.84 / 0.50 | 0.847 | 0.88 / 0.78 / 0.55 | 0.737 | 0.30 / 0.49 / 0.80 | 0.531 | 0.27 / 0.48 / 0.77 | 0.506 | 0.69 | 0.95 |
| Q12 | 1.39 / 1.58 / 1.64 | 1.537 | 1.32 / 1.32 / 1.15 | 1.264 | 0.93 / 0.78 / 0.74 | 0.818 | 0.82 / 0.71 / 0.70 | 0.747 | 0.59 | 0.91 |
| Q13 | 7.55 / 8.72 / 8.81 | 8.360 | 6.88 / 8.01 / 8.48 | 7.789 | 7.61 / 6.84 / 6.73 | 7.059 | 7.43 / 6.65 / 6.78 | 6.954 | 0.89 | 0.99 |
| Q14 | 3.23 / 2.15 / 2.07 | 2.487 | 3.43 / 1.79 / 2.40 | 2.541 | 1.47 / 1.45 / 1.08 | 1.336 | 1.09 / 0.84 / 1.06 | 0.999 | 0.39 | 0.75 |
| Q15 | 1.37 / 0.97 / 1.18 | 1.173 | 0.94 / 0.92 / 0.92 | 0.926 | 0.80 / 0.74 / 0.74 | 0.760 | 0.75 / 0.73 / 0.74 | 0.741 | 0.80 | 0.98 |
| Q16 | 2.53 / 2.12 / 1.95 | 2.199 | 1.54 / 1.67 / 1.42 | 1.544 | 0.89 / 0.89 / 0.89 | 0.891 | 0.80 / 0.82 / 0.80 | 0.805 | 0.52 | 0.90 |
| Q17 | 2.52 / 2.05 / 2.77 | 2.446 | 1.92 / 3.05 / 2.60 | 2.522 | 1.12 / 1.13 / 1.05 | 1.100 | 1.12 / 1.23 / 1.21 | 1.187 | 0.47 | 1.08 |
| Q18 | 9.73 / 10.27 / 10.22 | 10.075 | 8.89 / 8.98 / 9.54 | 9.139 | 5.82 / 5.47 / 5.55 | 5.613 | 5.20 / 4.96 / 5.04 | 5.067 | 0.55 | 0.90 |
| Q19 | 2.53 / 2.72 / 2.34 | 2.530 | 1.98 / 2.92 / 2.38 | 2.426 | 0.73 / 0.65 / 0.64 | 0.672 | 0.77 / 0.69 / 0.66 | 0.707 | 0.29 | 1.05 |
| Q20 | 2.68 / 2.53 / 2.62 | 2.611 | 2.15 / 1.94 / 1.63 | 1.906 | 1.32 / 1.16 / 1.13 | 1.205 | 0.91 / 0.88 / 0.90 | 0.897 | 0.47 | 0.74 |
| Q21 | 8.17 / 12.03 / 8.85 | 9.686 | 10.92 / 11.68 / 9.90 | 10.833 | 2.68 / 3.38 / 3.35 | 3.136 | 2.34 / 2.50 / 2.40 | 2.412 | 0.22 | 0.77 |
| Q22 | 2.46 / 1.78 / 1.96 | 2.066 | 1.84 / 1.51 / 1.66 | 1.671 | 1.26 / 1.17 / 1.04 | 1.154 | 0.84 / 0.77 / 0.73 | 0.779 | 0.47 | 0.67 |
| **Total** | 84.29 / 79.04 / 78.10 | **80.475** | 83.62 / 74.78 / 72.47 | **76.957** | 57.18 / 45.46 / 45.73 | **49.455** | 49.94 / 38.89 / 39.33 | **42.722** | **0.56** | **0.86** |

### Totals and cost per run

| Server | Engine | Total of 22 means (s) | Cost per run (USD) |
|---|---|---:|---:|
| Published GizmoSQL (2026-09-04) | DuckDB v1.5.5 | 80.475 | 0.1719 |
| GizmoSQL stable | DuckDB v1.5.6 (069cc9f9b) | 76.957 | 0.1644 |
| GizmoSQL edge | DuckDB v2.0.0-alpha46057 (b15358c371) | 49.455 | 0.1056 |
| Hardcharger | DuckDB v2.0.0-alpha48624 (ffe0b8b7a2) | 42.722 | 0.0913 |

### Row counts (run 1; identical across runs and servers unless listed below)

| Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 | Q17 | Q18 | Q19 | Q20 | Q21 | Q22 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 100 | 10 | 5 | 5 | 1 | 4 | 2 | 175 | 20 | 0 | 2 | 30 | 1 | 1 | 27840 | 1 | 63430 | 1 | 112013 | 100 | 7 |

### Checks

- All runs succeeded; every query's row count matches across its three runs and across the servers.
