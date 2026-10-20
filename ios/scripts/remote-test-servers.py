#!/usr/bin/env python3
"""The remote end of the app's remote-server tests (ios/scripts/test-remote.sh): a desktop
hardcharger-server on 127.0.0.1 (the simulator shares the Mac's loopback) that takes the
password grant and browser sign-in through server/tests' mock OpenID Connect provider,
which approves at once.

    remote-test-servers.py <hardcharger-server> <port file>

Writes "<port> <password>" to the port file once the server serves, then runs until
stopped (SIGTERM/SIGINT). The password is made up for this run.
"""
import os
import pathlib
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server" / "tests"))
from mock_idp import MockIdP  # noqa: E402

AUDIENCE = "hardcharger-ios-test"
CLIENT_ID = "ios-test-client"
CLIENT_SECRET = secrets.token_hex(16)


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    binary, port_file = sys.argv[1], pathlib.Path(sys.argv[2])
    idp = MockIdP(audience=AUDIENCE, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    port = free_port()
    password = secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")
    work = pathlib.Path(tempfile.mkdtemp(prefix="hardcharger-ios-remote-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARDCHARGER_")}
    env.update(HARDCHARGER_ADMIN_PASSWORD=password, HARDCHARGER_OAUTH_CLIENT_SECRET=CLIENT_SECRET, HOME=str(work))
    server = subprocess.Popen(
        [binary, "--host=127.0.0.1", f"--port={port}", f"--database={work / 'remote.duckdb'}", "--web-ui=true",
         f"--log-file={work / 'server.log'}", f"--token-allowed-issuer={idp.issuer}",
         f"--token-allowed-audience={AUDIENCE}", "--token-default-role=admin", "--token-authorized-emails=*@gizmodata.com",
         f"--oauth-client-id={CLIENT_ID}", f"--oauth-base-url=http://127.0.0.1:{port}",
         "--oauth-provider-name=Mock IdP",
         "--init-sql-commands=CREATE TABLE IF NOT EXISTS remote_people AS SELECT 1 AS id, 'Grace' AS name;"],
        env=env)
    deadline = time.time() + 60
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/warp/v1/health", timeout=2)
            break
        except Exception:
            if server.poll() is not None or time.time() > deadline:
                print((work / "server.log").read_text(errors="replace"), file=sys.stderr)
                return 1
            time.sleep(0.2)
    port_file.write_text(f"{port} {password}\n")
    print(f"remote server on 127.0.0.1:{port} (log {work / 'server.log'})", flush=True)
    stop = lambda *_: (server.terminate(), idp.stop(), sys.exit(0))  # noqa: E731
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait()
    idp.stop()
    return 1


if __name__ == "__main__":
    sys.exit(main())
