#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
if [ ! -d "$ROOT/ui/node_modules" ]; then (cd "$ROOT/ui" && npm ci); fi
(cd "$ROOT/ui" && npm run build && npm run package)
"$ROOT/android/scripts/build-native.sh" "${1:-arm64-v8a}"
(cd "$ROOT/android" && ./gradlew :app:testDebugUnitTest :app:assembleDebug :app:assembleDebugAndroidTest)
