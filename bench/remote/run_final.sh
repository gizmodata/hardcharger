#!/usr/bin/env bash
# The 2026-10-19 matrix (docs/benchmarks.md): every shape for all three systems, TLS,
# then the plaintext supplement. Hardcharger runs with the Go/Python driver's defaults
# (no option set: auto compression, up to 32 result partitions, auto upload parts), plus
# one stream, uncompressed, for reference. REPS measured repetitions (default 5), one
# warm-up each. Results go to ~/bench/results-final[-plain]-<network>.jsonl on the clients.
#
#   run_final.sh lan|wan|plain
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MODE=${1:?lan, wan or plain}
export REPS=${REPS:-5}
SYSTEMS=${SYSTEMS:-hardcharger quack gizmosql}   # which systems to run
want() { [[ " $SYSTEMS " == *" $1 "* ]]; }
case $MODE in
  lan)
    want hardcharger && printf '%s\n' "--workload fetch --dataset sf1 --driver-defaults" "--workload fetch --dataset sf10 --driver-defaults" \
      "--workload fetch --dataset narrow --driver-defaults" "--workload ingest --dataset sf1 --driver-defaults" \
      "--workload ingest --dataset sf10 --driver-defaults" "--workload fetch --dataset sf10" \
      "--workload ingest --dataset sf10" | "$HERE/run_cases.sh" lan hardcharger final
    want quack && printf '%s\n' "--workload fetch --dataset sf1" "--workload fetch --dataset sf10" "--workload fetch --dataset narrow" \
      "--workload ingest --dataset sf1 --preserve-order true" "--workload ingest --dataset sf1 --preserve-order false" \
      "--workload ingest --dataset sf10 --preserve-order true" "--workload ingest --dataset sf10 --preserve-order false" |
      "$HERE/run_cases.sh" lan quack final
    want gizmosql && printf '%s\n' "--workload fetch --dataset sf1" "--workload fetch --dataset sf10" "--workload fetch --dataset narrow" \
      "--workload ingest --dataset sf1" "--workload ingest --dataset sf10" | "$HERE/run_cases.sh" lan gizmosql final ;;
  wan)
    want hardcharger && printf '%s\n' "--workload fetch --dataset sf1 --driver-defaults" "--workload ingest --dataset sf1 --driver-defaults" \
      "--workload fetch --dataset sf1" | "$HERE/run_cases.sh" wan hardcharger final
    want quack && printf '%s\n' "--workload fetch --dataset sf1" "--workload ingest --dataset sf1 --preserve-order true" \
      "--workload ingest --dataset sf1 --preserve-order false" | "$HERE/run_cases.sh" wan quack final
    want gizmosql && printf '%s\n' "--workload fetch --dataset sf1" "--workload ingest --dataset sf1" | "$HERE/run_cases.sh" wan gizmosql final ;;
  plain)
    export TLS=0
    want hardcharger && printf '%s\n' "--workload fetch --dataset sf10 --driver-defaults --tls false" "--workload ingest --dataset sf10 --driver-defaults --tls false" |
      "$HERE/run_cases.sh" lan hardcharger final-plain
    want quack && printf '%s\n' "--workload fetch --dataset sf10 --tls false" "--workload ingest --dataset sf10 --preserve-order false --tls false" |
      "$HERE/run_cases.sh" lan quack final-plain
    want gizmosql && printf '%s\n' "--workload fetch --dataset sf10 --tls false" "--workload ingest --dataset sf10 --tls false" |
      "$HERE/run_cases.sh" lan gizmosql final-plain ;;
esac
