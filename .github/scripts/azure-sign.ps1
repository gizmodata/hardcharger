# Signs Windows files (executables, MSIs) with GizmoData's code-signing certificate in Azure
# Key Vault, through AzureSignTool (the key never leaves the vault):
#
#   pwsh .github/scripts/azure-sign.ps1 <file>...
#
# Needs an Azure sign-in (azure/login, OIDC) that may use the vault's key, and the vault and
# certificate from the environment: KEY_VAULT_URI and CERT_NAME (windows.yml passes the
# `production` environment's AZURE_KEY_VAULT_URI and AZURE_CERT_NAME secrets). SHA-256 file
# digests and an RFC 3161 timestamp (DigiCert), as GizmoSQL's releases. Fails on the first
# file AzureSignTool can't sign; verify-windows-signature.ps1 checks the result.
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]] $Path)
$ErrorActionPreference = 'Stop'
if (-not $env:KEY_VAULT_URI) { throw "KEY_VAULT_URI is not set" }
if (-not $env:CERT_NAME) { throw "CERT_NAME is not set" }
foreach ($file in $Path) {
  $full = (Resolve-Path -LiteralPath $file).Path
  AzureSignTool sign `
    --azure-key-vault-url $env:KEY_VAULT_URI `
    --azure-key-vault-certificate $env:CERT_NAME `
    --azure-key-vault-managed-identity `
    --timestamp-rfc3161 http://timestamp.digicert.com `
    --timestamp-digest sha256 `
    --file-digest sha256 `
    --verbose `
    $full
  if ($LASTEXITCODE -ne 0) { throw "AzureSignTool failed on ${file}: exit $LASTEXITCODE" }
}
