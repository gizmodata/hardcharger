#!/usr/bin/env bash
# Which build platforms main lacks a fresh compile cache (sccache directory) for, so that a push
# to main whose tests are skipped (already-tested.sh) still leaves main a cache that branch builds
# start from. GitHub Actions caches are per branch: a branch restores its own and main's, never
# another branch's, so without main's every new branch compiled cold.
#
#   stale-compile-caches.sh <owner/repo> <platform>...
#
# Prints `warm=<JSON list of the platforms to build>` as a GITHUB_OUTPUT line; a Markdown summary
# goes to stderr. A platform is fresh, and left out, when main has an sccache entry for it
#   - with the toolchain the newest entry on any branch was built with (the toolchain the
#     runners have now: keys are sccache-v1-<platform>-<toolchain>-<run id>-<attempt>), created
#     within the last FRESH_DAYS days (7: GitHub evicts an entry nobody reads for 7 days), or
#   - with any toolchain, created within the last RECENT_HOURS hours (24): a warm-up just ran
#     here, and the toolchain differs only while a runner image rolls out (or on a branch that
#     tries a new one), when warming main again would make no entry with the other toolchain.
# Any doubt (an API error, an answer that doesn't parse, no entry anywhere) makes the platform
# stale: a failed lookup costs one build, never a skipped one.
#
# The sanitizer jobs' entries (sccache-v1-linux-amd64-asan-..., -tsan-...) never count.
set -uo pipefail

repo="${1:?owner/repo}"
shift
[ $# -gt 0 ] || { echo "usage: $0 <owner/repo> <platform>..." >&2; exit 2; }

FRESH_DAYS="${FRESH_DAYS:-7}"
RECENT_HOURS="${RECENT_HOURS:-24}"
now="${NOW:-$(date +%s)}"          # NOW: epoch seconds (the tests set it)
retry_sleep="${RETRY_SLEEP:-10}"   # seconds per attempt between retries (the tests set 0)

# gh api, retried as in already-tested.sh; prints only a complete answer.
api() {
  local attempt out
  for attempt in 1 2 3 4; do
    if out=$(gh api "$@"); then printf '%s\n' "$out"; return 0; fi
    [ "$attempt" -lt 4 ] && sleep $(( attempt * retry_sleep ))
  done
  return 1
}

# The cache entries' creation times, as epoch seconds (GitHub writes fractional seconds, which
# jq's fromdateiso8601 doesn't take).
# shellcheck disable=SC2016 # (jq variables, not the shell's)
JQ_DEFS='def created: .created_at | sub("\\.[0-9]+"; "") | fromdateiso8601;
         def plain($prefix): .key | test("^" + $prefix + "(asan|tsan)-") | not;'

stale=()
{
  echo "### Compile caches on main"
  echo
  echo "| platform | main's cache | warm-up build |"
  echo "|---|---|---|"
} >&2
row() { echo "| $1 | $2 | $3 |" >&2; }

for platform in "$@"; do
  prefix="sccache-v1-$platform-"

  # The newest entry for this platform on any branch: its toolchain is the current one.
  if ! any=$(api "repos/$repo/actions/caches?key=$prefix&sort=created_at&direction=desc&per_page=100"); then
    stale+=("$platform"); row "$platform" "the cache list could not be read" yes; continue
  fi
  if ! newest=$(jq -er --arg p "$prefix" "$JQ_DEFS"'
         [.actions_caches[] | select(plain($p))] | (.[0].key // "")' <<< "$any" 2>/dev/null); then
    stale+=("$platform"); row "$platform" "the cache list did not parse" yes; continue
  fi
  if [ -z "$newest" ]; then
    stale+=("$platform"); row "$platform" "no entry on any branch" yes; continue
  fi
  toolchain_prefix=$(sed -E 's/[0-9]+-[0-9]+$//' <<< "$newest")
  if [ "$toolchain_prefix" = "$newest" ] || [[ "$toolchain_prefix" != "$prefix"?* ]]; then
    stale+=("$platform"); row "$platform" "unexpected key \`$newest\`" yes; continue
  fi

  if ! mine=$(api "repos/$repo/actions/caches?ref=refs/heads/main&key=$prefix&sort=created_at&direction=desc&per_page=100"); then
    stale+=("$platform"); row "$platform" "main's cache list could not be read" yes; continue
  fi
  # "current <age in hours>", "recent <age>" or "none".
  if ! verdict=$(jq -er --arg p "$prefix" --arg t "$toolchain_prefix" \
         --argjson now "$now" --argjson days "$FRESH_DAYS" --argjson hours "$RECENT_HOURS" "$JQ_DEFS"'
         [.actions_caches[] | select(plain($p)) | {key, age: ($now - created)}] as $mine
         | ([$mine[] | select((.key | startswith($t)) and .age < $days * 86400)] | first) as $current
         | ([$mine[] | select(.age < $hours * 3600)] | first) as $recent
         | if $current then "current \($current.age / 3600 | floor)"
           elif $recent then "recent \($recent.age / 3600 | floor)"
           else "none" end' <<< "$mine" 2>/dev/null); then
    stale+=("$platform"); row "$platform" "main's cache list did not parse" yes; continue
  fi
  case "$verdict" in
    current\ *) row "$platform" "\`${toolchain_prefix%-}\`, ${verdict#current } h old" no ;;
    recent\ *)  row "$platform" "another toolchain than \`${toolchain_prefix%-}\`, ${verdict#recent } h old" "no (one a day at most)" ;;
    *)          stale+=("$platform"); row "$platform" "none for \`${toolchain_prefix%-}\` newer than $FRESH_DAYS days" yes ;;
  esac
done

echo "warm=$(jq -cn '$ARGS.positional' --args ${stale[@]+"${stale[@]}"})"
