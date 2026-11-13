#!/usr/bin/env bash
# Only for a disposable GitHub runner emulator: this resets application data.
set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ] || { echo 'CI emulator only' >&2; exit 1; }
reset_display() {
  adb shell wm size reset
  adb shell wm density reset
  adb shell settings put system font_scale 1.0
}
trap reset_display EXIT
for distribution in ${HARDCHARGER_TEST_DISTRIBUTIONS:-play local}; do
  [[ "$distribution" == play || "$distribution" == local ]] || exit 1
  reset_display
  adb shell am force-stop com.gizmodata.hardcharger
  adb install -r "android/build/ci/$distribution/app.apk"
  adb install -r "android/build/ci/$distribution/test.apk"
  adb shell pm clear com.gizmodata.hardcharger
  adb shell am instrument -w \
    -e class com.gizmodata.hardcharger.PlayDistributionTest,com.gizmodata.hardcharger.AppWorkflowTest,com.gizmodata.hardcharger.ServerWorkflowTest \
    com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner \
    | tee "android/build/ci/$distribution-instrumentation.txt"
  grep -q '^OK (' "android/build/ci/$distribution-instrumentation.txt"
  # Force a wide, short tablet window; exercise the same physical touch targets
  # users need after opening About and recreating the activity.
  adb shell wm density 160
  for scenario in 1280x600:1.0 1280x600:2.0 1280x400:2.0; do
    size="${scenario%:*}"; scale="${scenario#*:}"
    adb shell wm size "$size"
    adb shell settings put system font_scale "$scale"
    adb shell am force-stop com.gizmodata.hardcharger
    adb shell pm clear com.gizmodata.hardcharger
    adb shell am instrument -w \
      -e class com.gizmodata.hardcharger.AppWorkflowTest#tabletRailDestinationsRemainReachableFromAbout \
      com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner \
      | tee "android/build/ci/$distribution-tablet-$size-$scale.txt"
    grep -q '^OK (1 test)' "android/build/ci/$distribution-tablet-$size-$scale.txt"
  done
done
