[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [switch]$ConfirmRestore
)

$ErrorActionPreference = 'Stop'
$packageRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'verify-package.ps1') -PathType Leaf) {
    (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..') -ErrorAction Stop).Path
}
$helperPath = Join-Path $packageRoot 'portable\install\package-safety.ps1'
if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Package safety helper is missing: $helperPath"
}
. $helperPath
$verifier = Join-Path $packageRoot 'verify-package.ps1'
& $verifier -PackageRoot $packageRoot -Quiet | Out-Null
if (-not $?) {
    throw 'Package verification failed. No restore was attempted.'
}

$manifestFull = Assert-PackageExistingPathSafe -Path $Manifest -Label 'Restore manifest' -PathType File
Write-Output "Verified restore manifest: $manifestFull"
if (-not $ConfirmRestore) {
    Write-Output 'No changes made. Re-run with -ConfirmRestore after reviewing the manifest.'
    return
}

$restore = Join-Path $packageRoot 'plugin\restore.ps1'
Assert-PackageExistingPathSafe -Path $restore -Label 'Bundled restore script' -PathType File | Out-Null
& $restore -Manifest $manifestFull
if (-not $?) {
    throw 'Portable restore failed.'
}
Write-Output 'Portable restore completed.'
