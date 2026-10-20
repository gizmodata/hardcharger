# Checks that Windows files (executables, MSIs) carry GizmoData's release signature, and
# fails otherwise:
#
#   pwsh .github/scripts/verify-windows-signature.ps1 <file>...
#
# For each file: Windows reports its Authenticode signature Valid (Get-AuthenticodeSignature:
# intact, chaining to a trusted root, so an unsigned, tampered or self-signed file fails), it
# has an RFC 3161 timestamp (so it stays valid after the certificate expires), signtool from
# the Windows SDK verifies it under the default Authenticode policy (`signtool verify /pa`),
# and every file is signed by the same certificate. Every check is a status or an exit code.
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]] $Path)
$ErrorActionPreference = 'Stop'

# signtool: the newest Windows SDK's, for this machine's architecture if it has one (an x64
# signtool runs emulated on arm64).
$kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
$signtool = Get-ChildItem -Path $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
  Sort-Object @{ Expression = { $_.Directory.Name -eq $arch }; Descending = $true },
              @{ Expression = { $_.Directory.Parent.Name }; Descending = $true } |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) { throw "no signtool.exe under $kits" }
Write-Host "signtool: $signtool"

$failures = 0
$thumbprints = @{}
foreach ($file in $Path) {
  Write-Host "--- $file"
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { Write-Host "FAIL: ${file}: no such file"; $failures++; continue }
  $sig = Get-AuthenticodeSignature -LiteralPath $file
  if ($sig.Status -ne 'Valid') {
    Write-Host "FAIL: ${file}: signature status $($sig.Status): $($sig.StatusMessage)"; $failures++; continue
  }
  Write-Host "  signer: $($sig.SignerCertificate.Subject) (thumbprint $($sig.SignerCertificate.Thumbprint), until $($sig.SignerCertificate.NotAfter.ToString('yyyy-MM-dd')))"
  if (-not $sig.TimeStamperCertificate) { Write-Host "FAIL: ${file}: no timestamp"; $failures++ }
  else { Write-Host "  timestamp: $($sig.TimeStamperCertificate.Subject)" }
  $thumbprints[$sig.SignerCertificate.Thumbprint] = $true
  & $signtool verify /pa /v $file
  if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: ${file}: signtool verify /pa exited $LASTEXITCODE"; $failures++ }
}
if ($thumbprints.Count -gt 1) {
  Write-Host "FAIL: the files are signed by $($thumbprints.Count) different certificates: $($thumbprints.Keys -join ', ')"; $failures++
}
if ($failures -gt 0) { throw "$failures signature check(s) failed over $($Path.Count) file(s)" }
Write-Host "all $($Path.Count) file(s): Authenticode Valid, timestamped, signtool-verified, one signer"
