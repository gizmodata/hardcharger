"""The suite's data, statements and expected answers.

TPC-H: DuckDB's tpch extension generates the tables once (one server, CALL dbgen), they are
written to Parquet, and every server version loads the same files. The 22 query texts and, for
scale factors 0.01, 0.1 and 1, the reference answers come from tpch_queries()/tpch_answers() and
are saved next to the data, so every version runs identical SQL and is checked against the same
answers.

Many pipelines: the shape of DB Olympics' q311 (a 30-way UNION ALL of small aggregates joined
to one more aggregate) and a statement of 30 scalar subqueries. Each part is a pipeline or two of
its own; 2.9.0 to 2.13.1 ran such plans 150-1350 ms slower than 2.4.1 at times on AWS (per-session
progress tracking). Their tables are built from range(), and their exact answers are computed
here in Python, independently of any server.

Derived from the TPC-H benchmark; not an official TPC-H result and not comparable to published
TPC-H results. TPC-H is a trademark of the Transaction Processing Performance Council (TPC).
"""

import datetime
import decimal
import json
import math
import pathlib

TPCH_TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]
ANSWER_SCALES = (0.01, 0.1, 1.0)  # tpch_answers() has these

PARTS = ["brands", "categories", "products", "product_variants", "collections", "users", "billing_profiles",
         "plans", "price_lists", "discounts", "coupons", "promotions", "campaigns", "email_templates", "segments",
         "landing_pages", "referrals", "loyalty_programs", "ab_tests", "experiments", "cohorts", "funnels",
         "contacts", "accounts", "knowledge_articles", "macros", "files", "utm_links", "suppliers", "warehouses"]
TENANTS = 97


def scale_name(scale: float) -> str:
    return f"sf{scale:g}".replace(".", "_")


# ---- TPC-H -------------------------------------------------------------------------------------

def tpch_dir(data_root: pathlib.Path, scale: float) -> pathlib.Path:
    return data_root / f"tpch-{scale_name(scale=scale)}"


def generate_tpch(session, data_root: pathlib.Path, scale: float) -> dict:
    """Generates TPC-H at `scale` through `session` (any server with the tpch extension
    installable) into Parquet, with the query texts and answers. Idempotent: an existing
    manifest is returned as it is."""
    directory = tpch_dir(data_root=data_root, scale=scale)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    directory.mkdir(parents=True, exist_ok=True)
    session.query(sql="INSTALL tpch")
    session.query(sql="LOAD tpch")
    session.query(sql=f"CALL dbgen(sf = {scale})")
    rows = {}
    for table in TPCH_TABLES:
        session.query(sql=f"COPY {table} TO '{directory / (table + '.parquet')}' (FORMAT parquet)")
        rows[table] = session.scalar(sql=f"SELECT count(*) FROM {table}")
    queries = {int(r["query_nr"]): r["query"]
               for r in session.query(sql="SELECT query_nr, query FROM tpch_queries()").to_pylist()}
    answers = {}
    if any(math.isclose(scale, s) for s in ANSWER_SCALES):
        table = session.query(sql=f"SELECT query_nr, answer FROM tpch_answers() WHERE scale_factor = {scale}")
        answers = {int(r["query_nr"]): r["answer"] for r in table.to_pylist()}
    (directory / "queries.json").write_text(json.dumps({str(k): v for k, v in sorted(queries.items())}, indent=1))
    (directory / "answers.json").write_text(json.dumps({str(k): v for k, v in sorted(answers.items())}, indent=1))
    manifest = {"scale": scale, "rows": rows, "generated_with": session.scalar(sql="SELECT version()"),
                "queries": len(queries), "answers": len(answers)}
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return manifest


def tpch_queries(data_root: pathlib.Path, scale: float) -> list[tuple[str, str]]:
    raw = json.loads((tpch_dir(data_root=data_root, scale=scale) / "queries.json").read_text())
    return [(f"q{int(k):02d}", sql) for k, sql in sorted(raw.items(), key=lambda kv: int(kv[0]))]


def tpch_expected(data_root: pathlib.Path, scale: float) -> dict[str, list[list[str]]]:
    """Reference answers by query name: rows of text cells (the header line dropped)."""
    raw = json.loads((tpch_dir(data_root=data_root, scale=scale) / "answers.json").read_text())
    expected = {}
    for number, text in raw.items():
        # One line per row after the header, each ending in a newline; a NULL prints as nothing
        # (q17 at scale 0.01 answers one NULL: "avg_yearly\n\n").
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        expected[f"q{int(number):02d}"] = [line.split("|") for line in lines[1:]]
    return expected


