#!/usr/bin/env bash
# Runs the benchmark matrix from the operator's machine: for each system, start it on the
# server (fresh database copy), then run every workload from the client.
#
#   run_matrix.sh lan|wan [systems...]          (default: gizmosql quack hardcharger)
#
# FETCH and INGEST name the datasets (defaults below; WAN runs SF1 only). REPS sets the
# measured repetitions (one warm-up each). Results append to ~/bench/results-<network>.jsonl
# on the client; the server logs CPU use to ~/bench/cpu-server.log throughout.
set -euo pipefail
NETWORK=${1:?lan or wan}
shift
[ $# -gt 0 ] || set -- gizmosql quack hardcharger
SYSTEMS=("$@")
STATE=${STATE:-$HOME/.hardcharger-bench}
SSHO=(-o UserKnownHostsFile="$STATE/known_hosts" -i "$STATE/hardcharger-bench-key")
SERVER=ubuntu@$(cat "$STATE/server.public")
CLIENT=ubuntu@$(cat "$STATE/client-$NETWORK.public")
if [ "$NETWORK" = lan ]; then
  HOST=$(cat "$STATE/server.private")
  FETCH=${FETCH:-sf1 sf10 narrow}
  INGEST=${INGEST:-sf1 sf10}
else
  HOST=$(cat "$STATE/server.public")
  FETCH=${FETCH:-sf1}
  INGEST=${INGEST:-sf1}
fi
REPS=${REPS:-7}
OUT="~/bench/results-$NETWORK.jsonl"

run() { # run <client.py args...>
  ssh "${SSHO[@]}" "$CLIENT" "cd ~/bench && ~/venv/bin/python client.py --host $HOST \
    --network $NETWORK --reps $REPS --out $OUT $*" | grep -E '^SUMMARY|rep' || {
    echo "FAILED: $*" >&2
    return 1
  }
}

for system in "${SYSTEMS[@]}"; do
  ssh "${SSHO[@]}" "$SERVER" "bash ~/bench/serve.sh $system"
  case "$system" in
    gizmosql) variants=("") ;;
    quack) variants=("--preserve-order true" "--preserve-order false") ;;
    hardcharger) variants=("--compression none" "--compression zstd") ;;
  esac
  for v in "${variants[@]}"; do
    for d in $FETCH; do
      # preserve_insertion_order changes only how Quack plans inserts; fetch once.
      [[ "$v" == "--preserve-order false" ]] && continue
      run --system "$system" --workload fetch --dataset "$d" $v
    done
    for d in $INGEST; do run --system "$system" --workload ingest --dataset "$d" $v; done
  done
done
ssh "${SSHO[@]}" "$SERVER" "bash ~/bench/serve.sh stop"
