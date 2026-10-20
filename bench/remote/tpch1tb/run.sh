#!/usr/bin/env bash
# From the controller (a laptop with AWS credentials in the environment, never printed):
#
#   run.sh push <sf> <hardcharger-linux-arm64.tar.gz> <adbc_driver_hardcharger-*.whl>
#                    copy the scripts, the two private builds and a fresh presigned manifest
#                    to the VM, then run vm-setup.sh there
#   run.sh sync                   copy just the scripts again
#   run.sh start <sf> [step...]   start pipeline.sh on the VM, detached (survives SSH drops)
#   run.sh wait <sf>              block until the VM's pipeline finishes; print its log
#   run.sh pull <sf> <dir>        copy the VM's results/sf<N> and logs into <dir>
#   run.sh ssh [command...]
# The VM comes from provision.sh up (same STATE directory). push needs HARDCHARGER_BENCH_DATA_BUCKET
# (the private S3 bucket tpch-datagen staged tpch/sf=<N>/ in; HARDCHARGER_BENCH_DATA_REGION, default
# us-east-2, is its region): presign.py reads them.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE=${STATE:-$HOME/.hardcharger-bench-tpch}
KEY="$STATE/hardcharger-bench-tpch-key"
IP=$(cat "$STATE/vm.public")
SSH=(ssh -i "$KEY" -o UserKnownHostsFile="$STATE/known_hosts" -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30 -o ConnectTimeout=15 "ubuntu@$IP")
SCP=(scp -q -i "$KEY" -o UserKnownHostsFile="$STATE/known_hosts" -o StrictHostKeyChecking=accept-new)

case "${1:-}" in
  push)
    SF=${2:?sf}; TARBALL=${3:?hardcharger tarball}; WHEEL=${4:?driver wheel}
    : "${HARDCHARGER_BENCH_DATA_BUCKET:?set it to the S3 bucket the TPC-H-derived data is staged in}"
    for _ in $(seq 1 30); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
    "${SSH[@]}" "mkdir -p ~/tpch1tb"
    "${SCP[@]}" "$HERE"/*.sh "$HERE"/*.py "ubuntu@$IP:tpch1tb/"
    "${SCP[@]}" "$TARBALL" "ubuntu@$IP:tpch1tb/hardcharger-linux-arm64.tar.gz"
    "${SCP[@]}" "$WHEEL" "ubuntu@$IP:tpch1tb/"
    manifest=$(mktemp "${TMPDIR:-/tmp}/urls.XXXXXX")
    uv run --quiet --with boto3 python "$HERE/presign.py" --scale-factor "$SF" --output "$manifest"
    "${SCP[@]}" "$manifest" "ubuntu@$IP:tpch1tb/urls.tsv"
    rm -f "$manifest"
    "${SSH[@]}" "chmod 600 ~/tpch1tb/urls.tsv && bash ~/tpch1tb/vm-setup.sh" 2>&1 | tail -25
    ;;
  sync) "${SCP[@]}" "$HERE"/*.sh "$HERE"/*.py "ubuntu@$IP:tpch1tb/" ;;
  start)
    SF=${2:?sf}; shift 2
    # pipeline.sh writes its own PID; `wait` watches that PID (never a pattern match).
    "${SSH[@]}" "cd ~/tpch1tb && rm -f pipeline.pid && (setsid nohup bash pipeline.sh $SF $* >pipeline-sf$SF.out 2>&1 </dev/null &) && sleep 3 && echo started pid \$(cat pipeline.pid)"
    ;;
  wait)
    SF=${2:?sf}
    until "${SSH[@]}" 'p=$(cat ~/tpch1tb/pipeline.pid) && ! kill -0 "$p" 2>/dev/null'; do sleep 30; done
    "${SSH[@]}" "tail -60 ~/tpch1tb/pipeline-sf$SF.out"
    ;;
  pull)
    SF=${2:?sf}; DEST=${3:?dir}
    mkdir -p "$DEST"
    "${SSH[@]}" "cd ~/tpch1tb && tar -czf - results/sf$SF logs pipeline-sf$SF.out" | tar -xzf - -C "$DEST"
    ls -la "$DEST/results/sf$SF"
    ;;
  ssh) shift; "${SSH[@]}" "$@" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
