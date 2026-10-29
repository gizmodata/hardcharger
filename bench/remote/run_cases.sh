#!/usr/bin/env bash
# Runs a list of client.py cases against one system, from the operator's machine:
#
#   run_cases.sh lan|wan <system> <label> < cases.txt
#
# Each line of stdin is one case: client.py's arguments after --system/--host (e.g.
# "--workload fetch --dataset sf10 --compression lz4_frame --partitions 32"). The server
# is started once (serve.sh <system>, with HC_EXTRA passed through) and stopped at the end.
# REPS (default 5) measured repetitions per case; DRIVER_LIB, when set, is the Go driver's
# shared library the client loads (CHARGEWIRE_ADBC_DRIVER_LIB). Results append to
# ~/bench/results-<label>-<network>.jsonl on the client.
set -uo pipefail
NETWORK=${1:?lan or wan}
SYSTEM=${2:?system}
LABEL=${3:?label}
STATE=${STATE:-$HOME/.hardcharger-bench}
SSHO=(-o UserKnownHostsFile="$STATE/known_hosts" -i "$STATE/hardcharger-bench-key" -o ServerAliveInterval=30)
SERVER=ubuntu@$(cat "$STATE/server.public")
CLIENT=ubuntu@$(cat "$STATE/client-$NETWORK.public")
if [ "$NETWORK" = lan ]; then HOST=$(cat "$STATE/server.private"); else HOST=$(cat "$STATE/server.public"); fi
REPS=${REPS:-5}
OUT="~/bench/results-$LABEL-$NETWORK.jsonl"
ENVS=""
[ -n "${DRIVER_LIB:-}" ] && ENVS="CHARGEWIRE_ADBC_DRIVER_LIB=$DRIVER_LIB"
CASES=()
while IFS= read -r line; do CASES+=("$line"); done
ssh "${SSHO[@]}" "$SERVER" "TLS=${TLS:-1} HC_EXTRA='${HC_EXTRA:-}' CHARGEWIRE_STREAM_STATS=${STREAM_STATS:-0} bash ~/bench/serve.sh $SYSTEM" ||
  { echo "FAILED to start $SYSTEM" >&2; exit 1; }
for c in "${CASES[@]}"; do
  [ -z "$c" ] && continue
  ssh "${SSHO[@]}" "$CLIENT" "cd ~/bench && $ENVS ~/venv/bin/python client.py --system $SYSTEM --host $HOST \
    --network $NETWORK --reps $REPS --out $OUT --label $LABEL $c" </dev/null | grep -E '^SUMMARY|VERIFY|Error|error' |
    sed -E 's/^SUMMARY \{[^}]*\} /'"$SYSTEM"' '"$(echo "$c" | sed 's/--workload //; s/--dataset //; s/--compression /c=/; s/--partitions /p=/; s/--ingest-parts /parts=/; s/--order /o=/; s/--preserve-order /po=/')"': /' ||
    echo "FAILED: $c" >&2
done
[ -n "${KEEP_SERVER:-}" ] || ssh "${SSHO[@]}" "$SERVER" "bash ~/bench/serve.sh stop"
