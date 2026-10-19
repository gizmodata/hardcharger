#!/usr/bin/env bash
# Tests for stale-compile-caches.sh, against a fake `gh` that answers from fixtures:
#   bash .github/scripts/test-stale-compile-caches.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

# `gh api <url>`: the fixture for main's list (ref=refs/heads/main) or for every branch's, by
# platform; no fixture is an API error. Every call is logged.
cat > "$work/bin/gh" <<'EOF'
#!/usr/bin/env bash
url="$2"
echo "$url" >> "$FAKE/calls"
platform=$(sed -nE 's/.*key=sccache-v1-([a-z0-9]+-[a-z0-9]+)-.*/\1/p' <<< "$url")
case "$url" in *ref=refs/heads/main*) f="$FAKE/main-$platform.json" ;; *) f="$FAKE/any-$platform.json" ;; esac
[ -f "$f" ] || { echo "HTTP 502" >&2; exit 1; }
cat "$f"
EOF
chmod +x "$work/bin/gh"

NOW=$(jq -n '"2026-10-19T20:00:00Z" | fromdateiso8601')
ago() {  # hours -> an ISO time that many hours before NOW, with fractional seconds as GitHub writes
  jq -nr --argjson t "$((NOW - $1 * 3600))" '$t | todate | sub("Z$"; ".985231Z")'
}
# entries "<key> <hours ago>"... -> a cache list answer (in the order given: newest first)
list() {
  local entries=() key hours
  while [ $# -gt 0 ]; do
    key="$1" hours="$2"; shift 2
    entries+=("{\"key\": \"$key\", \"created_at\": \"$(ago "$hours")\"}")
  done
  local IFS=,
  echo "{\"total_count\": ${#entries[@]}, \"actions_caches\": [${entries[*]-}]}"
}

LIN="sccache-v1-linux-amd64-manylinux-2-28-x86-64-2026.10.10-1-4131ddd7acb9-"
LIN_NEW="sccache-v1-linux-amd64-manylinux-2-28-x86-64-2026.11.01-1-0123456789ab-"
ASAN="sccache-v1-linux-amd64-asan-c-Ubuntu-13.3.0-6ubuntu2-24.04.1-13.3.0-"
TSAN="sccache-v1-linux-amd64-tsan-Ubuntu-clang-version-18.1.3-1ubuntu1-"
WIN="sccache-v1-windows-amd64-MSVC-14.51.36231-"
WIN_NEW="sccache-v1-windows-amd64-MSVC-14.52.10000-"

fails=0
case_dir() { rm -rf "$work/fake"; mkdir -p "$work/fake"; export FAKE="$work/fake"; }
run() {  # expected-warm-json platforms...
  local expected="$1"; shift
  local out
  out=$(PATH="$work/bin:$PATH" NOW="$NOW" RETRY_SLEEP=0 bash "$here/stale-compile-caches.sh" gizmodata/hardcharger "$@" 2> "$work/summary")
  if [ "$out" = "warm=$expected" ]; then
    echo "ok   $name"
  else
    echo "FAIL $name: expected warm=$expected, got '$out'"; cat "$work/summary"; fails=$((fails + 1))
  fi
}

name="main has the current toolchain, 2 days old: fresh"
case_dir
list "${LIN}900-1" 1 "${LIN}800-1" 48 > "$FAKE/any-linux-amd64.json"
list "${LIN}800-1" 48 > "$FAKE/main-linux-amd64.json"
run '[]' linux-amd64

name="main's entry is 8 days old: stale"
case_dir
list "${LIN}900-1" 1 > "$FAKE/any-linux-amd64.json"
list "${LIN}800-1" 192 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="main has no entry: stale"
case_dir
list "${WIN}900-1" 1 > "$FAKE/any-windows-amd64.json"
list > "$FAKE/main-windows-amd64.json"
run '["windows-amd64"]' windows-amd64

name="no entry on any branch (nothing to learn the toolchain from): stale"
case_dir
list > "$FAKE/any-windows-amd64.json"
list > "$FAKE/main-windows-amd64.json"
run '["windows-amd64"]' windows-amd64

name="a branch built with a new toolchain; main's (old) is 2 days old: stale"
case_dir
list "${WIN_NEW}900-1" 1 "${WIN}800-1" 48 > "$FAKE/any-windows-amd64.json"
list "${WIN}800-1" 48 > "$FAKE/main-windows-amd64.json"
run '["windows-amd64"]' windows-amd64

name="a branch built with a new toolchain; main warmed (old toolchain) 3 h ago: fresh, one a day"
case_dir
list "${WIN_NEW}900-1" 1 "${WIN}850-1" 3 > "$FAKE/any-windows-amd64.json"
list "${WIN}850-1" 3 > "$FAKE/main-windows-amd64.json"
run '[]' windows-amd64

name="main has the new toolchain too: fresh"
case_dir
list "${LIN_NEW}900-1" 1 "${LIN}800-1" 30 > "$FAKE/any-linux-amd64.json"
list "${LIN_NEW}890-1" 30 "${LIN}800-1" 30 > "$FAKE/main-linux-amd64.json"
run '[]' linux-amd64

name="the newest entries are the sanitizers': the toolchain comes from the plain build's"
case_dir
list "${ASAN}900-1" 1 "${TSAN}900-1" 1 "${LIN}800-1" 48 > "$FAKE/any-linux-amd64.json"
list "${ASAN}900-1" 1 "${LIN}800-1" 48 > "$FAKE/main-linux-amd64.json"
run '[]' linux-amd64

name="main has only fresh sanitizer entries: stale (they never count, not even as recent)"
case_dir
list "${TSAN}900-1" 1 "${LIN}800-1" 48 > "$FAKE/any-linux-amd64.json"
list "${ASAN}900-1" 1 "${TSAN}900-1" 1 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="only sanitizer entries anywhere: stale"
case_dir
list "${ASAN}900-1" 1 > "$FAKE/any-linux-amd64.json"
list "${ASAN}900-1" 1 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="the branch-wide lookup fails (API error, after the retries): stale"
case_dir
list "${LIN}800-1" 2 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64
tries=$(grep -c 'per_page' "$FAKE/calls")
if [ "$tries" = 4 ]; then
  echo "ok   ...and it was tried 4 times"
else
  echo "FAIL the failed lookup was tried $tries times, not 4"; fails=$((fails + 1))
fi

name="main's lookup fails: stale"
case_dir
list "${LIN}800-1" 2 > "$FAKE/any-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="an answer that isn't the cache list: stale"
case_dir
echo '<html>rate limited</html>' > "$FAKE/any-linux-amd64.json"
list "${LIN}800-1" 2 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="main's answer has no created_at that parses: stale"
case_dir
list "${LIN}800-1" 2 > "$FAKE/any-linux-amd64.json"
echo "{\"actions_caches\": [{\"key\": \"${LIN}800-1\", \"created_at\": \"yesterday\"}]}" > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="a key without the run id and attempt: stale"
case_dir
list "sccache-v1-linux-amd64-manylinux" 1 > "$FAKE/any-linux-amd64.json"
list "sccache-v1-linux-amd64-manylinux" 1 > "$FAKE/main-linux-amd64.json"
run '["linux-amd64"]' linux-amd64

name="five platforms, two stale, in the order given"
case_dir
for p in linux-amd64 linux-arm64 macos-arm64; do
  list "sccache-v1-$p-tool-1-900-1" 5 > "$FAKE/any-$p.json"
  list "sccache-v1-$p-tool-1-900-1" 30 > "$FAKE/main-$p.json"
done
for p in windows-amd64 windows-arm64; do
  list "sccache-v1-$p-MSVC-14.51.36231-900-1" 5 > "$FAKE/any-$p.json"
  list > "$FAKE/main-$p.json"
done
run '["windows-amd64","windows-arm64"]' linux-amd64 linux-arm64 macos-arm64 windows-amd64 windows-arm64

if [ "$fails" -ne 0 ]; then echo "$fails failed"; exit 1; fi
echo "all passed"
