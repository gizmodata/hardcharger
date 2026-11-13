#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
# Instrumentation intentionally exercises credentials, settings and database state.
# Give it a dedicated emulator, preserving the phone used to try the product.
export HARDCHARGER_AVD="${HARDCHARGER_TEST_AVD:-Hardcharger_Test}"
export HARDCHARGER_EMULATOR_PORT="${HARDCHARGER_TEST_PORT:-5558}"
export ANDROID_SERIAL="emulator-$HARDCHARGER_EMULATOR_PORT"
export HARDCHARGER_HOST_PORT=31438
manager="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"
[ -x "$manager" ] || manager="$ANDROID_HOME/cmdline-tools/19.0/bin/avdmanager"
if ! "$ANDROID_HOME/emulator/emulator" -list-avds | grep -qx "$HARDCHARGER_AVD"; then
  echo no | "$manager" create avd --name "$HARDCHARGER_AVD" \
    --package 'system-images;android-36;google_apis;arm64-v8a' --device pixel_7
fi
(cd "$ROOT/android" && ./gradlew :app:testDebugUnitTest :app:assembleDebug :app:assembleDebugAndroidTest :app:lintDebug)
"$ROOT/android/scripts/run-emulator.sh"
"$ANDROID_HOME/platform-tools/adb" -s "$ANDROID_SERIAL" install -r "$ROOT/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"
report="$ROOT/android/build/instrumentation-tests.txt"
"$ANDROID_HOME/platform-tools/adb" -s "$ANDROID_SERIAL" shell am instrument -w \
  com.gizmodata.hardcharger.test/androidx.test.runner.AndroidJUnitRunner | tee "$report"
grep -q '^OK (' "$report" || { echo 'Instrumentation did not pass.' >&2; exit 1; }
