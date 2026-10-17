#!/usr/bin/env bash
# Runs build-portable-linux.sh (its arguments are passed on) inside the manylinux_2_28
# image $HARDCHARGER_MANYLINUX_IMAGE (pinned by .github/workflows/server.yml), with the
# checkout mounted at its own path. When mozilla-actions/sccache-action has run
# ($SCCACHE_PATH; its Linux build is a static binary), sccache is mounted in and the
# GitHub cache service's settings are passed on, so compiles in the container share the
# workflow's compiler cache. The files the build writes are handed back to the caller.
#
#   HARDCHARGER_MANYLINUX_IMAGE=quay.io/pypa/manylinux_2_28_x86_64:<tag>@<digest> \
#     server/scripts/in-manylinux.sh server server/build
set -euo pipefail
image="${HARDCHARGER_MANYLINUX_IMAGE:?the manylinux_2_28 image to build in}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
args=(--rm -v "$ROOT:$ROOT" -w "$ROOT" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)"
      -e CMAKE_BUILD_PARALLEL_LEVEL)
if [ -n "${SCCACHE_PATH:-}" ]; then
  args+=(-v "$SCCACHE_PATH:/usr/local/bin/sccache:ro" -e SCCACHE_GHA_ENABLED -e ACTIONS_CACHE_SERVICE_V2
         -e ACTIONS_RESULTS_URL -e ACTIONS_RUNTIME_TOKEN -e ACTIONS_CACHE_URL)
fi
exec docker run "${args[@]}" "$image" server/scripts/build-portable-linux.sh "$@"
