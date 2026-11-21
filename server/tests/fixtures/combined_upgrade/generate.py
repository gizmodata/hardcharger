"""Generate the forward-upgrade fixtures with a released binary and the tests' PostgreSQL.

Usage: python generate.py VERSION /path/to/released/hardcharger-server
Both services are enabled together. The old binary writes all schemas, grants, sessions
and query history. Strip host information before packaging the two DuckDB files and the
PostgreSQL schemas. The tests restore these fixtures without fetching a release binary.
"""
import gzip
import json
import pathlib
import subprocess
import sys
import tempfile
import urllib.parse

import psycopg

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from conftest import Launcher
from rbac import RbacServer, Store, rbac_env

PASSWORD = "reader-upgrade-fixture"
PROBE = "SELECT 17 AS released_probe"
FIXTURE_HOST = ("SET hostname = 'fixture', database_path = NULL, os_name = NULL, os_version = NULL, "
                "cpu_model = NULL, memory_total_bytes = NULL")


def seed(launcher, binary, version, uri, args):
    running = launcher.start(binary=binary, env=rbac_env(store_uri=uri), args=args)
    server = RbacServer(running=running, store_uri=uri)
    admin = server.admin()
    assert admin.run(sql="SELECT HARDCHARGER_VERSION()").column(0)[0].as_py() == version
    for sql in [f"CREATE USER reader WITH PASSWORD '{PASSWORD}'", "CREATE ROLE analysts",
                "GRANT SELECT ON SCHEMA main TO analysts", "GRANT analysts TO reader"]:
        admin.run(sql=sql)
    reader = server.user(name="reader", password=PASSWORD)
    assert reader.run(sql=PROBE).column(0)[0].as_py() == 17
    reader.close()
    admin.close()
    running.stop()
    return reader.session_id


def pack(path, data):
    path.write_bytes(gzip.compress(data=data, mtime=0))


def generate(version, binary, destination, tmp):
    launcher = Launcher(tmp_path=tmp)
    store = Store(kind="postgres")
    sessions = {}
    try:
        directory = tmp / "private"
        directory.mkdir(mode=0o700)
        files = {"rbac": directory / "rbac.db", "history": tmp / "history.db"}
        sessions["duckdb"] = seed(launcher, binary, version, f"duckdb:{files['rbac']}",
                                  ["--enable-instrumentation", "--instrumentation-db-path", str(files["history"])])
        # Copy live rows into clean files so neither deleted data nor the build host's
        # name can be retained in old database pages.
        plain = launcher.start(binary=binary)
        admin = plain.admin()
        for name, source in files.items():
            cleaned = tmp / f"packed-{name}.db"
            admin.execute(sql=f"ATTACH '{source}' AS src")
            if name == "history":
                admin.execute(sql=f"UPDATE src.main.instances {FIXTURE_HOST}")
            admin.execute(sql=f"ATTACH '{cleaned}' AS dst")
            admin.execute(sql="COPY FROM DATABASE src TO dst")
            admin.execute(sql="DETACH dst")
            admin.execute(sql="DETACH src")
            pack(destination / f"{name}.duckdb.gz", cleaned.read_bytes())
        plain.stop()

        uri = store.new_database()
        sessions["postgres"] = seed(launcher, binary, version, uri, [
            "--enable-instrumentation", "--init-sql-commands", f"ATTACH '{uri}' AS history (TYPE postgres)",
            "--instrumentation-catalog", "history", "--instrumentation-schema", "instr"])
        with psycopg.connect(conninfo=uri) as conn:
            conn.execute(query=f"UPDATE instr.instances {FIXTURE_HOST}")
        parts = urllib.parse.urlsplit(uri)
        dump = subprocess.run(args=["docker", "exec", "-e", f"PGPASSWORD={parts.password}",
                                    "hardcharger-test-pg", "pg_dump", "--column-inserts", "--no-owner",
                                    "--no-privileges", "--schema=hardcharger_rbac", "--schema=instr",
                                    "-U", parts.username, parts.path.lstrip("/")],
                              capture_output=True, text=True, check=True).stdout
        sql = "\n".join(line for line in dump.splitlines() if line.strip() and
                        not line.startswith(("--", "\\", "SET ", "SELECT pg_catalog.set_config")))
        pack(destination / "postgres.sql.gz", (sql + "\n").encode())
        (destination / "sessions.json").write_text(json.dumps(sessions, indent=2) + "\n")
    finally:
        launcher.stop_all()
        store.drop_all()


if __name__ == "__main__":
    version, binary = sys.argv[1:]
    destination = HERE / version
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        generate(version, pathlib.Path(binary).resolve(), destination, pathlib.Path(tmp))
