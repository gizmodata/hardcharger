"""benchmark-flight-sql's query loop, over Warp SQL to Hardcharger.

    python warp_benchmark.py --uri hardcharger://127.0.0.1:31337 --tls-skip-verify \
        --username bench --password ... --query-yaml-filename queries/tpc_h.yaml \
        --num-query-runs 3 --output-filename hardcharger.json

A line-for-line port of benchmark_flight_sql.benchmark (gizmodata/benchmark-flight-sql 0.0.6,
28bb41e) with the Flight SQL driver swapped for adbc-driver-hardcharger, so both servers see the
same session semantics and the JSON has the same shape:
- one connection for the whole run; `SET schema=main` first, then `SELECT VERSION()`;
- each query in the YAML, in order, `--num-query-runs` times back to back;
- each run: a new cursor, execute, fetch_arrow_table (every row), close the cursor; run_time is
  time.time() from before the cursor to after it is closed;
- per query: mean, min and max of the run times; status and row_count per run.
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from adbc_driver_hardcharger import dbapi


def run_query(conn, query: dict, run_number: int) -> dict:
    details = {"run_number": run_number, "start_datetime": datetime.now().astimezone(),
               "start_time": time.time()}
    cursor = conn.cursor()
    try:
        cursor.execute(operation=query["sql"])
        details["row_count"] = cursor.fetch_arrow_table().num_rows
    except Exception as error:  # recorded, as benchmark-flight-sql does
        details["status"] = "ERROR"
        details["exception"] = str(error)
    else:
        details["status"] = "SUCCESS"
    finally:
        cursor.close()
        details["end_time"] = time.time()
        details["end_datetime"] = datetime.now().astimezone()
        details["run_time"] = details["end_time"] - details["start_time"]
    return details


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--tls-skip-verify", action="store_true")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--schema", default="main")
    parser.add_argument("--query-yaml-filename", required=True)
    parser.add_argument("--num-query-runs", type=int, default=3)
    parser.add_argument("--output-filename", required=True)
    args = parser.parse_args()

    queries = yaml.safe_load(Path(args.query_yaml_filename).read_text())["queries"]
    conn = dbapi.connect(uri=args.uri, username=args.username, password=args.password,
                         tls_skip_verify=args.tls_skip_verify)
    try:
        with conn.cursor() as cursor:
            cursor.execute(operation=f"SET schema={args.schema}")
        result = {"run_date": datetime.now().astimezone(), "uri": args.uri,
                  "disable_certificate_validation": args.tls_skip_verify, "username": args.username,
                  "schema": args.schema, "query_yaml_filename": args.query_yaml_filename,
                  "client": "adbc-driver-hardcharger (warp_benchmark.py)",
                  "overall_start_datetime": datetime.now().astimezone(), "overall_start_time": time.time(),
                  "overall_success_count": 0, "overall_failure_count": 0, "query_run_results": []}
        with conn.cursor() as cursor:
            cursor.execute(operation="SELECT VERSION()")
            result["database_version"] = cursor.fetchone()[0]
        for query in queries:
            batch = {"query": query, "batch_start_datetime": datetime.now().astimezone(),
                     "batch_start_time": time.time(), "run_count": 0, "success_count": 0,
                     "failure_count": 0, "runs": []}
            run_times = []
            for number in range(1, args.num_query_runs + 1):
                batch["run_count"] += 1
                details = run_query(conn=conn, query=query, run_number=number)
                batch["success_count" if details["status"] == "SUCCESS" else "failure_count"] += 1
                run_times.append(details["run_time"])
                batch["runs"].append(details)
                print(f"{query['query_id']} run {number}: {details['run_time']:.3f} s "
                      f"rows={details.get('row_count')} {details['status']}", flush=True)
            batch["batch_end_datetime"] = datetime.now().astimezone()
            batch["batch_end_time"] = time.time()
            batch["batch_run_time"] = batch["batch_end_time"] - batch["batch_start_time"]
            if batch["success_count"] > 0:
                batch["mean_runtime"] = sum(run_times) / len(run_times)
                batch["min_runtime"] = min(run_times)
                batch["max_runtime"] = max(run_times)
            result["query_run_results"].append(batch)
            result["overall_success_count"] += batch["success_count"]
            result["overall_failure_count"] += batch["failure_count"]
        result["overall_end_datetime"] = datetime.now().astimezone()
        result["overall_end_time"] = time.time()
        result["overall_run_time"] = result["overall_end_time"] - result["overall_start_time"]
        Path(args.output_filename).write_text(json.dumps(result, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
