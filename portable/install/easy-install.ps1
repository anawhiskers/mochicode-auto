[CmdletBinding()]
param(
    [string]$UserHome = $env:USERPROFILE,
    [switch]$ConfirmInstall,
    [switch]$UpdateExisting,
    [switch]$SkipPluginCommand
)

$ErrorActionPreference = 'Stop'
$packageRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'verify-package.ps1') -PathType Leaf) {
    (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..') -ErrorAction Stop).Path
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
& $install @PSBoundParameters
if (-not $?) {
    throw 'Portable easy install failed.'
}
