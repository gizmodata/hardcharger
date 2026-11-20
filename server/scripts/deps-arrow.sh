#!/usr/bin/env bash
# Builds Arrow C++ (IPC only) into server/.deps/arrow-install with chargewire's
# build-arrow.sh. `build-deps.sh arrow` runs it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
if [ "$(uname)" = Linux ]; then
  # Arrow's bundled uriparser calls reallocarray when CMake finds it. glibc 2.28 (the
  # portable build's) has it but declares it only under _GNU_SOURCE (2.29 moved it to
  # _DEFAULT_SOURCE, which is all uriparser asks for), and gcc 14 refuses the implicit
  # declaration. Arrow's C code (uriparser, lz4, zstd) is built with _GNU_SOURCE, as g++
  # builds all C++ anyway.
  export CFLAGS="${CFLAGS:-} -D_GNU_SOURCE"
fi
if [ -n "${HARDCHARGER_ANDROID_ABI:-}" ]; then
  # Arrow includes visibility.h (and tz.h) before tz.cpp defines BUILD_TZ_LIB.
  # Android needs that define for the tzdb forward declaration and friend access.
  # The NDK can initialize CMAKE_CXX_FLAGS before CMake reads CXXFLAGS on a clean
  # configure. Apply this as a compile definition in the Android toolchain instead.
  export HARDCHARGER_ANDROID_ARROW=ON
fi
# HARDCHARGER_CPU_TARGET (server/scripts/cpu-target.sh): optimized keeps Arrow's default SIMD
# level (SSE4.2 on x86_64), baseline builds for any x86-64 CPU. Always passed explicitly, so a
# reused build directory never keeps the other target's setting.
CPU_TARGET=$("$HERE/scripts/cpu-target.sh")
SIMD_LEVEL=DEFAULT
[ "$CPU_TARGET" != baseline ] || SIMD_LEVEL=NONE
CHARGEWIRE_ARROW_SIMD_LEVEL="$SIMD_LEVEL" CHARGEWIRE_DEPS_DIR="$DEPS" \
  "$HERE/../third_party/chargewire/cpp/scripts/build-arrow.sh"
# CMake installs libraries into lib64 on RPM-based systems; the server's CMake uses lib.
if [ -d "$DEPS/arrow-install/lib64" ] && [ ! -e "$DEPS/arrow-install/lib" ]; then
  ln -s lib64 "$DEPS/arrow-install/lib"
fi
# What the server's CMake reads to know which CPUs this Arrow runs on (cpu-target.sh).
mkdir -p "$DEPS/arrow-install/share"
echo "$CPU_TARGET" > "$DEPS/arrow-install/share/hardcharger-cpu-target"
