"""Issue #8: "Too many open files" reading a partitioned DuckLake.

A scan of a DuckLake table holds many of its data files open at once: with a thousand
partition files (four threads) more than 256, and with the reporter's tens of thousands
more than the 1024 that Docker, systemd and Kubernetes start a process with. The server
now raises its soft open-file limit to the hard limit at startup. Here it is started at a
soft limit of 256, which 2.1.0 kept, and the scan failed there.
"""

import sys

import pytest

from conftest import BINARY
from warp_client import read_table

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no open-file soft limit (RLIMIT_NOFILE)")

PARTITIONS = 1000


@pytest.fixture
def low_limit_server(launcher, tmp_path):
    wrapper = tmp_path / "low-open-file-limit.sh"
    wrapper.write_text(f'#!/bin/sh\nulimit -Sn 256 || exit 1\nexec "{BINARY}" "$@"\n')
    wrapper.chmod(0o755)
    return launcher.start(binary=wrapper, args=["--log-format", "json"])


def test_startup_raises_a_low_soft_limit(low_limit_server):
    raised = [r for r in low_limit_server.log_records() if r.get("message") == "raised the open-file limit"]
    assert len(raised) == 1, low_limit_server.log()
    assert raised[0]["from"] == "256", raised[0]
    assert raised[0]["to"] == "unlimited" or int(raised[0]["to"]) > 256, raised[0]


def test_scan_of_a_many_file_partitioned_ducklake(low_limit_server, tmp_path):
    session = low_limit_server.admin()
    session.execute(sql="INSTALL ducklake; LOAD ducklake;")
    session.execute(sql=f"ATTACH 'ducklake:{tmp_path}/meta.ducklake' AS lake (DATA_PATH '{tmp_path}/lake/')")
    session.execute(sql="CREATE TABLE lake.points (k INTEGER, v BIGINT)")
    session.execute(sql="ALTER TABLE lake.points SET PARTITIONED BY (k)")
    session.execute(sql=f"INSERT INTO lake.points SELECT i % {PARTITIONS}, i FROM range({PARTITIONS * 10}) t(i)")
    assert session.scalar(sql=f"SELECT count(*) FROM glob('{tmp_path}/lake/**/*.parquet')") == PARTITIONS
    session.execute(sql="SET threads = 4")
    # On the JSON path a failure comes back as the statement's error (2.1.0: "IO Error:
    # Cannot open file ...: Too many open files"); the streamed path would cut the stream.
    doc = session.execute(sql="SELECT count(*) AS n, sum(v) AS total FROM lake.points")
    result = session.result(statement_id=doc["statement_id"])
    assert result.status_code == 200, result.text
    assert read_table(content=result.content).to_pylist() == [
        {"n": PARTITIONS * 10, "total": sum(range(PARTITIONS * 10))}]
