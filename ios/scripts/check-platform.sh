#!/usr/bin/env bash
# Checks that every object in a static library was built for the platform it is for:
#
#   check-platform.sh iphoneos|iphonesimulator <library.a>
#
# A sub-build that ignores the toolchain (a dependency's ExternalProject, say) compiles for
# the Mac, and the app's link then fails, or worse, a member nobody calls slips through.
# Each object's LC_BUILD_VERSION names its platform: 2 is iOS, 7 the iOS simulator.
set -euo pipefail
sdk="$1"
lib="$2"
case "$sdk" in
  iphoneos) want=2 ;;          # PLATFORM_IOS
  iphonesimulator) want=7 ;;   # PLATFORM_IOSSIMULATOR
  *) echo "check-platform.sh: iphoneos or iphonesimulator, not $sdk" >&2; exit 2 ;;
esac
# otool prints a "platform" line per object (one LC_BUILD_VERSION each), as a number.
platforms=$(xcrun otool -l "$lib" 2>/dev/null | awk '/cmd LC_BUILD_VERSION/ {b = 1} b && /platform/ {print $2; b = 0}' |
  sort | uniq -c)
bad=$(echo "$platforms" | awk -v want="$want" '$2 != want && $2 != "" {print}')
if [ -n "$bad" ]; then
  echo "check-platform.sh: $lib has objects for other platforms than $want:" >&2
  echo "$platforms" >&2
  exit 1
fi
[ -n "$platforms" ] || { echo "check-platform.sh: no objects with a platform in $lib" >&2; exit 1; }
echo "check-platform.sh: every object in $(basename "$lib") is for platform $want ($(echo "$platforms" | awk '{s += $1} END {print s}') objects)"
