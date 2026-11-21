#!/usr/bin/env bash
# Tests select-jobs.sh: which legs and jobs each `only` value keeps (server.yml's changes job
# runs this; act ran the workflow's own expressions on the outputs once, 2026-11-19).
set -euo pipefail
cd "$(dirname "$0")"
LEGS='[{"name":"linux-amd64","runner":"r1","manylinux":"m1"},{"name":"linux-amd64-baseline","runner":"r1","cpu_target":"baseline","qemu_cpu":"kvm64"},{"name":"linux-arm64","runner":"r2","manylinux":"m2"},{"name":"macos-arm64","runner":"r3"}]'
failed=0
check() {  # only, expected "key=value" lines (space-separated)
  local only="$1" out
  shift
  out=$(ONLY="$only" LEGS="$LEGS" ./select-jobs.sh)
  for want in "$@"; do
    if ! grep -qxF -- "$want" <<<"$out"; then
      echo "FAIL only=$only: no line $want in:"; echo "$out"; failed=1
    fi
  done
  echo "ok   only=${only:-<empty>}"
}
names() { jq -c '[.[] | .name]'; }
all_names='["linux-amd64","linux-amd64-baseline","linux-arm64","macos-arm64"]'
out=$(ONLY="" LEGS="$LEGS" ./select-jobs.sh)
[ "$(sed -n 's/^build_legs=//p' <<<"$out" | names)" = "$all_names" ] || { echo "FAIL empty only: not every leg"; failed=1; }
check "" 'windows_platforms=["windows-amd64","windows-arm64"]' run_ui=true run_asan=true run_tsan=true run_ios=true run_android=true run_rest=true
check all 'windows_platforms=["windows-amd64","windows-arm64"]' run_rest=true
check linux-amd64-baseline 'portable_legs=[{"name":"linux-amd64-baseline","runner":"r1","cpu_target":"baseline","qemu_cpu":"kvm64"}]' run_rest=false
check linux-arm64 'build_legs=[{"name":"linux-arm64","runner":"r2","manylinux":"m2"}]' \
  'portable_legs=[{"name":"linux-arm64","runner":"r2","manylinux":"m2"}]' 'windows_platforms=[]' run_ui=false run_rest=false
check macos-arm64 'build_legs=[{"name":"macos-arm64","runner":"r3"}]' 'portable_legs=[]' run_rest=false
check windows-arm64 'build_legs=[]' 'windows_platforms=["windows-arm64"]' run_rest=false
check web-ui 'build_legs=[{"name":"linux-arm64","runner":"r2","manylinux":"m2"}]' run_ui=true run_rest=false
check asan 'build_legs=[]' 'windows_platforms=[]' run_asan=true run_tsan=false run_rest=false
check tsan run_tsan=true run_asan=false
check ios run_ios=true run_android=false
check android run_android=true run_ios=false
check all 'legs={"linux-amd64":[{"name":"linux-amd64","runner":"r1","manylinux":"m1"}],"linux-amd64-baseline":[{"name":"linux-amd64-baseline","runner":"r1","cpu_target":"baseline","qemu_cpu":"kvm64"}],"linux-arm64":[{"name":"linux-arm64","runner":"r2","manylinux":"m2"}],"macos-arm64":[{"name":"macos-arm64","runner":"r3"}]}'
if ONLY=bogus LEGS="$LEGS" ./select-jobs.sh >/dev/null 2>&1; then echo "FAIL an unknown only was accepted"; failed=1; else echo "ok   only=bogus refused"; fi
if ONLY=macos-arm64 LEGS='[{"name":"linux-amd64","runner":"r1"}]' ./select-jobs.sh >/dev/null 2>&1; then
  echo "FAIL a leg missing from LEGS was accepted"; failed=1
else
  echo "ok   a leg missing from LEGS is refused"
fi
[ "$failed" -eq 0 ] && echo "all passed"
exit "$failed"
