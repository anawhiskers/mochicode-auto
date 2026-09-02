[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest
)

$ErrorActionPreference = 'Stop'
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$data = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$userRoot = [System.IO.Path]::GetFullPath([string]$data.user_root)
$backupRoot = [System.IO.Path]::GetFullPath([string]$data.backup_root)
$restorableWorkflowSkills = @(
    'alignment-watchdog',
    'ask-clarify',
    'chat-overflow-router',
    'coder',
    'debugger',
    'design-intake',
    'design-planning',
    'goal-loop',
    'model-routing',
    'orchestrator',
    'project-brief',
    'reviewer',
    'route-personal-mcp',
    'test-driven-development',
    'token-economy',
    'master-status',
    'obsidian-second-brain'
)
if ([System.IO.Path]::GetFullPath((Split-Path -Parent $manifestPath)) -ne $backupRoot) {
    throw "Manifest backup root does not match its containing directory."
}
function Assert-NoReparseEscape {
    param([string]$Root, [string]$Candidate, [string]$Label)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $fullCandidate = [System.IO.Path]::GetFullPath($Candidate)
    $relative = [System.IO.Path]::GetRelativePath($fullRoot, $fullCandidate)
    if ($relative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "$Label escapes its recorded root: $fullCandidate"
    }
    $cursor = $fullCandidate
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $item.LinkType) {
                throw "$Label crosses a reparse point: $cursor"
            }
            $resolved = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $cursor).Path)
            $resolvedRelative = [System.IO.Path]::GetRelativePath($fullRoot, $resolved)
            if ($resolvedRelative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($resolvedRelative)) {
                throw "$Label resolves outside its recorded root: $resolved"
            }
        }
        if ([string]::Equals($cursor.TrimEnd('\', '/'), $fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) {
            throw "$Label did not reach its recorded root: $fullCandidate"
        }
        $parentRelative = [System.IO.Path]::GetRelativePath($fullRoot, $parent)
        if ($parentRelative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($parentRelative)) {
            throw "$Label ancestor escapes its recorded root: $parent"
        }
        $cursor = $parent
    }
}
function Confirm-RestoreTarget {
    param([string]$Target, [string]$Backup)
    $fullTarget = [System.IO.Path]::GetFullPath($Target)
    $fullBackup = [System.IO.Path]::GetFullPath($Backup)
    $relative = [System.IO.Path]::GetRelativePath($userRoot, $fullTarget).Replace('/', '\')
    if ($relative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "Restore target escapes the recorded user root: $fullTarget"
    }
    $allowed = @(
        '^plugins\\mochicode-auto$',
        '^\.agents\\plugins\\marketplace\.json$',
        '^\.codex\\AGENTS\.md$',
        '^\.codex\\config\.toml$',
        '^\.codex\\mochicode-auto-install\.json$',
        '^\.codex\\agents\\mochicode-[a-z0-9-]+\.toml$',
        '^\.codex\\agents\\(?:explore-cheap|worker-sonnet)\.toml$',
        '^\.codex\\plugins\\cache\\personal\\mochicode-auto$'
    )
    $allowedWorkflowAgents = $restorableWorkflowSkills | Where-Object {
        $relative -eq ".agents\skills\$_\agents"
    }
    $allowedWorkflowPolicy = $restorableWorkflowSkills | Where-Object {
        $relative -eq ".agents\skills\$_\agents\openai.yaml"
    }
    if (
        -not ($allowed | Where-Object { $relative -match $_ }) -and
        -not $allowedWorkflowAgents -and
        -not $allowedWorkflowPolicy
    ) {
        throw "Restore target is outside the MochiCode activation allowlist: $relative"
    }
    $backupRelative = [System.IO.Path]::GetRelativePath($backupRoot, $fullBackup)
    if ($backupRelative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($backupRelative)) {
        throw "Backup source escapes the recorded backup root: $fullBackup"
    }
    Assert-NoReparseEscape $userRoot $fullTarget 'Restore target'
    Assert-NoReparseEscape $backupRoot $fullBackup 'Backup source'
}

function Get-TreeStateHash {
    param(
        [string]$Root,
        [string]$ContainmentRoot,
        [string]$Label
    )
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $fullRoot)) {
        throw "$Label is missing: $fullRoot"
    }
    Assert-NoReparseEscape $ContainmentRoot $fullRoot $Label
    $entries = [System.Collections.Generic.List[string]]::new()
    $rootItem = Get-Item -LiteralPath $fullRoot -Force
    $items = @($rootItem) + @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force)
    foreach ($item in $items) {
        Assert-NoReparseEscape $ContainmentRoot $item.FullName $Label
        $relative = [System.IO.Path]::GetRelativePath(
            $fullRoot,
            [System.IO.Path]::GetFullPath($item.FullName)
        ).Replace('\', '/')
        if ($item.PSIsContainer) {
            $entries.Add("D`0$relative`n")
        } else {
            $fileHash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $entries.Add("F`0$relative`0$($item.Length)`0$fileHash`n")
        }
    }
    $entries.Sort([System.StringComparer]::Ordinal)
    $canonical = ($entries -join '')
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($canonical))
    ).ToLowerInvariant()
}

