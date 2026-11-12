#!/usr/bin/env bash
# Only for a disposable GitHub runner emulator: this resets application data.
set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ] || { echo 'CI emulator only' >&2; exit 1; }
for distribution in play local; do
  adb shell am force-stop com.gizmodata.hardcharger
  adb install -r "android/build/ci/$distribution/app.apk"
  adb install -r "android/build/ci/$distribution/test.apk"
  adb shell pm clear com.gizmodata.hardcharger
  adb shell am instrument -w \
    -e class com.gizmodata.hardcharger.PlayDistributionTest,com.gizmodata.hardcharger.AppWorkflowTest,com.gizmodata.hardcharger.ServerWorkflowTest \
    com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner \
    | tee "android/build/ci/$distribution-instrumentation.txt"
  grep -q '^OK (' "android/build/ci/$distribution-instrumentation.txt"
done
