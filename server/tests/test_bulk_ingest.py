"""Port of GizmoSQL 1.x tests/integration/test_bulk_ingest.cpp (8 cases).

Bulk ingest (Warp SQL §8.1: POST /sessions/{id}/ingest, an Arrow IPC stream). Modes map
from 1.x's Flight table-definition options to Warp's mode parameter (create, append,
replace, create_append). Ingest works inside an open transaction and into temporary
tables, repeatably; every Arrow type DuckDB can read gets the column type DuckDB's own
ingest gives it, and one it can't is refused (415 unsupported_arrow_type) with the
reason, never stored as text. The log contract (kind ingest, status attempt and success,
user, session, catalog, schema, table, rows, duration) is read from JSON log lines; the
Flight method name becomes "ingest". All 8 apply. Not ported: 1.x's Flight-only "ingest
options" map, which has no Warp SQL equivalent.
"""

import datetime
import decimal
import threading
import uuid

import pyarrow as pa
import pytest

from conftest import STOP_SIGNAL


@pytest.fixture(scope="module")
def server(module_launcher):
    return module_launcher.start(env={"HARDCHARGER_LOG_FORMAT": "json", "HARDCHARGER_PRINT_QUERIES": "true"},
                                 args=["--database", "bulk_ingest_tester.duckdb"])


@pytest.fixture
def session(server):
    return server.admin()


SAMPLE = pa.table({"id": pa.array([1, 2, 3], type=pa.int64()), "name": ["alice", "bob", "carol"]})


def ingested(response) -> int:
    assert response.status_code == 200, response.text
    return response.json()["rows_ingested"]


def column_types(session, table: str) -> dict[str, str]:
    rows = session.rows(sql=f"SELECT column_name, data_type FROM information_schema.columns "
                            f"WHERE table_name = '{table}' ORDER BY ordinal_position")
    return {r["column_name"]: r["data_type"] for r in rows}


def test_execute_ingest_end_to_end(session):
    assert ingested(session.ingest(table=SAMPLE, name="ingest_e2e", mode="create")) == 3
    assert session.rows(sql="SELECT id, name FROM ingest_e2e ORDER BY id") == SAMPLE.to_pylist()


def test_execute_ingest_inside_open_transaction(session):
    assert session.set_options(options={"autocommit": False}).status_code == 200
    assert ingested(session.ingest(table=SAMPLE, name="ingest_txn", mode="create")) == 3
    assert session.scalar(sql="SELECT count(*) FROM ingest_txn") == 3
    session.http.post(url=f"{session.url}/commit", headers=session.headers())
    assert ingested(session.ingest(table=SAMPLE, name="ingest_txn", mode="append")) == 3
    session.http.post(url=f"{session.url}/commit", headers=session.headers())
    session.set_options(options={"autocommit": True})
    assert session.scalar(sql="SELECT count(*) FROM ingest_txn") == 6


def test_execute_ingest_temporary_repeatable(session):
    for mode, expected in [("create_append", 3), ("create_append", 6), ("replace", 3), ("replace", 3)]:
        assert ingested(session.ingest(table=SAMPLE, name="ingest_temp", mode=mode, temporary=True)) == 3
        assert session.scalar(sql="SELECT count(*) FROM temp.main.ingest_temp") == expected
    assert ingested(session.ingest(table=SAMPLE, name="ingest_temp2", mode="create", temporary=True)) == 3
    assert ingested(session.ingest(table=SAMPLE, name="ingest_temp2", mode="append", temporary=True)) == 3
    assert session.scalar(sql="SELECT count(*) FROM temp.main.ingest_temp2") == 6


def test_null_typed_column_ingests_as_varchar(session):
    first = pa.table({"id": pa.array([1, 2], type=pa.int64()), "s": pa.nulls(2)})
    assert ingested(session.ingest(table=first, name="ingest_null", mode="create")) == 2
    later = pa.table({"id": pa.array([3], type=pa.int64()), "s": ["value"]})
    assert ingested(session.ingest(table=later, name="ingest_null", mode="append")) == 1
    assert column_types(session=session, table="ingest_null")["s"] == "VARCHAR"
    assert session.rows(sql="SELECT count(*) AS total, count(s) AS non_null FROM ingest_null") == [{"total": 3, "non_null": 1}]


