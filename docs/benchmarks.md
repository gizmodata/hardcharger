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
