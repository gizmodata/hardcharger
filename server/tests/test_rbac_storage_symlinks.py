"""A location on the server's file system covers what is inside it, not what its symlinks
lead to: every file a statement opens, lists or globs there is checked by where it really is.
A glob below a location's fixed part (shared/**) walks into a symlinked directory, so the
check runs where DuckDB opens each file, not only on the path the statement names; a match
that leads out of the location is left out of the listing and refused when opened. DuckLake's
own files, beside or below a location, are DuckLake's (test_rbac_ducklake_storage.py)."""

import os
import pathlib

import pytest

from rbac import expect_allowed, expect_blocked, start_rbac_server


@pytest.fixture(scope="module")
def world(module_launcher, store, tmp_path_factory):
    work = tmp_path_factory.mktemp("symlinks").resolve()
    server = start_rbac_server(launcher=module_launcher, store_uri=store.new_database(),
                               args=["--database", str(work / "links.duckdb")],
                               env={"HARDCHARGER_INIT_SQL_COMMANDS": "INSTALL ducklake; LOAD ducklake;"})
    admin = server.admin()
    for directory in ["shared", "secret", "out", "lakes/local", "inbox"]:
        (work / directory).mkdir(parents=True)
    (work / "shared" / "a.csv").write_text("x\n1\n")
    (work / "secret" / "s.csv").write_text("x\n666\n")
    os.symlink(work / "secret", work / "shared" / "link")  # a directory symlink out of the location
    os.symlink(work / "secret" / "s.csv", work / "shared" / "s_link.csv")  # a file symlink out of it
    os.symlink(work / "lakes" / "local", work / "shared" / "lake_link")  # into a lake's storage
    os.symlink(work / "out", work / "inbox" / "to_out")  # from a write location to elsewhere
    for sql in [
        f"ATTACH 'ducklake:{work}/lakes/local.ducklake' AS lake (DATA_PATH '{work}/lakes/local/')",
        "CREATE TABLE lake.main.t AS SELECT range + 1000 AS x FROM range(100)",
        "CREATE ROLE located", "CREATE USER lee", "GRANT located TO lee",
        f"GRANT READ FILESYSTEM ON LOCATION '{work}/shared' TO located",
        f"GRANT WRITE FILESYSTEM ON LOCATION '{work}/inbox' TO located",
        "GRANT SELECT ON lake.main.t TO located",
        "CREATE ROLE everywhere", "CREATE USER eve", "GRANT everywhere TO eve",
        "GRANT READ FILESYSTEM TO everywhere",
    ]:
        expect_allowed(client=admin, sql=sql)
    # A symlink to one of the lake's data files, beside the other files.
    data_file = admin.run(sql="SELECT data_file FROM ducklake_list_files('lake', 't')").column(0).to_pylist()[0]
    (work / "beside").mkdir()
    os.symlink(data_file, work / "beside" / "lake_file.parquet")
    (work / "beside" / "mine.csv").write_text("x\n7\n")
    return {"server": server, "admin": admin, "lee": server.user(name="lee"), "eve": server.user(name="eve"),
            "work": work}


def test_a_glob_does_not_follow_a_symlink_out_of_the_location(world):
    lee, work = world["lee"], world["work"]
    # The same glob as the administrator reads the secret file through the symlink.
    everything = world["admin"].run(sql=f"SELECT x FROM read_csv('{work}/shared/**/*.csv')").column("x").to_pylist()
    assert 666 in everything, everything
    assert lee.run(sql=f"SELECT x FROM read_csv('{work}/shared/**/*.csv')").column("x").to_pylist() == [1]
    def listed(client):
        files = client.run(sql=f"SELECT file FROM glob('{work}/shared/**/*.csv')").column("file").to_pylist()
        return {pathlib.Path(path).relative_to(work / "shared").as_posix() for path in files}
    # DuckDB's ** matches a symlink to a file (it doesn't descend into a symlinked directory;
    # naming one, shared/link/*.csv, is the plan check's, below). The administrator lists it;
    # the user lists only what is really inside.
    assert listed(client=world["admin"]) == {"a.csv", "s_link.csv"}, listed(client=world["admin"])
    assert listed(client=lee) == {"a.csv"}, listed(client=lee)
    expect_blocked(client=lee, sql=f"SELECT * FROM glob('{work}/shared/link/*.csv')", why="on a location that covers it")
    expect_blocked(client=lee, sql=f"SELECT * FROM glob('{work}/shared/lake_link/**')", why="DuckLake")


def test_a_file_symlink_out_of_the_location_is_refused(world):
    lee, work = world["lee"], world["work"]
    expect_blocked(client=lee, sql=f"SELECT * FROM read_csv('{work}/shared/s_link.csv')",
                   why="on a location that covers it")
    expect_blocked(client=lee, sql=f"SELECT * FROM read_csv('{work}/shared/link/s.csv')",
                   why="on a location that covers it")
    expect_allowed(client=lee, sql=f"SELECT * FROM read_csv('{work}/shared/a.csv')")


def test_a_write_does_not_follow_a_symlink_out_of_the_location(world):
    lee, work = world["lee"], world["work"]
    expect_allowed(client=lee, sql=f"COPY (SELECT 1 AS i) TO '{work}/inbox/ok.csv'")
    expect_blocked(client=lee, sql=f"COPY (SELECT 1 AS i) TO '{work}/inbox/to_out/escaped.csv'",
                   why="on a location that covers it")
    assert not (work / "out" / "escaped.csv").exists()


def test_ducklake_still_reads_its_own_files_for_a_located_user(world):
    assert world["lee"].run(sql="SELECT count(*) AS n FROM lake.main.t").column("n").to_pylist() == [100]


def test_no_symlink_leads_a_reader_of_every_file_into_a_lake(world):
    # READ FILESYSTEM everywhere still stops at DuckLake's storage: a glob that names no lake
    # path still matches a symlink into one, which is left out (and so finds no Parquet file).
    eve, work = world["eve"], world["work"]
    assert world["admin"].run(sql=f"SELECT count(*) AS n FROM read_parquet('{work}/beside/*.parquet')"
                              ).column("n").to_pylist() == [100]
    with pytest.raises(Exception) as raised:
        eve.run(sql=f"SELECT count(*) AS n FROM read_parquet('{work}/beside/*.parquet')")
    assert "No files found" in str(raised.value), raised.value
    expect_blocked(client=eve, sql=f"SELECT * FROM read_parquet('{work}/beside/lake_file.parquet')", why="DuckLake")
    expect_allowed(client=eve, sql=f"SELECT * FROM read_csv('{work}/beside/*.csv')")
