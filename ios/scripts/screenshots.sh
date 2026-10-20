#!/usr/bin/env bash
# The app's screenshots (HardchargerScreenshots): every main screen on an iPhone and an iPad
# simulator, light and dark, with a tidy status bar (9:41, full battery), into a directory:
#
#   ios/scripts/screenshots.sh <out dir> [iphone simulator] [ipad simulator]
set -euo pipefail
IOS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mkdir -p "${1:?usage: screenshots.sh <out dir> [iphone] [ipad]}" && cd "$1" && pwd)"
IPHONE="${2:-iPhone 17 Pro}"
IPAD="${3:-iPad Pro 13-inch (M5)}"
udid() {  # the newest iOS runtime's simulator of this name
  xcrun simctl list devices available -j | NAME="$1" python3 -c '
import json, os, sys
for runtime, devices in sorted(json.load(sys.stdin)["devices"].items(), reverse=True):
    if "iOS" in runtime:
        for d in devices:
            if d["name"] == os.environ["NAME"]:
                print(d["udid"]); sys.exit()'
}
for pair in "iphone:$IPHONE" "ipad:$IPAD"; do
  kind="${pair%%:*}"
  device=$(udid "${pair#*:}")
  [ -n "$device" ] || { echo "no simulator named ${pair#*:}" >&2; exit 1; }
  xcrun simctl boot "$device" 2>/dev/null || true
  xcrun simctl bootstatus "$device" -b >/dev/null
  xcrun simctl status_bar "$device" override --time 9:41 --batteryState charged --batteryLevel 100 \
    --wifiBars 3 --cellularBars 4 --dataNetwork wifi
  for appearance in light dark; do
    xcrun simctl ui "$device" appearance "$appearance"
    TEST_RUNNER_HC_SCREENSHOT_DIR="$OUT" TEST_RUNNER_HC_SCREENSHOT_PREFIX="$kind-$appearance" \
      xcodebuild test -project "$IOS/Hardcharger.xcodeproj" -scheme HardchargerScreenshots \
        -destination "id=$device" -derivedDataPath "$IOS/build/DerivedData" -quiet
  done
  xcrun simctl ui "$device" appearance light
  xcrun simctl status_bar "$device" clear
done
ls "$OUT"
