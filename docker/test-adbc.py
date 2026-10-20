"""Run in a temporary test layer: load all 15 drivers and query the local server by manifest."""
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
for name in DRIVERS:
    manifest = tomllib.loads(Path(f"/etc/adbc/drivers/{name}.toml").read_text())
    driver = manifest["Driver"]
    path = driver["shared"][f"linux_{arch}"]
    library = ctypes.CDLL(name=path)
    getattr(library, driver.get("entrypoint", "AdbcDriverInit"))
    print(f"Loaded {name}: {manifest['version']}", flush=True)

with dbapi.connect(driver="hardcharger", db_kwargs={
    "uri": "hardcharger://127.0.0.1:31337?tls=false",
    "username": "hardcharger", "password": "smoke",
}) as connection:
    with connection.cursor() as cursor:
        cursor.execute(operation="SELECT 42 AS answer")
        assert cursor.fetchone() == (42,)
print("Hardcharger manifest discovery and SELECT 42 passed")