def tpch_load_sql(data_root: pathlib.Path, scale: float) -> list[str]:
    directory = tpch_dir(data_root=data_root, scale=scale)
    return [f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{directory / (t + '.parquet')}')" for t in TPCH_TABLES]


# ---- Many pipelines ----------------------------------------------------------------------------

def mp_sizes(scale: float) -> tuple[int, int]:
    """Rows per part table and in audit_log: 50,000 and 1,000,000 at scale 1."""
    return max(1_000, int(50_000 * scale)), max(20_000, int(1_000_000 * scale))


def mp_load_sql(scale: float) -> list[str]:
    part_rows, audit_rows = mp_sizes(scale=scale)
    statements = [
        f"CREATE TABLE {name} AS SELECT i AS id, i % {TENANTS} AS tenant_id, "
        f"CASE WHEN i % 11 = {n % 11} THEN TIMESTAMP '2025-01-01' + INTERVAL (i) SECOND END AS deleted_at "
        f"FROM range({part_rows}) t(i)" for n, name in enumerate(PARTS)]
    names = "', '".join(PARTS)
    statements.append(
        f"CREATE TABLE audit_log AS SELECT i AS id, ['{names}'][1 + i % {len(PARTS)}] AS entity_table, "
        f"CASE WHEN i % 3 = 0 THEN 'delete' ELSE 'update' END AS action FROM range({audit_rows}) t(i)")
    return statements


_UNION = "\n  UNION ALL ".join(
    f"SELECT '{name}' AS entity_table, COUNT(*) AS total_soft_deleted, COUNT(DISTINCT tenant_id) AS tenant_count "
    f"FROM {name} WHERE deleted_at IS NOT NULL" for name in PARTS)

MP_QUERIES = {
    "union30": f"""WITH soft_deleted AS (
  {_UNION}
),
audit_counts AS (
  SELECT entity_table, COUNT(*) AS deleted_audit_rows FROM audit_log WHERE action = 'delete' GROUP BY entity_table
)
SELECT sd.entity_table, sd.total_soft_deleted, COALESCE(ac.deleted_audit_rows, 0) AS matching_audit_rows,
       CASE WHEN sd.total_soft_deleted = 0 THEN NULL
            ELSE 100.0 * COALESCE(ac.deleted_audit_rows, 0) / sd.total_soft_deleted END AS coverage_rate_pct,
       sd.tenant_count
FROM soft_deleted sd LEFT JOIN audit_counts ac ON ac.entity_table = sd.entity_table
ORDER BY sd.total_soft_deleted DESC NULLS FIRST, sd.entity_table ASC NULLS LAST""",
    "scalar30": "SELECT " + ",\n       ".join(
        f"(SELECT COUNT(*) FROM {name} WHERE deleted_at IS NOT NULL AND tenant_id < {10 + n}) AS {name}"
        for n, name in enumerate(PARTS))
        + ",\n       (SELECT COUNT(DISTINCT entity_table) FROM audit_log WHERE action = 'delete') AS audited_tables",
}


def mp_expected(scale: float) -> dict[str, list[list]]:
    """The exact answers of MP_QUERIES, computed from the generating expressions."""
    part_rows, audit_rows = mp_sizes(scale=scale)
    audit = [0] * len(PARTS)
    for i in range(0, audit_rows, 3):  # action = 'delete' where i % 3 = 0
        audit[i % len(PARTS)] += 1
    union_rows, scalar_row = [], []
    for n, name in enumerate(PARTS):
        deleted = range(n % 11, part_rows, 11)
        total = len(deleted)
        tenants = len({i % TENANTS for i in deleted})
        coverage = None if total == 0 else 100.0 * audit[n] / total
        union_rows.append([name, total, audit[n], coverage, tenants])
        scalar_row.append(sum(1 for i in deleted if i % TENANTS < 10 + n))
    union_rows.sort(key=lambda r: (-r[1], r[0]))
    scalar_row.append(sum(1 for count in audit if count > 0))
    return {"union30": union_rows, "scalar30": [scalar_row]}


# ---- Checking answers --------------------------------------------------------------------------

def _cell_matches(actual, expected) -> bool:
    if expected is None or expected in ("NULL", ""):
        return actual is None or actual == expected
    if actual is None:
        return False
    if isinstance(actual, bool):
        return str(actual).lower() == str(expected).lower()
    if isinstance(actual, (int, float, decimal.Decimal)):
        try:
            a, e = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return math.isclose(a, e, rel_tol=1e-6, abs_tol=1e-6)
    if isinstance(actual, (datetime.date, datetime.datetime)):
        return actual.isoformat(sep=" ") == str(expected) if isinstance(actual, datetime.datetime) \
            else actual.isoformat() == str(expected)
    return str(actual).rstrip() == str(expected).rstrip()


def _rows_match(actual: list[list], expected: list[list]) -> bool:
    return all(len(a) == len(e) and all(_cell_matches(actual=x, expected=y) for x, y in zip(a, e))
               for a, e in zip(actual, expected))


def check_answer(table, expected: list[list] | None) -> str | None:
    """None when `table` holds `expected` (row order first; rows that tie on the ORDER BY may
    come in any order, so a second, order-free comparison follows), else what differs."""
    if table is None:
        return "no result set"
    if expected is None:
        return None
    actual = [list(row.values()) for row in table.to_pylist()]
    if len(actual) != len(expected):
        return f"{len(actual)} rows, expected {len(expected)}"
    if _rows_match(actual=actual, expected=expected):
        return None
    key = lambda row: [str(cell) for cell in row]
    if _rows_match(actual=sorted(actual, key=key), expected=sorted(expected, key=key)):
        return None
    for index, (a, e) in enumerate(zip(actual, expected)):
        if not (len(a) == len(e) and all(_cell_matches(actual=x, expected=y) for x, y in zip(a, e))):
            return f"row {index}: {a!r} expected {e!r}"[:500]
    return "rows differ"
