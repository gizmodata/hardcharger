#!/usr/bin/env bash
# Builds the Hardcharger server for the iOS app: hardcharger_embedded (server/,
# HARDCHARGER_EMBEDDED=ON) and every library it links, cross-compiled for iOS devices
# (iphoneos, arm64) and the simulator on Apple silicon (iphonesimulator, arm64), merged
# into one static library per platform and packaged as
#
#   ios/build/HardchargerServer.xcframework   (the app links it; module HardchargerServer)
#
# The dependencies are server/scripts' own recipes at their pinned versions (OpenSSL,
# Arrow, DuckDB with the same patches and built-in extensions, CRoaring, libpq, libcurl,
# replxx),
# run with HARDCHARGER_IOS_SDK and ios/cmake/ios.toolchain.cmake, so the app runs the same
# server as every other platform. Each platform's build is kept in ios/build/<sdk>/ and
# only redone when its inputs change (the recipes' stamps, ninja).
#
#   ios/scripts/build-server-ios.sh                      # both platforms
#   ios/scripts/build-server-ios.sh iphonesimulator      # just the simulator
#   HARDCHARGER_WEB_UI=<hardcharger-ui-*.tar.gz> ...     # the UI to embed (default: the
#                                                        # newest ui/release/ tarball)
#   HARDCHARGER_IOS_SKIP_SMOKE=1 ...                     # don't run the smoke test
#
# The simulator build's smoke test (server/embedded/smoke_test.cc: start, SELECT 42, a CSV
# into a table, stop, start again) runs in a booted iOS simulator (xcrun simctl spawn), or
# boots one (HARDCHARGER_IOS_SIMULATOR names it; default: the first available iPhone).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/ios/build"
TOOLCHAIN="$ROOT/ios/cmake/ios.toolchain.cmake"
export HARDCHARGER_IOS_DEPLOYMENT_TARGET="${HARDCHARGER_IOS_DEPLOYMENT_TARGET:-18.0}"
JOBS="${CMAKE_BUILD_PARALLEL_LEVEL:-$(sysctl -n hw.ncpu)}"
export CMAKE_BUILD_PARALLEL_LEVEL="$JOBS"
[ "$#" -gt 0 ] || set -- iphonesimulator iphoneos
for sdk in "$@"; do
  case "$sdk" in iphoneos|iphonesimulator) ;; *) echo "build-server-ios.sh: iphoneos or iphonesimulator, not $sdk" >&2; exit 2 ;; esac
done

# Hosted macOS runners export SDKROOT and friends for the macOS SDK; the host tool below
# must build for macOS and the iOS builds name their SDK themselves.
unset SDKROOT IPHONEOS_DEPLOYMENT_TARGET MACOSX_DEPLOYMENT_TARGET TVOS_DEPLOYMENT_TARGET WATCHOS_DEPLOYMENT_TARGET \
  XROS_DEPLOYMENT_TARGET

WEB_UI="${HARDCHARGER_WEB_UI:-}"
if [ -z "$WEB_UI" ]; then
  WEB_UI=$(ls -t "$ROOT"/ui/release/hardcharger-ui-*.tar.gz 2>/dev/null | head -1 || true)
fi
[ -n "$WEB_UI" ] && [ -f "$WEB_UI" ] || {
  echo "no web UI tarball: build it (cd ui && npm ci && npm run build && npm run package) or set HARDCHARGER_WEB_UI" >&2
  exit 1
}

# Build-time tools a cross build can't run: chargewire's chargewire_openapi_json, compiled for
# the Mac; ios/scripts/run-host-tool.sh (CMAKE_CROSSCOMPILING_EMULATOR) runs it in place
# of the iOS binary CMake builds.
HOST_TOOLS="$BUILD/host-tools"
mkdir -p "$HOST_TOOLS"
OPENAPI_SRC="$ROOT/third_party/chargewire/cpp/tools/openapi_json.cc"
if [ ! -x "$HOST_TOOLS/chargewire_openapi_json" ] || [ "$OPENAPI_SRC" -nt "$HOST_TOOLS/chargewire_openapi_json" ]; then
  xcrun --sdk macosx clang++ -std=c++20 -O2 "$OPENAPI_SRC" -o "$HOST_TOOLS/chargewire_openapi_json"
fi

