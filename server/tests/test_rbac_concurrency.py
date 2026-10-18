"""Access control must not cost concurrency. The same TPC-H workload runs against two
servers that differ only in access control: one without it, one where every client is a
store user whose statements are checked and rewritten for a row policy and a mask. For
each, one client and then many clients run every TPC-H query; the test fails if access
control makes the concurrent run much slower, or scales worse with more clients, than
the baseline.

HARDCHARGER_TEST_CONCURRENCY_SF sets the scale factor (default 0.1: small queries make
any per-statement or shared-lock cost easier to see), and HARDCHARGER_TEST_CONCURRENCY_ROUNDS
the passes per client (default 10; the TSan job, 5-15x slower, runs 3)."""

import os
import statistics
import threading
import time

import pytest

from rbac import Client, expect_allowed, mint, start_rbac_server

CLIENTS = 10
# Passes over the 22 queries per client (long enough runs for stable ratios).
ROUNDS = int(os.environ.get("HARDCHARGER_TEST_CONCURRENCY_ROUNDS", "10"))
TRIALS = 3  # medians over this many measurements per mode
SCALE_FACTOR = os.environ.get("HARDCHARGER_TEST_CONCURRENCY_SF", "0.1")


def prepare(admin, rbac: bool) -> list[str]:
    for sql in ["INSTALL tpch", "LOAD tpch", f"CALL dbgen(sf={SCALE_FACTOR})"]:
        expect_allowed(client=admin, sql=sql)
    if rbac:
        # A row policy on the largest table (it keeps every row, so results match the
        # baseline) and a mask: the policy rewrite runs too.
        for sql in ["CREATE ROLE bench", "GRANT SELECT ON SCHEMA main TO bench",
                    "CREATE ROW POLICY bench_rows ON lineitem TO bench USING (l_orderkey > 0)",
                    "CREATE MASKING POLICY bench_mask AS (val VARCHAR) RETURNS VARCHAR -> 'x'",
                    "ALTER TABLE nation MODIFY COLUMN n_comment SET MASKING POLICY bench_mask"]:
            expect_allowed(client=admin, sql=sql)
        for i in range(CLIENTS + 1):
            expect_allowed(client=admin, sql=f"CREATE USER bench{i}")
            expect_allowed(client=admin, sql=f"GRANT bench TO bench{i}")
    return admin.run(sql="SELECT query FROM tpch_queries() ORDER BY query_nr").column("query").to_pylist()


def run(base_url: str, rbac: bool, clients: int, queries: list[str], expected: list[int]) -> float:
    """`clients` sessions at once each run every query ROUNDS times; seconds taken."""
    # Baseline clients carry server-signed user tokens too, so both servers take the
    # same path except for access control itself.
    sessions = [Client(base_url=base_url, token=mint(user=f"bench{c}" if rbac else "bench"))
                for c in range(clients)]
    for session in sessions:
        session.run(sql="SELECT 1")  # up before timing
    wrong: list[str] = []

    def work(c: int) -> None:
        for _ in range(ROUNDS):
            for q, sql in enumerate(queries):
                try:
                    rows = sessions[c].run(sql=sql).num_rows
                except Exception as error:  # noqa: BLE001 - every failure is a wrong result here
                    rows = f"error {error}"
                if rows != expected[q]:
                    wrong.append(f"{'rbac' if rbac else 'baseline'} client {c} q{q + 1}: {rows}, expected {expected[q]}")

    threads = [threading.Thread(target=work, args=(c,)) for c in range(clients)]
    start = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seconds = time.monotonic() - start
    for session in sessions:
        session.close()
    assert wrong == [], wrong[:5]
    return seconds


# Serial: a throughput ratio between two servers means nothing while other tests share
# the CPUs.
@pytest.mark.performance
@pytest.mark.serial
def test_access_control_does_not_cost_concurrency(launcher, store):
    baseline = launcher.start(env={"HARDCHARGER_SECRET_KEY": "hardcharger-rbac-tests-secret-key-0123456789",
                                   "HARDCHARGER_ALLOW_CROSS_INSTANCE_TOKENS": "true",
                                   "HARDCHARGER_PRINT_QUERIES": "false"})
    governed = start_rbac_server(launcher=launcher, store_uri=store.new_database(),
                                 env={"HARDCHARGER_PRINT_QUERIES": "false"})
    base_admin = Client(base_url=baseline.base_url, token=baseline.token())
    queries = prepare(admin=base_admin, rbac=False)
    assert len(queries) == 22
    assert prepare(admin=governed.admin(), rbac=True) == queries

    # Reference row counts, and a warm-up pass on each server.
    expected = [base_admin.run(sql=sql).num_rows for sql in queries]
    run(base_url=baseline.base_url, rbac=False, clients=1, queries=queries, expected=expected)
    run(base_url=governed.base_url, rbac=True, clients=1, queries=queries, expected=expected)

    # Alternate the servers so drift on the machine hits both alike.
    timings = {"base_single": [], "rbac_single": [], "base_multi": [], "rbac_multi": []}
    for _ in range(TRIALS):
        timings["base_single"].append(run(base_url=baseline.base_url, rbac=False, clients=1, queries=queries,
                                          expected=expected))
        timings["rbac_single"].append(run(base_url=governed.base_url, rbac=True, clients=1, queries=queries,
                                          expected=expected))
        timings["base_multi"].append(run(base_url=baseline.base_url, rbac=False, clients=CLIENTS, queries=queries,
                                         expected=expected))
        timings["rbac_multi"].append(run(base_url=governed.base_url, rbac=True, clients=CLIENTS, queries=queries,
                                         expected=expected))
    median = {name: statistics.median(values) for name, values in timings.items()}

    def scaling(single: float, multi: float) -> float:
        per_client = 22.0 * ROUNDS
        return (per_client * CLIENTS / multi) / (per_client / single)

    base_scaling = scaling(single=median["base_single"], multi=median["base_multi"])
    rbac_scaling = scaling(single=median["rbac_single"], multi=median["rbac_multi"])
    print(f"\n=== Concurrency, TPC-H SF {SCALE_FACTOR}, {CLIENTS} clients x {ROUNDS} passes, median of {TRIALS} ===\n"
          f"               1 client (s)   {CLIENTS} clients (s)   scaling (x)\n"
          f"baseline       {median['base_single']:10.2f}{median['base_multi']:16.2f}{base_scaling:14.2f}\n"
          f"access control {median['rbac_single']:10.2f}{median['rbac_multi']:16.2f}{rbac_scaling:14.2f}\n"
          f"overhead       {(median['rbac_single'] / median['base_single'] - 1) * 100:9.1f}%"
          f"{(median['rbac_multi'] / median['base_multi'] - 1) * 100:15.1f}%")
    # A shared lock or anything else that serializes sessions shows up as the concurrent
    # run slowing down far more than the single-client one, and as worse scaling. Allow
    # for timing noise on a busy machine.
    assert median["rbac_multi"] <= median["base_multi"] * 1.25 + 0.2, "access control slowed down concurrent clients"
    assert rbac_scaling >= base_scaling * 0.8, "access control scales worse with concurrent clients than the baseline"
