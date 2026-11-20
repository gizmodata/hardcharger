#!/usr/bin/env bash
set -euo pipefail
mkdir -p android/build/ci
adb shell wm size 320x640
adb shell wm density 160
adb shell settings put system font_scale 1.0
adb shell settings put secure show_ime_with_hard_keyboard 1
adb install -r android/build/ci/local/app.apk
adb install -r android/build/ci/local/test.apk
run_one() {
  adb shell am force-stop com.gizmodata.hardcharger
  adb shell pm clear com.gizmodata.hardcharger
  adb shell am instrument -w -e class com.gizmodata.hardcharger.AppWorkflowTest#consoleNavigationRestartConfirmationAndSettings \
    com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner | tee "$1"
}
for n in $(seq 1 20); do
  run_one "android/build/ci/console-fixed-$n.txt"
  grep -q '^OK (1 test)' "android/build/ci/console-fixed-$n.txt"
done
test_source=android/app/src/androidTest/java/com/gizmodata/hardcharger/AppWorkflowTest.kt
cp "$test_source" "$RUNNER_TEMP/AppWorkflowTest.fixed.kt"
python3 - <<'PY'
from pathlib import Path
p = Path('android/app/src/androidTest/java/com/gizmodata/hardcharger/AppWorkflowTest.kt')
s = p.read_text()
line = '        androidx.test.espresso.Espresso.closeSoftKeyboard()\n'
assert s.count(line) == 1
p.write_text(s.replace(line, ''))
PY
android/gradlew -p android :app:assembleDebugAndroidTest
adb install -r android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
reproduced=false
for n in $(seq 1 5); do
  run_one "android/build/ci/console-mutation-$n.txt"
  if grep -q 'ComposeTimeoutException' "android/build/ci/console-mutation-$n.txt"; then
    adb logcat -d -s ConsoleWorkflowFailure > android/build/ci/console-mutation-tree.txt
    cat android/build/ci/console-mutation-tree.txt
    reproduced=true
    break
  fi
  grep -q '^OK (1 test)' "android/build/ci/console-mutation-$n.txt"
done
test "$reproduced" = true
cp "$RUNNER_TEMP/AppWorkflowTest.fixed.kt" "$test_source"
adb install -r android/build/ci/local/test.apk
run_one android/build/ci/console-restored.txt
grep -q '^OK (1 test)' android/build/ci/console-restored.txt
