""":class:`Server`: ``hardcharger-server`` as a managed subprocess.

::

    import hardcharger

    with hardcharger.Server() as server:
        print(server.uri)  # hardcharger://127.0.0.1:<port>?tls=false
        with server.connect() as conn, conn.cursor() as cur:  # pip install "hardcharger[adbc]"
            cur.execute(operation="SELECT HARDCHARGER_VERSION()")
            print(cur.fetchone())
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from hardcharger._install import InstallError, ensure_server


class ServerError(RuntimeError):
    """The server didn't start, or exited while starting."""


def _free_port(exclude: Iterable[int] = ()) -> int:
    excluded = set(exclude)
    for _ in range(16):
        with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in excluded:
            return port
    raise ServerError("couldn't find a free TCP port")


def _signal(process: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the server's process group (its own session), or the process on Windows."""
    with contextlib.suppress(ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


class Server:
    """Run ``hardcharger-server`` as a subprocess.

    The binary is downloaded on first use (see :func:`hardcharger.ensure_server`) unless
    ``binary`` or ``HARDCHARGER_SERVER_BINARY`` names one.

    Args:
        database: DuckDB database file, or ``":memory:"`` (the default).
        host: Address to listen on (default loopback only).
        port: Chargewire port; ``None`` picks a free one.
        health_port: Port answering ``/health``; ``None`` picks a free one.
        username: The administrator's user name (default ``hardcharger``).
        password: The administrator's password; ``None`` generates a random one
            (``server.password``). Credentials reach the server through its
            environment, so they don't show in ``ps``.
        tls_cert, tls_key: PEM certificate chain and key; both turn TLS on.
        version: Server version to run (default: ``HARDCHARGER_VERSION``, then the
            version this package pins).
        binary: A ``hardcharger-server`` binary to run instead of a downloaded one.
        extra_args: More command-line options, passed as they are.
        env: More environment variables for the server (``HARDCHARGER_*`` options).
        log_file: Where the server's output goes; ``None`` writes a temporary file,
            removed by :meth:`stop` (``server.log_path`` while running).
        startup_timeout: Seconds to wait for the server to answer.
    """

    def __init__(
        self,
        *,
        database: str | os.PathLike[str] = ":memory:",
        host: str = "127.0.0.1",
        port: int | None = None,
        health_port: int | None = None,
        username: str = "hardcharger",
        password: str | None = None,
        tls_cert: str | os.PathLike[str] | None = None,
        tls_key: str | os.PathLike[str] | None = None,
        version: str | None = None,
        binary: str | os.PathLike[str] | None = None,
        extra_args: Iterable[str] = (),
        env: Mapping[str, str] | None = None,
        log_file: str | os.PathLike[str] | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        if (tls_cert is None) != (tls_key is None):
            raise ValueError("tls_cert and tls_key go together: pass both or neither")
        self._database = os.fspath(database)
        self._host = host
        self._port = port if port is not None else _free_port()
        self._health_port = (
            health_port if health_port is not None else _free_port(exclude=[self._port])
        )
        self._username = username
        self._password = password if password is not None else secrets.token_urlsafe(nbytes=24)
        self._tls_cert = None if tls_cert is None else os.fspath(tls_cert)
        self._tls_key = None if tls_key is None else os.fspath(tls_key)
        self._version = version
        self._binary = None if binary is None else Path(binary).expanduser()
        self._extra_args = list(extra_args)
        self._env = dict(env or {})
        self._log_file = None if log_file is None else Path(log_file)
        self._log_path: Path | None = None
        self._own_log = False
        self._startup_timeout = startup_timeout
        self._process: subprocess.Popen[bytes] | None = None

    # ---- properties -----------------------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def health_port(self) -> int:
        return self._health_port

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    @property
    def tls(self) -> bool:
        return self._tls_cert is not None

    @property
    def uri(self) -> str:
        """``hardcharger://host:port``, with ``?tls=false`` unless the server serves TLS."""
        host = f"[{self._host}]" if ":" in self._host else self._host
        uri = f"hardcharger://{host}:{self._port}"
        return uri if self.tls else f"{uri}?tls=false"

    @property
    def log_path(self) -> Path | None:
        """The file the server's output goes to (while it runs)."""
        return self._log_path

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ---- lifecycle --------------------------------------------------------------------

    def start(self) -> Server:
        """Start the server and wait until it answers; a no-op when it already runs."""
        if self.is_running():
            return self
        if self._binary is not None:
            if not self._binary.is_file():
                raise ServerError(f"binary {self._binary} doesn't exist")
            binary = self._binary
        else:
            try:
                binary = ensure_server(version=self._version)
            except InstallError as error:
                raise ServerError(str(error)) from error

        args = [
            str(binary),
            "--host",
            self._host,
            "--port",
            str(self._port),
            "--health-port",
            str(self._health_port),
            "--database",
            self._database,
        ]
        if self._tls_cert is not None:
            args += ["--tls-cert", self._tls_cert, "--tls-key", str(self._tls_key)]
        args += self._extra_args
        env = {
            **os.environ,
            **self._env,
            "HARDCHARGER_ADMIN_USERNAME": self._username,
            "HARDCHARGER_ADMIN_PASSWORD": self._password,
        }

        if self._log_file is not None:
            self._log_path = self._log_file
            self._own_log = False
        else:
            handle, name = tempfile.mkstemp(prefix="hardcharger-server-", suffix=".log")
            os.close(handle)
            self._log_path = Path(name)
            self._own_log = True
        with self._log_path.open(mode="ab") as log:
            self._process = subprocess.Popen(
                args=args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            self._wait_until_ready()
        except BaseException:
            self.stop(keep_log=True)
            raise
        return self

    def stop(self, *, timeout: float = 20.0, keep_log: bool = False) -> None:
        """Stop the server (SIGTERM, then SIGKILL after ``timeout`` seconds)."""
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            _signal(process=process, sig=signal.SIGTERM)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal(process=process, sig=getattr(signal, "SIGKILL", signal.SIGTERM))
                process.wait(timeout=10)
        if self._own_log and not keep_log and self._log_path is not None:
            with contextlib.suppress(OSError):
                self._log_path.unlink()
            self._log_path = None

    def connect(self, **kwargs: Any) -> Any:
        """A DB-API connection to this server, from ``adbc-driver-hardcharger``
        (``pip install "hardcharger[adbc]"``). ``kwargs`` go to its ``connect()``."""
        try:
            from adbc_driver_hardcharger import dbapi
        except ImportError as error:
            raise ImportError(
                'Server.connect() needs the ADBC driver: pip install "hardcharger[adbc]"'
            ) from error
        if self.tls:
            kwargs.setdefault("tls_root_certs", self._tls_cert)
        return dbapi.connect(
            uri=self.uri, username=self._username, password=self._password, **kwargs
        )

    def __enter__(self) -> Server:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.stop(timeout=5.0)

    # ---- internals ------------------------------------------------------------------

    def _log_tail(self, head: int = 10, tail: int = 30) -> str:
        """The server's output, its first and last lines when it is long (an error is
        often first, followed by the usage text)."""
        if self._log_path is None:
            return ""
        try:
            lines = self._log_path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        if len(lines) > head + tail:
            lines = [*lines[:head], f"... ({len(lines) - head - tail} lines) ...", *lines[-tail:]]
        return "\n".join(lines)

    def _wait_until_ready(self) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        scheme = "https" if self.tls else "http"
        host = f"[{self._host}]" if ":" in self._host else self._host
        probe_host = "127.0.0.1" if self._host in ("0.0.0.0", "") else host
        url = f"{scheme}://{probe_host}:{self._port}/chargewire"
        deadline = time.monotonic() + self._startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            assert self._process is not None
            if self._process.poll() is not None:
                raise ServerError(
                    f"hardcharger-server exited while starting (exit code "
                    f"{self._process.returncode}). Its output:\n{self._log_tail()}"
                )
            try:
                with urllib.request.urlopen(url, timeout=2, context=context if self.tls else None):
                    return
            except urllib.error.HTTPError:
                # Any HTTP answer means it is serving: a server released before the protocol's
                # rename answers 404 here.
                return
            except (urllib.error.URLError, OSError) as error:
                last_error = error
            time.sleep(0.1)
        raise ServerError(
            f"hardcharger-server didn't answer at {url} within {self._startup_timeout:g} s "
            f"({last_error}). Its output:\n{self._log_tail()}"
        )
