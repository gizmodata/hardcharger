#!/usr/bin/env bash
# Checks the optimized Linux x86_64 tarball's CPU check (src/cpu_check.c, issue #62) under
# QEMU user-mode emulation with chosen CPU models:
#
#   kvm64    (x86-64-v1: QEMU's "Common KVM processor") both programs refuse to start, exit 1,
#            and name the level they need and the baseline download, instead of dying with
#            "Illegal instruction";
#   Nehalem  (x86-64-v2) the client runs (its Arrow needs SSE4.2), the server refuses (its
#            DuckDB needs AVX2);
#   Haswell  (x86-64-v3) both run.
#
#   server/scripts/check-cpu-gate.sh <qemu-x86_64> <hardcharger-linux-amd64.tar.gz>
#
# Run on an x86_64 Linux host (CI: ubuntu-24.04 with qemu-user-static).
set -euo pipefail
qemu="${1:?qemu-x86_64 binary}"
tarball="${2:?the optimized hardcharger-linux-amd64 tarball}"
prefix=$(mktemp -d)
tar -C "$prefix" -xzf "$tarball"
bin=$(echo "$prefix"/hardcharger-*/bin)
[ -x "$bin/hardcharger-server" ] && [ -x "$bin/hardcharger" ] || { echo "no binaries in $tarball"; exit 1; }
failures=0
fail() { echo "FAIL: $*"; failures=$((failures + 1)); }

refuses() {  # cpu program level
  local out status=0
  out=$("$qemu" -cpu "$1" "$bin/$2" --version 2>&1) || status=$?
  echo "-- $2 on $1: exit $status"
  echo "$out" | sed 's/^/   /'
  [ "$status" -eq 1 ] || fail "$2 on $1 exited $status, not 1"
  grep -qF "this build needs an x86-64-v$3 CPU" <<< "$out" || fail "$2 on $1 did not name x86-64-v$3"
  grep -qF "hardcharger-linux-amd64-baseline.tar.gz" <<< "$out" || fail "$2 on $1 did not name the baseline download"
  if grep -qi "illegal instruction" <<< "$out"; then fail "$2 on $1 crashed"; fi
}
runs() {  # cpu program
  local out status=0
  out=$("$qemu" -cpu "$1" "$bin/$2" --version 2>&1) || status=$?
  echo "-- $2 on $1: exit $status"
  echo "$out" | grep -v "TCG doesn't support" | sed 's/^/   /'
  [ "$status" -eq 0 ] || fail "$2 on $1 exited $status"
  grep -q "^Hardcharger" <<< "$out" || fail "$2 on $1 printed no version"
}

refuses kvm64 hardcharger-server 3
refuses kvm64 hardcharger 2
refuses Nehalem hardcharger-server 3
runs Nehalem hardcharger
runs Haswell hardcharger-server
runs Haswell hardcharger
[ "$failures" -eq 0 ] || { echo "$failures CPU check failure(s)"; exit 1; }
echo "CPU checks passed"
