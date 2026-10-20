#!/usr/bin/env bash
# On the VM: servers.sh start <stable|edge|hardcharger> <database-file> | stop
#
# One server at a time, on port 31337, TLS with ~/tpch1tb/tls.crt, and otherwise every server
# default (no threads, memory or other tuning), as the published run. The server's PID goes to
# ~/tpch1tb/server.pid and `stop` signals that PID only (never a pattern match).
#
# Every server starts in /data: Hardcharger's default temp_directory is `.tmp` relative to its
# working directory (GizmoSQL's is beside the database file), so this keeps every server's spill
# space on the NVMe array rather than the EBS root.
# SERVER_LD_PRELOAD (diagnostics only, never in the reported runs): preloaded into the server,
# e.g. the TCP_NODELAY shim (nodelay.c) that showed Hardcharger's ~41 ms per-statement stall.
set -euo pipefail
B=~/tpch1tb
PIDFILE=$B/server.pid
PORT=31337
export BENCH_PASSWORD=${BENCH_PASSWORD:-bench-password-not-secret}

stop() {
  [ -f "$PIDFILE" ] || return 0
  pid=$(cat "$PIDFILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    for _ in $(seq 1 240); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$pid" 2>/dev/null; then echo "server $pid ignored SIGTERM for 120 s; SIGKILL" >&2; kill -KILL "$pid"; fi
  fi
  rm -f "$PIDFILE"
  for _ in $(seq 1 60); do grep -q ":$PORT " <<<"$(ss -ltn)" || return 0; sleep 0.5; done
  echo "port $PORT still open after stop" >&2; return 1
}

case "${1:-}" in
  stop) stop ;;
  start)
    SYSTEM=${2:?stable, edge or hardcharger}
    DB=${3:?database file}
    stop
    if grep -q ":$PORT " <<<"$(ss -ltn)"; then echo "port $PORT is in use" >&2; exit 1; fi
    LOG=$B/logs/server-$SYSTEM-$(date -u +%Y%m%dT%H%M%SZ).log
    mkdir -p "$B/logs"
    cd /data
    # SF1000 lineitem is 755 Parquet files; GizmoSQL edge (DuckDB 2.0 alpha) opens more of them at
    # once than the default soft limit of 1024 descriptors ("Too many open files" in the first
    # SF1000 load). Same raised OS limit for every server; no server setting changes.
    ulimit -n 1048576
    PRELOAD=(); [ -n "${SERVER_LD_PRELOAD:-}" ] && PRELOAD=(env "LD_PRELOAD=$SERVER_LD_PRELOAD")
    case "$SYSTEM" in
      stable|edge)
        GIZMOSQL_PASSWORD="$BENCH_PASSWORD" nohup ${PRELOAD[@]+"${PRELOAD[@]}"} "$B/gizmosql-$SYSTEM/server" \
          --database-filename "$DB" --username bench --port $PORT \
          --tls "$B/tls.crt" "$B/tls.key" >"$LOG" 2>&1 </dev/null &
        ;;
      hardcharger)
        HARDCHARGER_ADMIN_PASSWORD="$BENCH_PASSWORD" nohup ${PRELOAD[@]+"${PRELOAD[@]}"} "$B/hardcharger/bin/hardcharger-server" \
          --database "$DB" --admin-username bench --port $PORT \
          --tls-cert "$B/tls.crt" --tls-key "$B/tls.key" >"$LOG" 2>&1 </dev/null &
        ;;
      *) echo "unknown server $SYSTEM" >&2; exit 2 ;;
    esac
    echo $! >"$PIDFILE"
    for _ in $(seq 1 600); do
      if grep -q ":$PORT " <<<"$(ss -ltn)"; then echo "$SYSTEM (pid $(cat "$PIDFILE")) listening on $PORT, log $LOG"; exit 0; fi
      kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
      sleep 0.5
    done
    echo "$SYSTEM did not start; log:" >&2; tail -30 "$LOG" >&2; exit 1
    ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
