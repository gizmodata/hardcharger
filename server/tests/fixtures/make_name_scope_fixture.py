"""Writes the fixture test_rbac_name_scope.py opens: a database, and its access-control store
(a DuckDB one), made by a Hardcharger from before stored expressions had a name scope (2.13.0).
Run it with that server binary, once:

    HARDCHARGER_TEST_BINARY=/path/to/2.13.0/hardcharger-server HARDCHARGER_TEST_RBAC_STORE=duckdb \
        python -m pytest fixtures/make_name_scope_fixture.py -p no:cacheprovider

(pytest collects it only when named). The files it writes, fixtures/name_scope_213.duckdb.gz and
fixtures/name_scope_213.rbac.duckdb.gz, are committed.
"""

import gzip
import pathlib

from name_scope_legacy import (LEGACY, LEGACY_ACCESS, LEGACY_METHOD_POLICY, LEGACY_MIA, LEGACY_MIA_ACCESS,
                               LEGACY_OBJECTS, LEGACY_POLICIES, LEGACY_PRINCIPALS, LEGACY_SCHEMAS,
                               LEGACY_TRIGGERS_AND_VIEWS, LEGACY_WES)
from rbac import RbacServer, expect_allowed, rbac_env

FIXTURES = pathlib.Path(__file__).resolve().parent


def test_write_the_fixture(launcher, tmp_path):
    database = tmp_path / f"{LEGACY}.duckdb"
    store = tmp_path / f"{LEGACY}.rbac.duckdb"
    running = launcher.start(args=["--database", str(database)], env=rbac_env(store_uri=f"duckdb:{store}"),
                             startup_seconds=60)
    server = RbacServer(running=running, store_uri=f"duckdb:{store}")
    admin = server.admin()
    assert "2.13.0" in admin.run(sql="SELECT hardcharger_version() AS v").column("v").to_pylist()[0]
    for sql in LEGACY_SCHEMAS + LEGACY_PRINCIPALS + LEGACY_OBJECTS:
        expect_allowed(client=admin, sql=sql)
    wes = server.user(name="wes")
    for sql in LEGACY_WES:
        expect_allowed(client=wes, sql=sql)
    for sql in LEGACY_TRIGGERS_AND_VIEWS + LEGACY_POLICIES + LEGACY_METHOD_POLICY + LEGACY_ACCESS:
        expect_allowed(client=admin, sql=sql)
    mia = server.user(name="mia")
    for sql in LEGACY_MIA:
        expect_allowed(client=mia, sql=sql)
    for sql in LEGACY_MIA_ACCESS:
        expect_allowed(client=admin, sql=sql)
    stored = admin.run(sql="SELECT sql FROM duckdb_tables() WHERE table_name IN ('checked', 'mchecked', 'mvault', 'mlog')"
                           " ORDER BY table_name").column("sql").to_pylist()
    assert "CHECK(is_valid(x))" in stored[0] and "x.is_valid()" in stored[1], stored
    assert "stamp()" in stored[2] and "util." not in stored[2] and "fmt(" in stored[3] and "main.fmt" not in stored[3], stored
    expect_allowed(client=admin, sql="CHECKPOINT")
    running.stop()
    for path in (database, store):
        assert not path.with_name(path.name + ".wal").exists(), f"{path.name} was not checkpointed"
        with gzip.GzipFile(filename=str(FIXTURES / (path.name + ".gz")), mode="wb", compresslevel=9, mtime=0) as packed:
            packed.write(path.read_bytes())
