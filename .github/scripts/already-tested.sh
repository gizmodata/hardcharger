#!/usr/bin/env bash
# Whether a commit already passed this workflow's full matrix (server.yml), so a push of it
# to main need not test it again: main moves by fast-forwarding a branch whose head ran the
# matrix by workflow_dispatch.
#
#   already-tested.sh <owner/repo> <sha> [<this run's id>]
#
# Prints `tested=true` and `run=<id>` (the run whose binaries the image job then uses), or
# `tested=false` and the reason, as GITHUB_OUTPUT lines; a Markdown summary goes to stderr.
# Any doubt (an API error, a missing job, an expired artifact) answers false: the commit is
# then tested again, never skipped by mistake.
#
# A run counts when: it ran server.yml on exactly this commit (a push or a workflow_dispatch;
# never a pull_request, which tests a merge commit, not this one), it concluded success,
# every job listed in REQUIRED below succeeded in its latest attempt, every other job
# succeeded or is one that only tags (or a `full` run) start, and the Linux tarballs it
# built are still downloadable. A run that itself skipped its tests never counts: its build
# jobs are skipped, not successful.
set -uo pipefail

repo="${1:?owner/repo}"
sha="${2:?commit SHA}"
self="${3:-0}"

REQUIRED=(
  "web UI (build)"
  "web UI (lint, typecheck, unit tests, client provenance)"
  "build and test (linux-amd64)"
  "build and test (linux-arm64)"
  "build and test (macos-arm64)"
  "install on glibc 2.28 (linux-amd64)"
  "install on glibc 2.28 (linux-arm64)"
  "web UI e2e (chromium)"
  "web UI e2e (webkit)"
  "windows / build and test (windows-amd64)"
  "windows / build and test (windows-arm64)"
  "windows / msi (windows-amd64)"
  "windows / msi (windows-arm64)"
  "checksums"
  "image"
)
# Jobs a branch run skips by design: the release (tags only) and the sanitizers (tags, or a
# manual run with `full`). And the compile-cache warm-up jobs, which run only when a push to
# main skips the tests (a skipped matrix job is listed under its unexpanded name, a skipped
# call of a reusable workflow under its job id): see may_skip().
MAY_SKIP=("release" "address sanitizer (linux-amd64)" "thread sanitizer (linux-amd64)" "windows-warm")
ARTIFACTS=("hardcharger-linux-amd64" "hardcharger-linux-arm64")

answer() {  # tested run reason
  echo "tested=$1"
  echo "run=$2"
  echo "reason=$3"
  {
    if [ "$1" = true ]; then
      echo "### Tests skipped: ${sha:0:12} already passed the full matrix"
      echo
      echo "Run [$2](https://github.com/$repo/actions/runs/$2) tested this exact commit and every job in it passed."
      echo "This run builds the image from that run's Linux tarballs (the binaries that were tested) and runs no tests."
    else
      echo "### Full matrix: ${sha:0:12} has no earlier passing run"
      echo
      echo "$3"
    fi
  } >&2
  exit 0
}

# gh api, retried: the jobs endpoint answers 502 now and then (2026-10-19: for minutes, on
# some pages of a run and not others), and a failure here only costs a full run.
# Only a complete answer is printed (a failed --paginate may print some pages first).
api() {
  local attempt out
  for attempt in 1 2 3 4; do
    if out=$(gh api "$@"); then printf '%s\n' "$out"; return 0; fi
    [ "$attempt" -lt 4 ] && sleep $(( attempt * 10 ))
  done
  return 1
}

in_list() {  # needle list...
  local needle="$1"; shift
  local item
  for item in "$@"; do [ "$item" = "$needle" ] && return 0; done
  return 1
}

may_skip() {  # job name
  case "$1" in
    "warm compile cache ("* | "windows / warm compile cache ("*) return 0 ;;
  esac
  in_list "$1" "${MAY_SKIP[@]}"
}

runs=$(api "repos/$repo/actions/workflows/server.yml/runs?head_sha=$sha&status=success&per_page=100" \
         --jq '.workflow_runs[] | select(.head_sha == "'"$sha"'") | select(.event == "push" or .event == "workflow_dispatch") | select(.path == ".github/workflows/server.yml") | "\(.id) \(.event) \(.head_branch)"') \
  || answer false "" "Could not list the earlier runs (API error), so this run tests the commit."
[ -n "$runs" ] || answer false "" "No earlier successful push or workflow_dispatch run of server.yml on this commit."

notes=""
while read -r id event branch; do
  [ -n "$id" ] && [ "$id" != "$self" ] || continue
  jobs=$(api --paginate "repos/$repo/actions/runs/$id/jobs?filter=latest&per_page=30" \
           --jq '.jobs[] | "\(.conclusion)\t\(.name)"') || { notes+="run $id: jobs unreadable. "; continue; }
  ok=true
  for name in "${REQUIRED[@]}"; do
    if ! grep -qxF -- "success	$name" <<< "$jobs"; then
      ok=false; notes+="run $id ($event, $branch): \"$name\" did not succeed. "; break
    fi
  done
  [ "$ok" = true ] || continue
  while IFS=$'\t' read -r conclusion name; do
    [ -n "$name" ] || continue
    if [ "$conclusion" = success ]; then continue; fi
    if [ "$conclusion" = skipped ] && may_skip "$name"; then continue; fi
    ok=false; notes+="run $id ($event, $branch): \"$name\" is $conclusion. "; break
  done <<< "$jobs"
  [ "$ok" = true ] || continue
  arts=$(api --paginate "repos/$repo/actions/runs/$id/artifacts?per_page=100" \
           --jq '.artifacts[] | select(.expired == false) | .name') || { notes+="run $id: artifacts unreadable. "; continue; }
  for art in "${ARTIFACTS[@]}"; do
    if ! grep -qxF -- "$art" <<< "$arts"; then
      ok=false; notes+="run $id ($event, $branch): artifact $art is gone. "; break
    fi
  done
  [ "$ok" = true ] && answer true "$id" "run $id ($event, $branch) passed every job"
done <<< "$runs"

answer false "" "No earlier run qualifies: ${notes:-none found}"
