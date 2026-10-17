# Benchmarks

## TPC-H SF10: Hardcharger 0.1.0 vs GizmoSQL 1.41.0 (2026-10-17)

Same machine, same data, each server on loopback with no TLS. Each server loads the
TPC-H SF10 Parquet files (lineitem: 59,986,052 rows) into its own DuckDB file through
init SQL. Every query runs once to warm up, then five times; the figure is the median,
measured at the client from submit to the last row in a `pyarrow.Table`. Hardcharger is
called over Warp SQL's one-round-trip path (`Accept: application/vnd.apache.arrow.stream`,
plain `httpx`); GizmoSQL through `adbc-driver-gizmosql` 2.0.15. Script: `bench/tpch.py`.

Machine: Apple M5 Max, 18 cores, 64 GB, macOS 27. Hardcharger on DuckDB
v2.0.0-alpha48624, GizmoSQL on DuckDB v1.5.6. "DuckDB 1.5.6 in-process" is the same
database file queried from the `duckdb` Python package, with no server at all.

| Query | Hardcharger (ms) | GizmoSQL 1.41 (ms) | Speedup | DuckDB 1.5.6 in-process (ms) |
|---|---:|---:|---:|---:|
| Q01 | 41.9 | 96.0 | 2.29× | 85.3 |
| Q02 | 8.2 | 20.1 | 2.45× | 16.1 |
| Q03 | 28.5 | 43.4 | 1.52× | 36.9 |
| Q04 | 27.2 | 40.7 | 1.50× | 36.5 |
| Q05 | 32.1 | 38.1 | 1.19× | 30.9 |
| Q06 | 11.2 | 14.8 | 1.32× | 11.1 |
| Q07 | 27.1 | 39.9 | 1.47× | 27.3 |
| Q08 | 29.9 | 39.4 | 1.32× | 30.0 |
| Q09 | 99.0 | 102.4 | 1.03× | 96.5 |
| Q10 | 64.1 | 74.6 | 1.16× | 69.1 |
| Q11 | 6.1 | 10.0 | 1.64× | 5.2 |
| Q12 | 26.3 | 34.6 | 1.32× | 30.1 |
| Q13 | 116.7 | 133.5 | 1.14× | 126.2 |
| Q14 | 23.8 | 28.9 | 1.21× | 23.3 |
| Q15 | 16.8 | 22.2 | 1.32× | 17.9 |
| Q16 | 29.1 | 33.5 | 1.15× | 27.7 |
| Q17 | 28.7 | 34.6 | 1.21× | 30.0 |
| Q18 | 83.4 | 98.9 | 1.19× | 90.8 |
| Q19 | 22.0 | 54.1 | 2.46× | 48.2 |
| Q20 | 29.6 | 33.9 | 1.15× | 26.3 |
| Q21 | 60.4 | 144.8 | 2.40× | 125.8 |
| Q22 | 20.4 | 62.8 | 3.08× | 25.1 |
| **Total** | **832.5** | **1,201.2** | **1.44×** | **1,016.3** |

Bulk transfer, `SELECT * FROM lineitem LIMIT 10000000` (1.73 GB of Arrow), median of 3:

| | Seconds |
|---|---:|
| Hardcharger | 0.83 |
| Hardcharger, `Warp-IPC-Compression: zstd` | 0.96 |
| GizmoSQL 1.41 | 1.32 |
| DuckDB 1.5.6 in-process | 0.88 |

Every query returned the same row count on both servers (Q11 returns no rows at SF10
because the query text carries SF1's fraction; it does on both).

### Reading the numbers

- Part of the gap is the engine: Hardcharger beats even in-process DuckDB 1.5.6 (832 vs
  1,016 ms), so DuckDB 2.0 is faster on these queries before any protocol is involved
  (Q01, Q19, Q21 most of all).
- GizmoSQL 1.x adds about 185 ms over in-process DuckDB across the 22 queries (≈8 ms a
  query: gRPC, Flight's GetFlightInfo then DoGet round trips, and the server's per-statement work).
  Hardcharger's one-round-trip path answers small queries within about a millisecond of
  in-process 1.5.6 (Q11: 6.1 vs 5.2 ms) while running a different engine, so the protocol
  cost is small; a DuckDB 2.0 in-process baseline would pin it down exactly.
- Bulk transfer over HTTP/1.1 moves ~2 GB/s on loopback, faster than the gRPC stream and
  on par with handing Arrow over in-process. zstd costs time on loopback, where bandwidth
  is free; it pays off on real networks.
- Not measured yet: concurrency (many clients), TLS, and transfer across a real network.
