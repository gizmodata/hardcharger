"""hardcharger: run Hardcharger, GizmoData's DuckDB 2.0 server, from Python.

``pip install hardcharger`` gets the server. Its ``hardcharger-server`` binary is
downloaded for this platform on first use and cached; :class:`Server` runs it as a
subprocess::

    import hardcharger

    with hardcharger.Server() as server:
        print(server.uri)  # hardcharger://127.0.0.1:<port>?tls=false

``pip install "hardcharger[adbc]"`` adds the client, ``adbc-driver-hardcharger``, so
:meth:`Server.connect` returns a connection::

    with hardcharger.Server() as server, server.connect() as conn, conn.cursor() as cur:
        cur.execute(operation="SELECT 42 AS answer")
        print(cur.fetch_arrow_table())
"""

from hardcharger._install import SERVER_VERSION, InstallError, ensure_server
from hardcharger.server import Server, ServerError

__version__ = "2.3.0"

__all__ = [
    "SERVER_VERSION",
    "InstallError",
    "Server",
    "ServerError",
    "__version__",
    "ensure_server",
]
