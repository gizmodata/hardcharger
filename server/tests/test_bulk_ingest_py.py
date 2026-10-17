"""Port of GizmoSQL 1.x tests/test_bulk_ingest.py (1 case).

TPC-H lineitem at scale factor 0.01, ingested in mode replace in 10,000-row batches,
arrives whole: the row count matches, and so do the sums (1.x printed the sums without
asserting them; here they are asserted). 1.x generated the data with a local DuckDB; here
the server generates it, the test reads it back as Arrow and ingests it into a new table.
Named test_bulk_ingest_py.py so it doesn't collide with the port of test_bulk_ingest.cpp.
"""

import pyarrow as pa
import pytest

SUMS = ("SELECT count(*) AS n, sum(l_quantity)::DOUBLE AS quantity, sum(l_extendedprice)::DOUBLE AS price, "
        "sum(l_orderkey) AS orderkeys FROM {table}")


@pytest.fixture(scope="module")
def session(module_launcher):
    session = module_launcher.start(env={"PRINT_QUERIES": "false"}).admin()
    session.execute(sql="INSTALL tpch; LOAD tpch;")
    session.execute(sql="CALL dbgen(sf = 0.01)")
    return session


def test_bulk_ingest_lineitem(session):
    lineitem = session.query(sql="SELECT * FROM lineitem")
    expected = session.rows(sql=SUMS.format(table="lineitem"))[0]
    batches = lineitem.to_batches(max_chunksize=10_000)
    assert len(batches) > 1
    response = session.ingest(table=pa.Table.from_batches(batches=batches), name="bulk_ingest_lineitem", mode="replace")
    assert response.status_code == 200, response.text
    assert response.json()["rows_ingested"] == lineitem.num_rows == expected["n"]
    assert session.rows(sql=SUMS.format(table="bulk_ingest_lineitem"))[0] == expected
