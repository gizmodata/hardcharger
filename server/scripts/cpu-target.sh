#!/usr/bin/env bash
# Prints the CPU target the dependencies are built for, from HARDCHARGER_CPU_TARGET:
#
#   optimized  (the default) each dependency's own default: on Linux x86_64, DuckDB for
#              x86-64-v3 (AVX2) and Arrow for x86-64-v2 (SSE4.2). The programs then check
#              the CPU before anything else runs (src/cpu_check.c).
#   baseline   DuckDB (DUCKDB_OPTIMIZATION_PROFILE=NONE) and Arrow (ARROW_SIMD_LEVEL=NONE)
#              for any x86-64 CPU, such as a VM with QEMU's kvm64 model (issue #62). Slower:
#              DuckDB does not choose instructions at run time.
#
# deps-duckdb.sh and deps-arrow.sh build by it and record it in their installs
# (share/hardcharger-cpu-target), which server/CMakeLists.txt reads. Build each target into
# its own dependency directory (HARDCHARGER_DEPS_DIR), e.g.
#
#   HARDCHARGER_CPU_TARGET=baseline HARDCHARGER_DEPS_DIR=$PWD/server/.deps-baseline \
#     server/scripts/build-deps.sh
#   cmake -S server -B server/build-baseline -DHARDCHARGER_DEPS=$PWD/server/.deps-baseline ...
set -euo pipefail
target="${HARDCHARGER_CPU_TARGET:-optimized}"
case "$target" in
  optimized | baseline) echo "$target" ;;
  *) echo "HARDCHARGER_CPU_TARGET must be optimized or baseline, not '$target'" >&2; exit 2 ;;
esac
