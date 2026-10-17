"""The DuckDB version the server is linked with, read independently of the server's own
--print-duckdb-version: from a running server's version() SQL function."""

import functools

from conftest import Launcher


@functools.cache
def linked_version() -> str:
    import pathlib
    import tempfile
    launcher = Launcher(tmp_path=pathlib.Path(tempfile.mkdtemp()), signer_factory=lambda: None)
    try:
        server = launcher.start()
        return server.admin().scalar(sql="SELECT version()")
    finally:
        launcher.stop_all()
