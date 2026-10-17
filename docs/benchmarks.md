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
- Not measured yet: TLS, and transfer across a real network.

### Ten clients at once

`bench/concurrency.py`: ten clients, each with its own connection and session, each
running Q01, Q03, Q06, Q10, Q14 and Q19 six times (360 queries), same machine and data:

| | Throughput | Median latency | p95 latency |
|---|---:|---:|---:|
| Hardcharger | 39.0 queries/s | 223 ms | 471 ms |
| GizmoSQL 1.41 | 21.9 queries/s | 277 ms | 1,328 ms |

Hardcharger finishes the 360 queries in 9.2 s against 16.5 s (1.8× the throughput), and its
slowest queries wait far less (p95 2.8× lower).

## Remote throughput: Flight SQL vs Quack vs Warp SQL (2026-10-17)

Three DuckDB servers measured from a second machine over a real network: how fast each
moves large results to a client and bulk-loads a client's Arrow table, on one LAN and one
cross-country WAN link. Scripts: `bench/remote/` (provisioning, server start, client
harness, summarizer); raw results: `bench/remote/results/`.

### Systems

| | Version | Protocol | Client |
|---|---|---|---|
| GizmoSQL | 1.42.0 release binary (`gizmosql_cli_linux_arm64.zip`), DuckDB v1.5.6 | Arrow Flight SQL over gRPC (HTTP/2) | `adbc-driver-gizmosql` 2.0.15 |
| Quack | extension `e7a9be3f8c` (the commit DuckDB's `quack.cmake` pins), on DuckDB v2.0.0-alpha48624 (`ffe0b8b7a2`) from the `duckdb==2.0.0.dev2610161340` wheel | DuckDB's own protocol over HTTP/1.1 | the same DuckDB wheel: `ATTACH 'quack:host:9494'` |
| Hardcharger | 0.2.0, the `hardcharger-linux-arm64` CI artifact of `93a3ab2` (DuckDB v2.0.0-alpha48624, `ffe0b8b7a2`) | Warp SQL over HTTP/1.1, Arrow IPC | `adbc-driver-warpsql` from warp-sql-adbc `2b6d775`, built for linux-arm64 |

Quack and Hardcharger run the same DuckDB commit; GizmoSQL runs DuckDB 1.5.6, as every
GizmoSQL 1.x release does. Client side: Python 3.12.3, pyarrow 26.0.0,
adbc-driver-manager 1.12.0.

### Setup

- **Machines:** two c8gn.8xlarge (Graviton4 Neoverse-V2, 32 vCPU, 64 GiB, 100 Gbit/s),
  Ubuntu 24.04.5 arm64 (kernel 7.0.0-1014-aws), Spot. c8gn rather than c8g.8xlarge
  (15 Gbit/s) so the network card is never the limit; the only network limit left is
  AWS's per-flow cap.
- **LAN:** server and client in us-east-2c, in one cluster placement group. RTT 0.09 ms
  (TCP connect). iperf3 (10 s, stock Ubuntu TCP settings, cubic): 1 stream 9.53 Gbit/s
  either way (AWS caps one flow at ~10 Gbit/s inside a placement group); 8 streams
  74.4 Gbit/s client to server and 72.5 Gbit/s server to client.
- **WAN:** the same server; the client is a third c8gn.8xlarge in us-west-2c, over public
  IPs. RTT 46.3 ms. iperf3: 1 stream 0.56 Gbit/s either way (stock `tcp_wmem` max of
  4 MiB limits one flow to ~85 MB/s at 46 ms); 8 streams 4.5 Gbit/s. TCP settings were
  left at Ubuntu's defaults on purpose: that is what a client usually has.
- **TLS on all three**, with one self-signed certificate (P-256). Every connection
  negotiated TLS 1.3 with TLS_AES_256_GCM_SHA384. GizmoSQL and Hardcharger use the
  system OpenSSL 3.0.13. The ADBC clients verify the certificate against it as a root;
  the Quack client pins its SHA-256 fingerprint (Quack's way to trust a self-signed
  server).
- **Server settings, equal for all:** `threads=32`, `memory_limit=40GB`, everything else
  at defaults (so GizmoSQL and Hardcharger cut results into 2,048-row batches; Quack sends
  ~32 MB messages). One server runs at a time.
- **Data:** `CALL dbgen(sf=1)` and `CALL dbgen(sf=10)` (lineitem only kept) and
  `main.narrow`, 100,000,000 rows of `id BIGINT, bucket BIGINT, x DOUBLE, y DOUBLE` from
  `range()`, generated on the server by DuckDB 1.5.6 into one file (storage format
  v1.0.0+, 3.04 GB). DuckDB 2.0 reads and writes that format without upgrading it, so all
  three servers serve a fresh copy of the same file, in `/dev/shm`, so no server waits on
  EBS.
- **Fetch:** `SELECT * FROM <table>`; the client drains the stream as Arrow record batches
  (counting rows and bytes, then dropping each batch). For Quack, `FROM remote.<table>` on
  the attached catalog, read with `to_arrow_reader()`. A pilot showed this is Quack's
  fastest read path: SF1 in 0.16 s, against 0.18 s building a `pyarrow.Table`, 0.24 s via
  `quack_query()` and 0.26 s via `quack_query()` into a table.
- **Ingest:** the client loads lineitem (generated client-side with the same `dbgen`) from
  an Arrow IPC file into memory first, in 122,880-row batches (SF1: 1.05 GB of Arrow,
  SF10: 10.48 GB), then creates a new server table from it. Flight SQL and Warp SQL use
  `adbc_ingest(mode="create")`. Quack uses `CREATE TABLE remote.main.t AS SELECT * FROM
  <arrow table>` (its bulk path: the rows stream to the server), measured both with the
  default `preserve_insertion_order=true` and with `false`, which lets Quack insert in
  parallel. Every repetition is checked afterwards through the same protocol: row count,
  `sum(l_extendedprice)` and `count(DISTINCT l_orderkey)` all matched the source on every
  run.
- **Timing:** one warm-up, then 7 measured repetitions (5 in the supplementary runs);
  median, p10 and p90. Connecting is not timed. MB/s is Arrow in-memory bytes (10^6) at
  the median. "Wire MB" is what crossed the client's network interface in both
  directions, TLS included. Client cores is the client process's CPU time over wall time;
  server cores is the whole server's busy CPU averaged over the run, sampled every 0.25 s.

### Results (TLS)

#### LAN fetch sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.743 | 1.712 | 1.824 | 3.4 | 595 | 1,050 | 1.1 | 1.2 |
| Quack (attach) | 0.158 | 0.157 | 0.302 | 37.9 | 6,620 | 744 | 9.5 | 5.1 |
| Hardcharger 0.2.0 (Warp SQL) | 2.136 | 2.123 | 2.148 | 2.8 | 486 | 1,049 | 0.5 | 1.7 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 4.420 | 4.411 | 4.430 | 1.4 | 235 | 244 | 1.9 | 1.2 |

#### LAN fetch sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 17.847 | 17.576 | 18.198 | 3.4 | 581 | 10,496 | 1.1 | 1.2 |
| Quack (attach) | 0.895 | 0.783 | 0.985 | 67.0 | 11,719 | 7,427 | 16.2 | 9.7 |
| Hardcharger 0.2.0 (Warp SQL) | 21.170 | 21.073 | 21.298 | 2.8 | 490 | 10,492 | 0.6 | 1.7 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 43.107 | 43.075 | 43.261 | 1.4 | 240 | 2,516 | 1.9 | 1.2 |

#### LAN fetch narrow (100,000,000 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 5.679 | 5.646 | 5.731 | 17.6 | 704 | 4,056 | 1.7 | 1.3 |
| Quack (attach) | 0.400 | 0.380 | 0.755 | 249.9 | 10,122 | 4,039 | 14.0 | 10.4 |
| Hardcharger 0.2.0 (Warp SQL) | 10.264 | 10.241 | 10.355 | 9.7 | 390 | 4,056 | 0.6 | 1.7 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 19.030 | 19.006 | 19.095 | 5.3 | 210 | 888 | 2.5 | 1.2 |

#### LAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.610 | 1.525 | 1.619 | 3.7 | 644 | 1,047 | 0.4 | 5.0 |
| Quack (attach, insertion order kept) | 1.004 | 0.917 | 1.065 | 6.0 | 1,033 | 857 | 1.5 | 8.2 |
| Quack (attach, unordered) | 0.623 | 0.542 | 0.719 | 9.6 | 1,666 | 854 | 2.4 | 14.8 |
| Hardcharger 0.2.0 (Warp SQL) | 10.200 | 10.177 | 10.214 | 0.6 | 102 | 1,051 | 0.1 | 1.2 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 8.159 | 7.710 | 9.393 | 0.7 | 127 | 283 | 0.5 | 1.1 |

#### LAN ingest sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 9.427 | 9.308 | 9.442 | 6.4 | 1,099 | 10,465 | 0.6 | 7.8 |
| Quack (attach, insertion order kept) | 2.957 | 2.791 | 3.219 | 20.3 | 3,505 | 8,574 | 4.9 | 24.2 |
| Quack (attach, unordered) | 2.671 | 2.645 | 2.773 | 22.5 | 3,880 | 8,552 | 6.5 | 27.7 |
| Hardcharger 0.2.0 (Warp SQL) | 102.469 | 102.113 | 102.753 | 0.6 | 101 | 10,501 | 0.1 | 1.2 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 74.681 | 74.565 | 74.844 | 0.8 | 139 | 2,883 | 0.5 | 1.2 |

#### WAN fetch sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 12.298 | 12.276 | 12.308 | 0.5 | 84 | 1,081 | 0.2 | 0.2 |
| Quack (attach) | 2.286 | 1.990 | 2.503 | 2.6 | 459 | 767 | 0.8 | 0.4 |
| Hardcharger 0.2.0 (Warp SQL) | 4.905 | 4.679 | 5.091 | 1.2 | 211 | 1,081 | 0.3 | 0.8 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 5.004 | 4.905 | 5.022 | 1.2 | 207 | 251 | 1.8 | 1.0 |

#### WAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 12.894 | 12.803 | 13.095 | 0.5 | 80 | 1,087 | 0.1 | 0.6 |
| Quack (attach, insertion order kept) | 11.272 | 10.169 | 13.889 | 0.5 | 92 | 889 | 0.1 | 0.7 |
| Quack (attach, unordered) | 1.785 | 1.699 | 2.212 | 3.4 | 581 | 888 | 0.9 | 4.3 |
| Hardcharger 0.2.0 (Warp SQL) | 15.027 | 14.988 | 15.225 | 0.4 | 69 | 1,088 | 0.1 | 0.8 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 8.229 | 7.947 | 8.306 | 0.7 | 126 | 294 | 0.5 | 1.0 |

### Supplementary runs (LAN)

Plaintext instead of TLS, on all three, to see what TLS costs each:

#### LAN fetch sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 14.744 | 14.573 | 14.937 | 4.1 | 703 | 10,479 | 1.1 | 1.1 |
| Quack (attach) | 0.898 | 0.876 | 0.931 | 66.8 | 11,681 | 7,417 | 16.0 | 7.7 |
| Hardcharger 0.2.0 (Warp SQL) | 8.725 | 8.725 | 8.726 | 6.9 | 1,188 | 10,461 | 0.9 | 1.8 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 43.021 | 42.935 | 43.188 | 1.4 | 241 | 2,512 | 2.0 | 1.0 |

#### LAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.606 | 1.601 | 1.612 | 3.7 | 646 | 1,046 | 0.2 | 4.9 |
| Quack (attach, insertion order kept) | 0.997 | 0.985 | 1.023 | 6.0 | 1,040 | 853 | 0.8 | 7.7 |
| Quack (attach, unordered) | 0.601 | 0.537 | 0.622 | 10.0 | 1,724 | 856 | 1.6 | 11.4 |
| Hardcharger 0.2.0 (Warp SQL) | 21.687 | 21.666 | 21.751 | 0.3 | 48 | 1,049 | 0.0 | 1.5 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 10.472 | 10.443 | 10.944 | 0.6 | 99 | 282 | 0.3 | 1.4 |

Hardcharger with `--result-batch-rows 122880` instead of the default 2,048 (TLS):

#### LAN fetch sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Hardcharger 0.2.0 (Warp SQL) | 2.022 | 1.997 | 2.051 | 3.0 | 513 | 1,047 | 0.4 | 1.8 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 1.378 | 1.374 | 1.394 | 4.4 | 752 | 248 | 1.5 | 2.4 |

#### LAN fetch sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Hardcharger 0.2.0 (Warp SQL) | 22.582 | 22.397 | 22.783 | 2.7 | 459 | 10,464 | 0.4 | 1.8 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 13.454 | 13.403 | 13.812 | 4.5 | 770 | 2,560 | 1.5 | 2.9 |

#### LAN fetch narrow (100,000,000 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Hardcharger 0.2.0 (Warp SQL) | 7.103 | 7.090 | 7.128 | 14.1 | 563 | 4,037 | 0.4 | 1.6 |
| Hardcharger 0.2.0 (Warp SQL, zstd) | 4.571 | 4.555 | 4.628 | 21.9 | 875 | 958 | 1.9 | 2.4 |

### Reading the numbers

- **Quack is far ahead on every workload but one, because it opens many connections.**
  An attached Quack client kept 128 TCP connections to the server and fetched
  in parallel. SF10 lineitem crossed in 0.9 s at 11.7 GB/s of Arrow, 66 Gbit/s on the wire,
  using 16 client cores and 10 server cores. That is well past the 9.5 Gbit/s one TCP flow
  can carry. GizmoSQL and Hardcharger send a result over one connection, so 1.19 GB/s is
  their ceiling here, and neither reached it with TLS. Quack's wire format is also
  smaller than Arrow IPC (7.4 GB on the wire for 10.5 GB of Arrow), and its speed is the
  same with and without TLS, because the encryption is spread over many threads. The
  exception is its ingest with insertion order kept, the default, over the WAN: 11.3 s,
  slower than zstd Hardcharger. Unordered, Quack's ingest is 6x faster there.
- **Hardcharger loses to GizmoSQL 1.42 on the LAN.** SF10 fetch: 21.2 s against 17.8 s
  (490 vs 581 MB/s). Narrow fetch: 10.3 s against 5.7 s. SF1 ingest: 10.2 s against
  1.6 s; SF10 ingest: 102 s against 9.4 s. Over the WAN it fetches 2.5x faster than
  GizmoSQL (211 vs 84 MB/s; GizmoSQL's gRPC stream gets less from a 46 ms link than one
  HTTP/1.1 response does, presumably because of HTTP/2 flow-control windows) and ingests about as slowly (69 vs 80 MB/s; 126 MB/s with zstd).
- **zstd helps only where bandwidth is scarce, and with the default batches it is slow
  everywhere.** With 2,048-row batches, Hardcharger's zstd fetch tops out at about
  240 MB/s on any network: a quarter of the bytes, but compressed on about one core
  (`ZSTD_compressBlock_fast` leads the profile). With 122,880-row batches it reaches
  770 MB/s on SF10, faster than uncompressed TLS (459 MB/s). The batch size makes no
  difference to the uncompressed stream.

### Hardcharger performance problems found

Profiled with `perf record -g` on the server (the release binary has symbols); the server
was the limit in each case, not the client or the network.

1. **TLS caps the result stream at ~0.5 GB/s.** `raw_fetch.sh` (curl, no ADBC driver)
   gets 0.48 GB/s for SF1 over TLS even on the server's own loopback, and the same over
   the LAN, so neither the Go driver nor the network is the cause. Over plaintext the same
   server sends 1.44 GB/s on loopback and 1.19 GB/s over the LAN (the one-flow cap): SF10
   in 8.7 s instead of 21.2 s, which beats GizmoSQL's plaintext 14.7 s. In the TLS profile,
   about 47% of server samples are under Beast's write path through `boost::asio::ssl`
   (`SSL_write`, then `sendmsg`), while no thread is near 100% (the whole server averages
   1.7 cores, spread over threads at ~5% each). That points to serialized work, not a
   saturated core. The likely shape is that the stream is produced and sent in turn
   (asio's SSL stream writes one TLS record of at most 16 KB per socket write), with no
   overlap between DuckDB filling the next batch and the send of the last one.
2. **Bulk ingest runs on one thread: ~0.6 M rows/s, ~100 MB/s.** Ingest is
   `CREATE TABLE t AS SELECT * FROM <upload table function>()`, where the C-API table
   function reads the one uploaded Arrow stream. DuckDB therefore runs the insert pipeline
   on a single thread: `PhysicalInsert::Sink` holds 53% of samples, nearly all of it
   compressing row groups (`OptimisticDataWriter::FlushToDisk`: FSST, dictionary,
   bitpacking analysis), and the HTTP thread decoding the upload takes another 35%. The
   server averages 1.2 cores against GizmoSQL's 5 to 8 and Quack's 8 to 28. zstd uploads
   barely help (SF10: 75 s), because the network was never the limit.
3. **Plaintext ingest is slower than TLS ingest: 21.7 s vs 10.2 s for SF1.** The upload
   is handed from the HTTP thread to DuckDB's thread one Beast `read_some` at a time.
   Plaintext reads return a few KB each, and every one costs a strand hop plus a
   condition-variable wake (`pthread_cond_signal`/`wait` and futexes are ~30% of samples,
   with a per-read stream timer adding ~7%). DuckDB's own work is only ~22%. The server's
   receive window stayed at ~56 KB (`rcv_space`), because the application drains the
   socket slowly. TLS hands over whole 16 KB records, so it makes fewer handoffs.

### What was not equal

- GizmoSQL runs DuckDB 1.5.6, the other two DuckDB 2.0 (same commit). All three read the
  same file format.
- The protocols differ by design, and that is what was measured: Quack's client is a
  DuckDB that splits a scan across 128 connections. The other two clients read one
  stream. A one-stream client on a link with more per-flow bandwidth would narrow the
  gap, but no single flow here could carry Quack's 11.7 GB/s.
- Quack's client converts DuckDB vectors to Arrow on the client, using up to 16 cores.
  The ADBC clients receive Arrow as it is.
- The ADBC ingest clients sent the source batches as they were (122,880 rows, ~21 MB).
  The Go Flight SQL client refuses gRPC messages over 16 MiB by default, so GizmoSQL's
  client was given `adbc.flight.sql.client_option.with_max_msg_size` = 1 GiB.
- Only Hardcharger compresses (zstd). GizmoSQL's driver exposes no compression option,
  and Quack has none.
- Over the WAN only SF1 was measured (fetch and ingest). The WAN client ran Ubuntu's
  default TCP buffers; tuned buffers would raise every one-stream number there.
- Spot instances and one placement group, on one day. Most configurations were stable
  (p90 within a few percent of the median). The exceptions are Quack's fetches (p90 up
  to 1.9x the median) and its WAN ingest with order kept (p10 10.2 s, p90 13.9 s).

### Reproducing

```bash
set -a; . <AWS credentials .env>; set +a
bench/remote/provision.sh lan                 # server + LAN client (and `wan` for the WAN client)
# copy bench/remote/*, the TLS cert/key, the Hardcharger tarball (server) and the
# adbc_driver_warpsql wheel (clients) to ~/bench on each VM, then on each:
bash ~/bench/setup-vm.sh server|client
~/venv15/bin/python ~/bench/gen_data.py --path ~/bench/base.duckdb     # server
~/venv/bin/python ~/bench/prepare_data.py --data-dir ~/bench/data      # clients
nohup python3 ~/bench/cpu_log.py --out ~/bench/cpu-server.log &        # server
bench/remote/run_matrix.sh lan                # then: run_matrix.sh wan
python3 bench/remote/summarize.py --results results-lan.jsonl results-wan.jsonl --cpu cpu-server.log
bench/remote/provision.sh down                # terminate and delete everything, then verify
```

Cost: about 1.7 hours of two Spot c8gn.8xlarge (~$0.30/h each) plus 1 hour of a third
(~$0.45/h), and ~70 GB across regions ($0.02/GB): about $3. Every resource was tagged
`Purpose=hardcharger-benchmark` and deleted afterwards.

## Remote throughput after the transfer work (2026-10-17, feature/throughput)

The same benchmark as the section above, re-run on fresh VMs after five changes to
Hardcharger's data path (each commit message has its own before/after numbers):

1. **TLS and the result pipeline** (warp-sql "SDK: result streams overlap producing,
   encoding and TLS writes"). Asio's `ssl::stream` wrote one 16 KB TLS record per socket
   write and completion handler; the SDK's own `TlsStream` encrypts a whole gathered write
   (up to 1 MiB) at once and sends it while it encrypts the next one. Results flow through
   a staged pipeline: DuckDB produces into a buffer bounded by bytes (32 MiB), an encode
   pool encodes (and zstd-compresses) batches several at once, and the connection's
   strand writes them in order in ~1 MiB writes.
2. **122,880-row result batches** by default instead of 2,048 (hardcharger "Results come
   in 122,880-row batches by default").
3. **Parallel ingest** (warp-sql "Ingest inserts in parallel"): the upload is a parallel
   DuckDB table function with batch indexes, so DuckDB's batch insert runs on many
   threads, in one transaction, keeping the upload's row order. Uploads are decoded and
   validated on those threads.
4. **Uploads read in 1 MiB pieces** (warp-sql "SDK: uploads are read from the connection
   in 1 MiB pieces") straight from the socket instead of through Beast's 64 KB parser
   reads.
5. **Partitioned results** (spec §6.10; warp-sql, warp-sql-adbc, warp-sql-jdbc and the
   C++ client): a client that sends `Warp-Result-Partitions: n` may get the result split
   into up to n partitions (32 at most by default), which it reads at once, each over its
   own connection; `interleaved` partitions rebuild the result's order round robin, so
   `ORDER BY` holds. Without the header nothing changes.

### Setup

As above (`bench/remote/`): two c8gn.8xlarge Spot instances in one placement group in
us-east-2c (LAN, RTT 0.09 ms) and a third in us-west-2c (WAN, RTT 46 ms), Ubuntu
24.04 arm64, kernel 7.0.0-1014-aws, TLS on every system with one self-signed P-256
certificate, `threads=32`, `memory_limit=40GB`, the same database file in `/dev/shm`.
iperf3 server to client, this time: 1 stream 9.5 Gbit/s, 8 streams 75.7, 16 and 32
streams 99.8 Gbit/s.

- **Hardcharger**: the `hardcharger-linux-arm64` CI artifact of feature/throughput at
  `2024a0a5` (warp-sql `28e1164`; the branch was rebased on main afterwards, which changed
  no data-path code), default settings (122,880-row batches, `max_result_partitions` 32).
  Client: `adbc-driver-warpsql` from warp-sql-adbc feature/throughput (`b0c4359`), built
  for linux-arm64. "n partitions" rows set `adbc.warp.statement.result_partitions` = n and
  `result_order` = `any` or `preserve` ("in order").
- **GizmoSQL** 1.42.0 and **Quack** (DuckDB `2.0.0.dev2610161340`) exactly as above.
- **5 measured repetitions** (7 above), one warm-up. Every fetch is now checked in full:
  on the warm-up and on one extra untimed repetition the client sums three columns of
  every batch it received (lineitem: `l_orderkey`, `l_extendedprice`, the length of
  `l_comment`; narrow: `id`, `bucket`, `x`) and compares the row count and sums with the
  server's own; every ingest repetition is checked as before. All matched, for all three
  systems. Raw results: `bench/remote/results/2026-10-17-throughput/`.

### Headline (LAN, TLS)

| Workload | GizmoSQL 1.42.0 | Quack | Hardcharger 0.2.0 | Hardcharger now, 1 stream | Hardcharger now, 32 partitions |
|---|---:|---:|---:|---:|---:|
| Fetch SF1 lineitem | 1.738 s | 0.167 s | 2.136 s | 0.900 s | **0.148 s** |
| Fetch SF10 lineitem | 15.217 s | **0.803 s** | 21.170 s | 8.760 s | 0.971 s |
| Fetch narrow (100M rows) | 5.431 s | 0.548 s | 10.264 s | 3.368 s | **0.452 s** |
| Ingest SF1 | 1.605 s | 1.001 s (0.529 s unordered) | 10.200 s | 1.299 s | n/a |
| Ingest SF10 | 9.427 s | 2.969 s (2.648 s unordered) | 102.469 s | 9.342 s | n/a |

("Hardcharger 0.2.0" is from the section above, same instance type and method.)

### Results (TLS)

#### LAN fetch sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.738 | 1.717 | 1.746 | 3.5 | 597 | 1,050 | 1.1 | 1.1 |
| Quack (attach) | 0.167 | 0.154 | 0.256 | 36.0 | 6,287 | 743 | 9.4 | 4.1 |
| Hardcharger (Warp SQL) | 0.900 | 0.898 | 0.912 | 6.7 | 1,152 | 1,045 | 0.7 | 2.1 |
| Hardcharger (Warp SQL, zstd) | 1.409 | 1.402 | 1.418 | 4.3 | 736 | 248 | 1.4 | 2.3 |
| Hardcharger (Warp SQL, 8 partitions, any order) | 0.181 | 0.176 | 0.183 | 33.2 | 5,741 | 1,045 | 3.9 | 11.0 |
| Hardcharger (Warp SQL, 8 partitions, in order) | 0.183 | 0.174 | 0.208 | 32.8 | 5,670 | 1,046 | 4.2 | 10.8 |
| Hardcharger (Warp SQL, 16 partitions, any order) | 0.210 | 0.207 | 0.213 | 28.6 | 4,940 | 1,046 | 4.2 | 9.8 |
| Hardcharger (Warp SQL, 16 partitions, in order) | 0.195 | 0.191 | 0.199 | 30.8 | 5,325 | 1,046 | 4.5 | 10.6 |
| Hardcharger (Warp SQL, 32 partitions, any order) | 0.148 | 0.138 | 0.175 | 40.6 | 7,016 | 1,046 | 6.4 | 17.2 |
| Hardcharger (Warp SQL, 32 partitions, in order) | 0.168 | 0.148 | 0.177 | 35.8 | 6,187 | 1,046 | 6.2 | 11.5 |

#### LAN fetch sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 15.217 | 15.174 | 15.233 | 3.9 | 681 | 10,489 | 1.2 | 1.3 |
| Quack (attach) | 0.803 | 0.792 | 0.923 | 74.7 | 13,063 | 7,428 | 17.4 | 11.1 |
| Hardcharger (Warp SQL) | 8.760 | 8.750 | 8.762 | 6.8 | 1,183 | 10,447 | 0.7 | 2.1 |
| Hardcharger (Warp SQL, zstd) | 13.489 | 13.454 | 13.594 | 4.4 | 768 | 2,564 | 1.5 | 2.5 |
| Hardcharger (Warp SQL, 8 partitions, any order) | 1.430 | 1.425 | 1.445 | 41.9 | 7,247 | 10,449 | 4.5 | 13.9 |
| Hardcharger (Warp SQL, 8 partitions, in order) | 1.446 | 1.432 | 1.485 | 41.5 | 7,166 | 10,450 | 4.7 | 13.6 |
| Hardcharger (Warp SQL, 16 partitions, any order) | 1.590 | 1.585 | 1.614 | 37.7 | 6,519 | 10,455 | 4.8 | 13.0 |
| Hardcharger (Warp SQL, 16 partitions, in order) | 1.610 | 1.591 | 1.617 | 37.3 | 6,439 | 10,455 | 4.8 | 13.2 |
| Hardcharger (Warp SQL, 32 partitions, any order) | 1.013 | 0.987 | 1.056 | 59.2 | 10,235 | 10,454 | 9.1 | 21.3 |
| Hardcharger (Warp SQL, 32 partitions, in order) | 0.971 | 0.957 | 0.976 | 61.8 | 10,673 | 10,454 | 9.9 | 21.2 |
| Hardcharger (Warp SQL, zstd, 32 partitions, any order) | 1.337 | 1.324 | 1.350 | 44.9 | 7,754 | 2,562 | 15.6 | 22.3 |

#### LAN fetch narrow (100,000,000 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 5.431 | 5.422 | 5.497 | 18.4 | 736 | 4,056 | 1.6 | 1.3 |
| Quack (attach) | 0.548 | 0.447 | 0.559 | 182.4 | 7,385 | 4,039 | 10.3 | 9.2 |
| Hardcharger (Warp SQL) | 3.368 | 3.368 | 3.370 | 29.7 | 1,188 | 4,033 | 0.7 | 1.5 |
| Hardcharger (Warp SQL, zstd) | 4.522 | 4.505 | 4.539 | 22.1 | 885 | 958 | 1.9 | 2.1 |
| Hardcharger (Warp SQL, 8 partitions, any order) | 0.586 | 0.550 | 0.591 | 170.7 | 6,830 | 4,033 | 4.7 | 11.6 |
| Hardcharger (Warp SQL, 8 partitions, in order) | 0.554 | 0.543 | 0.568 | 180.6 | 7,226 | 4,032 | 4.9 | 12.5 |
| Hardcharger (Warp SQL, 16 partitions, any order) | 0.656 | 0.618 | 0.659 | 152.5 | 6,098 | 4,035 | 5.1 | 11.0 |
| Hardcharger (Warp SQL, 16 partitions, in order) | 0.654 | 0.634 | 0.659 | 152.9 | 6,115 | 4,034 | 5.2 | 10.8 |
| Hardcharger (Warp SQL, 32 partitions, any order) | 0.452 | 0.403 | 0.498 | 221.0 | 8,841 | 4,034 | 8.2 | 16.8 |
| Hardcharger (Warp SQL, 32 partitions, in order) | 0.479 | 0.442 | 0.513 | 208.8 | 8,353 | 4,034 | 7.7 | 16.3 |

#### LAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.605 | 1.495 | 1.614 | 3.7 | 646 | 1,047 | 0.3 | 5.1 |
| Quack (attach, insertion order kept) | 1.001 | 0.929 | 1.005 | 6.0 | 1,036 | 856 | 1.5 | 8.0 |
| Quack (attach, unordered) | 0.529 | 0.501 | 0.592 | 11.4 | 1,962 | 855 | 3.0 | 14.6 |
| Hardcharger (Warp SQL) | 1.299 | 1.292 | 1.484 | 4.6 | 798 | 1,048 | 0.5 | 6.0 |
| Hardcharger (Warp SQL, zstd) | 3.081 | 3.055 | 3.109 | 1.9 | 337 | 283 | 1.2 | 2.5 |

#### LAN ingest sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 9.427 | 9.404 | 9.441 | 6.4 | 1,099 | 10,466 | 0.5 | 7.9 |
| Quack (attach, insertion order kept) | 2.969 | 2.825 | 2.986 | 20.2 | 3,492 | 8,572 | 5.0 | 24.2 |
| Quack (attach, unordered) | 2.648 | 2.632 | 2.736 | 22.7 | 3,914 | 8,545 | 6.5 | 27.5 |
| Hardcharger (Warp SQL) | 9.342 | 9.257 | 9.431 | 6.4 | 1,110 | 10,476 | 0.6 | 8.1 |
| Hardcharger (Warp SQL, zstd) | 27.663 | 27.386 | 27.996 | 2.2 | 375 | 2,882 | 1.4 | 2.6 |

#### WAN fetch sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 12.260 | 12.252 | 12.265 | 0.5 | 85 | 1,081 | 0.2 | 0.2 |
| Quack (attach) | 1.766 | 1.630 | 2.034 | 3.4 | 594 | 766 | 1.0 | 0.6 |
| Hardcharger (Warp SQL) | 12.643 | 12.582 | 12.997 | 0.5 | 82 | 1,077 | 0.1 | 0.2 |
| Hardcharger (Warp SQL, zstd) | 4.049 | 4.038 | 4.065 | 1.5 | 256 | 256 | 0.5 | 0.8 |
| Hardcharger (Warp SQL, 8 partitions, any order) | 2.471 | 2.217 | 2.659 | 2.4 | 420 | 1,078 | 0.4 | 0.9 |
| Hardcharger (Warp SQL, 8 partitions, in order) | 1.964 | 1.879 | 4.246 | 3.1 | 528 | 1,077 | 0.5 | 1.2 |
| Hardcharger (Warp SQL, 32 partitions, any order) | 2.882 | 2.560 | 3.299 | 2.1 | 360 | 1,078 | 0.4 | 0.8 |
| Hardcharger (Warp SQL, 32 partitions, in order) | 27.658 | 4.753 | 32.096 | 0.2 | 37 | 1,079 | 0.0 | 0.1 |

#### WAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 12.852 | 12.820 | 13.063 | 0.5 | 81 | 1,087 | 0.1 | 0.6 |
| Quack (attach, insertion order kept) | 2.013 | 1.821 | 2.063 | 3.0 | 515 | 888 | 0.8 | 3.7 |
| Quack (attach, unordered) | 2.351 | 2.030 | 2.538 | 2.6 | 441 | 888 | 0.7 | 3.1 |
| Hardcharger (Warp SQL) | 12.756 | 12.668 | 14.188 | 0.5 | 81 | 1,089 | 0.1 | 0.6 |
| Hardcharger (Warp SQL, zstd) | 6.134 | 5.945 | 6.170 | 1.0 | 169 | 294 | 0.7 | 1.2 |


### Supplementary runs (LAN, plaintext)

#### LAN fetch sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 13.477 | 13.364 | 13.672 | 4.5 | 769 | 10,477 | 1.2 | 1.2 |
| Quack (attach) | 0.903 | 0.894 | 0.994 | 66.4 | 11,613 | 7,417 | 17.5 | 7.3 |
| Hardcharger (Warp SQL) | 8.747 | 8.744 | 8.749 | 6.9 | 1,185 | 10,433 | 0.6 | 1.3 |
| Hardcharger (Warp SQL, 32 partitions, any order) | 0.908 | 0.898 | 0.927 | 66.0 | 11,412 | 10,434 | 6.2 | 12.1 |

#### LAN ingest sf1 (6,001,215 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 1.608 | 1.598 | 1.613 | 3.7 | 645 | 1,046 | 0.2 | 4.6 |
| Quack (attach, insertion order kept) | 1.000 | 0.951 | 1.038 | 6.0 | 1,037 | 854 | 0.8 | 8.0 |
| Quack (attach, unordered) | 0.538 | 0.506 | 0.587 | 11.2 | 1,928 | 855 | 1.8 | 13.1 |
| Hardcharger (Warp SQL) | 1.302 | 1.289 | 1.433 | 4.6 | 796 | 1,045 | 0.2 | 5.7 |

#### LAN ingest sf10 (59,986,052 rows)

| System | Median s | p10 s | p90 s | Mrows/s | MB/s (Arrow) | Wire MB | Client cores | Server cores |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GizmoSQL 1.42.0 (Flight SQL) | 9.431 | 9.359 | 9.477 | 6.4 | 1,099 | 10,453 | 0.3 | 7.5 |
| Quack (attach, insertion order kept) | 2.837 | 2.789 | 2.942 | 21.1 | 3,654 | 8,600 | 2.9 | 25.3 |
| Hardcharger (Warp SQL) | 9.407 | 9.186 | 9.417 | 6.4 | 1,102 | 10,453 | 0.3 | 8.1 |


### Reading the numbers

- **One TLS stream now runs at the network's per-flow limit, like plaintext.** SF10
  lineitem over TLS took 8.760 s (1.18 GB/s of Arrow, 9.5 Gbit/s on the wire), against
  8.747 s over plaintext and 21.170 s before. With `WARPSQL_STREAM_STATS=1` the server
  reported where each stream's time went: waiting for rows ~0 s (the engine thread mostly
  waited for room), encoding about 1 ms of CPU per stream (uncompressed encoding only
  frames batches and references their buffers), encrypting at 3.2 GB/s on the
  connection's strand (TLS_AES_128_GCM_SHA256, which the Go client prefers), and the rest
  waiting for the socket. On loopback (Apple M5 Max) one TLS stream reached 2.7-3.0 GB/s,
  bound by the client's decryption; the server encrypted at 6.1 GB/s.
- **Split results make Hardcharger as fast as Quack on the LAN, and faster on two of three
  fetches.** With 32 partitions SF1 took 0.148 s (Quack 0.167 s), narrow 0.452 s (Quack
  0.548 s), SF10 0.971 s (Quack 0.803 s; 10.7 GB/s of Arrow, 86 Gbit/s on the wire). Over
  plaintext, 32 partitions fetched SF10 in 0.908 s against Quack's 0.903 s. Quack sends
  30% fewer bytes for lineitem (7.4 GB against 10.4 GB of Arrow IPC) and uses 17 client
  cores to Hardcharger's 10. Order costs nothing on the LAN: interleaved partitions (in
  order) were as fast as any-order ones.
- **8 partitions beat 16, and 32 beat both.** SF10: 8 partitions 1.43 s, 16 1.59 s, 32
  0.97 s. The server was never the limit (its streams spent their time waiting for the
  socket) and the network carries 99.8 Gbit/s over 16 flows; the client's reading was.
  Why 16 did worse than 8 is not understood yet.
- **Ingest went from 10x slower than GizmoSQL to as fast or faster.** SF1: 1.30 s
  (GizmoSQL 1.61 s, before 10.20 s); SF10: 9.34 s (GizmoSQL 9.43 s, before 102.5 s).
  Plaintext ingest is no longer slower than TLS (1.30 s either way; it was 21.7 s). SF10
  over one connection is at the per-flow limit (1.1 GB/s of Arrow).
- **zstd is now only for slow links.** On the LAN, zstd fetch is slower than none (SF10:
  13.5 s against 8.8 s: decompression in the client on 1.5 cores), and zstd ingest much
  slower (SF10: 27.7 s: the Go driver compresses the upload on one core). With 32
  partitions, zstd SF10 took 1.34 s. Over the WAN, zstd fetch on one stream is 3x faster
  than none (4.05 s against 12.64 s).
- **WAN: partitions win, one stream doesn't.** SF1 fetch with 8 partitions in order:
  1.96 s (Quack 1.77 s, GizmoSQL 12.26 s). One stream: 12.64 s, the same as GizmoSQL,
  where Hardcharger 0.2.0 took 4.9 s. That earlier speed came from the old TLS path, which
  wrote one 16 KB record per `sendmsg`: the kernel let that socket's queue grow to about
  21 MB (`ss` showed Send-Q 21 MB and a 42 MB congestion window), beyond the 4 MB
  `tcp_wmem` limit that bounds every plaintext stream, and now the TLS stream too, at
  about 87 MB/s over 46 ms (re-measured: 0.2.0 gets 75 MB/s over plaintext, the new build
  73 MB/s). 32 partitions in order over the WAN was erratic (median 27.7 s, p10 4.8 s):
  interleaving waits for the slowest of 32 long-distance flows; 8 partitions, or any
  order, avoid it.
- **kTLS** (`bench/remote/ktls_check.sh`: OpenSSL's `s_server` sending 8 GB to curl on the
  server itself). Ubuntu 24.04's OpenSSL 3.0.13 is built without kTLS (`s_server` has no
  `-ktls`), so the check builds OpenSSL 3.5.0 with `enable-ktls`. User-space TLS cost the
  server 0.64 s of CPU per GB (AES-128-GCM) and 0.67 s/GB (AES-256-GCM); kTLS with
  `sendfile` 0.46 and 0.51 s/GB, about 25% less. OpenSSL 3.5's AES-256-GCM runs at 5.8 GB/s
  on one Graviton4 core against 4.2 GB/s for the system's 3.0.13. With the TLS stream at
  3.2 GB/s per connection and the per-flow limit at 1.19 GB/s, encryption doesn't limit a
  stream; kTLS would save CPU on a server busy with many streams, but needs an OpenSSL
  built with kTLS and the TLS stream switched to a socket BIO after the handshake. Not done.
- **Cipher**: AES-128-GCM is 5-15% cheaper than AES-256-GCM on Graviton4; the server keeps
  OpenSSL's default list and the client's preference decides (Go's picks AES-128-GCM).

### Where Hardcharger still loses

- **SF10 fetch to Quack, by 21%** (0.971 s against 0.803 s with TLS): Quack's wire format
  is 30% smaller for lineitem, and its client spreads decoding over more cores.
- **Ingest over the LAN, to Quack by 2.5-3.5x.** Quack's client splits one upload over
  many connections (2.97 s for SF10); Warp SQL's ingest is one upload on one connection,
  so SF10 can't beat the 9.5 Gbit/s flow limit (9.34 s). Partitioned uploads would need a
  protocol addition, as results got.
- **WAN on one stream**: as slow as GizmoSQL, and slower than 0.2.0, whenever the client
  doesn't ask for partitions. On a high-latency link a client should.
- **zstd over fast links**, on the client side (one core compresses or decompresses in the
  Go driver).

### Local measurements (loopback, Apple M5 Max, 2026-10-17)

A busy machine (other builds and tests ran meanwhile), so these are indicative; each
commit message has interleaved A/B runs. TLS unless marked; median seconds.

| Workload | Hardcharger main | Hardcharger now | now, 8 partitions | GizmoSQL 1.42.0 | Quack |
|---|---:|---:|---:|---:|---:|
| Fetch SF1 | 1.334 | 0.475 | 0.124 | 0.958 | 0.275 |
| Fetch SF10 | 12.979 | 3.614 | 1.055 | 9.074 | 2.018 |
| Fetch SF10, plaintext | 3.122 | 1.084 | 0.923 | 8.473 | 1.089 |
| Fetch narrow | 5.709 | 1.472 | 0.369 | 2.932 | 1.083 |
| Fetch SF1, zstd | 2.739 | 0.685 | | | |
| Ingest SF1 | 4.325 | 0.933 | | 1.057 | 0.809 (0.648 unordered) |
| Ingest SF1, plaintext | 10.533 | 0.664 | | 0.878 | 0.872 |
| Ingest SF10 | 44.061 | 8.062 | | 6.053 | 9.101 (5.566 unordered) |

### Did the engine need a patch for partitions?

No. DuckDB 2.0 at ffe0b8b already converts a query's output to Arrow on its worker
threads (each producer's sink appends to its own Arrow appender; the result's consumer
only pops finished arrays), so one engine stream dealt round robin into partitions is not
a bottleneck: on loopback, with 122,880-row batches, the engine thread delivered SF10
lineitem at over 20 GB/s while the readers took their time, and on AWS every partition's
stream spent its time waiting for the socket, not for rows. A patch that feeds several
result streams straight from the pipelines was therefore not written.

### Cost

About 1.3 hours of three Spot c8gn.8xlarge (~$0.35/h each) and ~25 GB across regions:
about $2. Everything was tagged `Purpose=hardcharger-benchmark` and deleted; describe
calls in both regions showed no instances, volumes, security groups, key pairs,
placement groups or Spot requests left.
