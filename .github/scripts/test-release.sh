#!/usr/bin/env bash
# Tests of .github/scripts/release.sh and ui/scripts/version.mjs: the version string, the
# image tags, the MSI version, the CHANGELOG extraction, for v2.0.0-rc.1, v2.0.0-rc.10 and
# v2.0.0, and that what must fail (a missing or empty CHANGELOG section, a malformed version,
# a tag that isn't the version) fails, for the reason it should. server.yml's `changes` job
# runs it on every run.
#
#   bash .github/scripts/test-release.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
RELEASE="$HERE/release.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
failures=0
passes=0

pass() { passes=$((passes + 1)); }
bad() { echo "FAIL: $*"; failures=$((failures + 1)); }

# expect <want stdout> <command...>: exit 0 and exactly that output.
expect() {
  local want="$1" got status
  shift
  got=$("$@" 2>&1)
  status=$?
  if [ "$status" -ne 0 ]; then bad "$* exited $status: $got"
  elif [ "$got" != "$want" ]; then bad "$*: got [$got], want [$want]"
  else pass; fi
}

# refuse <message fragment> <command...>: a non-zero exit whose message says why.
refuse() {
  local why="$1" got status
  shift
  got=$("$@" 2>&1)
  status=$?
  if [ "$status" -eq 0 ]; then bad "$* succeeded (want a failure saying \"$why\"): $got"
  elif [[ "$got" != *"$why"* ]]; then bad "$* failed for another reason (want \"$why\"): $got"
  else pass; fi
}

# A server/CMakeLists.txt with these numbers and suffix (the real one's first lines, edited).
cmakelists() {
  local file="$WORK/CMakeLists-$1$2.txt"
  sed -e "s/^project(hardcharger VERSION [0-9.]*/project(hardcharger VERSION $1/" \
      -e "s/^set(HARDCHARGER_VERSION_SUFFIX \"[^\"]*\")/set(HARDCHARGER_VERSION_SUFFIX \"$2\")/" \
      "$ROOT/server/CMakeLists.txt" > "$file"
  echo "$file"
}

ui_version() {
  node --input-type=module -e '
    const { uiVersion } = await import(process.argv[1]);
    console.log(uiVersion(process.argv[2]));
  ' "$ROOT/ui/scripts/version.mjs" "$1"
}

