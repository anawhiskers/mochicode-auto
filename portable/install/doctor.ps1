[CmdletBinding()]
param(
    [string]$UserHome = $env:USERPROFILE,
    [switch]$PackageOnly
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
    throw 'Package verification failed.'
}
Write-Output "Package integrity: PASS ($packageRoot)"
if ($PackageOnly) {
    return
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw 'The portable release supports Windows only.'
}
$userRoot = Assert-PackageExistingPathSafe -Path $UserHome -Label 'User profile' -PathType Directory
$python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $python) {
    throw 'Python is required for the controller doctor check.'
}
$doctorScript = Join-Path $packageRoot 'plugin\scripts\mochicode.py'
Assert-PackageExistingPathSafe -Path $doctorScript -Label 'Bundled controller entrypoint' -PathType File | Out-Null

$oldUserProfile = [System.Environment]::GetEnvironmentVariable('USERPROFILE', 'Process')
$oldCodexHome = [System.Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process')
$oldNoBytecode = [System.Environment]::GetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', 'Process')
try {
    [System.Environment]::SetEnvironmentVariable('USERPROFILE', $userRoot, 'Process')
    [System.Environment]::SetEnvironmentVariable('CODEX_HOME', (Join-Path $userRoot '.codex'), 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', '1', 'Process')
    & $python.Source -B $doctorScript doctor --json
    if ($LASTEXITCODE -ne 0) {
        throw "Controller doctor failed with exit code $LASTEXITCODE."
    }
} finally {
    [System.Environment]::SetEnvironmentVariable('USERPROFILE', $oldUserProfile, 'Process')
    [System.Environment]::SetEnvironmentVariable('CODEX_HOME', $oldCodexHome, 'Process')
    [System.Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', $oldNoBytecode, 'Process')
}
Write-Output 'Controller doctor: PASS'
