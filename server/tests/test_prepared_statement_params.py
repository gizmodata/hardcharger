"""Port of GizmoSQL 1.x tests/integration/test_prepared_statement_params.cpp (9 cases).

Prepared statements with Arrow parameters (Warp SQL §7: one execution per parameter
row). All 9 apply; three are re-expressed where Warp SQL decided differently:

- MultiRowBatchIsRejectedForQuery: 1.x refused a 2-row batch on a query ("exactly one
  row"); Warp SQL §7.2 runs it once per row and concatenates the rows, which is what
  the case asserts here.
- UntypedPlaceholderResolvesAtExecution: 1.x reported VARCHAR schemas for SELECT ? at
  prepare time; Warp SQL allows result_schema null until execution. The case asserts
  the behaviour that matters: an int binds as an int result, a string as a string.
- TypedPreparedQueryStillReportsSchemaInFlightInfo: the schema arrives with the result
  stream (§6.4) rather than in a separate FlightInfo; it is checked there.
"""

import datetime
import uuid

import pyarrow as pa
import pytest

from warp_client import read_table


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def run(session, sql: str, params: pa.Table, arrow: bool = True):
    prepared = session.prepare(sql=sql)
    response = session.execute_prepared(prepared_id=prepared["prepared_id"], arrow_parameters=params, arrow=arrow)
    assert response.status_code in (200, 201), response.text
    return response


def affected(response) -> int:
    doc = response.json()
    return doc["result"]["rows_affected"]


def test_null_parameter_is_stored_as_sql_null(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_null (id INT, name VARCHAR)")
    params = pa.table({"id": pa.array([1], type=pa.int32()), "name": pa.array([None], type=pa.utf8())})
    assert affected(run(session=session, sql="INSERT INTO pp_null VALUES (?, ?)", params=params, arrow=False)) == 1
    assert session.scalar(sql="SELECT name IS NULL FROM pp_null WHERE id = 1") is True
    assert session.scalar(sql="SELECT count(*) FROM pp_null WHERE name = 'null'") == 0


def test_dictionary_encoded_string_parameter_is_decoded(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_dict (id INT, name VARCHAR)")
    names = pa.DictionaryArray.from_arrays(indices=pa.array([1], type=pa.int32()), dictionary=pa.array(["alpha", "beta"]))
    params = pa.table({"id": pa.array([1], type=pa.int32()), "name": names})
    assert affected(run(session=session, sql="INSERT INTO pp_dict VALUES (?, ?)", params=params, arrow=False)) == 1
    assert session.scalar(sql="SELECT name FROM pp_dict WHERE id = 1") == "beta"


def test_multi_row_batch_executes_update_once_per_row(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_multi (id INT, name VARCHAR)")
    params = pa.table({"id": pa.array([1, 2, 3], type=pa.int32()), "name": ["one", "two", "three"]})
    assert affected(run(session=session, sql="INSERT INTO pp_multi VALUES (?, ?)", params=params, arrow=False)) == 3
    assert session.scalar(sql="SELECT count(*) FROM pp_multi") == 3
    assert session.scalar(sql="SELECT string_agg(name, ',' ORDER BY id) FROM pp_multi") == "one,two,three"


def test_multi_row_batch_on_a_query_concatenates_the_executions(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_q (id INT)")
    session.execute(sql="INSERT INTO pp_q VALUES (1), (2)")
    params = pa.table({"id": pa.array([1, 2], type=pa.int32())})
    table = read_table(content=run(session=session, sql="SELECT id FROM pp_q WHERE id = ?", params=params).content)
    assert table.column("id").to_pylist() == [1, 2]


def test_typed_parameters_keep_their_types(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_typed (id BIGINT, ratio DOUBLE, d DATE, ts TIMESTAMP, b BLOB, flag BOOLEAN)")
    params = pa.table({"id": pa.array([9007199254740993], type=pa.int64()),
                       "ratio": pa.array([0.25], type=pa.float64()),
                       "d": pa.array([datetime.date(2024, 1, 1)], type=pa.date32()),
                       "ts": pa.array([1704067200123], type=pa.timestamp("ms")),
                       "b": pa.array([b"\x00\x01\xff"], type=pa.binary()),
                       "flag": pa.array([True])})
    assert affected(run(session=session, sql="INSERT INTO pp_typed VALUES (?, ?, ?, ?, ?, ?)", params=params,
                        arrow=False)) == 1
    assert session.scalar(sql="SELECT id FROM pp_typed") == 9007199254740993
    assert session.scalar(sql="SELECT ratio FROM pp_typed") == 0.25
    assert session.scalar(sql="SELECT CAST(d AS VARCHAR) FROM pp_typed") == "2024-01-01"
    assert session.scalar(sql="SELECT CAST(ts AS VARCHAR) FROM pp_typed") == "2024-01-01 00:00:00.123"
    assert session.scalar(sql="SELECT octet_length(b) FROM pp_typed") == 3
    assert session.scalar(sql="SELECT flag FROM pp_typed") is True


def test_untyped_placeholder_resolves_at_execution(session):
    prepared = session.prepare(sql="SELECT ? AS x")
    from warp_client import ipc_bytes  # noqa: F401  (parameter schema is base64 Arrow IPC)
    assert prepared["parameter_schema"], prepared
    as_int = session.execute_prepared(prepared_id=prepared["prepared_id"],
                                      arrow_parameters=pa.table({"x": pa.array([42], type=pa.int32())}))
    table = read_table(content=as_int.content)
    assert table.schema.field(0).type == pa.int32() and table.column(0).to_pylist() == [42]
    as_text = session.execute_prepared(prepared_id=prepared["prepared_id"], arrow_parameters=pa.table({"x": ["hello"]}))
    table = read_table(content=as_text.content)
    assert pa.types.is_string(table.schema.field(0).type) and table.column(0).to_pylist() == ["hello"]


def test_parameterless_prepared_update_still_executes_once(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_noparam (id INT)")
    prepared = session.prepare(sql="INSERT INTO pp_noparam VALUES (7)")
    response = session.execute_prepared(prepared_id=prepared["prepared_id"], arrow=False)
    assert affected(response) == 1
    assert session.scalar(sql="SELECT count(*) FROM pp_noparam") == 1


def test_typed_prepared_query_reports_its_schema(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_sel (id INT, name VARCHAR)")
    session.execute(sql="INSERT INTO pp_sel VALUES (1, 'a'), (2, 'b')")
    response = run(session=session, sql="SELECT name FROM pp_sel WHERE id = ?",
                   params=pa.table({"id": pa.array([2], type=pa.int32())}))
    table = read_table(content=response.content)
    assert pa.types.is_string(table.schema.field("name").type)
    assert table.column("name").to_pylist() == ["b"]


def test_uuid_extension_parameter_binds_as_uuid(session):
    session.execute(sql="CREATE OR REPLACE TABLE pp_uuid (id UUID, v INT)")
    value = uuid.UUID("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
    session.execute(sql=f"INSERT INTO pp_uuid VALUES ('{value}', 7)")
    ids = pa.ExtensionArray.from_storage(typ=pa.uuid(), storage=pa.array([value.bytes], type=pa.binary(16)))
    params = pa.table({"id": ids})
    table = read_table(content=run(session=session, sql="SELECT v FROM pp_uuid WHERE id = ?", params=params).content)
    assert table.column("v").to_pylist() == [7]
    assert affected(run(session=session, sql="DELETE FROM pp_uuid WHERE id = ?", params=params, arrow=False)) == 1
    assert session.scalar(sql="SELECT count(*) FROM pp_uuid") == 0
