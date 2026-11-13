#!/usr/bin/env python3
"""Local remote-connection fixture, reachable through adb reverse only.
Run with uv --with pyjwt --with cryptography. Never uses real credentials.
Writes a JSON fixture for the instrumentation tests, including throwaway credentials.
"""
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server' / 'tests'))
from mock_idp import MockIdP


def main():
    binary = sys.argv[1]
    output = Path(sys.argv[2])
    sdk = Path(os.environ.get('ANDROID_HOME', str(Path.home() / 'Library/Android/sdk')))
    adb = sdk / 'platform-tools/adb'
    serials = sys.argv[3:] or ['emulator-5554']
    password = secrets.token_urlsafe(24)
    secret = secrets.token_urlsafe(24)
    idp = MockIdP(audience='android-test', client_id='android-test', client_secret=secret)
    with socket.socket() as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    directory = Path(tempfile.mkdtemp(prefix='hardcharger-android-remote-'))
    env = {k: v for k, v in os.environ.items() if not k.startswith('HARDCHARGER_')}
    env.update(HARDCHARGER_ADMIN_PASSWORD=password, HARDCHARGER_OAUTH_CLIENT_SECRET=secret)
    log = (directory / 'process.log').open('w')
    server = subprocess.Popen(args=[binary, '--host=127.0.0.1', f'--port={port}',
        f'--database={directory / "remote.duckdb"}', '--web-ui=true',
        f'--token-allowed-issuer={idp.issuer}', '--token-allowed-audience=android-test',
        '--token-default-role=admin', '--token-authorized-emails=*@gizmodata.com',
        '--oauth-client-id=android-test', f'--oauth-base-url=http://127.0.0.1:{port}',
        '--oauth-provider-name=Android test identity provider'], env=env, stdout=log, stderr=log)
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError(f'Remote fixture failed. Inspect {directory / "process.log"}')
            try:
                try:
                    urllib.request.urlopen(url=f'http://127.0.0.1:{port}/chargewire/v1/health', timeout=1)
                except urllib.error.HTTPError:
                    urllib.request.urlopen(url=f'http://127.0.0.1:{port}/warp/v1/health', timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError('Remote fixture did not become ready')
        info = {'origin': f'http://127.0.0.1:{port}', 'username': 'hardcharger', 'password': password}
        output.write_text(data=json.dumps(obj=info))
        output.chmod(mode=0o600)
        for serial in serials:
            for value in [port, idp.server.server_address[1]]:
                subprocess.run(args=[str(adb), '-s', serial, 'reverse', f'tcp:{value}', f'tcp:{value}'], check=True)
            subprocess.run(args=[str(adb), '-s', serial, 'push', str(output), '/data/local/tmp/hardcharger-remote.json'], check=True, stdout=subprocess.DEVNULL)
        print('Local remote fixture ready (credentials are in the private fixture file).', flush=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        server.wait()
    finally:
        server.terminate()
        server.wait(timeout=10)
        idp.stop()
        log.close()


if __name__ == '__main__':
    main()
