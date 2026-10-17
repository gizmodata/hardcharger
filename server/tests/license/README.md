# Test license key

A key pair for minting GizmoData license keys in tests. Only a server built with
`-DHARDCHARGER_TRUST_TEST_LICENSES=ON` trusts the public half; such a build says so in
`--version`, in its startup log and in its server info, and must never be shipped. The
private half is public on purpose: it unlocks nothing in a release build.
