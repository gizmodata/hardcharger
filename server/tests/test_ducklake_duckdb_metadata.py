"""DuckLake with a DuckDB metadata file stays readable by the DuckDB release that made it.

The bundled ducklake attaches an EXISTING metadata file without STORAGE_VERSION, so the
file keeps its own storage version (scripts/extension-patches/ducklake): a DuckLake that
DuckDB 1.5 created is still readable by DuckDB 1.5 after the server writes to it. Upstream
attached it with STORAGE_VERSION 'latest', and the next checkpoint upgraded the file, after
which DuckDB 1.5 refused it ("cannot initialize database with storage version ... lower
than what the database itself uses"). META_STORAGE_VERSION in the ATTACH still upgrades
on purpose. A NEW metadata file takes --storage-version when set, else the latest.

DuckDB 1.5.6 is the Python package (pyproject.toml), with its ducklake extension from
DuckDB's extension repository: needs the network once per module; without it these fail.
"""

import pathlib

import duckdb
import pytest

# The newest DuckLake format DuckDB 1.5's ducklake reads (a new lake is 1.1-dev1 otherwise).
DUCKLAKE_FORMAT = "1.0"
# How DuckDB 1.5 refuses a metadata file in a newer storage format: an upgraded one, a new one.
TOO_NEW = "lower than what the database itself uses|created with a newer version of DuckDB"


@pytest.fixture(scope="module")
def extension_directory(tmp_path_factory) -> pathlib.Path:
    return tmp_path_factory.mktemp("duckdb15-extensions")


def duckdb15(extension_directory: pathlib.Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB 1.5 with ducklake loaded."""
    assert duckdb.__version__.startswith("1.5."), duckdb.__version__
    con = duckdb.connect(database=":memory:", config={"extension_directory": str(extension_directory)})
    con.execute(query="INSTALL ducklake")
    con.execute(query="LOAD ducklake")
    return con


def lake_paths(tmp_path: pathlib.Path) -> tuple[str, str]:
    data = tmp_path / "lake-data"
    data.mkdir()
    return (tmp_path / "lake.ducklake").as_posix(), data.as_posix() + "/"


def read_with_duckdb15(extension_directory: pathlib.Path, metadata: str, data: str) -> list[tuple]:
    con = duckdb15(extension_directory=extension_directory)
    try:
        con.execute(query=f"ATTACH 'ducklake:{metadata}' AS lake (DATA_PATH '{data}')")
        return con.execute(query="SELECT id, name FROM lake.t ORDER BY id").fetchall()
    finally:
        con.close()


def write_from_server(launcher, metadata: str, data: str, args=None, attach_options: str = "") -> None:
    """Attaches the lake on a server, inserts a row, detaches it and stops the server, so
    the metadata file is checkpointed and closed. (The metadata database is attached
    hidden: its storage version tag isn't in duckdb_databases().)"""
    server = launcher.start(args=args)
    session = server.admin()
    session.execute(sql=f"ATTACH 'ducklake:{metadata}' AS lake (DATA_PATH '{data}'{attach_options})")
    session.execute(sql="CREATE TABLE IF NOT EXISTS lake.t (id INTEGER, name VARCHAR)")
    session.execute(sql="INSERT INTO lake.t VALUES (2, 'two')")
    session.execute(sql="DETACH lake")
    server.stop()


def create_with_duckdb15(extension_directory: pathlib.Path, metadata: str, data: str) -> None:
    con = duckdb15(extension_directory=extension_directory)
    try:
        con.execute(query=f"ATTACH 'ducklake:{metadata}' AS lake (DATA_PATH '{data}')")
        con.execute(query="CREATE TABLE lake.t (id INTEGER, name VARCHAR)")
        con.execute(query="INSERT INTO lake.t VALUES (1, 'one')")
    finally:
        con.close()


def test_existing_duckdb15_lake_stays_readable_by_duckdb15(launcher, tmp_path, extension_directory):
    metadata, data = lake_paths(tmp_path=tmp_path)
    create_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data)
    write_from_server(launcher=launcher, metadata=metadata, data=data)
    assert read_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data) == [
        (1, "one"), (2, "two")]


def test_meta_storage_version_upgrades_an_existing_lake_on_purpose(launcher, tmp_path, extension_directory):
    metadata, data = lake_paths(tmp_path=tmp_path)
    create_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data)
    write_from_server(launcher=launcher, metadata=metadata, data=data, attach_options=", META_STORAGE_VERSION 'latest'")
    with pytest.raises(duckdb.Error, match=TOO_NEW):
        read_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data)


def test_new_lake_takes_the_server_storage_version(launcher, tmp_path, extension_directory):
    metadata, data = lake_paths(tmp_path=tmp_path)
    write_from_server(launcher=launcher, metadata=metadata, data=data, args=["--storage-version", "v1.5.0"],
                      attach_options=f", DUCKLAKE_VERSION '{DUCKLAKE_FORMAT}'")
    assert read_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data) == [
        (2, "two")]


def test_new_lake_without_storage_version_is_latest(launcher, tmp_path, extension_directory):
    metadata, data = lake_paths(tmp_path=tmp_path)
    write_from_server(launcher=launcher, metadata=metadata, data=data,
                      attach_options=f", DUCKLAKE_VERSION '{DUCKLAKE_FORMAT}'")
    with pytest.raises(duckdb.Error, match=TOO_NEW):
        read_with_duckdb15(extension_directory=extension_directory, metadata=metadata, data=data)