$entries = @($data.entries)
Assert-NoReparseEscape $userRoot $backupRoot 'Backup root'
Assert-NoReparseEscape $backupRoot $manifestPath 'Manifest path'
foreach ($entry in $entries) {
    Confirm-RestoreTarget ([string]$entry.path) ([string]$entry.backup)
    if ([bool]$entry.existed -and -not (Test-Path -LiteralPath ([string]$entry.backup))) {
        throw "Recorded backup is missing before restore: $([string]$entry.backup)"
    }
}
$registrationWarning = $null
if ($null -ne $data.plugin_registration -and [bool]$data.plugin_registration.attempted) {
    $selector = [string]$data.plugin_registration.selector
    if ($selector -ne 'mochicode-auto@personal') {
        throw "Manifest contains an unexpected plugin registration selector: $selector"
    }
    $codexCommand = Get-Command codex.cmd -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    }
    if ($null -eq $codexCommand) {
        $registrationWarning = 'Codex CLI was unavailable, so registration was reversed from its backed-up config and cache only.'
    } else {
        $priorCodexHome = $env:CODEX_HOME
        $priorUserProfile = $env:USERPROFILE
        $priorHome = $env:HOME
        try {
            $env:CODEX_HOME = Join-Path $userRoot '.codex'
            $env:USERPROFILE = $userRoot
            $env:HOME = $userRoot
            & $codexCommand.Source plugin remove $selector --json | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $registrationWarning = "Codex plugin remove exited $LASTEXITCODE; backed-up config and cache restoration continued."
            }
        } finally {
            if ($null -eq $priorCodexHome) { Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue } else { $env:CODEX_HOME = $priorCodexHome }
            if ($null -eq $priorUserProfile) { Remove-Item Env:USERPROFILE -ErrorAction SilentlyContinue } else { $env:USERPROFILE = $priorUserProfile }
            if ($null -eq $priorHome) { Remove-Item Env:HOME -ErrorAction SilentlyContinue } else { $env:HOME = $priorHome }
        }
    }
}
foreach ($entry in $entries | Sort-Object path -Descending) {
    $target = [string]$entry.path
    Confirm-RestoreTarget $target ([string]$entry.backup)
    if ([bool]$entry.existed) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath ([string]$entry.backup) -Destination $target -Recurse -Force
    } elseif (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
foreach ($entry in $entries) {
    $target = [System.IO.Path]::GetFullPath([string]$entry.path)
    $backup = [System.IO.Path]::GetFullPath([string]$entry.backup)
    if ([bool]$entry.existed) {
        $targetHash = Get-TreeStateHash $target $userRoot 'Restored target'
        $backupHash = Get-TreeStateHash $backup $backupRoot 'Restore backup'
        if ($targetHash -cne $backupHash) {
            throw "Restored target is not byte-equivalent to its backup: $target"
        }
    } elseif (Test-Path -LiteralPath $target) {
        throw "Restore left a newly-created target: $target"
    }
}
Write-Output "Restored files from $manifestPath"
if ($null -ne $registrationWarning) {
    Write-Warning $registrationWarning
}
