#!/usr/bin/env bash
# Checks that macOS executables carry GizmoData's release signature, and fails otherwise:
#
#   APPLE_TEAM_ID=<team> verify-macos-signature.sh [--entitlements <plist>] <file>... [-- [--entitlements <plist>] <file>...]
#
# For each file: the signature is intact (codesign --verify --strict), it is a Developer ID
# Application signature of APPLE_TEAM_ID's team (the Developer ID designated requirement,
# checked by codesign, so an ad-hoc or another team's signature fails), with the hardened
# runtime and a secure timestamp, Apple notarized it (codesign --check-notarization looks the
# ticket up online: a bare executable can't be stapled, so `stapler validate` has nothing to
# check), and Gatekeeper accepts it (spctl, the primary-signature context, which is how it
# assesses a command-line tool). With --entitlements, the files after it carry exactly that
# plist's entitlements; without, none at all. Every check is an exit status, or a
# comparison of codesign's own fields. The team ID comes from the environment (server.yml
# passes the APPLE_TEAM_ID secret) and is never printed by this script.
set -uo pipefail

team="${APPLE_TEAM_ID:-}"
[ -n "$team" ] || { echo "APPLE_TEAM_ID is not set" >&2; exit 2; }
# Apple's designated requirement for a Developer ID Application signature: an Apple anchor,
# the Developer ID intermediate (6.2.6), the Developer ID Application leaf (6.1.13), our team.
requirement="anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] and certificate leaf[field.1.2.840.113635.100.6.1.13] and certificate leaf[subject.OU] = \"$team\""
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0
checked=0
fail() { echo "FAIL: $1: $2" >&2; failures=$((failures + 1)); }

# The entitlements a file is signed with, as JSON ("{}" for none), or the plist's.
entitlements_json() {  # file|--plist path
  local out="$work/ent.plist"
  if [ "$1" = --plist ]; then
    cp "$2" "$out"
  else
    codesign -d --entitlements - --xml "$1" > "$out" 2> /dev/null || return 1
    [ -s "$out" ] || { echo "{}"; return 0; }
  fi
  plutil -convert json -o - "$out"
}

verify() {  # file entitlements-plist-or-empty
  local file="$1" plist="$2" details want got
  checked=$((checked + 1))
  echo "--- $file"
  if [ ! -f "$file" ]; then fail "$file" "no such file"; return; fi
  if ! codesign --verify --strict --verbose=2 "$file"; then
    fail "$file" "the signature is missing, broken or not strictly valid"; return
  fi
  if ! codesign --verify --strict -R "=$requirement" "$file" 2> "$work/req.err"; then
    sed 's/^/  /' "$work/req.err" >&2
    fail "$file" "not a Developer ID Application signature of the expected team"; return
  fi
  details=$(codesign -d --verbose=2 "$file" 2>&1)
  grep -E '^(Authority=|Timestamp=|CodeDirectory )' <<< "$details" | sed 's/^/  /'
  [[ "$details" == *"(runtime)"* ]] || fail "$file" "signed without the hardened runtime"
  grep -q '^Timestamp=' <<< "$details" || fail "$file" "no secure timestamp"
  # Apple can accept a submission before the online ticket reaches this runner.
  # Retry only that lookup; a broken signature or wrong identity already failed above.
  local notarized=false attempt
  for attempt in {1..12}; do
    if codesign --verify -R "=notarized" --check-notarization "$file"; then
      notarized=true
      break
    fi
    if [ "$attempt" -lt 12 ]; then
      echo "Waiting for Apple's notarization ticket ($attempt/12): $file" >&2
      sleep 10
    fi
  done
  if [ "$notarized" != true ]; then
    fail "$file" "Apple has no notarization ticket for it"
  fi
  if ! spctl --assess --type open --context context:primary-signature --verbose=2 "$file"; then
    fail "$file" "Gatekeeper rejects it"
  fi
  if [ -n "$plist" ]; then want=$(entitlements_json --plist "$plist"); else want="{}"; fi
  if ! got=$(entitlements_json "$file"); then
    fail "$file" "its entitlements are unreadable"
  elif [ "$got" != "$want" ]; then
    fail "$file" "entitlements $got, want $want"
  fi
}

plist=""
while [ $# -gt 0 ]; do
  case "$1" in
    --entitlements) plist="${2:?--entitlements needs a plist}"; shift 2 ;;
    --) plist=""; shift ;;
    *) verify "$1" "$plist"; shift ;;
  esac
done

[ "$checked" -gt 0 ] || { echo "no files given" >&2; exit 2; }
if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed over $checked file(s)" >&2
  exit 1
fi
echo "all $checked file(s): Developer ID signed, hardened runtime, timestamped, notarized, accepted by Gatekeeper"
