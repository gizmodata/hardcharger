#!/usr/bin/env bash
# Prepare a Google Play bundle locally. This script never uploads or publishes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
preview=false
if [ "${1:-}" = --unsigned-preview ]; then preview=true; shift; fi
ABI="${1:-arm64-v8a}"
if [ "$preview" = true ]; then
  unset HARDCHARGER_ANDROID_KEYSTORE HARDCHARGER_ANDROID_STORE_PASSWORD HARDCHARGER_ANDROID_KEY_ALIAS HARDCHARGER_ANDROID_KEY_PASSWORD
else
  : "${HARDCHARGER_ANDROID_KEYSTORE:?Set the upload keystore, or use --unsigned-preview for local review}"
  : "${HARDCHARGER_ANDROID_STORE_PASSWORD:?Set the keystore password}"
  : "${HARDCHARGER_ANDROID_KEY_ALIAS:?Set the upload key alias}"
  : "${HARDCHARGER_ANDROID_KEY_PASSWORD:?Set the upload key password}"
  : "${HARDCHARGER_ANDROID_BUILD_NUMBER:?Choose the next unused Play version code}"
fi
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
"$ROOT/android/scripts/build-native.sh" "$ABI" play
"$ROOT/android/gradlew" -p "$ROOT/android" -PhardchargerDistribution=play \
  :app:testDebugUnitTest :app:lintRelease :app:bundleRelease
if [ "$preview" = true ]; then
  python3 "$ROOT/android/scripts/check-play-bundle.py" \
    "$ROOT/android/build/app-play/outputs/bundle/release/app-release.aab"
  echo 'Unsigned review bundle only. Configure the upload key and rebuild before uploading to Play.'
else
  python3 "$ROOT/android/scripts/check-play-bundle.py" \
    "$ROOT/android/build/app-play/outputs/bundle/release/app-release.aab" --require-signed
  jarsigner -verify "$ROOT/android/build/app-play/outputs/bundle/release/app-release.aab"
  echo 'Signed Play bundle prepared locally. Nothing was uploaded.'
fi
