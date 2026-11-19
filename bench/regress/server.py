"""Starting, measuring and stopping one Hardcharger server binary, the same way for every version.

Every version gets the same settings through the HARDCHARGER_* environment variables, which all
releases since 2.0 read: address, admin credentials, database file, memory limit, and the thread
count as init SQL (SET threads). On Linux the server can be pinned to a core list (taskset), so
it never shares cores with the client processes.

Released versions install with the official installer into a prefix of their own:
    sh install.sh --version <v> --prefix <dir>
"""

import hashlib
import os
import pathlib
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

import psutil

from client import Session

INSTALLER_URL = "https://install.hardcharger.gizmodata.com"
USERNAME = "bench"
PASSWORD = "bench-regress-pw"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def install_release(version: str, cache: pathlib.Path) -> pathlib.Path:
    """The hardcharger-server of release `version`, installed once into cache/hc-<version>."""
    prefix = cache / f"hc-{version}"
    binary = prefix / "bin" / "hardcharger-server"
    if binary.exists():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    installer = cache / "install.sh"
    if not installer.exists():
        with urllib.request.urlopen(url=INSTALLER_URL, timeout=60) as response:
            installer.write_bytes(response.read())
    subprocess.run(args=["sh", str(installer), "--version", version, "--prefix", str(prefix)], check=True,
                   stdout=subprocess.DEVNULL)
    if not binary.exists():
        raise RuntimeError(f"the installer left no server at {binary}")
    return binary


def describe_binary(binary: pathlib.Path) -> dict:
    """Version strings and a fingerprint of the binary (what exactly ran)."""
    def output(*args: str) -> str:
        done = subprocess.run(args=[str(binary), *args], capture_output=True, text=True, timeout=60)
        return (done.stdout or done.stderr).strip()
    version = output("--version")
    match = re.search(r"(\d+\.\d+\.\d+\S*)", version)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return {"binary": str(binary), "version": match.group(1) if match else version,
            "version_text": version, "duckdb_version": output("--print-duckdb-version"), "sha256": digest}


def parse_cores(spec: str | None) -> list[int] | None:
    """'0-7' or '0-3,8,10-11' -> core numbers; None or '' -> no pinning."""
    if not spec:
        return None
    cores = []
    for part in spec.split(","):
        low, _, high = part.partition("-")
        cores.extend(range(int(low), int(high or low) + 1))
    return cores


def drop_os_caches() -> bool:
    """Linux only, with passwordless sudo: the page cache emptied (a cold start reads the disk)."""
    if platform.system() != "Linux":
        return False
    subprocess.run(args=["sync"], check=True)
    done = subprocess.run(args=["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                          capture_output=True)
    return done.returncode == 0


class ResourceSampler:
    """The server's CPU time between two marks, and its largest resident set while sampling."""

    def __init__(self, pid: int, interval_s: float = 0.05):
        self.process = psutil.Process(pid=pid)
        self.interval_s = interval_s
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
            except psutil.Error:
                return
            self._stop.wait(timeout=self.interval_s)

    def cpu_seconds(self) -> float:
        times = self.process.cpu_times()
        return times.user + times.system

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


class Server:
    def __init__(self, label: str, binary: pathlib.Path, database: pathlib.Path, log: pathlib.Path,
                 threads: int, memory_limit: str, cores: list[int] | None):
        self.label = label
        self.binary = binary
        self.database = database
        self.log_path = log
        self.threads = threads
        self.memory_limit = memory_limit
        self.cores = cores
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None
        self.sampler: ResourceSampler | None = None
        self.start_seconds = 0.0

    def start(self, timeout_s: float = 600) -> None:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "HARDCHARGER_HOST": "127.0.0.1", "HARDCHARGER_PORT": str(self.port),
               "HARDCHARGER_HEALTH_PORT": "0", "HARDCHARGER_ADMIN_USERNAME": USERNAME,
               "HARDCHARGER_ADMIN_PASSWORD": PASSWORD, "HARDCHARGER_PRINT_QUERIES": "false",
               "HARDCHARGER_DATABASE": str(self.database), "HARDCHARGER_MEMORY_LIMIT": self.memory_limit,
               "HARDCHARGER_INIT_SQL_COMMANDS": f"SET threads = {self.threads}"}
        args = [str(self.binary)]
        if self.cores is not None and shutil.which("taskset"):
            args = ["taskset", "-c", ",".join(str(c) for c in self.cores)] + args
        began = time.monotonic()
        self.process = subprocess.Popen(args=args, env=env, stdout=open(self.log_path, "a"),
                                        stderr=subprocess.STDOUT)
        deadline = began + timeout_s
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.label} exited with {self.process.returncode} at start; see {self.log_path}")
            try:
                Session(base_url=self.base_url, username=USERNAME, password=PASSWORD, timeout_s=10).close()
                break
            except Exception:  # noqa: BLE001 - not up yet (refused, or signing in not open)
                if time.monotonic() > deadline:
                    self.stop()
                    raise RuntimeError(f"{self.label} not ready after {timeout_s:.0f} s; see {self.log_path}")
                time.sleep(0.1)
        self.start_seconds = time.monotonic() - began
        self.sampler = ResourceSampler(pid=self.process.pid)

    def session(self) -> Session:
        return Session(base_url=self.base_url, username=USERNAME, password=PASSWORD)

    def peak_rss_linux(self) -> int | None:
        """VmHWM: the kernel's own peak resident set since the process started."""
        try:
            for line in pathlib.Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
        except OSError:
            return None
        return None

    def stop(self) -> None:
        if self.sampler:
            self.sampler.stop()
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)
