#!/usr/bin/env bash
# Checks that the C/C++ lines changed since <base> (a commit: the pull request's base) are
# formatted as .clang-format says, with the pinned clang-format below; lines nobody changed are
# left alone. Prints the diff that would fix them and exits 1 when any aren't.
#
#   .github/scripts/check-format.sh origin/main          # what CI runs, against the PR's base
#   .github/scripts/check-format.sh origin/main --fix    # rewrite the changed lines in place
#
# Needs uv (https://docs.astral.sh/uv/): it runs clang-format from PyPI at the pinned version,
# so every machine formats alike.
set -euo pipefail
CLANG_FORMAT_VERSION=23.1.5
base="${1:?usage: $0 <base commit> [--fix]}"
fix="${2:-}"
cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "$base^{commit}" >/dev/null || { echo "no such commit: $base" >&2; exit 2; }
merge_base=$(git merge-base "$base" HEAD)
paths=(server android/native)
tool=(uvx --quiet --from "clang-format==$CLANG_FORMAT_VERSION")
clang_format=$("${tool[@]}" python -c 'import clang_format, os; print(os.path.join(clang_format.get_executable("clang-format")))' 2>/dev/null || true)
if [ -z "$clang_format" ] || [ ! -x "$clang_format" ]; then
  echo "could not get clang-format $CLANG_FORMAT_VERSION from PyPI" >&2
  exit 2
fi
"$clang_format" --version
args=(--binary "$clang_format" --extensions c,cc,cpp,h,hpp "$merge_base" -- "${paths[@]}")
if [ "$fix" = --fix ]; then
  "${tool[@]}" git-clang-format --force "${args[@]}"
  exit 0
fi
out=$("${tool[@]}" git-clang-format --diff "${args[@]}" 2>&1) || true
case "$out" in
  "no modified files to format"* | "clang-format did not modify any files"* | "")
    echo "The changed C/C++ lines are formatted."
    ;;
  *)
    echo "$out"
    echo
    echo "These changed lines are not formatted as .clang-format says. Fix them with:"
    echo "  .github/scripts/check-format.sh $base --fix"
    exit 1
    ;;
esac
