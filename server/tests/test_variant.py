"""Port of GizmoSQL 1.x tests/integration/test_variant.cpp (4 cases; DuckDB 2 only in 1.x, so all apply).

One case fails today and runs as a strict expected failure (it turns red once it passes):
binding a VARIANT parameter (the SDK can't bind struct or extension Arrow parameters).

VARIANT exports as Arrow's canonical arrow.parquet.variant extension (a struct of
binary metadata and value; only the NULL row is null at the top level), ingests back
as a VARIANT column, binds as a prepared parameter, and the parameter schema of
SELECT ?::VARIANT advertises the extension.
"""

import pyarrow as pa
import pytest

from chargewire_client import read_table

EXTENSION = "arrow.parquet.variant"
ROWS = ("SELECT * FROM (VALUES (1, 42::VARIANT), (2, 'hello'::VARIANT), (3, 3.5::DOUBLE::VARIANT), "
        "(4, {'a': 1, 'b': [true, NULL, false]}::VARIANT), (5, NULL::VARIANT)) AS t(id, v)")


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def extension_name(field: pa.Field) -> str | None:
    if isinstance(field.type, pa.ExtensionType):
        return field.type.extension_name
    return (field.metadata or {}).get(b"ARROW:extension:name", b"").decode() or None


def storage(field: pa.Field) -> pa.DataType:
    return field.type.storage_type if isinstance(field.type, pa.ExtensionType) else field.type


def expect_variant_field(field: pa.Field) -> None:
    assert extension_name(field) == EXTENSION, field
    struct = storage(field)
    assert pa.types.is_struct(struct) and struct.num_fields == 2, struct
    for child in ("metadata", "value"):
        child_type = struct.field(child).type
        assert pa.types.is_binary(child_type) or pa.types.is_large_binary(child_type) or \
            pa.types.is_binary_view(child_type), struct


@pytest.fixture(scope="module")
def expected_text(session):
    return session.query(sql=f"SELECT v::VARCHAR FROM ({ROWS}) ORDER BY id").column(0).to_pylist()


def test_variant_exports_as_canonical_arrow_extension(session):
    table = session.query(sql=f"{ROWS} ORDER BY id")
    assert table.num_rows == 5
    expect_variant_field(table.schema.field("v"))
    assert table.column("v").null_count == 1
    assert table.column("v")[4].is_valid is False


def test_variant_round_trips_through_ingest(session, expected_text):
    exported = session.query(sql=f"{ROWS} ORDER BY id")
    response = session.ingest(table=exported, name="variant_ingest", mode="replace")
    assert response.status_code == 200 and response.json()["rows_ingested"] == 5, response.text
    assert session.scalar(sql="SELECT data_type FROM information_schema.columns "
                              "WHERE table_name = 'variant_ingest' AND column_name = 'v'") == "VARIANT"
    assert session.query(sql="SELECT v::VARCHAR FROM variant_ingest ORDER BY id").column(0).to_pylist() == expected_text


@pytest.mark.xfail(strict=True, reason="SDK gap: Arrow parameters of struct and extension types (VARIANT's "
                   "struct<metadata, value>) can't be bound yet (415 unsupported_arrow_type)")
def test_variant_binds_as_prepared_statement_parameter(session, expected_text):
    exported = session.query(sql=f"{ROWS} ORDER BY id")
    prepared = session.prepare(sql="SELECT typeof(?::VARIANT), ?::VARIANT::VARCHAR")
    field = exported.schema.field("v")
    for row in range(exported.num_rows):
        value = exported.column("v").slice(row, 1).combine_chunks()
        params = pa.Table.from_arrays([value, value], schema=pa.schema([field.with_name("p1"), field.with_name("p2")]))
        result = read_table(content=session.execute_prepared(prepared_id=prepared["prepared_id"],
                                                             arrow_parameters=params).content)
        assert result.num_rows == 1
        assert result.column(0).to_pylist() == ["VARIANT"], row
        assert result.column(1)[0].as_py() == expected_text[row], row


def test_variant_parameter_schema_advertises_extension(session):
    import base64
    import pyarrow.ipc as ipc
    prepared = session.prepare(sql="SELECT ?::VARIANT")
    import io
    schema = ipc.open_stream(source=io.BytesIO(initial_bytes=base64.b64decode(prepared["parameter_schema"]))).schema
    assert len(schema) == 1
    expect_variant_field(schema.field(0))
