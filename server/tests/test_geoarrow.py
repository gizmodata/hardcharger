"""Port of GizmoSQL 1.x tests/integration/test_geoarrow.cpp (3 cases) and tests/test_geoarrow.py (1).

With the spatial extension, GEOMETRY exports as a GeoArrow extension type
(geoarrow.* or ogc.*) and a geoarrow.wkb field ingests as GEOMETRY in create, append and
replace modes, never as text. Strengthened: 1.x also accepted plain binary for the
export; on DuckDB 2.0 the extension name is required. Needs the network once to INSTALL
spatial (DuckDB's extension repository); without it these fail, they don't skip.
"""

import struct

import pyarrow as pa
import pytest


@pytest.fixture(scope="module")
def session(module_launcher):
    # Loaded by the init SQL, before the server runs anything else (its health check, a
    # session): spatial is DuckDB's prebuilt download, not built with ThreadSanitizer, so
    # under TSan its LOAD racing any other thread's planning is reported as a data race
    # (it swaps DuckDB's extension-callback registry, and TSan can't see the uninstrumented
    # reference count that orders the swap after the planner's read; TSan CI, twice). The
    # download can take a while: the startup wait is a minute.
    return module_launcher.start(args=["--init-sql-commands", "INSTALL spatial; LOAD spatial;"],
                                 startup_seconds=60).admin()


def extension_name(field: pa.Field) -> str:
    if isinstance(field.type, pa.ExtensionType):
        return field.type.extension_name
    return (field.metadata or {}).get(b"ARROW:extension:name", b"").decode()


def test_geometry_exports_as_geoarrow(session):
    session.execute(sql="CREATE OR REPLACE TABLE test_geo (id INTEGER, name VARCHAR, geom GEOMETRY)")
    session.execute(sql="INSERT INTO test_geo VALUES (1, 'a', ST_Point(1, 2)), (2, 'b', ST_Point(3, 4)), "
                        "(3, 'c', ST_MakeLine(ST_Point(0, 0), ST_Point(1, 1)))")
    table = session.query(sql="SELECT * FROM test_geo ORDER BY id")
    assert table.num_rows == 3
    name = extension_name(table.schema.field("geom"))
    assert name.startswith("geoarrow.") or name.startswith("ogc."), table.schema


def test_geometry_ingests_as_geometry(session):
    def batch(first_id: int) -> pa.Table:
        wkb = [b"\x01" + struct.pack("<I", 1) + struct.pack("<dd", i, i * 10.0) for i in (first_id, first_id + 1)]
        field = pa.field("geom", pa.binary(), metadata={b"ARROW:extension:name": b"geoarrow.wkb",
                                                        b"ARROW:extension:metadata": b"{}"})
        return pa.Table.from_arrays([pa.array([first_id, first_id + 1], type=pa.int32()), pa.array(wkb)],
                                    schema=pa.schema([pa.field("id", pa.int32()), field]))

    def check(rows: int, last: str) -> None:
        row = session.query(sql="SELECT typeof(geom) AS t, ST_AsText(geom) AS w, count(*) OVER () AS n "
                                "FROM geo_ingest ORDER BY id DESC LIMIT 1").to_pylist()[0]
        assert row == {"t": "GEOMETRY", "w": last, "n": rows}, row

    session.execute(sql="DROP TABLE IF EXISTS geo_ingest")
    for first_id, mode, rows, last in [(1, "create", 2, "POINT (2 20)"), (3, "append", 4, "POINT (4 40)"),
                                       (7, "replace", 2, "POINT (8 80)")]:
        response = session.ingest(table=batch(first_id), name="geo_ingest", mode=mode)
        assert response.status_code == 200 and response.json()["rows_ingested"] == 2, response.text
        check(rows=rows, last=last)


def test_various_geometry_types(session):
    for sql in ["SELECT ST_Point(1.0, 2.0) AS g", "SELECT ST_GeomFromText('LINESTRING(0 0, 1 1, 2 2)') AS g",
                "SELECT ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))') AS g"]:
        table = session.query(sql=sql)
        assert table.num_rows == 1
        name = extension_name(table.schema.field("g"))
        assert name.startswith("geoarrow.") or name.startswith("ogc."), (sql, table.schema)


def test_python_geoarrow_distance_and_area(session):
    # tests/test_geoarrow.py: mixed geometries round trip, ST_Distance and ST_Area.
    assert session.scalar(sql="SELECT ST_Distance(ST_Point(0, 0), ST_Point(3, 4))") == 5.0
    assert session.scalar(sql="SELECT ST_Area(ST_GeomFromText('POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))'))") == 100.0
    mixed = session.query(sql="SELECT * FROM (VALUES (ST_Point(1, 1)), (ST_GeomFromText('LINESTRING(0 0, 1 1)'))) t(g)")
    assert mixed.num_rows == 2
