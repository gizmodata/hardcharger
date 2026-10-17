"""Port of GizmoSQL 1.x tests/integration/test_result_streaming.cpp (2 cases).

A 100,000-row result arrives in many Arrow batches (Warp SQL §6.4), every column intact
(types, ids contiguous across batches, every 7th value NULL, timestamps, booleans, sums
in closed form), and no batch is larger than one DuckDB vector (2048 rows,
result_batch_rows' default, as 1.x streamed). A prepared WHERE id >= ? bound to 0 and
then to 90000 on the same handle streams 100,000 and then 10,000 rows. Both apply.
"""

import datetime

import pyarrow as pa
import pytest

from warp_client import read_batches

ROWS = 100_000
INIT_SQL = ("CREATE TABLE stream_rows AS SELECT i AS id, 'row-' || i AS label, "
            "CASE WHEN i % 7 = 0 THEN NULL ELSE (i * 1.5)::DOUBLE END AS value, "
            "TIMESTAMP '2026-01-01 00:00:00' + to_seconds(i) AS ts, (i % 3 = 0) AS flag FROM range(100000) t(i);")


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_INIT_SQL_COMMANDS": INIT_SQL}).admin()


def stats(batches: list[pa.RecordBatch]) -> dict:
    return {"batches": len(batches), "rows": sum(b.num_rows for b in batches),
            "max_batch_rows": max(b.num_rows for b in batches)}


def test_ad_hoc_query_streams_many_batches_with_every_column_intact(session):
    response = session.submit(sql="SELECT id, label, value, ts, flag FROM stream_rows ORDER BY id", arrow=True)
    assert response.status_code == 200, response.text
    batches = read_batches(content=response.content)
    schema = batches[0].schema
    assert [f.type for f in schema] == [pa.int64(), pa.string(), pa.float64(), pa.timestamp("us"), pa.bool_()], schema
    base = datetime.datetime(2026, 1, 1)
    expected_id = 0
    nulls = 0
    value_sum = 0.0
    for batch in batches:
        ids = batch.column(0).to_pylist()
        assert ids == list(range(expected_id, expected_id + batch.num_rows)), "ids not contiguous"
        values = batch.column(2).to_pylist()
        for i, v in zip(ids, values):
            if i % 7 == 0:
                assert v is None
                nulls += 1
            else:
                assert v == i * 1.5
                value_sum += v
        assert batch.column(3).to_pylist() == [base + datetime.timedelta(seconds=i) for i in ids]
        assert batch.column(4).to_pylist() == [i % 3 == 0 for i in ids]
        labels = batch.column(1)
        for k in (0, batch.num_rows - 1):
            assert labels[k].as_py() == f"row-{ids[k]}"
        expected_id += batch.num_rows
    s = stats(batches=batches)
    assert s["rows"] == ROWS
    assert nulls == (ROWS + 6) // 7
    assert value_sum == sum(i * 1.5 for i in range(ROWS) if i % 7 != 0)
    assert s["batches"] > 10, "the result was not streamed in batches"
    assert s["max_batch_rows"] <= 2048


def test_prepared_query_streams_many_batches_in_order(session):
    prepared = session.prepare(sql="SELECT id, label FROM stream_rows WHERE id >= ? ORDER BY id")
    full = read_batches(content=session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[0]).content)
    s = stats(batches=full)
    assert s["rows"] == ROWS and s["batches"] > 10 and s["max_batch_rows"] <= 2048, s
    assert pa.Table.from_batches(full).column("id").to_pylist() == list(range(ROWS))
    tail = read_batches(content=session.execute_prepared(prepared_id=prepared["prepared_id"], parameters=[90000]).content)
    t = stats(batches=tail)
    assert t["rows"] == 10000 and t["batches"] > 1, t
