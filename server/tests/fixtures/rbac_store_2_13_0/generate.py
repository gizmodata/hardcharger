"""Writes what a Hardcharger 2.13.0 server made, for the tests that open it with the
current build (test_rbac_system_admin_upgrade.py, test_rbac_system_admin_audit.py):

  rbac.duckdb.gz     a DuckDB-file access-control store (gzip: the file is mostly empty blocks)
  postgres.sql       a PostgreSQL store, as pg_dump --inserts wrote its hardcharger_rbac schema
  audit-chain.jsonl  the access-control audit chain of a 2.13.0 server (log_format json),
                     sealed with the audit tests' key

Run once, with a 2.13.0 server binary and the tests' PostgreSQL (rbac.py):

  python fixtures/rbac_store_2_13_0/generate.py /path/to/2.13.0/hardcharger-server

The stores hold what the statements below made and nothing else: no cluster id, and one
password, the one written here.
"""

import gzip
import pathlib
import subprocess
import sys
import tempfile
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from conftest import Launcher  # noqa: E402
from rbac import ChargewireError, Store, expect_allowed, start_rbac_server  # noqa: E402

ANN_PASSWORD = "ann-2-13-password"

# As the administrator. Names a later version gives a meaning to are here on purpose: a
# role called sysadmin, and one called "SYSTEM ADMIN".
ADMINISTRATOR = [
    f"CREATE USER ann WITH PASSWORD '{ANN_PASSWORD}'",
    "CREATE USER bob", "CREATE USER cleo", "CREATE USER dora",
    "CREATE ROLE analysts", "CREATE ROLE sysadmin", 'CREATE ROLE "SYSTEM ADMIN"',
    "GRANT SELECT ON SCHEMA main TO analysts",
    "GRANT analysts TO ann",
    "GRANT sysadmin TO bob WITH ADMIN OPTION",
    'GRANT "SYSTEM ADMIN" TO cleo',
    "GRANT READ FILESYSTEM TO sysadmin",
    "CREATE GROUP MAPPING 'ops' TO ROLE sysadmin",
    "CREATE TENANT acme", "CREATE USER acme/amy",
]
# As bob, who holds sysadmin WITH ADMIN OPTION: a membership an administrator did not grant.
BOB = ["GRANT sysadmin TO dora"]


def fill(server) -> None:
    admin = server.admin()
    for sql in ADMINISTRATOR:
        expect_allowed(client=admin, sql=sql)
    bob = server.user(name="bob")
    for sql in BOB:
        expect_allowed(client=bob, sql=sql)
    version = admin.run(sql="SELECT HARDCHARGER_VERSION() AS v").column("v").to_pylist()[0]
    assert version == "2.13.0", f"this is Hardcharger {version}, not 2.13.0"
    server.running.stop()


def main() -> None:
    binary = pathlib.Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        launcher = Launcher(tmp_path=pathlib.Path(tmp))
        # The server opens its own DuckDB store only 0600, in a directory of its own.
        directory = pathlib.Path(tmp) / "store"
        directory.mkdir(mode=0o700)
        file = directory / "rbac.duckdb"
        fill(server=start_rbac_server(launcher=_with(launcher, binary), store_uri=f"duckdb:{file}"))
        with gzip.GzipFile(filename=HERE / "rbac.duckdb.gz", mode="wb", mtime=0) as packed:
            packed.write(file.read_bytes())
        audit_log(launcher=_with(launcher, binary), directory=pathlib.Path(tmp) / "audited")

        store = Store(kind="postgres")
        try:
            uri = store.new_database()
            fill(server=start_rbac_server(launcher=_with(launcher, binary), store_uri=uri))
            parts = urllib.parse.urlsplit(uri)
            dump = subprocess.run(
                args=["docker", "exec", "-e", f"PGPASSWORD={parts.password}", "hardcharger-test-pg", "pg_dump",
                      "--inserts", "--no-owner", "--no-privileges", "--schema=hardcharger_rbac",
                      "-U", parts.username, parts.path.lstrip("/")],
                capture_output=True, text=True, check=True).stdout
            # Statements only: psql's own commands (\restrict, \connect) and the session
            # settings a restore sets are not part of the store.
            kept = [line for line in dump.splitlines()
                    if line.strip() and not line.startswith(("--", "\\", "SET ", "SELECT pg_catalog.set_config"))]
            (HERE / "postgres.sql").write_text("\n".join(kept) + "\n")
        finally:
            store.drop_all()
            launcher.stop_all()


def audit_log(launcher, directory: pathlib.Path) -> None:
    """Allowed, denied, refused and run statements, as test_rbac_audit_chain.py writes them."""
    directory.mkdir(mode=0o700)
    server = start_rbac_server(launcher=launcher, store_uri=f"duckdb:{directory / 'rbac.duckdb'}",
                               env={"HARDCHARGER_LOG_FORMAT": "json",
                                    "HARDCHARGER_AUDIT_CHAIN_KEY": "audit-chain-test-key",
                                    "HARDCHARGER_PRINT_QUERIES": "false"})
    admin = server.admin()
    for sql in ["CREATE TABLE ledger AS SELECT 1 AS id", "CREATE TABLE vault AS SELECT 1 AS id",
                "CREATE ROLE clerks", "GRANT SELECT ON ledger TO clerks", "CREATE USER cleo", "GRANT clerks TO cleo"]:
        expect_allowed(client=admin, sql=sql)
    cleo = server.user(name="cleo")
    expect_allowed(client=cleo, sql="SELECT * FROM ledger")
    for sql in ["SELECT * FROM vault", "GRANT SELECT ON vault TO clerks"]:
        try:
            cleo.run(sql=sql)
        except ChargewireError:
            pass
    chained = [line for line in server.log().splitlines() if '"audit_hash"' in line]
    assert len(chained) >= 6, chained
    (HERE / "audit-chain.jsonl").write_text("\n".join(chained) + "\n")
    server.running.stop()


def _with(launcher: Launcher, binary: pathlib.Path):
    class Old:
        def start(self, **kwargs):
            return launcher.start(binary=binary, **kwargs)
    return Old()


if __name__ == "__main__":
    main()
