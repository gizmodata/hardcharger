"""Regenerate the bundled, explicit-join examples from the pinned DuckDB checkout.

Run with Python packages duckdb==2.0.0 and sqlglot==30.29.0:
  python ui/scripts/generate-benchmark-queries.py
Use --verify to compare every query with its source on independently generated
TPC-H SF0.01 and TPC-DS SF1 data. Requires DuckDB's signed tpch/tpcds extensions.
The output is examples, not an audited TPC benchmark implementation.
"""

import argparse
import json
import math
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "server/.deps/duckdb"
DEST = ROOT / "ui/src/benchmarks"
SUITES = {"tpch": ("dbgen", "extension/tpch/dbgen/queries", 0.01),
          "tpcds": ("dsdgen", "extension/tpcds/dsdgen/queries", 1)}


def explicit_joins(*, sql, schema):
    tree = qualify(expression=sqlglot.parse_one(sql=sql, read="duckdb"), dialect="duckdb",
                   schema=schema, quote_identifiers=False, identify=False)
    for select in tree.find_all(exp.Select):
        joins = select.args.get("joins", [])
        # Preserve the order and predicates of outer joins. No predicate may cross
        # an outer-join boundary; a comma in such a scope becomes explicit CROSS.
        if any(j.side or j.args.get("on") or j.args.get("using") for j in joins):
            for join in joins:
                if not any(join.args.get(k) for k in ("on", "using", "kind", "side")):
                    join.set("kind", "CROSS")
            continue
        if not joins:
            continue
        where = select.args.get("where")
        predicates = list(where.this.flatten()) if where and isinstance(where.this, exp.And) else ([where.this] if where else [])
        available = {select.args["from_"].this.alias_or_name}
        remaining = list(joins)
        ordered = []
        while remaining:
            choices = []
            for join in remaining:
                name = join.alias_or_name
                connected = []
                for predicate in predicates:
                    if predicate.find(exp.Query):
                        continue
                    refs = {c.table for c in predicate.find_all(exp.Column)}
                    if name in refs and refs & available and refs <= available | {name}:
                        connected.append(predicate)
                choices.append((join, connected))
            join, conditions = next(((j, p) for j, p in choices if p), choices[0])
            if conditions:
                join.set("kind", "INNER")
                join.set("on", exp.and_(*conditions, copy=True))
                predicates = [p for p in predicates if p not in conditions]
            else:
                join.set("kind", "CROSS")
            available.add(join.alias_or_name)
            remaining.remove(join)
            ordered.append(join)
        select.set("joins", ordered)
        select.set("where", exp.Where(this=exp.and_(*predicates, copy=True)) if predicates else None)
    for join in tree.find_all(exp.Join):
        assert join.args.get("on") or join.args.get("using") or join.kind == "CROSS", join
    return tree.sql(dialect="duckdb", pretty=True) + ";\n"


def same_rows(*, before, after):
    # ORDER BY can have ties and floating point aggregates can differ by reduction
    # order. Compare row multisets after rounding floats to 8 significant digits.
    def canonical(row):
        return tuple(format(v, ".8g") if isinstance(v, float) and math.isfinite(v) else str(v) for v in row)
    return sorted(map(canonical, before)) == sorted(map(canonical, after))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    for suite, (generator, relative, scale) in SUITES.items():
        conn = duckdb.connect()
        conn.execute(query="SET threads=4")
        conn.execute(query=f"INSTALL {suite}; LOAD {suite}; CALL {generator}(sf={scale if args.verify else 0})")
        schema = {t: {r[0]: r[1] for r in conn.execute(query=f'DESCRIBE "{t}"').fetchall()}
                  for (t,) in conn.execute(query="SHOW TABLES").fetchall()}
        queries = []
        for source in sorted((SOURCE / relative).glob("*.sql")):
            original = source.read_text()
            rewritten = explicit_joins(sql=original, schema=schema)
            queries.append(rewritten)
            if args.verify:
                before = conn.execute(query=original).fetchall()
                after = conn.execute(query=rewritten).fetchall()
                assert same_rows(before=before, after=after), f"Results differ: {suite} {source.name}"
                print(f"Verified {suite} {source.name} ({len(after)} rows)", flush=True)
        (DEST / f"{suite}.json").write_text(json.dumps(queries, indent=2) + "\n")
        print(f"Wrote {len(queries)} {suite} queries", flush=True)
        conn.close()


if __name__ == "__main__":
    main()
