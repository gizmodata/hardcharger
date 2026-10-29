"""Port of GizmoSQL 1.x tests/integration/test_table_column_metadata.cpp (3 cases).

Table schemas from GET .../metadata/tables?include_schema=true (Chargewire §10) carry what
Flight SQL drivers read: PRIMARY KEY and NOT NULL columns are not nullable,
ARROW:FLIGHT:SQL:REMARKS holds a column comment, ARROW:FLIGHT:SQL:IS_AUTO_INCREMENT is
1 for a sequence default, ARROW:FLIGHT:SQL:TYPE_NAME is the base type, and
CHARGEWIRE:SQL:COLUMN_DEFAULT holds a default (Chargewire's key; 1.x's GIZMOSQL:COLUMN_DEFAULT is
not reported: Hardcharger takes only its own names). View
columns are all nullable, and metadata comes from the requested catalog when two have a
table of the same name. All 3 apply.
"""

import base64

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

ABSENT = "<absent>"


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def table_schema(session, catalog: str, table: str) -> pa.Schema:
    tables = session.metadata(kind="tables", catalog=catalog, table_name_filter_pattern=table, include_schema="true")
    rows = [r for r in tables.to_pylist() if r["table_name"] == table]
    assert len(rows) == 1, rows
    return ipc.read_schema(pa.py_buffer(rows[0]["table_schema"]))


def meta(schema: pa.Schema, column: str, key: str) -> str:
    metadata = schema.field(column).metadata or {}
    return metadata.get(key.encode(), ABSENT.encode()).decode()


def test_table_columns_carry_constraints_comments_and_defaults(session):
    session.execute(sql="CREATE OR REPLACE SEQUENCE colmeta_seq START 1")
    session.execute(sql="CREATE OR REPLACE TABLE colmeta (id INT PRIMARY KEY DEFAULT nextval('colmeta_seq'), "
                        "name VARCHAR NOT NULL, salary DECIMAL(10,2) DEFAULT 50000.00, note VARCHAR)")
    session.execute(sql="COMMENT ON COLUMN colmeta.name IS 'employee name'")
    schema = table_schema(session=session, catalog="memory", table="colmeta")
    assert [schema.field(c).nullable for c in ("id", "name", "salary", "note")] == [False, False, True, True]
    assert meta(schema, "name", "ARROW:FLIGHT:SQL:REMARKS") == "employee name"
    assert meta(schema, "note", "ARROW:FLIGHT:SQL:REMARKS") == ABSENT
    assert meta(schema, "id", "ARROW:FLIGHT:SQL:IS_AUTO_INCREMENT") == "1"
    assert meta(schema, "name", "ARROW:FLIGHT:SQL:IS_AUTO_INCREMENT") == "0"
    assert "nextval" in meta(schema, "id", "CHARGEWIRE:SQL:COLUMN_DEFAULT")
    assert "50000" in meta(schema, "salary", "CHARGEWIRE:SQL:COLUMN_DEFAULT")
    assert meta(schema, "note", "CHARGEWIRE:SQL:COLUMN_DEFAULT") == ABSENT
    assert meta(schema, "id", "GIZMOSQL:COLUMN_DEFAULT") == ABSENT
    assert meta(schema, "salary", "ARROW:FLIGHT:SQL:TYPE_NAME") == "DECIMAL"
    assert meta(schema, "id", "ARROW:FLIGHT:SQL:TYPE_NAME") == "INTEGER"


def test_view_columns_carry_comments_and_types(session):
    session.execute(sql="CREATE OR REPLACE TABLE colmeta_base (id INT NOT NULL, name VARCHAR)")
    session.execute(sql="CREATE OR REPLACE VIEW colmeta_view AS SELECT id, name AS employee FROM colmeta_base")
    session.execute(sql="COMMENT ON COLUMN colmeta_view.employee IS 'who'")
    schema = table_schema(session=session, catalog="memory", table="colmeta_view")
    assert meta(schema, "employee", "ARROW:FLIGHT:SQL:REMARKS") == "who"
    assert meta(schema, "employee", "ARROW:FLIGHT:SQL:TYPE_NAME") == "VARCHAR"
    assert meta(schema, "id", "ARROW:FLIGHT:SQL:TYPE_NAME") == "INTEGER"
    assert schema.field("id").nullable is True


def test_metadata_comes_from_the_requested_catalog(session):
    session.execute(sql="CREATE OR REPLACE TABLE twin (x INT, label VARCHAR)")
    session.execute(sql="ATTACH IF NOT EXISTS ':memory:' AS other_catalog")
    session.execute(sql="CREATE OR REPLACE TABLE other_catalog.main.twin (x INT NOT NULL, label VARCHAR)")
    session.execute(sql="COMMENT ON COLUMN other_catalog.main.twin.label IS 'other label'")
    other = table_schema(session=session, catalog="other_catalog", table="twin")
    assert other.field("x").nullable is False
    assert meta(other, "label", "ARROW:FLIGHT:SQL:REMARKS") == "other label"
    local = table_schema(session=session, catalog="memory", table="twin")
    assert local.field("x").nullable is True
    assert meta(local, "label", "ARROW:FLIGHT:SQL:REMARKS") == ABSENT
