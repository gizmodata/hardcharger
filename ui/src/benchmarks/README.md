# Benchmark query examples

`tpch.json` contains Q1–Q22; `tpcds.json` contains Q1–Q99, in numeric order.
They come from the server's pinned DuckDB commit
`d54cbbe3df17e8e5ce68312f483b4155836112aa`, under
`extension/tpch/dbgen/queries` and `extension/tpcds/dsdgen/queries`.
These use DuckDB's fixed example parameters. They are examples for exploring data,
not an audited TPC benchmark implementation.

The generator qualifies column references and replaces comma joins with explicit
`INNER JOIN … ON` and, where appropriate, `CROSS JOIN`. It preserves outer joins,
correlated subqueries, filters, limits and ordering. SQL still uses DuckDB functions
and date syntax; ANSI join syntax does not imply portability to every SQL engine.

To regenerate from `server/.deps/duckdb`, use a separate Python environment with
`duckdb==2.0.0` and `sqlglot==30.29.0`, then run from the repository root:

```sh
python ui/scripts/generate-benchmark-queries.py --verify
```

Verification generates TPC-H SF0.01 and TPC-DS SF1 fixtures and compares all 121
rewritten queries against their sources. It compares row multisets because ties
may reorder, rounding floating point values to eight significant digits. The
runtime web UI does not depend on Python or SQLGlot. Each suite loads lazily.

Detection requires all eight TPC-H or all 24 TPC-DS table names (case insensitive)
in the selected catalog/schema's permission-filtered metadata; views count too.
It does not inspect columns or validate data completeness. Query tabs inherit the
chosen server, role, catalog, schema and row limit. Selection never runs SQL.

[NOTICE.txt](NOTICE.txt) preserves the upstream notices and identifies our changes.
The Vite notices plugin appends it to the shipped `third-party-notices.txt`, which
is accessible from the About dialog.
