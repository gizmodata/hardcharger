#!/usr/bin/env bash
# The iOS app's signed device build in CI (.github/workflows/server.yml, ios-archive: v*
# tags only, in the `production` environment). Never run it on a person's Mac: it makes a
# keychain and installs a provisioning profile.
#
#   ci-signing.sh install            the distribution certificate into a keychain of the
#                                    run's own, the profile where Xcode finds it
#   ci-signing.sh archive <out dir>  archive (Release, devices), export the App Store .ipa
#   ci-signing.sh cleanup            remove that keychain and the profile again
#
# From the environment (secrets; none is ever printed):
#   IOS_DIST_CERT_B64             the "Apple Distribution: Gizmo Data LLC (XYB42X6NFX)"
#                                 certificate with its private key, a base64 .p12
#   IOS_DIST_CERT_PASSWORD        its password
#   IOS_PROVISIONING_PROFILE_B64  the App Store profile for XYB42X6NFX.com.gizmodata.hardcharger
#                                 ("Hardcharger App Store"), base64
# Nothing is uploaded: the .ipa is the run's artifact. (An App Store Connect upload, with
# ASC_KEY_B64, ASC_KEY_ID and ASC_ISSUER_ID, is a separate step Philip approves.)
set -euo pipefail
set +x
IOS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEAM=XYB42X6NFX
BUNDLE_ID=com.gizmodata.hardcharger
WORK="${RUNNER_TEMP:?ci-signing.sh runs in CI (RUNNER_TEMP)}/hardcharger-signing"
KEYCHAIN="$WORK/signing.keychain-db"
PROFILES="$HOME/Library/MobileDevice/Provisioning Profiles"

case "${1:-}" in
  install)
    : "${IOS_DIST_CERT_B64:?}" "${IOS_DIST_CERT_PASSWORD:?}" "${IOS_PROVISIONING_PROFILE_B64:?}"
    mkdir -p "$WORK" "$PROFILES"
    keychain_password=$(openssl rand -hex 24)
    printf '%s' "$keychain_password" >"$WORK/keychain-password"
    printf '%s' "$IOS_DIST_CERT_B64" | base64 --decode >"$WORK/certificate.p12"
    printf '%s' "$IOS_PROVISIONING_PROFILE_B64" | base64 --decode >"$WORK/profile.mobileprovision"
    security create-keychain -p "$keychain_password" "$KEYCHAIN"
    security set-keychain-settings -lut 21600 "$KEYCHAIN"
    security unlock-keychain -p "$keychain_password" "$KEYCHAIN"
    security import "$WORK/certificate.p12" -k "$KEYCHAIN" -P "$IOS_DIST_CERT_PASSWORD" -f pkcs12 \
      -T /usr/bin/codesign -T /usr/bin/security >/dev/null
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$KEYCHAIN" >/dev/null
    rm -f "$WORK/certificate.p12"
    # The run's keychain first in the search list, the others kept.
    existing=$(security list-keychains -d user | tr -d '"' | xargs)
    printf '%s' "$existing" >"$WORK/keychains-before"
    # shellcheck disable=SC2086
    security list-keychains -d user -s "$KEYCHAIN" $existing
    identity=$(security find-identity -v -p codesigning "$KEYCHAIN" | sed -n 's/.*"\(Apple Distribution: [^"]*\)".*/\1/p' | head -1)
    [ -n "$identity" ] || { echo "no Apple Distribution identity in the certificate" >&2; exit 1; }
    # The profile: where Xcode looks, under its UUID; its name and team checked.
    security cms -D -i "$WORK/profile.mobileprovision" >"$WORK/profile.plist"
    uuid=$(plutil -extract UUID raw "$WORK/profile.plist")
    name=$(plutil -extract Name raw "$WORK/profile.plist")
    app_id=$(plutil -extract Entitlements.application-identifier raw "$WORK/profile.plist")
    [ "$app_id" = "$TEAM.$BUNDLE_ID" ] || { echo "the profile is for $app_id, not $TEAM.$BUNDLE_ID" >&2; exit 1; }
    cp "$WORK/profile.mobileprovision" "$PROFILES/$uuid.mobileprovision"
    mkdir -p "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
    cp "$WORK/profile.mobileprovision" "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles/$uuid.mobileprovision"
    printf '%s\n%s\n%s\n' "$uuid" "$name" "$identity" >"$WORK/signing"
    echo "signing as \"$identity\" with the profile \"$name\" ($uuid), expires $(plutil -extract ExpirationDate raw "$WORK/profile.plist")"
    ;;
  archive)
    out="${2:?usage: ci-signing.sh archive <out dir>}"
    { read -r uuid; read -r name; read -r identity; } <"$WORK/signing"
    archive="$WORK/Hardcharger.xcarchive"
    xcodebuild archive -project "$IOS/Hardcharger.xcodeproj" -scheme Hardcharger -configuration Release \
      -destination generic/platform=iOS -archivePath "$archive" -derivedDataPath "$IOS/build/DerivedData-device" \
      CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_IDENTITY="$identity" \
      PROVISIONING_PROFILE_SPECIFIER="$name" OTHER_CODE_SIGN_FLAGS="--keychain $KEYCHAIN"
    cat >"$WORK/ExportOptions.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>export</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>manual</string>
  <key>signingCertificate</key><string>Apple Distribution</string>
  <key>provisioningProfiles</key><dict><key>$BUNDLE_ID</key><string>$name</string></dict>
  <key>uploadSymbols</key><true/>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
EOF
    xcodebuild -exportArchive -archivePath "$archive" -exportOptionsPlist "$WORK/ExportOptions.plist" -exportPath "$out"
    ipa=$(ls "$out"/*.ipa)
    # The signature names the team and the bundle, and the profile went in.
    unzip -q -o "$ipa" -d "$WORK/ipa"
    codesign --verify --strict --verbose=2 "$WORK/ipa/Payload/Hardcharger.app"
    codesign -dv "$WORK/ipa/Payload/Hardcharger.app" 2>&1 | grep -E "^(Identifier|TeamIdentifier)=" | tee "$WORK/sig"
    grep -qx "Identifier=$BUNDLE_ID" "$WORK/sig" && grep -qx "TeamIdentifier=$TEAM" "$WORK/sig"
    [ -f "$WORK/ipa/Payload/Hardcharger.app/embedded.mobileprovision" ]
    echo "exported $ipa ($(du -h "$ipa" | cut -f1))"
    ;;
  cleanup)
    if [ -f "$WORK/keychains-before" ]; then
      # shellcheck disable=SC2046
      security list-keychains -d user -s $(cat "$WORK/keychains-before")
    fi
    security delete-keychain "$KEYCHAIN" 2>/dev/null || true
    if [ -f "$WORK/signing" ]; then
      uuid=$(head -1 "$WORK/signing")
      rm -f "$PROFILES/$uuid.mobileprovision" "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles/$uuid.mobileprovision"
    fi
    rm -rf "$WORK"
    ;;
  *)
    echo "usage: ci-signing.sh install | archive <out dir> | cleanup" >&2
    exit 2
    ;;
esac
