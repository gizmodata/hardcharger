"""Writes the fixture test_rbac_trigger_names.py opens: a database, and its access-control
store (a DuckDB one), with triggers made by a Hardcharger from before trigger actions were
stored with their names in full (2.13.0). Run it with that server binary, once:

    HARDCHARGER_TEST_BINARY=/path/to/2.13.0/hardcharger-server HARDCHARGER_TEST_RBAC_STORE=duckdb \
        python -m pytest fixtures/make_trigger_names_fixture.py -p no:cacheprovider

(pytest collects it only when named: its file name is no test's). The files it writes,
fixtures/trigger_names_213.duckdb.gz and fixtures/trigger_names_213.rbac.duckdb.gz, are
committed (gzipped: the files are mostly empty blocks).
"""

import gzip
import pathlib

from rbac import RbacServer, expect_allowed, rbac_env
from test_rbac_trigger_names import LEGACY, LEGACY_ACCESS, LEGACY_MIA, LEGACY_MIA_ACCESS, LEGACY_OBJECTS

FIXTURES = pathlib.Path(__file__).resolve().parent


def test_write_the_fixture(launcher, tmp_path):
    database = tmp_path / f"{LEGACY}.duckdb"
    store = tmp_path / f"{LEGACY}.rbac.duckdb"
    running = launcher.start(args=["--database", str(database)], env=rbac_env(store_uri=f"duckdb:{store}"),
                             startup_seconds=60)
    server = RbacServer(running=running, store_uri=f"duckdb:{store}")
    admin = server.admin()
    assert "2.13.0" in admin.run(sql="SELECT hardcharger_version() AS v").column("v").to_pylist()[0]
    for sql in LEGACY_OBJECTS + LEGACY_ACCESS:
        expect_allowed(client=admin, sql=sql)
    mia = server.user(name="mia")
    for sql in LEGACY_MIA + LEGACY_MIA_ACCESS:
        expect_allowed(client=mia, sql=sql)
    stored = admin.run(sql="SELECT sql FROM duckdb_triggers() ORDER BY trigger_name").column("sql").to_pylist()
    assert len(stored) == 11 and not any(f"{LEGACY}." in sql for sql in stored), stored
    expect_allowed(client=admin, sql="CHECKPOINT")
    running.stop()
    for path in (database, store):
        assert not path.with_name(path.name + ".wal").exists(), f"{path.name} was not checkpointed"
        with gzip.GzipFile(filename=str(FIXTURES / (path.name + ".gz")), mode="wb", compresslevel=9, mtime=0) as packed:
            packed.write(path.read_bytes())