def test_execute_ingest_logs_caller_target_and_row_count(server, session):
    assert ingested(session.ingest(table=SAMPLE, name="ingest_logging_table", mode="create")) == 3
    records = [r for r in server.log_records() if r.get("kind") == "ingest" and r.get("table") == "ingest_logging_table"]
    attempt = [r for r in records if r.get("status") == "attempt"]
    success = [r for r in records if r.get("status") == "success"]
    assert attempt and success, records
    for record in (attempt[0], success[0]):
        assert record["level"] == "info" and record["user"] == "admin" and record["session_id"], record
        assert record["table"] == "ingest_logging_table" and record["flight_method"] == "ingest", record
    assert success[0]["rows_ingested"] == "3" and success[0]["duration_ms"].isdigit(), success[0]


def test_month_day_nano_interval_ingests_as_interval(session):
    table = pa.table({"mdn": pa.array([pa.MonthDayNano([0, 0, 7_200_000_000_000])], type=pa.month_day_nano_interval()),
                      "ym": pa.array([pa.MonthDayNano([14, 3, 0])], type=pa.month_day_nano_interval()),
                      "dur": pa.array([7_200_000_000], type=pa.duration("us"))})
    assert ingested(session.ingest(table=table, name="interval_ingest", mode="replace")) == 1
    assert column_types(session=session, table="interval_ingest") == {"mdn": "INTERVAL", "ym": "INTERVAL", "dur": "INTERVAL"}
    row = session.query(sql="SELECT epoch(mdn)::VARCHAR AS a, epoch(dur)::VARCHAR AS b, ym::VARCHAR AS c, "
                            "ym = INTERVAL '1 year 2 months 3 days' AS d FROM interval_ingest").to_pylist()[0]
    assert row == {"a": "7200.0", "b": "7200.0", "c": "1 year 2 months 3 days", "d": True}, row


def test_arrow_types_ingest_as_their_duckdb_types(session):
    union = pa.UnionArray.from_sparse(types=pa.array([1], type=pa.int8()),
                                      children=[pa.array([0], type=pa.int64()), pa.array(["u"])], field_names=["i", "s"])
    columns = {
        "sview": pa.array(["view"], type=pa.string_view()),
        "bview": pa.array([b"bv"], type=pa.binary_view()),
        "fsb": pa.array([b"abcd"], type=pa.binary(4)),
        "dict": pa.DictionaryArray.from_arrays(indices=pa.array([0], type=pa.int32()), dictionary=pa.array(["dict"])),
        "dec32": pa.array([decimal.Decimal("12.34")], type=pa.decimal32(7, 2)),
        "dec64": pa.array([decimal.Decimal("123.456")], type=pa.decimal64(15, 3)),
        "lview": pa.ListViewArray.from_arrays(offsets=pa.array([0], type=pa.int32()), sizes=pa.array([2], type=pa.int32()),
                                              values=pa.array([1, 2], type=pa.int32())),
        "llview": pa.LargeListViewArray.from_arrays(offsets=pa.array([0], type=pa.int64()),
                                                    sizes=pa.array([1], type=pa.int64()),
                                                    values=pa.array([7], type=pa.int32())),
        "months": pa.array([14], type=pa.month_interval()) if hasattr(pa, "month_interval") else None,
        "ree": pa.RunEndEncodedArray.from_arrays(run_ends=pa.array([1], type=pa.int32()), values=pa.array(["ree"])),
        "uuid": pa.ExtensionArray.from_storage(typ=pa.uuid(), storage=pa.array([b"0123456789abcdef"], type=pa.binary(16))),
        "b8": pa.ExtensionArray.from_storage(typ=pa.bool8(), storage=pa.array([1], type=pa.int8())),
        "sunion": union,
    }
    columns = {k: v for k, v in columns.items() if v is not None}
    table = pa.table(columns)
    assert ingested(session.ingest(table=table, name="arrow_types_ingest", mode="replace")) == 1
    expected = {"sview": "VARCHAR", "bview": "BLOB", "fsb": "BLOB", "dict": "VARCHAR", "dec32": "DECIMAL(7,2)",
                "dec64": "DECIMAL(15,3)", "lview": "INTEGER[]", "llview": "INTEGER[]", "months": "INTERVAL",
                "ree": "VARCHAR", "uuid": "UUID", "b8": "BOOLEAN", "sunion": "UNION(i BIGINT, s VARCHAR)"}
    expected = {k: v for k, v in expected.items() if k in columns}
    assert column_types(session=session, table="arrow_types_ingest") == expected
    row = session.query(sql="SELECT sview, bview::VARCHAR AS bview, fsb::VARCHAR AS fsb, dict, dec32::VARCHAR AS dec32, "
                            "dec64::VARCHAR AS dec64, lview::VARCHAR AS lview, llview::VARCHAR AS llview, "
                            "ree, uuid::VARCHAR AS uuid, b8::VARCHAR AS b8, sunion::VARCHAR AS sunion "
                            "FROM arrow_types_ingest").to_pylist()[0]
    assert row == {"sview": "view", "bview": "bv", "fsb": "abcd", "dict": "dict", "dec32": "12.34", "dec64": "123.456",
                   "lview": "[1, 2]", "llview": "[7]", "ree": "ree", "uuid": "30313233-3435-3637-3839-616263646566",
                   "b8": "true", "sunion": "u"}, row


