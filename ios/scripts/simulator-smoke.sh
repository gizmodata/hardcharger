#!/usr/bin/env bash
# Runs the embedded server's smoke test (server/embedded/smoke_test.cc), built for the iOS
# simulator, inside one: `xcrun simctl spawn` runs a command-line program in the
# simulator's own iOS userland (its libc, its file system, its sandbox paths).
#
#   simulator-smoke.sh <hardcharger-embedded-smoke built for iphonesimulator>
#
# Uses a booted simulator, else boots HARDCHARGER_IOS_SIMULATOR (a name or UDID; default:
# the first available iPhone) and shuts it down again afterwards.
set -euo pipefail
binary="$1"
booted=$(xcrun simctl list devices booted -j | python3 -c '
import json, sys
for runtime, devices in json.load(sys.stdin)["devices"].items():
    for d in devices:
        if d["state"] == "Booted":
            print(d["udid"]); sys.exit()')
started=""
if [ -z "$booted" ]; then
  want="${HARDCHARGER_IOS_SIMULATOR:-}"
  booted=$(xcrun simctl list devices available -j | WANT="$want" python3 -c '
import json, os, sys
want = os.environ["WANT"]
for runtime, devices in sorted(json.load(sys.stdin)["devices"].items(), reverse=True):
    if "iOS" not in runtime:
        continue
    for d in devices:
        if (want and want in (d["name"], d["udid"])) or (not want and d["name"].startswith("iPhone")):
            print(d["udid"]); sys.exit()')
  [ -n "$booted" ] || { echo "simulator-smoke.sh: no simulator to boot" >&2; exit 1; }
  xcrun simctl boot "$booted"
  started="$booted"
fi
trap '[ -n "$started" ] && xcrun simctl shutdown "$started" >/dev/null 2>&1 || true' EXIT
# The simulator's own temporary directory, which the test may write.
scratch="$(xcrun simctl getenv "$booted" TMPDIR 2>/dev/null || echo /tmp)/hardcharger-smoke"
echo "smoke test in simulator $booted"
xcrun simctl spawn "$booted" "$binary" "$scratch"
