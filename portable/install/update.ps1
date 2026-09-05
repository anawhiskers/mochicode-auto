#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$UserHome = $env:USERPROFILE,
    [switch]$ConfirmUpdate,
    [switch]$ConfirmInstall,
    [switch]$SkipPluginCommand,
    [bool]$AstraFirst = $false
)

$ErrorActionPreference = 'Stop'
$packageRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'verify-package.ps1') -PathType Leaf) {
    (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..') -ErrorAction Stop).Path
}
$verifier = Join-Path $packageRoot 'verify-package.ps1'
if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
    throw "Package verifier is missing: $verifier"
}
& $verifier -PackageRoot $packageRoot -Quiet | Out-Null
if (-not $?) {
    throw 'Package verification failed. No update was attempted.'
}

$wrapperRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'verify-package.ps1') -PathType Leaf) {
    $packageRoot
} else {
    $PSScriptRoot
}
$install = Join-Path $wrapperRoot 'install.ps1'
if (-not (Test-Path -LiteralPath $install -PathType Leaf)) {
    throw "Package install wrapper is missing: $install"
}
$parameters = @{
    UserHome = $UserHome
    UpdateExisting = $true
}
if ($ConfirmUpdate -or $ConfirmInstall) {
    $parameters.ConfirmInstall = $true
}
if ($SkipPluginCommand) {
    $parameters.SkipPluginCommand = $true
}
if ($AstraFirst) {
    $parameters.AstraFirst = $true
}
& $install @parameters
if (-not $?) {
    throw 'Portable plugin update failed.'
}
