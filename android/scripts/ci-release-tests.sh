#!/usr/bin/env bash
# Disposable emulator only. The self-instrumenting tests run outside the optimized app.
set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ] || { echo 'CI emulator only' >&2; exit 1; }

# Retry idempotent setup and evidence collection when the emulator loses its ADB transport.
adb_retry() {
  local attempt output result
  for attempt in 1 2 3; do
    if output=$(timeout --kill-after=2s 30s adb "$@" 2>&1); then
      printf '%s\n' "$output"
      return 0
    else
      result=$?
    fi
    printf '%s\n' "$output" >&2
    if [[ "$result" != 124 && "$result" != 137 ]]; then
      case "$output" in
        *'device offline'*|*'no devices/emulators found'*|*"device '"*"' not found"*|*'- waiting for device -'*) ;;
        *) return "$result" ;;
      esac
    fi
    if [ "$attempt" -lt 3 ]; then
      printf 'ADB transport interrupted during %s (attempt %s/3); trying to reconnect\n' "$1" "$attempt" >&2
      timeout --kill-after=2s 5s adb reconnect offline > /dev/null || true
      timeout --kill-after=2s 15s adb wait-for-device || true
    fi
  done
  return "$result"
}

reset_display() {
  adb_retry shell wm size reset &&
    adb_retry shell wm density reset &&
    adb_retry shell settings put system font_scale 1.0
}

finish() {
  local result=$?
  local cleanup_result=0
  if [ "$result" -ne 0 ]; then
    timeout --kill-after=2s 10s adb logcat -d > android/build/ci/release-logcat.txt || true
    timeout --kill-after=2s 10s adb logcat -d -b crash > android/build/ci/release-crashes.txt || true
  fi
  reset_display || cleanup_result=$?
  exit "$((result != 0 ? result : cleanup_result))"
}
trap finish EXIT
adb_retry shell am force-stop com.gizmodata.hardcharger
adb_retry install -r android/build/ci/release/app.apk
adb_retry install -r android/build/ci/release/test.apk
for scenario in phone 1280x600:1.0 1280x600:2.0 1280x400:2.0; do
  evidence_name=${scenario//:/-}
  method=optimizedTabletNavigation
  if [ "$scenario" = phone ]; then
    reset_display
    method=optimizedServerQueriesAndPersistence
  else
    adb_retry shell wm density 160
    adb_retry shell wm size "${scenario%:*}"
    adb_retry shell settings put system font_scale "${scenario#*:}"
  fi
  adb_retry shell am force-stop com.gizmodata.hardcharger
  adb_retry shell pm clear com.gizmodata.hardcharger
  # An instrumentation run can change app state, so execute it only once per scenario.
  adb shell am instrument -w \
    -e class "com.gizmodata.hardcharger.releasetests.ReleaseWorkflowTest#$method" \
    com.gizmodata.hardcharger.releasetests/androidx.test.runner.AndroidJUnitRunner \
    | tee "android/build/ci/release-$evidence_name.txt"
  mkdir -p "android/build/ci/release-evidence/$evidence_name"
  adb_retry pull /sdcard/Android/data/com.gizmodata.hardcharger.releasetests/files/. \
    "android/build/ci/release-evidence/$evidence_name/" > /dev/null || true
  grep -q '^OK (1 test)' "android/build/ci/release-$evidence_name.txt"
done
