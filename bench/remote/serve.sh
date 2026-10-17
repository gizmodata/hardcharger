#!/usr/bin/env bash
# Starts one system under test on the server VM, on a fresh copy of the benchmark database
# in /dev/shm (tmpfs, so no system waits on EBS). One server runs at a time.
#
#   serve.sh gizmosql|hardcharger|quack|stop
#
# All three: TLS with the same self-signed certificate (~/bench/tls.crt, .key),
# threads=32, memory_limit=40GB, the same database file (storage format v1.0.0+).
# GizmoSQL and Hardcharger listen on 31337, Quack on its default port, 9494.
# TLS=0 serves plaintext instead (the supplementary TLS-cost runs).
set -euo pipefail
SYSTEM=${1:?gizmosql, hardcharger, quack or stop}
B=$HOME/bench
DB=/dev/shm/run.duckdb
THREADS=${THREADS:-32}
MEMORY_LIMIT=${MEMORY_LIMIT:-40GB}
TLS=${TLS:-1}
export BENCH_PASSWORD=${BENCH_PASSWORD:-bench-password-not-secret}

# Match the server processes exactly (pgrep -x on the name, or an anchored command line),
# never a pattern that a shell command naming the same path would also match.
PATTERN="^($B/gizmosql/gizmosql_server|$B/hardcharger-linux-arm64/bin/hardcharger|$HOME/venv/bin/python $B/serve_quack.py)"
stop() {
  pkill -f "$PATTERN" || true
  for _ in $(seq 1 60); do
    pgrep -f "$PATTERN" >/dev/null || break
    sleep 0.5
  done
  rm -f "$DB" "$DB.wal"
}

stop
[ "$SYSTEM" = stop ] && exit 0
cp "$B/base.duckdb" "$DB"
LOG="$B/server-$SYSTEM.log"
case "$SYSTEM" in
  gizmosql)
    GIZMOSQL_PASSWORD="$BENCH_PASSWORD" nohup "$B/gizmosql/gizmosql_server" \
      --database-filename "$DB" --username bench --port 31337 \
      $([ "$TLS" = 1 ] && echo --tls "$B/tls.crt" "$B/tls.key") --memory-limit "$MEMORY_LIMIT" \
      --init-sql-commands "SET threads=$THREADS;" >"$LOG" 2>&1 </dev/null &
    ;;
  hardcharger)
    HC=$(ls -d "$B"/hardcharger-linux-arm64)
    HARDCHARGER_PASSWORD="$BENCH_PASSWORD" nohup "$HC/bin/hardcharger" \
      --database "$DB" --username bench --port 31337 \
      $([ "$TLS" = 1 ] && echo --tls-cert "$B/tls.crt" --tls-key "$B/tls.key") --memory-limit "$MEMORY_LIMIT" \
      --init-sql-commands "SET threads=$THREADS;" ${HC_EXTRA:-} >"$LOG" 2>&1 </dev/null &
    ;;
  quack)
    nohup ~/venv/bin/python "$B/serve_quack.py" --database "$DB" --threads "$THREADS" \
      --memory-limit "$MEMORY_LIMIT" --cert "$B/tls.crt" --key "$B/tls.key" \
      $([ "$TLS" = 1 ] || echo --plaintext) >"$LOG" 2>&1 </dev/null &
    ;;
  *) echo "unknown system $SYSTEM" >&2; exit 2 ;;
esac
PORT=31337; [ "$SYSTEM" = quack ] && PORT=9494
for _ in $(seq 1 120); do
  if ss -ltn | grep -q ":$PORT "; then echo "$SYSTEM listening on $PORT"; exit 0; fi
  sleep 0.5
done
echo "$SYSTEM did not start; log:" >&2; tail -20 "$LOG" >&2; exit 1
