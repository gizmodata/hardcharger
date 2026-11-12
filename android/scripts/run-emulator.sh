#!/usr/bin/env bash
# Start the local ARM64 phone, install the app, and expose its server to this Mac only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
AVD="${HARDCHARGER_AVD:-Hardcharger_Phone}"
ADB="$SDK/platform-tools/adb"
PORT="${HARDCHARGER_EMULATOR_PORT:-5554}"
SERIAL="${ANDROID_SERIAL:-emulator-$PORT}"
HOST_PORT="${HARDCHARGER_HOST_PORT:-31437}"
APK="$ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK" ] || { echo 'Build the app first: android/scripts/build.sh' >&2; exit 1; }
# avdmanager's phone profiles disable the host keyboard by default. Enable it before
# boot so the local preview accepts the Mac keyboard as well as the on-screen keyboard.
python3 - "$AVD" "${ANDROID_AVD_HOME:-${ANDROID_USER_HOME:-$HOME/.android}/avd}" <<'PY'
from pathlib import Path
import re
import sys
name, avd_home = sys.argv[1:]
metadata = Path(avd_home) / f"{name}.ini"
if metadata.exists():
    values = dict(line.split("=", 1) for line in metadata.read_text().splitlines() if "=" in line)
    directory = Path(values.get("path", str(Path(avd_home) / f"{name}.avd")))
else:
    directory = Path(avd_home) / f"{name}.avd"
config = directory / "config.ini"
if config.exists():
    original = config.read_text()
    updated, count = re.subn(r"(?m)^hw\.keyboard\s*=.*$", "hw.keyboard = yes", original)
    if not count:
        updated = original.rstrip() + "\nhw.keyboard = yes\n"
    if updated != original:
        config.write_text(updated)
        print("Host keyboard enabled for this virtual device (takes effect at next emulator boot).")
PY
if ! "$ADB" -s "$SERIAL" get-state >/dev/null 2>&1; then
  mkdir -p "$ROOT/android/build"
  nohup "$SDK/emulator/emulator" -avd "$AVD" -port "$PORT" -no-snapshot -no-boot-anim >"$ROOT/android/build/emulator.log" 2>&1 &
fi
for attempt in $(seq 1 120); do
  [ "$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != 1 ] || break
  sleep 1
done
[ "$("$ADB" -s "$SERIAL" shell getprop sys.boot_completed | tr -d '\r')" = 1 ] || { echo 'Emulator did not boot in two minutes.' >&2; exit 1; }
"$ADB" -s "$SERIAL" install -r "$APK"
"$ADB" -s "$SERIAL" forward "tcp:$HOST_PORT" tcp:31337
"$ADB" -s "$SERIAL" shell am start -n com.gizmodata.hardcharger/.MainActivity
printf 'Hardcharger is installed. The Mac can reach the device server at http://127.0.0.1:%s (https when TLS/sharing is enabled).\n' "$HOST_PORT"
