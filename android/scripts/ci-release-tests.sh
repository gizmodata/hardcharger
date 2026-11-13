#!/usr/bin/env bash
# Disposable emulator only. The self-instrumenting tests run outside the optimized app.
set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ] || { echo 'CI emulator only' >&2; exit 1; }
finish() {
  local result=$?
  if [ "$result" -ne 0 ]; then
    adb logcat -d > android/build/ci/release-logcat.txt || true
    adb logcat -d -b crash > android/build/ci/release-crashes.txt || true
  fi
  adb shell wm size reset
  adb shell wm density reset
  adb shell settings put system font_scale 1.0
  exit "$result"
}
trap finish EXIT
adb shell am force-stop com.gizmodata.hardcharger
adb install -r android/build/ci/release/app.apk
adb install -r android/build/ci/release/test.apk
for scenario in phone 1280x600:1.0 1280x600:2.0 1280x400:2.0; do
  evidence_name=${scenario//:/-}
  method=optimizedTabletNavigation
  if [ "$scenario" = phone ]; then
    adb shell wm size reset
    adb shell wm density reset
    adb shell settings put system font_scale 1.0
    method=optimizedServerQueriesAndPersistence
  else
    adb shell wm density 160
    adb shell wm size "${scenario%:*}"
    adb shell settings put system font_scale "${scenario#*:}"
  fi
  adb shell am force-stop com.gizmodata.hardcharger
  adb shell pm clear com.gizmodata.hardcharger
  adb shell am instrument -w \
    -e class "com.gizmodata.hardcharger.releasetests.ReleaseWorkflowTest#$method" \
    com.gizmodata.hardcharger.releasetests/androidx.test.runner.AndroidJUnitRunner \
    | tee "android/build/ci/release-$evidence_name.txt"
  mkdir -p "android/build/ci/release-evidence/$evidence_name"
  adb pull /sdcard/Android/data/com.gizmodata.hardcharger.releasetests/files/. \
    "android/build/ci/release-evidence/$evidence_name/" > /dev/null || true
  grep -q '^OK (1 test)' "android/build/ci/release-$evidence_name.txt"
done
