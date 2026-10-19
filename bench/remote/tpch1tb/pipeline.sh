#!/usr/bin/env bash
# On the VM: pipeline.sh <scale-factor> [step...]
#
# Steps, in order (default: all):
#   fetch     Parquet from the presigned manifest to /data/tpch/sf<N>/ (not timed)
#   load      file A, loaded by GizmoSQL stable (DuckDB 1.5.x): the published setup;
#             file B, loaded by GizmoSQL edge (DuckDB 2.0 alpha), for both DuckDB 2.0 servers
#   stable    timed run: GizmoSQL stable on A
#   edge      timed run: GizmoSQL edge on B
#   hardcharger  timed run: Hardcharger on B if it opens B, else on file C that it loads itself
#   probe     on a throwaway copy of A: do edge and Hardcharger open a DuckDB 1.5 file?
# Every server: defaults plus TLS; one server at a time; page cache dropped before each run.
set -euo pipefail  # pipefail: a failed step piped into tee stops the pipeline
SF=${1:?scale factor}; shift
STEPS=("$@"); [ ${#STEPS[@]} -gt 0 ] || STEPS=(fetch load stable edge hardcharger probe)
B=~/tpch1tb
echo $$ >"$B/pipeline.pid"   # run.sh wait watches this PID
R=$B/results/sf$SF
mkdir -p "$R"
A=/data/tpch_sf${SF}_stable.duckdb
DB2=/data/tpch_sf${SF}_edge.duckdb
C=/data/tpch_sf${SF}_hardcharger.duckdb
trap 'bash "$B/servers.sh" stop' EXIT   # never leave a server running
log() { echo "[$(date -u +%T)] $*" | tee -a "$R/pipeline.log"; }

load() { # load <server> <file>
  rm -f "$2" "$2.wal"
  bash "$B/servers.sh" start "$1" "$2"
  venv=~/venv-bfs; [ "$1" = hardcharger ] && venv=~/venv-hc
  "$venv/bin/python" "$B/sql_tool.py" --server "$1" load --scale-factor "$SF" --output "$R/load-$1.json" |
    tee -a "$R/pipeline.log"
  bash "$B/servers.sh" stop
  log "loaded $2 with $1: $(du -h "$2" | cut -f1)"
}

opens() { # opens <server> <file> <info-output>: can this server open and read the file?
  bash "$B/servers.sh" start "$1" "$2" >/dev/null 2>&1 || return 1
  venv=~/venv-bfs; [ "$1" = hardcharger ] && venv=~/venv-hc
  local ok=0
  "$venv/bin/python" "$B/sql_tool.py" --server "$1" info --output "$3" >/dev/null 2>&1 || ok=1
  bash "$B/servers.sh" stop
  return $ok
}

for step in "${STEPS[@]}"; do
  log "== $step"
  case "$step" in
    fetch) bash "$B/fetch_data.sh" "$SF" | tee -a "$R/pipeline.log" ;;
    load) load stable "$A"; load edge "$DB2" ;;
    stable) bash "$B/bench.sh" stable "$A" "$R" | tee -a "$R/pipeline.log" ;;
    edge) bash "$B/bench.sh" edge "$DB2" "$R" | tee -a "$R/pipeline.log" ;;
    hardcharger)
      sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
      if opens hardcharger "$DB2" "$R/probe-hardcharger-on-edge-file.json"; then
        HCDB=$DB2; log "Hardcharger opens the edge-loaded file; using it"
      else
        log "Hardcharger can't open the edge-loaded file; loading its own"
        load hardcharger "$C"; HCDB=$C
      fi
      echo "$HCDB" >"$R/hardcharger-database.txt"
      bash "$B/bench.sh" hardcharger "$HCDB" "$R" | tee -a "$R/pipeline.log"
      ;;
    probe)
      cp "$A" "$A.probe"
      for s in edge hardcharger; do
        if opens "$s" "$A.probe" "$R/probe-$s-on-stable-file.json"; then
          log "probe: $s opens the DuckDB 1.5 file"
        else
          log "probe: $s can NOT open the DuckDB 1.5 file"
          tail -5 "$(ls -t "$B"/logs/server-"$s"-*.log | head -1)" | tee -a "$R/pipeline.log"
        fi
        rm -f "$A.probe.wal"; cp "$A" "$A.probe"
      done
      rm -f "$A.probe" "$A.probe.wal"
      ;;
    *) echo "unknown step $step" >&2; exit 2 ;;
  esac
done
log "== done"