for sdk in "$@"; do
  echo "=== $sdk ==="
  OUT="$BUILD/$sdk"
  DEPS="$OUT/deps"
  mkdir -p "$DEPS"
  (
    export HARDCHARGER_IOS_SDK="$sdk"
    export CMAKE_TOOLCHAIN_FILE="$TOOLCHAIN"
    export HARDCHARGER_DEPS_DIR="$DEPS"
    export OPENSSL_ROOT_DIR="$DEPS/openssl-install"
    # replxx: the command-line client's line editor, which its library (the app's SQL
    # console) links; the console itself never reads a terminal.
    "$ROOT/server/scripts/build-deps.sh" openssl replxx
    # A stamp for Arrow too: build-arrow.sh reconfigures and reinstalls every time.
    if [ ! -f "$DEPS/arrow-install/.ios-arrow.stamp" ]; then
      "$ROOT/server/scripts/build-deps.sh" arrow
      touch "$DEPS/arrow-install/.ios-arrow.stamp"
    fi
    # DuckDB: the stamp names the pinned commit and the recipe files, so a bump rebuilds.
    duckdb_key=$( (sed -n 's/^DUCKDB_COMMIT=//p' "$ROOT/third_party/chargewire/cpp/scripts/build-duckdb.sh";
                   cat "$ROOT"/server/scripts/patch-duckdb*.cmake "$ROOT/server/scripts/duckdb-extensions-ios.cmake" \
                       "$ROOT/server/scripts/retire-ducklake-patch.cmake" \
                       "$ROOT/server/scripts/deps-duckdb.sh" "$ROOT/server/scripts/build-extension-deps.sh") \
                 | shasum -a 256 | cut -c1-16)
    if [ ! -f "$DEPS/duckdb-install/.ios-duckdb-$duckdb_key.stamp" ]; then
      "$ROOT/server/scripts/build-deps.sh" duckdb
      touch "$DEPS/duckdb-install/.ios-duckdb-$duckdb_key.stamp"
    fi
  )

  # The server, cross-compiled, statically linked with DuckDB and its extensions.
  HARDCHARGER_IOS_SDK="$sdk" cmake -S "$ROOT/server" -B "$OUT/server" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_CROSSCOMPILING_EMULATOR="$ROOT/ios/scripts/run-host-tool.sh" \
    -DCMAKE_BUILD_TYPE=Release \
    -DHARDCHARGER_EMBEDDED=ON -DHARDCHARGER_STATIC=ON \
    -DHARDCHARGER_DEPS="$DEPS" \
    -DOPENSSL_ROOT_DIR="$DEPS/openssl-install" -DOPENSSL_USE_STATIC_LIBS=TRUE \
    -DHARDCHARGER_WEB_UI="$WEB_UI" >"$OUT/server-configure.log" || { cat "$OUT/server-configure.log"; exit 1; }
  HARDCHARGER_IOS_SDK="$sdk" cmake --build "$OUT/server" --target hardcharger-embedded-smoke

  # Every archive the smoke test links is what the app needs: merged into one library.
  # The link line comes from ninja; its -framework and -l flags are the app's to link
  # (project.yml lists them; this checks they haven't changed).
  link=$(ninja -C "$OUT/server" -t commands hardcharger-embedded-smoke | tail -1)
  archives=()
  for word in $link; do
    case "$word" in
      *.a) [ "${word#/}" != "$word" ] && archives+=("$word") || archives+=("$OUT/server/$word") ;;
    esac
  done
  lib="$OUT/libHardchargerServer.a"
  rm -f "$lib"
  libtool -static -no_warning_for_no_symbols -o "$lib" "${archives[@]}" 2>&1 | grep -v "same member name" || true
  echo "$link" | tr ' ' '\n' | grep -E '^-framework$|^-l' -A1 | grep -v '^--$' | paste -sd' ' - >"$OUT/system-libraries.txt"
  echo "$sdk: $(du -h "$lib" | cut -f1) libHardchargerServer.a from ${#archives[@]} archives; system: $(cat "$OUT/system-libraries.txt")"
  "$ROOT/ios/scripts/check-platform.sh" "$sdk" "$lib"
done

# The XCFramework: each platform's library with the C header and a module map, so Swift
# says `import HardchargerServer`.
HEADERS="$BUILD/headers"
rm -rf "$HEADERS" && mkdir -p "$HEADERS"
cp "$ROOT/server/embedded/hardcharger_embedded.h" "$HEADERS/"
cat >"$HEADERS/module.modulemap" <<'EOF'
module HardchargerServer {
  header "hardcharger_embedded.h"
  link "c++"
  export *
}
EOF
args=()
for sdk in iphoneos iphonesimulator; do
  [ -f "$BUILD/$sdk/libHardchargerServer.a" ] && args+=(-library "$BUILD/$sdk/libHardchargerServer.a" -headers "$HEADERS")
done
rm -rf "$BUILD/HardchargerServer.xcframework"
xcodebuild -create-xcframework "${args[@]}" -output "$BUILD/HardchargerServer.xcframework" >/dev/null
echo "built $BUILD/HardchargerServer.xcframework"

# The smoke test, in the simulator.
# (CMake builds an iOS program as an app bundle.)
SMOKE="$BUILD/iphonesimulator/server/hardcharger-embedded-smoke.app/hardcharger-embedded-smoke"
if [ -z "${HARDCHARGER_IOS_SKIP_SMOKE:-}" ] && [ -x "$SMOKE" ]; then
  "$ROOT/ios/scripts/simulator-smoke.sh" "$SMOKE"
fi
