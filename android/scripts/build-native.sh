#!/usr/bin/env bash
# Cross-compile the real server and console; do not substitute a different DuckDB engine.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$ANDROID_HOME/ndk/28.2.13676358}"
export HARDCHARGER_ANDROID_ABI="${1:-arm64-v8a}"
distribution="${2:-local}"
case "$distribution" in local|play) ;; *) echo 'Distribution must be local or play' >&2; exit 2;; esac
play=OFF
[ "$distribution" != play ] || play=ON
case "$HARDCHARGER_ANDROID_ABI" in arm64-v8a|x86_64) ;; *) echo 'Supported ABIs: arm64-v8a, x86_64' >&2; exit 2;; esac
export CMAKE_TOOLCHAIN_FILE="$ROOT/android/cmake/android.toolchain.cmake"
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-8}"
export HARDCHARGER_DEPS_DIR="$ROOT/android/build/$HARDCHARGER_ANDROID_ABI/deps"
export CHARGEWIRE_DEPS_DIR="$HARDCHARGER_DEPS_DIR"
export OPENSSL_ROOT_DIR="$HARDCHARGER_DEPS_DIR/openssl-install"
OUT="$ROOT/android/build/$HARDCHARGER_ANDROID_ABI"
[ "$distribution" != play ] || OUT="$OUT/play"
JNI_DIR="$ROOT/android/app/src/main/jniLibs"
[ "$distribution" != play ] || JNI_DIR="$ROOT/android/app/src/play/jniLibs"
[ -d "$ANDROID_NDK_HOME" ] || { echo "Missing NDK: $ANDROID_NDK_HOME" >&2; exit 1; }
mkdir -p "$OUT" "$ROOT/android/build/host-tools"
"$ROOT/server/scripts/build-deps.sh" openssl replxx
recipe_key() {
  { cat "$ANDROID_NDK_HOME/source.properties" "$CMAKE_TOOLCHAIN_FILE"; cat "$@"; } | shasum -a 256 | cut -c1-16
}
arrow_key=$(recipe_key "$ROOT/server/scripts/deps-arrow.sh" "$ROOT/third_party/chargewire/cpp/scripts/build-arrow.sh")
if [ ! -f "$HARDCHARGER_DEPS_DIR/arrow-install/.android-$arrow_key.stamp" ]; then
  "$ROOT/server/scripts/build-deps.sh" arrow
  touch "$HARDCHARGER_DEPS_DIR/arrow-install/.android-$arrow_key.stamp"
fi
duckdb_key=$(recipe_key "$ROOT/server/scripts/deps-duckdb.sh" "$ROOT/server/scripts/build-extension-deps.sh" \
  "$ROOT/server/scripts/duckdb-extensions-ios.cmake" "$ROOT/server/scripts/duckdb-extensions-android.cmake" \
  "$ROOT"/server/scripts/patch-duckdb*.cmake "$ROOT/server/scripts/retire-ducklake-patch.cmake" \
  "$ROOT/third_party/chargewire/cpp/scripts/build-duckdb.sh")
if [ ! -f "$HARDCHARGER_DEPS_DIR/duckdb-install/.android-$duckdb_key.stamp" ]; then
  "$ROOT/server/scripts/build-deps.sh" duckdb
  touch "$HARDCHARGER_DEPS_DIR/duckdb-install/.android-$duckdb_key.stamp"
fi
[ "${HARDCHARGER_ANDROID_DEPS_ONLY:-0}" != 1 ] || exit 0
WEB_UI="${HARDCHARGER_WEB_UI:-}"
if [ -z "$WEB_UI" ]; then WEB_UI=$(ls -t "$ROOT"/ui/release/hardcharger-ui-*.tar.gz 2>/dev/null | head -1 || true); fi
[ -f "$WEB_UI" ] || { echo 'Build ui/ and npm run package, or set HARDCHARGER_WEB_UI.' >&2; exit 1; }
# This generator runs on the build host, not Android.
CMAKE_TOOLCHAIN_FILE= c++ -std=c++20 -O2 "$ROOT/third_party/chargewire/cpp/tools/openapi_json.cc" -o "$ROOT/android/build/host-tools/chargewire_openapi_json"
cmake -S "$ROOT/server" -B "$OUT/server" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$CMAKE_TOOLCHAIN_FILE" \
  -DCMAKE_CROSSCOMPILING_EMULATOR="$ROOT/android/scripts/run-host-tool.sh" \
  -DCMAKE_BUILD_TYPE=Release -DHARDCHARGER_EMBEDDED=ON -DHARDCHARGER_STATIC=ON \
  -DHARDCHARGER_ANDROID_PLAY="$play" \
  -DHARDCHARGER_DEPS="$HARDCHARGER_DEPS_DIR" \
  -DOPENSSL_ROOT_DIR="$OPENSSL_ROOT_DIR" -DOPENSSL_USE_STATIC_LIBS=TRUE \
  -DHARDCHARGER_WEB_UI="$WEB_UI"
cmake --build "$OUT/server" --target hardcharger-embedded-smoke hardcharger_android
mkdir -p "$JNI_DIR/$HARDCHARGER_ANDROID_ABI"
cp "$OUT/server/libhardcharger_android.so" "$JNI_DIR/$HARDCHARGER_ANDROID_ABI/"

# Keep debug symbols in build/<abi>/server for diagnosis; ship only loadable code.
case "$(uname)" in Darwin) ndk_host=darwin-x86_64 ;; *) ndk_host=linux-x86_64 ;; esac
"$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/$ndk_host/bin/llvm-strip" --strip-unneeded \
  "$JNI_DIR/$HARDCHARGER_ANDROID_ABI/libhardcharger_android.so"
python3 "$ROOT/android/scripts/check-native-distribution.py" \
  "$OUT/server/libhardcharger_android.so" "$JNI_DIR/$HARDCHARGER_ANDROID_ABI/libhardcharger_android.so" \
  "$distribution" "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/$ndk_host/bin/llvm-nm" \
  "$JNI_DIR/$HARDCHARGER_ANDROID_ABI.json"
