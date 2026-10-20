#!/usr/bin/env bash
# Generates ios/Hardcharger.xcodeproj (xcodegen, from ios/project.yml), after writing
# ios/build/Version.xcconfig from the server's version (server/CMakeLists.txt):
#
#   MARKETING_VERSION        X.Y.Z (App Store versions are numbers only)
#   CURRENT_PROJECT_VERSION  the build number: N for a release candidate X.Y.Z-rc.N,
#                            100 for the release; HARDCHARGER_IOS_BUILD_NUMBER overrides it
#                            (CI: a number that grows with every upload of one version)
#   HARDCHARGER_VERSION      the full version, X.Y.Z or X.Y.Z-rc.N (the app shows the
#                            server's own, from the library)
set -euo pipefail
IOS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMAKE="$IOS/../server/CMakeLists.txt"
numbers=$(sed -n 's/^project(hardcharger VERSION \([0-9.]*\).*/\1/p' "$CMAKE")
suffix=$(sed -n 's/^set(HARDCHARGER_VERSION_SUFFIX "\([^"]*\)")$/\1/p' "$CMAKE")
[ -n "$numbers" ] || { echo "no project(hardcharger VERSION ...) in $CMAKE" >&2; exit 1; }
case "$suffix" in
  "") build=100 ;;
  -rc.*) build="${suffix#-rc.}" ;;
  *) echo "unexpected HARDCHARGER_VERSION_SUFFIX '$suffix'" >&2; exit 1 ;;
esac
build="${HARDCHARGER_IOS_BUILD_NUMBER:-$build}"
mkdir -p "$IOS/build"
cat >"$IOS/build/Version.xcconfig" <<EOF
// Written by ios/scripts/generate-project.sh from server/CMakeLists.txt. Do not edit.
MARKETING_VERSION = $numbers
CURRENT_PROJECT_VERSION = $build
HARDCHARGER_VERSION = $numbers$suffix
EOF
echo "version $numbers$suffix (marketing $numbers, build $build)"
command -v xcodegen >/dev/null || { echo "xcodegen is not installed (brew install xcodegen)" >&2; exit 1; }
xcodegen generate --spec "$IOS/project.yml" --project "$IOS" --quiet
echo "generated $IOS/Hardcharger.xcodeproj"
