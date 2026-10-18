"""Port of GizmoSQL 1.x tests/integration/test_tpch_benchmark.cpp (2 cases).

TPC-H at scale factor 1 through the server: INSTALL/LOAD tpch as one multi-statement
string, CALL dbgen(sf = 1), then all 22 queries. Single client, 3 iterations: every
query returns as many rows as the tpch extension's own answer (tpch_answers(); 1.x
compared with an embedded DuckDB), in under 30 s in all. Concurrent: 10 clients, each
with its own session, run the 22 queries with no failure, in under 30 s of wall clock.
Both apply; the 30 s limits are kept as the HTTP performance check. Needs the network
once to INSTALL tpch (DuckDB's extension repository).
"""

import concurrent.futures
import time

import pytest

from conftest import PERF_SCALE

# Serial: the 30 s limits are a performance check, which other tests on the same CPUs
# would turn into a measure of the machine's load. A sanitizer job stretches them
# (HARDCHARGER_TEST_PERF_SCALE); every other build checks them as they are.
pytestmark = pytest.mark.serial

LIMIT = 30 * PERF_SCALE

QUERIES = range(1, 23)


@pytest.fixture(scope="module")
def server(module_launcher, tmp_path_factory):
    server = module_launcher.start(env={"HARDCHARGER_PRINT_QUERIES": "false"})
    admin = server.admin()
    admin.execute(sql="INSTALL tpch; LOAD tpch;")
    admin.execute(sql="CALL dbgen(sf = 1)")
    assert admin.scalar(sql="SELECT count(*) FROM lineitem") == 6001215
    return server


@pytest.fixture(scope="module")
def expected_rows(server):
    answers = server.admin().rows(sql="SELECT query_nr, answer FROM tpch_answers() WHERE scale_factor = 1")
    # Each answer is the result as text: a header line, then one line per row.
    return {r["query_nr"]: len([line for line in r["answer"].splitlines() if line.strip()]) - 1 for r in answers}


def run_all(session) -> dict[int, int]:
    return {n: session.query(sql=f"PRAGMA tpch({n})").num_rows for n in QUERIES}


def test_single_client_runs_all_queries_with_correct_row_counts(server, expected_rows):
    session = server.admin()
    session.execute(sql="LOAD tpch")
    started = time.monotonic()
    for _ in range(3):
        assert run_all(session=session) == {n: expected_rows[n] for n in QUERIES}
    elapsed = time.monotonic() - started
    assert elapsed < LIMIT, f"3 x 22 queries took {elapsed:.1f}s (limit {LIMIT:.0f}s)"


def test_concurrent_clients_run_all_queries_without_failure(server, expected_rows):
    sessions = [server.admin() for _ in range(10)]
    for session in sessions:
        session.execute(sql="LOAD tpch")
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(run_all, sessions))
    elapsed = time.monotonic() - started
    for result in results:
        assert result == {n: expected_rows[n] for n in QUERIES}
    assert elapsed < LIMIT, f"10 clients x 22 queries took {elapsed:.1f}s (limit {LIMIT:.0f}s)"
