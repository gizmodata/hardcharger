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
CHARGEWIRE_DEPS_DIR="$DEPS" "$HERE/../third_party/chargewire/cpp/scripts/build-arrow.sh"
# CMake installs libraries into lib64 on RPM-based systems; the server's CMake uses lib.
if [ -d "$DEPS/arrow-install/lib64" ] && [ ! -e "$DEPS/arrow-install/lib" ]; then
  ln -s lib64 "$DEPS/arrow-install/lib"
fi
