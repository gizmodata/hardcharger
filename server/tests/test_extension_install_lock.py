"""conftest.installing: one process (or thread) at a time installs an extension into the
extension cache every test server shares.

Without it, two servers that INSTALL the same extension at the same moment each rename their
download into place, and on Windows the second rename fails once the first server has loaded
the file ("Could not move file ... Access is denied"); test_geoarrow.py and
test_client_cli_geometry.py did that on Windows CI. The lock is a file lock, so a second
holder waits whether it is another thread here or another pytest-xdist worker.

The second test is the race itself, made to happen: two servers started at the same moment,
both installing spatial into one extension directory that has nothing in it yet. Needs the
network to INSTALL spatial (DuckDB's extension repository), as test_geoarrow.py does; without
it this fails, it doesn't skip.
"""

import threading
import uuid

import pytest

from conftest import TIMEOUT_SCALE, installing


def test_a_second_installer_waits_for_the_first():
    name = f"lock-test-{uuid.uuid4().hex}"  # this test's own lock, whatever else runs
    events: list[str] = []
    first_in, let_first_go, second_tried = threading.Event(), threading.Event(), threading.Event()

    def first():
        with installing(extension=name):
            events.append("first in")
            first_in.set()
            let_first_go.wait(timeout=60 * TIMEOUT_SCALE)
            events.append("first out")

    def second():
        second_tried.set()
        with installing(extension=name):
            events.append("second in")

    # The one that waited says so (a warning, which a run's summary lists).
    with pytest.warns(UserWarning, match=f"waited .* installing the {name} extension"):
        holder = threading.Thread(target=first)
        holder.start()
        assert first_in.wait(timeout=30 * TIMEOUT_SCALE)
        waiter = threading.Thread(target=second)
        waiter.start()
        assert second_tried.wait(timeout=30 * TIMEOUT_SCALE)
        # The forcing step: the second one is at the lock while the first still holds it, and
        # stays there.
        waiter.join(timeout=1)
        assert waiter.is_alive() and events == ["first in"], events
        let_first_go.set()
        holder.join(timeout=30 * TIMEOUT_SCALE)
        waiter.join(timeout=30 * TIMEOUT_SCALE)
    assert not holder.is_alive() and not waiter.is_alive()
    assert events == ["first in", "first out", "second in"]


def test_two_servers_install_the_same_extension_into_a_cold_cache_at_once(launcher, tmp_path):
    cache = tmp_path / "cold-extension-cache"  # nothing installed here: both servers must install
    init = f"SET extension_directory = '{cache.as_posix()}'; INSTALL spatial; LOAD spatial;"
    servers: dict[str, object] = {}
    failures: dict[str, BaseException] = {}
    together = threading.Barrier(parties=2)

    def start(name: str) -> None:
        try:
            together.wait(timeout=30 * TIMEOUT_SCALE)  # both reach the lock at the same moment
            with installing(extension="spatial"):
                servers[name] = launcher.start(args=["--init-sql-commands", init], startup_seconds=120)
        except BaseException as failure:  # pytest.fail in a thread: carried to the test below
            failures[name] = failure

    # The forcing step: one of the two had to wait for the other (the warning says so). Without
    # the lock both download the extension and rename it into place, and on Windows the second
    # rename fails once the first server has loaded the file.
    with pytest.warns(UserWarning, match="waited .* installing the spatial extension"):
        threads = [threading.Thread(target=start, args=(name,)) for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300 * TIMEOUT_SCALE)
    assert not failures, failures
    assert sorted(servers) == ["first", "second"]
    for running in servers.values():
        assert running.admin().scalar(sql="SELECT ST_AsText(ST_Point(1, 2))") == "POINT (1 2)"
    installed = list(cache.rglob("spatial.duckdb_extension"))
    assert len(installed) == 1, installed  # one copy, in the directory both servers used
