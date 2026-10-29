"""Port of GizmoSQL 1.x tests/test_v1_22_1_features.py (2 cases).

PRIMARY KEY and UNIQUE constraints appear in hardcharger_index_info with NON_UNIQUE false
(beside a CREATE INDEX), and a table's schema carries nullability, REMARKS,
IS_AUTO_INCREMENT and CHARGEWIRE:SQL:COLUMN_DEFAULT (1.x's GIZMOSQL:COLUMN_DEFAULT; 1.x read it through ADBC's
adbc_get_table_schema; here through Chargewire's metadata/tables?include_schema=true). Both apply.
"""

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


@pytest.fixture(scope="module")
def session(module_launcher):
    return module_launcher.start().admin()


def test_constraint_backed_indexes_are_listed(session):
    session.execute(sql="CREATE TABLE v1221_t (id INT PRIMARY KEY, code VARCHAR UNIQUE, label VARCHAR)")
    session.execute(sql="CREATE INDEX v1221_t_label_idx ON v1221_t(label)")
    rows = session.rows(sql='SELECT "INDEX_NAME", "NON_UNIQUE", "COLUMN_NAME" FROM _hardcharger_system.main.hardcharger_index_info '
                            "WHERE \"TABLE_NAME\" = 'v1221_t'")
    by_column = {r["COLUMN_NAME"]: r for r in rows}
    assert by_column["id"]["NON_UNIQUE"] is False, rows    # the PRIMARY KEY's index
    assert by_column["code"]["NON_UNIQUE"] is False, rows  # the UNIQUE constraint's
    assert by_column["label"]["NON_UNIQUE"] is True and by_column["label"]["INDEX_NAME"] == "v1221_t_label_idx", rows


def test_table_schema_carries_column_metadata(session):
    session.execute(sql="CREATE SEQUENCE v1221_meta_seq START 1")
    session.execute(sql="CREATE TABLE v1221_meta (id INT PRIMARY KEY DEFAULT nextval('v1221_meta_seq'), "
                        "name VARCHAR NOT NULL, note VARCHAR DEFAULT 'n/a')")
    session.execute(sql="COMMENT ON COLUMN v1221_meta.name IS 'employee name'")
    tables = session.metadata(kind="tables", table_name_filter_pattern="v1221_meta", include_schema="true").to_pylist()
    schema = ipc.read_schema(pa.py_buffer(tables[0]["table_schema"]))
    assert schema.field("id").nullable is False
    assert schema.field("name").metadata[b"ARROW:FLIGHT:SQL:REMARKS"] == b"employee name"
    assert schema.field("id").metadata[b"ARROW:FLIGHT:SQL:IS_AUTO_INCREMENT"] == b"1"
    assert schema.field("note").metadata[b"CHARGEWIRE:SQL:COLUMN_DEFAULT"] == b"'n/a'"
    assert b"GIZMOSQL:COLUMN_DEFAULT" not in schema.field("note").metadata
