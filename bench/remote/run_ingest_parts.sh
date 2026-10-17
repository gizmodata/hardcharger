#!/usr/bin/env bash
# The partitioned-ingest matrix (docs/benchmarks.md, 2026-10-17): ingest with Hardcharger in
# 1, 8 and 16 upload parts, Quack with and without insertion order, and GizmoSQL; fetch with
# Hardcharger on one stream and in the drivers' default 8 partitions, in order.
#
#   run_ingest_parts.sh lan|wan        (SYSTEMS, INGEST, FETCH and REPS as in run_matrix.sh)
set -uo pipefail
NETWORK=${1:?lan or wan}
STATE=$HOME/.hardcharger-bench
SSHO=(-o UserKnownHostsFile="$STATE/known_hosts" -i "$STATE/hardcharger-bench-key" -o ServerAliveInterval=30)
SERVER=ubuntu@$(cat "$STATE/server.public")
CLIENT=ubuntu@$(cat "$STATE/client-$NETWORK.public")
if [ "$NETWORK" = lan ]; then HOST=$(cat "$STATE/server.private"); INGEST=${INGEST:-sf1 sf10}; FETCH=${FETCH:-sf1 sf10}
else HOST=$(cat "$STATE/server.public"); INGEST=${INGEST:-sf1}; FETCH=${FETCH:-sf1}; fi
REPS=${REPS:-5}
OUT="~/bench/results-r2-$NETWORK.jsonl"
run() {
  ssh "${SSHO[@]}" "$CLIENT" "cd ~/bench && ~/venv/bin/python client.py --host $HOST --network $NETWORK \
    --reps $REPS --out $OUT --label round2 $*" | grep -E '^SUMMARY|VERIFY' || echo "FAILED: $*" >&2
}
for system in ${SYSTEMS:-hardcharger quack gizmosql}; do
  ssh "${SSHO[@]}" "$SERVER" "bash ~/bench/serve.sh $system" || { echo "FAILED to start $system" >&2; continue; }
  case $system in
    hardcharger)
      for d in $INGEST; do for p in 1 8 16; do run --system hardcharger --workload ingest --dataset $d --ingest-parts $p; done; done
      for d in $FETCH; do
        run --system hardcharger --workload fetch --dataset $d --partitions 1
        run --system hardcharger --workload fetch --dataset $d --partitions 8 --order preserve
      done ;;
    quack)
      for d in $INGEST; do for o in true false; do run --system quack --workload ingest --dataset $d --preserve-order $o; done; done
      for d in $FETCH; do run --system quack --workload fetch --dataset $d; done ;;
    gizmosql)
      for d in $INGEST; do run --system gizmosql --workload ingest --dataset $d; done ;;
  esac
done
ssh "${SSHO[@]}" "$SERVER" "bash ~/bench/serve.sh stop"
