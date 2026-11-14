"""Run in a temporary test layer: load all 15 drivers, then use the five staged from
GizmoData releases through the driver manager, each by its manifest.

Hardcharger and Quack run queries: the local server, and a Quack server that same server
starts (`INSTALL quack` downloads DuckDB's extension, so the container needs the network).
Each reports the driver version its manifest names. GizmoSQL, Oracle and Db2 have no server
here: each must load through its manifest's entrypoint and fail with its own connection error.
"""
import ctypes
import platform
from pathlib import Path
import socket
import time
import tomllib

from adbc_driver_manager import dbapi

for attempt in range(60):
    try:
        with socket.create_connection(address=("127.0.0.1", 31337), timeout=1):
            break
    except OSError:
        time.sleep(1)
else:
    raise RuntimeError("Hardcharger did not start")

DRIVERS = (
    "bigquery", "databricks", "exasol", "flightsql", "mssql", "mysql",
    "postgresql", "redshift", "snowflake", "trino", "gizmosql", "oracle",
    "db2", "quack", "hardcharger",
)
arch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
versions = {}
for name in DRIVERS:
    manifest = tomllib.loads(Path(f"/etc/adbc/drivers/{name}.toml").read_text())
    driver = manifest["Driver"]
    path = driver["shared"][f"linux_{arch}"]
    library = ctypes.CDLL(name=path)
    getattr(library, driver.get("entrypoint", "AdbcDriverInit"))
    versions[name] = manifest["version"]
    print(f"Loaded {name}: {manifest['version']}", flush=True)


def check_version(*, name, connection, vendor):
    """The library the manifest points at must be the release the manifest names."""
    info = connection.adbc_get_info()
    assert info["driver_version"].removeprefix("v") == versions[name], (name, info)
    assert info["vendor_name"] == vendor, (name, info)
    print(f"{name} reports driver {info['driver_version']}, server {vendor} {info['vendor_version']}", flush=True)


QUACK_TOKEN = "smoke-quack-token"
with dbapi.connect(driver="hardcharger", autocommit=True, db_kwargs={
    "uri": "hardcharger://127.0.0.1:31337?tls=false",
    "username": "hardcharger", "password": "smoke",
}) as connection:
    check_version(name="hardcharger", connection=connection, vendor="Hardcharger")
    with connection.cursor() as cursor:
        cursor.execute(operation="SELECT 42 AS answer")
        assert cursor.fetchone() == (42,)
        # Hardcharger is a DuckDB 2.0, so it can serve Quack for the Quack driver's check.
        for statement in ("INSTALL quack", "LOAD quack",
                          f"CALL quack_serve('quack:localhost:9494', token => '{QUACK_TOKEN}')"):
            cursor.execute(operation=statement)
            cursor.fetchall()
print("Hardcharger manifest discovery and SELECT 42 passed", flush=True)

with dbapi.connect(driver="quack", db_kwargs={
    "uri": "quack://localhost:9494", "adbc.quack.token": QUACK_TOKEN,
}) as connection:
    check_version(name="quack", connection=connection, vendor="DuckDB (via Quack)")
    with connection.cursor() as cursor:
        cursor.execute(operation="SELECT 1 AS one")
        assert cursor.fetchone() == (1,)
print("Quack manifest discovery and SELECT 1 passed", flush=True)

# No server for these: nothing listens on port 1, so each driver must load through the
# driver manager (a manifest naming a missing entrypoint fails there) and refuse in its
# own words. Their versions are not read back; that needs a connection.
REFUSALS = {
    "gizmosql": ("grpc://127.0.0.1:1", "[GizmoSQL] [FlightSQL] connection error"),
    "oracle": ("oracle://127.0.0.1:1/FREEPDB1", "oracle: cannot connect to 127.0.0.1:1"),
    "db2": ("db2://127.0.0.1:1/SAMPLE", "drda: connect 127.0.0.1:1"),
}
for name, (uri, refusal) in REFUSALS.items():
    try:
        with dbapi.connect(driver=name, db_kwargs={"uri": uri, "username": "smoke", "password": "smoke"}):
            raise RuntimeError(f"{name} connected to a port nothing listens on")
    except dbapi.Error as error:
        assert refusal in str(error) and "127.0.0.1:1" in str(error), (name, str(error))
        print(f"{name} loaded by manifest and refused as expected: {error}", flush=True)
