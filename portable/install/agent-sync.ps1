[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('codex', 'claude', 'kimi', 'zai', 'generic')]
    [string]$Agent,
    [string]$UserHome = $env:USERPROFILE,
    [string]$Target,
    [string]$BackupRoot,
    [switch]$Apply,
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'
$packageRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'verify-package.ps1') -PathType Leaf) {
    (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..') -ErrorAction Stop).Path
}
$verifier = Join-Path $packageRoot 'verify-package.ps1'
& $verifier -PackageRoot $packageRoot -Quiet | Out-Null
if (-not $?) { throw 'Package verification failed. No adapter action was attempted.' }
$python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $python) {
    $python = Get-Command python -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
}
$script = Join-Path $packageRoot 'plugin\scripts\agent_adapter.py'
$arguments = @($script, $(if ($Apply) { 'apply' } else { 'audit' }), '--agent', $Agent, '--home', $UserHome)
if ($Target) { $arguments += @('--target', $Target) }
if ($Apply -and $BackupRoot) { $arguments += @('--backup-root', $BackupRoot) }
if ($Apply -and $Confirm) { $arguments += '--confirm' }
& $python.Source -B @arguments
if ($LASTEXITCODE -ne 0) { throw "Agent adapter failed with exit code $LASTEXITCODE." }
