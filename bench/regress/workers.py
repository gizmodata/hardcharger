"""One concurrent client: a process of its own (no shared interpreter lock between clients), with
its own session, pinned to the client cores on Linux. Each runs its statement list `passes`
times, starting `offset` statements in, so clients don't all run the same statement at once.
Every result is checked against its expected answer; a wrong answer or an error is recorded,
never retried."""

import os
import time
import traceback

from client import Session
from workloads import check_answer


def client_main(base_url: str, username: str, password: str, statements: list[tuple[str, str]],
                expected: dict, passes: int, offset: int, cores: list[int] | None, barrier, results) -> None:
    record = {"latencies": [], "errors": [], "wrong": [], "started": 0.0, "finished": 0.0}
    session = None
    try:
        if cores and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, cores)
        session = Session(base_url=base_url, username=username, password=password)
        barrier.wait(timeout=300)
        record["started"] = time.monotonic()
        order = statements[offset:] + statements[:offset]
        for _ in range(passes):
            for name, sql in order:
                began = time.perf_counter()
                try:
                    table = session.query(sql=sql)
                except Exception as error:  # noqa: BLE001 - every failure is a result
                    record["errors"].append(f"{name}: {error}"[:500])
                    continue
                record["latencies"].append((name, time.perf_counter() - began))
                problem = check_answer(table=table, expected=expected.get(name))
                if problem:
                    record["wrong"].append(f"{name}: {problem}")
        record["finished"] = time.monotonic()
    except Exception:  # noqa: BLE001
        record["errors"].append(traceback.format_exc()[-1000:])
        record["finished"] = time.monotonic()
        try:
            barrier.abort()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if session:
            session.close()
        results.put(record)
