#!/usr/bin/env bash
# On the VM: bench.sh <stable|edge|hardcharger> <database-file> <results-dir>
#
# One timed run of the 22 TPC-H queries x 3 against one server:
#   1. sync and drop the OS page cache, so each query's first run is cold;
#   2. start the server (defaults + TLS) on the database file;
#   3. GizmoSQL (stable, edge): benchmark-flight-sql --num-query-runs 3, as published;
#      Hardcharger: warp_benchmark.py, the same loop over Warp SQL (adbc-driver-hardcharger);
#   4. afterwards (outside the timed run) record the engine's version and the row counts;
#   5. stop the server by its PID.
# The client is on the same VM (127.0.0.1). vmstat runs alongside at 1 s for the record.
set -euo pipefail
SYSTEM=${1:?stable, edge or hardcharger}
DB=${2:?database file}
OUT=${3:?results directory}
B=~/tpch1tb
RUNS=${RUNS:-3}
export BENCH_PASSWORD=${BENCH_PASSWORD:-bench-password-not-secret}
mkdir -p "$OUT"
[ -f "$DB" ] || { echo "no database file $DB" >&2; exit 1; }

{
  echo "system=$SYSTEM db=$DB date=$(date -u +%FT%TZ)"
  echo "instance=$(cloud-init query ds.meta_data.instance_type 2>/dev/null || true)"
  nproc; free -g | head -2; df -h /data | tail -1; uname -r
  ls -la "$DB"
} >"$OUT/$SYSTEM-env.txt"

sync
echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
bash "$B/servers.sh" start "$SYSTEM" "$DB"
trap 'bash "$B/servers.sh" stop' EXIT
echo "server pid=$(cat "$B/server.pid") cwd=$(readlink "/proc/$(cat "$B/server.pid")/cwd")" >>"$OUT/$SYSTEM-env.txt"
vmstat -t 1 >"$OUT/$SYSTEM-vmstat.txt" 2>&1 &
VMSTAT=$!

set +e
if [ "$SYSTEM" = hardcharger ]; then
  ~/venv-hc/bin/python "$B/warp_benchmark.py" --uri hardcharger://127.0.0.1:31337 --tls-skip-verify \
    --username bench --password "$BENCH_PASSWORD" \
    --query-yaml-filename "$B/benchmark-flight-sql/queries/tpc_h.yaml" \
    --num-query-runs "$RUNS" --output-filename "$OUT/$SYSTEM.json" 2>&1 | tee "$OUT/$SYSTEM-client.log"
  status=${PIPESTATUS[0]}
else
  (cd "$B/benchmark-flight-sql" &&
    ~/venv-bfs/bin/benchmark-flight-sql --hostname 127.0.0.1 --port 31337 --no-certificate-validation \
      --username bench --password "$BENCH_PASSWORD" --num-query-runs "$RUNS" \
      --output-filename "$OUT/$SYSTEM.json") >"$OUT/$SYSTEM-client.log" 2>&1
  status=$?
fi
set -e
kill "$VMSTAT" 2>/dev/null || true
[ "$status" = 0 ] || { echo "$SYSTEM client failed ($status); see $OUT/$SYSTEM-client.log" >&2; exit 1; }

venv=~/venv-bfs; [ "$SYSTEM" = hardcharger ] && venv=~/venv-hc
"$venv/bin/python" "$B/sql_tool.py" --server "$SYSTEM" info --output "$OUT/$SYSTEM-info.json" >/dev/null
python3 - "$OUT/$SYSTEM.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
total = sum(q.get("mean_runtime", 0) for q in d["query_run_results"])
print(f"{sys.argv[1]}: {d['database_version']}, {d['overall_success_count']} ok, "
      f"{d['overall_failure_count']} failed, total of means {total:.3f} s")
sys.exit(1 if d["overall_failure_count"] else 0)
EOF
