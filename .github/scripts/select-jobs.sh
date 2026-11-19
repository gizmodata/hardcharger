#!/usr/bin/env bash
# Which of server.yml's jobs and legs a run runs, for its `changes` job: prints GITHUB_OUTPUT
# lines. ONLY is the manual run's `only` input ("all", or empty on every other event: then
# nothing is restricted and each job's own conditions decide, as before). LEGS is the build
# job's legs (a JSON list of {name, runner, manylinux?}), the one place they are defined.
#
#   only=all          everything the run would run anyway
#   only=linux-amd64  one build leg (and its glibc 2.28 install test); likewise linux-arm64,
#                     macos-arm64
#   only=windows-amd64, windows-arm64   one Windows leg (windows.yml)
#   only=web-ui       the UI's checks and browser tests (with the Linux arm64 build they use)
#   only=asan, tsan   that sanitizer job alone (without `full`; asan is ASan with UBSan)
#   only=ios, android        that app's jobs alone (without the `ios` / `android` input)
#
# Outputs: build_legs (JSON list of legs), portable_legs (the Linux ones of those), legs (every
# leg by name, for the compile-cache warm-up), windows_platforms (JSON list), and one
# true/false per optional job, which only narrows that job's own conditions: run_ui, run_asan,
# run_tsan, run_ios, run_android, and run_rest (the image, the signature checks and
# everything that packages a release: only when nothing is restricted).
set -euo pipefail
only="${ONLY:-all}"
legs="${LEGS:?the build legs, as JSON}"
jq -e 'type == "array" and length > 0 and all(.[]; .name and .runner)' <<<"$legs" >/dev/null \
  || { echo "LEGS is not a list of {name, runner}: $legs" >&2; exit 2; }

windows_all='["windows-amd64","windows-arm64"]'
pick() { jq -c --arg name "$1" '[.[] | select(.name == $name)]' <<<"$legs"; }
ui=false asan=false tsan=false ios=false android=false rest=false
build='[]' windows='[]'
case "$only" in
  all)
    build="$legs" windows="$windows_all"
    ui=true asan=true tsan=true ios=true android=true rest=true
    ;;
  linux-amd64 | linux-arm64 | macos-arm64) build=$(pick "$only") ;;
  windows-amd64 | windows-arm64) windows="[\"$only\"]" ;;
  web-ui) build=$(pick linux-arm64) ui=true ;;
  asan) asan=true ;;
  tsan) tsan=true ;;
  ios) ios=true ;;
  android) android=true ;;
  *) echo "unknown value for only: '$only'" >&2; exit 2 ;;
esac
# A leg asked for (or the one web-ui uses) that LEGS lacks is a mistake here, not an empty run.
if [[ "$only" == linux-* || "$only" == macos-* || "$only" == web-ui ]] && [ "$(jq length <<<"$build")" -eq 0 ]; then
  echo "LEGS has no build leg for only=$only" >&2
  exit 2
fi
echo "build_legs=$(jq -c . <<<"$build")"
echo "portable_legs=$(jq -c '[.[] | select(.name | startswith("linux-"))]' <<<"$build")"
echo "legs=$(jq -c 'map({(.name): [.]}) | add' <<<"$legs")"
echo "windows_platforms=$windows"
echo "run_ui=$ui"
echo "run_asan=$asan"
echo "run_tsan=$tsan"
echo "run_ios=$ios"
echo "run_android=$android"
echo "run_rest=$rest"
