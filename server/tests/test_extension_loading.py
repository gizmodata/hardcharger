"""Native extension automation, offline caches and customer repository replacement."""

import functools
import gzip
import http.server
import threading

import pytest

from conftest import TSAN_EXTENSIONS

# These two tests specifically replace/download signed public modules. They run on
# all native release targets and ASan; TSan's instrumented built-ins cannot be replaced.
pytestmark = pytest.mark.skipif(TSAN_EXTENSIONS, reason="runtime replacement is covered by native and ASan jobs")


def test_known_extensions_install_and_load_on_demand_then_work_offline(launcher, tmp_path):
    cache = tmp_path / "extensions"
    running = launcher.start(args=["--init-sql-commands", f"SET extension_directory = '{cache}';"])
    session = running.admin()
    for setting in ["autoinstall_known_extensions", "autoload_known_extensions"]:
        assert session.scalar(sql=f"SELECT current_setting('{setting}')") is True
    assert session.scalar(sql="SELECT current_setting('allow_unsigned_extensions')") is False
    assert session.scalar(sql="SELECT loaded FROM duckdb_extensions() WHERE extension_name = 'tpch'") is False
    session.execute(sql="CALL dbgen(sf = 0.001)")  # no INSTALL or LOAD
    assert session.scalar(sql="SELECT count(*) FROM nation") == 25
    assert session.scalar(sql="SELECT install_mode FROM duckdb_extensions() WHERE extension_name = 'tpch'") \
        != "STATICALLY_LINKED"
    running.stop()

    init = (f"SET extension_directory = '{cache}'; "
            "SET autoinstall_known_extensions = false; "
            "SET custom_extension_repository = 'http://127.0.0.1:9/offline'; "
            "SET autoinstall_extension_repository = 'http://127.0.0.1:9/offline';")
    offline = launcher.start(args=["--init-sql-commands", init])
    session = offline.admin()
    session.execute(sql="CALL dbgen(sf = 0.001)")  # autoload the signed installed module without a repository
    assert session.scalar(sql="SELECT count(*) FROM nation") == 25


def test_signed_optional_module_can_be_force_installed_from_a_customer_repository(launcher, tmp_path):
    cache = tmp_path / "first-cache"
    running = launcher.start(args=["--init-sql-commands", f"SET extension_directory = '{cache}'; INSTALL tpch;"])
    session = running.admin()
    version = session.scalar(sql="SELECT library_version FROM pragma_version()")
    platform = session.scalar(sql="PRAGMA platform")
    signed = cache / version / platform / "tpch.duckdb_extension"
    assert signed.is_file()
    running.stop()

    repository = tmp_path / "repository"
    asset = repository / version / platform / "tpch.duckdb_extension.gz"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(gzip.compress(signed.read_bytes()))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repository))
    http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{http_server.server_port}"
        target = launcher.start(args=["--init-sql-commands", f"SET extension_directory = '{tmp_path / 'target-cache'}';"])
        client = target.admin()
        client.execute(sql=f"FORCE INSTALL tpch FROM '{url}'; LOAD tpch;")
        client.execute(sql="CALL dbgen(sf = 0.001)")
        assert client.scalar(sql="SELECT count(*) FROM nation") == 25
        info = client.rows(sql="SELECT install_mode, installed_from FROM duckdb_extensions() "
                               "WHERE extension_name = 'tpch'")[0]
        assert info["install_mode"] != "STATICALLY_LINKED"
        assert url in info["installed_from"]
        assert client.scalar(sql="SELECT current_setting('allow_unsigned_extensions')") is False
    finally:
        http_server.shutdown()
        thread.join(timeout=5)
        http_server.server_close()