# ---- the version string ------------------------------------------------------------------
# The real file reads, whatever version it holds.
expect "$(sed -n 's/^project(hardcharger VERSION \([0-9.]*\).*/\1/p' "$ROOT/server/CMakeLists.txt")$(sed -n 's/^set(HARDCHARGER_VERSION_SUFFIX "\([^"]*\)").*/\1/p' "$ROOT/server/CMakeLists.txt")" \
  "$RELEASE" version
for case in "2.0.0 -rc.1 2.0.0-rc.1" "2.0.0 -rc.10 2.0.0-rc.10" "2.0.0 '' 2.0.0"; do
  eval "set -- $case"
  file=$(cmakelists "$1" "$2")
  expect "$3" "$RELEASE" version "$file"
  expect "" "$RELEASE" check-tag "v$3" "$file"
  expect "$3" ui_version "$file"
done
refuse "tag v2.0.0 != v2.0.0-rc.1" "$RELEASE" check-tag v2.0.0 "$(cmakelists 2.0.0 -rc.1)"
refuse "tag v2.0.0-rc.1 != v2.0.0" "$RELEASE" check-tag v2.0.0-rc.1 "$(cmakelists 2.0.0 "")"
refuse "tag 2.0.0 != v2.0.0" "$RELEASE" check-tag 2.0.0 "$(cmakelists 2.0.0 "")"
for suffix in -beta.1 -rc.0 -rc.01 rc1 -rc.1-x; do
  file=$(cmakelists 2.0.0 "$suffix")
  refuse "is not a release version" "$RELEASE" version "$file"
  refuse "is not a release version" ui_version "$file"
done
grep -v '^set(HARDCHARGER_VERSION_SUFFIX' "$(cmakelists 2.0.0 "")" > "$WORK/no-suffix.txt"
refuse "no \`set(HARDCHARGER_VERSION_SUFFIX" "$RELEASE" version "$WORK/no-suffix.txt"
refuse "no \`set(HARDCHARGER_VERSION_SUFFIX" ui_version "$WORK/no-suffix.txt"

# ---- pre-release, MSI version ------------------------------------------------------------
expect true "$RELEASE" prerelease 2.0.0-rc.1
expect true "$RELEASE" prerelease 2.0.0-rc.10
expect false "$RELEASE" prerelease 2.0.0
expect 2.0.0.1 "$RELEASE" msi-version 2.0.0-rc.1
expect 2.0.0.10 "$RELEASE" msi-version 2.0.0-rc.10
expect 2.0.0 "$RELEASE" msi-version 2.0.0
expect 0.3.0 "$RELEASE" msi-version 0.3.0
refuse "doesn't fit an MSI version" "$RELEASE" msi-version 256.0.0
refuse "doesn't fit an MSI version" "$RELEASE" msi-version 2.0.0-rc.65536
refuse "is not a release version" "$RELEASE" msi-version v2.0.0
refuse "usage: release.sh msi-version" "$RELEASE" msi-version ""

# ---- image tags --------------------------------------------------------------------------
sha=0123456789abcdef0123456789abcdef01234567
expect "ghcr.io/gizmodata/hardcharger:2.0.0-rc.1
ghcr.io/gizmodata/hardcharger:sha-0123456
docker.io/gizmodata/hardcharger:2.0.0-rc.1
docker.io/gizmodata/hardcharger:sha-0123456" "$RELEASE" image-tags 2.0.0-rc.1 "$sha"
expect "ghcr.io/gizmodata/hardcharger:2.0.0-rc.10
ghcr.io/gizmodata/hardcharger:sha-0123456
docker.io/gizmodata/hardcharger:2.0.0-rc.10
docker.io/gizmodata/hardcharger:sha-0123456" "$RELEASE" image-tags 2.0.0-rc.10 "$sha"
expect "ghcr.io/gizmodata/hardcharger:2.0.0
ghcr.io/gizmodata/hardcharger:latest
docker.io/gizmodata/hardcharger:2.0.0
docker.io/gizmodata/hardcharger:latest" "$RELEASE" image-tags 2.0.0 "$sha"
for v in 2.0.0-rc.1 2.0.0-rc.10; do
  tags=$("$RELEASE" image-tags "$v" "$sha")
  if [[ "$tags" == *":latest"* || "$tags" =~ :2(\.0)?$'\n' || "$tags" =~ :2(\.0)?$ ]]; then
    bad "a release candidate's tags move latest or a floating tag: $tags"
  else pass; fi
done
refuse "is not a commit SHA" "$RELEASE" image-tags 2.0.0 main
refuse "is not a release version" "$RELEASE" image-tags latest "$sha"

# ---- the CHANGELOG section ---------------------------------------------------------------
# A decoy whose heading matches 2.0.0-rc.1 only as a regex (dots match anything), and the
# neighbours rc.10 and 2.0.0 whose headings start with the same text.
cat > "$WORK/CHANGELOG.md" <<'EOF'
# Changelog

## [Unreleased]

- unreleased line

## [2.0.0] - 2026-11-01

- final line

## [2.0.0-rc.10] - 2026-10-30

- rc10 line

## [2x0x0-rc.1] - 2026-10-21

- decoy line

## [2.0.0-rc.1] - 2026-10-20

### Added

- rc1 line

## [2.0.0-rc.2]

## [0.3.0] - 2026-10-19

- old line

[2.0.0-rc.1]: https://github.com/gizmodata/hardcharger/releases/tag/v2.0.0-rc.1
EOF
expect "
### Added

- rc1 line" "$RELEASE" changelog 2.0.0-rc.1 "$WORK/CHANGELOG.md"
expect "
- rc10 line" "$RELEASE" changelog 2.0.0-rc.10 "$WORK/CHANGELOG.md"
expect "
- final line" "$RELEASE" changelog 2.0.0 "$WORK/CHANGELOG.md"
# A missing section and an empty one fail the release.
refuse "no \`## [2.0.0-rc.3]\` section" "$RELEASE" changelog 2.0.0-rc.3 "$WORK/CHANGELOG.md"
refuse "the \`## [2.0.0-rc.2]\` section in $WORK/CHANGELOG.md is empty" "$RELEASE" changelog 2.0.0-rc.2 "$WORK/CHANGELOG.md"
refuse "no \`## [1.0.0]\` section" "$RELEASE" changelog 1.0.0 "$WORK/CHANGELOG.md"
# The real CHANGELOG: the newest released section extracts.
newest=$(sed -n 's/^## \[\([0-9][^]]*\)\].*/\1/p' "$ROOT/CHANGELOG.md" | head -1)
if [ -z "$newest" ]; then bad "no released section in CHANGELOG.md"
elif ! "$RELEASE" changelog "$newest" > /dev/null; then bad "CHANGELOG.md's $newest section doesn't extract"
else pass; fi

echo "test-release.sh: $passes passed, $failures failed"
[ "$failures" -eq 0 ] && [ "$passes" -gt 0 ]
