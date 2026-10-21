# LightDB Studio optional code signing helper (Windows Authenticode).
#
# Signing is optional: release builds work unsigned (users may see SmartScreen
# warnings). If a certificate is available, sign the built exe and verify it.
#
# Usage:
#   .\scripts\sign_windows.ps1 -Exe "dist\studio\LightDB-Studio\LightDB-Studio.exe"
#   .\scripts\sign_windows.ps1 -Exe "..." -CertPath "C:\certs\codesign.pfx" -Password "***"
#
# Exit codes:
#   0 = signed + verified, OR no certificate configured (explicit skip)
#   1 = signing/verification failed
#   2 = bad usage (missing exe / signtool not found when a cert is provided)
#
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads BOM-less .ps1
# as ANSI/GBK; non-ASCII comments get mangled in the console).
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [string]$CertPath = $env:LIGHTDB_SIGN_CERT,
    [string]$Password = $env:LIGHTDB_SIGN_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Exe)) {
    Write-Error "exe not found: $Exe"
    exit 2
}

if (-not $CertPath) {
    Write-Host "[sign] no certificate configured (LIGHTDB_SIGN_CERT / -CertPath); skip signing."
    Write-Host "[sign] note: unsigned builds may trigger Windows SmartScreen warnings."
    exit 0
}

if (-not (Test-Path -LiteralPath $CertPath)) {
    Write-Error "certificate not found: $CertPath"
    exit 2
}

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (-not $signtool) {
    # Try common Windows SDK locations.
    $candidates = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" `
        -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'x64' } | Select-Object -First 1
    if ($candidates) { $signtool = $candidates.FullName }
}
if (-not $signtool) {
    Write-Error "signtool.exe not found (install Windows SDK or add it to PATH)."
    exit 2
}
$signtoolPath = if ($signtool -is [System.Management.Automation.ApplicationInfo]) { $signtool.Source } else { $signtool }

Write-Host "[sign] signing: $Exe"
$argsList = @('sign', '/fd', 'SHA256', '/f', $CertPath, '/tr', $TimestampUrl, '/td', 'SHA256')
if ($Password) { $argsList += @('/p', $Password) }
$argsList += @($Exe)
& $signtoolPath @argsList
if ($LASTEXITCODE -ne 0) {
    Write-Error "signtool failed with exit code $LASTEXITCODE"
    exit 1
}

$sig = Get-AuthenticodeSignature -LiteralPath $Exe
Write-Host "[sign] status: $($sig.Status)  signer: $($sig.SignerCertificate.Subject)"
if ($sig.Status -ne 'Valid') {
    Write-Error "signature verification failed: $($sig.Status) $($sig.StatusMessage)"
    exit 1
}
Write-Host "[sign] OK"
exit 0
