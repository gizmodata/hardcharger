#!/usr/bin/env bash
# Prints the CMake arguments for compiling through sccache, after starting its server and
# checking that a compile goes through it. When sccache is missing, or its server won't
# start or answer, it prints arguments that turn the launcher off and exits 1: the build
# then runs without the cache rather than failing.
#
# Why start the server first: when a build's first compiles start it themselves, ninja runs
# many at once, one of them starts the server and the rest give up after five seconds of
# connection retries if it isn't listening yet. On 2026-10-18 a restored cache made the
# server slow to start, and every compile of a CI build failed with "Connection to server
# timed out ... Connection refused". Here a single client starts it, with a longer wait.
#
#   launcher=($(server/scripts/sccache-launcher.sh))
#   cmake -S server -B server/build "${launcher[@]}" ...
set -u

off="-DCMAKE_C_COMPILER_LAUNCHER= -DCMAKE_CXX_COMPILER_LAUNCHER="
if ! command -v sccache >/dev/null 2>&1; then
  echo "$off"
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
conf="$work/sccache.conf"
printf 'server_startup_timeout_ms = 120000\n' > "$conf"
# sccache is a native program on Windows (Git Bash): give it a Windows path.
if command -v cygpath >/dev/null 2>&1; then conf="$(cygpath -m "$conf")"; fi

# One compile through sccache, with the compiler the build uses: MSVC's cl takes its own
# flags (and a file: it can't read stdin), any other compiler the gcc-style ones.
probe() {
  case "${CC:-cc}" in
    cl | cl.exe)
      printf 'int sccache_probe;\n' > "$work/probe.c"
      (cd "$work" && sccache cl -nologo -c probe.c -Foprobe.obj) >&2 ;;
    *)
      printf 'int sccache_probe;\n' | sccache "${CC:-cc}" -x c -c - -o /dev/null >&2 ;;
  esac
}

for attempt in 1 2 3; do
  # Fails with "Address in use" when a server already runs; the probe below decides.
  SCCACHE_CONF="$conf" sccache --start-server >&2 || true
  if probe; then
    echo "-DCMAKE_C_COMPILER_LAUNCHER=sccache -DCMAKE_CXX_COMPILER_LAUNCHER=sccache"
    exit 0
  fi
  echo "sccache: attempt $attempt to reach the server failed" >&2
  sccache --stop-server >/dev/null 2>&1 || true
  sleep 5
done

echo "sccache's server did not answer; building without the compiler cache" >&2
echo "$off"
exit 1