def test_unsupported_arrow_type_is_refused_not_stored_as_varchar(session):
    import struct
    halves = pa.Array.from_buffers(type=pa.float16(), length=1, buffers=[None, pa.py_buffer(struct.pack("<e", 1.5))])
    half = pa.table({"h": halves})
    response = session.ingest(table=half, name="halffloat_ingest", mode="replace")
    assert response.status_code == 415 and response.json()["code"] == "unsupported_arrow_type", response.text
    assert "halffloat" in response.json()["detail"], response.text
    assert column_types(session=session, table="halffloat_ingest") == {}
    # Nested day_time intervals: pyarrow can't build that type, so the refusal (and the
    # top-level day_time case of ArrowTypesIngestAsTheirDuckDBTypes) is the C++ unit test
    # IngestTypes.* in server/unit/ingest_test.cc, run here.
    from conftest import run_unit_tests
    results = run_unit_tests(gtest_filter="IngestTypes.*")
    assert results and all(results.values()), results
    single = pa.table({"h": pa.array([1.5], type=pa.float32())})
    assert ingested(session.ingest(table=single, name="float_ingest", mode="replace")) == 1
    assert column_types(session=session, table="float_ingest")["h"] == "FLOAT"


def test_failing_ingests_answer_their_error_and_the_server_exits_cleanly(launcher):
    # Many bulk loads failing in the engine at once (append to a table that doesn't exist):
    # the engine thread hands each failure to the request's I/O thread, which builds the
    # problem response from it. The error must be the request's own copy by then; under
    # ThreadSanitizer, an error freed on the engine thread while the response was read
    # from it made the server exit 66 (a report) instead of 0. (Stopped at once, not
    # drained, so this exit tests the ingests alone.)
    server = launcher.start(env={"HARDCHARGER_GRACEFUL_SHUTDOWN": "false"})
    errors = []

    def load(n: int) -> None:
        session = server.admin()
        for i in range(25):
            response = session.ingest(table=SAMPLE, name=f"no_such_table_{n}_{i}", mode="append")
            if response.status_code < 400 or "no_such_table" not in response.json().get("detail", ""):
                errors.append(response.text)

    threads = [threading.Thread(target=load, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [], errors[:3]
    server.process.send_signal(STOP_SIGNAL)
    assert server.process.wait(timeout=120) == 0, server.log()[-2000:]
