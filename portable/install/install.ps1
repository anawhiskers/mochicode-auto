#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$UserHome = $env:USERPROFILE,
    [switch]$ConfirmInstall,
    [switch]$UpdateExisting,
    [switch]$SkipPluginCommand,
    [bool]$DirectFirst = $false,
    [bool]$TerraFirst = $false,
    [bool]$AstraFirst = $false
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
$pluginRoot = Join-Path $packageRoot 'plugin'
$pluginInstaller = Join-Path $pluginRoot 'install.ps1'
foreach ($required in @($verifier, $pluginRoot, $pluginInstaller)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Package is incomplete. Required path is missing: $required"
    }
}

& $verifier -PackageRoot $packageRoot -Quiet | Out-Null
if (-not $?) {
    throw 'Package verification failed. No installation was attempted.'
}

$userRoot = Assert-PackageExistingPathSafe -Path $UserHome -Label 'User profile' -PathType Directory
$installTarget = Join-Path $userRoot 'plugins\mochicode-auto'
if ($UpdateExisting) {
    $installTarget = Assert-PackageExistingPathSafe -Path $installTarget -Label 'Existing plugin target' -PathType Directory
} else {
    $installTarget = Assert-PackageNewPathSafe -Path $installTarget -Label 'Plugin installation target'
}

Write-Output "Verified portable package at $packageRoot"
if ($UpdateExisting) {
    Write-Output "Planned update target: $installTarget"
} else {
    Write-Output "Planned install target: $installTarget"
}
if (-not $ConfirmInstall) {
    Write-Output 'No changes made. Re-run with -ConfirmInstall after reviewing the target.'
    return
}

$installerParameters = @{
    Source = $pluginRoot
    UserHome = $userRoot
}
if ($ConfirmInstall) {
    $installerParameters.ConfirmInstall = $true
}
if ($UpdateExisting) {
    $installerParameters.UpdateExisting = $true
}
if ($SkipPluginCommand) {
    $installerParameters.SkipPluginCommand = $true
}
if ($DirectFirst) {
    $installerParameters.DirectFirst = $true
}
if ($TerraFirst) {
    $installerParameters.TerraFirst = $true
}
if ($AstraFirst) {
    $installerParameters.AstraFirst = $true
}

& $pluginInstaller @installerParameters
if (-not $?) {
    throw 'Portable plugin installation failed.'
}
Write-Output 'Portable plugin installation completed.'
