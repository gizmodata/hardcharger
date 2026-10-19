#!/usr/bin/env bash
# The server release's version arithmetic, in one place, so the workflows (server.yml,
# windows.yml) and its test (test-release.sh) run the same code:
#
#   release.sh version [<CMakeLists.txt>]   the full version: 0.3.0, 2.0.0-rc.1
#   release.sh check-tag <tag> [<CMakeLists.txt>]  fails unless <tag> is v<the full version>
#   release.sh prerelease <version>         "true" for X.Y.Z-rc.N, "false" for X.Y.Z
#   release.sh msi-version <version>        the MSI's numeric ProductVersion
#   release.sh image-tags <version> <sha>   one image reference per line, for a release tag
#   release.sh changelog <version> [<file>] the CHANGELOG section `## [<version>]`
#
# Versions are X.Y.Z or X.Y.Z-rc.N, nothing else: server/CMakeLists.txt holds the numbers in
# `project(hardcharger VERSION X.Y.Z ...)` and the "-rc.N" in `set(HARDCHARGER_VERSION_SUFFIX
# "...")`. Every subcommand exits non-zero, with a message on stderr, on anything it can't
# answer exactly: an empty or malformed version, a missing CHANGELOG section.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SEMVER='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.([1-9][0-9]*))?$'
IMAGE_REPOS=(ghcr.io/gizmodata/hardcharger docker.io/gizmodata/hardcharger)

fail() { echo "release.sh: $*" >&2; exit 1; }

valid() {
  [[ "$1" =~ $SEMVER ]] || fail "\"$1\" is not a release version (X.Y.Z or X.Y.Z-rc.N)"
}

cmd_version() {
  local file="${1:-$ROOT/server/CMakeLists.txt}" numbers suffix
  [ -f "$file" ] || fail "no $file"
  numbers=$(sed -n 's/^project(hardcharger VERSION \([0-9.]*\).*/\1/p' "$file")
  [ -n "$numbers" ] || fail "no \`project(hardcharger VERSION X.Y.Z ...)\` in $file"
  # The line must be there, even when empty, so a typo can't drop a suffix silently.
  grep -Eq '^set\(HARDCHARGER_VERSION_SUFFIX "[^"]*"\)' "$file" \
    || fail "no \`set(HARDCHARGER_VERSION_SUFFIX \"...\")\` in $file"
  suffix=$(sed -n 's/^set(HARDCHARGER_VERSION_SUFFIX "\([^"]*\)").*/\1/p' "$file")
  valid "$numbers$suffix"
  echo "$numbers$suffix"
}

cmd_check_tag() {
  local tag="${1:?usage: release.sh check-tag <tag> [<CMakeLists.txt>]}" version
  version=$(cmd_version "${2:-}")
  [ "$tag" = "v$version" ] || fail "tag $tag != v$version (server/CMakeLists.txt)"
}

cmd_prerelease() {
  local version="${1:?usage: release.sh prerelease <version>}"
  valid "$version"
  if [[ "$version" == *-* ]]; then echo true; else echo false; fi
}

# Windows Installer takes three or four numbers (major and minor up to 255, build up to
# 65535), never "-rc.1". A release candidate X.Y.Z-rc.N becomes X.Y.Z.N; a release stays
# X.Y.Z. Windows Installer compares only the first three fields, so every candidate and the
# release of one X.Y.Z count as the same version: Hardcharger.wxs's AllowSameVersionUpgrades
# makes each replace whatever of that X.Y.Z is installed (candidate -> next candidate ->
# release), and the fourth field just tells the candidates apart in Programs and Features.
cmd_msi_version() {
  local version="${1:?usage: release.sh msi-version <version>}"
  valid "$version"
  [[ "$version" =~ $SEMVER ]]
  local major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}" patch="${BASH_REMATCH[3]}" rc="${BASH_REMATCH[5]}"
  { [ "$major" -le 255 ] && [ "$minor" -le 255 ] && [ "$patch" -le 65535 ]; } \
    || fail "$version doesn't fit an MSI version (major, minor <= 255; patch <= 65535)"
  if [ -n "$rc" ]; then
    [ "$rc" -le 65535 ] || fail "$version: the candidate number doesn't fit an MSI version (<= 65535)"
    echo "$major.$minor.$patch.$rc"
  else
    echo "$major.$minor.$patch"
  fi
}

# A release X.Y.Z: :X.Y.Z and :latest (no floating :X.Y or :X). A release candidate:
# :X.Y.Z-rc.N and :sha-<short>, never :latest. Both registries alike.
cmd_image_tags() {
  local version="${1:?usage: release.sh image-tags <version> <sha>}" sha="${2:?usage: release.sh image-tags <version> <sha>}"
  valid "$version"
  [[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || fail "\"$sha\" is not a commit SHA"
  local repo
  for repo in "${IMAGE_REPOS[@]}"; do
    echo "$repo:$version"
    if [ "$(cmd_prerelease "$version")" = true ]; then
      echo "$repo:sha-${sha::7}"
    else
      echo "$repo:latest"
    fi
  done
}

# The lines after `## [<version>]` (the heading matched as text, not as a regex: the dots
# and the dash are literal, and `## [2.0.0-rc.1]` is not `## [2.0.0-rc.10]`), up to the
# next `## [`. Fails when the heading is missing or the section has no text.
cmd_changelog() {
  local version="${1:?usage: release.sh changelog <version> [<file>]}" file="${2:-$ROOT/CHANGELOG.md}" notes
  valid "$version"
  [ -f "$file" ] || fail "no $file"
  notes=$(awk -v heading="## [$version]" '
    !found && (index($0, heading) == 1) {
      rest = substr($0, length(heading) + 1)
      if (rest == "" || rest ~ /^[ \t]/) { found = 1; next }
    }
    found && /^## \[/ { exit }
    found { print }
    END { if (!found) exit 3 }
  ' "$file") || fail "no \`## [$version]\` section in $file"
  grep -q '[^[:space:]]' <<< "$notes" || fail "the \`## [$version]\` section in $file is empty"
  printf '%s\n' "$notes"
}

sub="${1:-}"
[ $# -gt 0 ] && shift
case "$sub" in
  version) cmd_version "$@" ;;
  check-tag) cmd_check_tag "$@" ;;
  prerelease) cmd_prerelease "$@" ;;
  msi-version) cmd_msi_version "$@" ;;
  image-tags) cmd_image_tags "$@" ;;
  changelog) cmd_changelog "$@" ;;
  *) fail "usage: release.sh version|check-tag|prerelease|msi-version|image-tags|changelog ..." ;;
esac
