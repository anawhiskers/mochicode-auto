[CmdletBinding()]
param(
    [string]$Source = $PSScriptRoot,
    [string]$UserHome = $env:USERPROFILE,
    [switch]$ConfirmInstall,
    [switch]$SkipPluginCommand,
    [switch]$UpdateExisting,
    [switch]$SkipRoutingCleanup,
    [Alias('EnableRoutingCleanup', 'ApplyRoutingCleanup', 'AllowRoutingCleanup')]
    [switch]$RoutingCleanupOnly,
    [Alias('DisableStaleMcpServers')]
    [switch]$DisableStaleMcp,
    [switch]$RemoveStaleContext,
    [switch]$DirectFirst,
    [switch]$TerraFirst,
    [Alias('RoutingCanaryReceipt', 'FreshTaskCanaryReceipt')]
    [string]$CanaryReceipt
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $Source).Path
$userRoot = [System.IO.Path]::GetFullPath($UserHome)
$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$installedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
$operation = if ($RoutingCleanupOnly) { 'routing_cleanup' } elseif ($UpdateExisting) { 'update_existing' } else { 'install' }
$backupPrefix = if ($RoutingCleanupOnly) { 'mochicode-auto-routing-cleanup' } else { 'mochicode-auto' }
$backupRoot = Join-Path $userRoot ".codex\backups\$backupPrefix-$timestamp"
$manifestEntries = [System.Collections.Generic.List[object]]::new()
$pluginTarget = Join-Path $userRoot 'plugins\mochicode-auto'
$marketplacePath = Join-Path $userRoot '.agents\plugins\marketplace.json'
$latestReceipt = Join-Path $userRoot '.codex\mochicode-auto-install.json'
$manifestPath = Join-Path $backupRoot 'manifest.json'
$configPath = Join-Path $userRoot '.codex\config.toml'
$pluginCacheTarget = Join-Path $userRoot '.codex\plugins\cache\personal\mochicode-auto'
$registrationAttempted = $false
$registrationSucceeded = $false
$routingCanaryValidation = $null
$restoreScript = Join-Path $sourceRoot 'restore.ps1'
$adaptiveConfigScript = Join-Path $sourceRoot 'scripts\adaptive_config.py'
$adaptiveConfigAttempted = $false
$staleMcpChanges = $null
$removedStaleAgents = [System.Collections.Generic.List[string]]::new()
if (-not (Test-Path -LiteralPath $restoreScript -PathType Leaf)) {
    throw "Plugin source is missing restore.ps1: $restoreScript"
}
$conflictingSkills = @(
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
$staleAgentNames = @('explore-cheap.toml', 'worker-sonnet.toml')
$staleMcpNames = @(
    'windows-mcp',
    'filesystem',
    'sequential-thinking',
    'rtk',
    'ruflo',
    'playwright',
    'obsidian'
)
$agentSource = Join-Path $sourceRoot 'config\agents'
$agentTarget = Join-Path $userRoot '.codex\agents'
$agentFiles = @(Get-ChildItem -LiteralPath $agentSource -Filter '*.toml' -File)
$agentsPath = Join-Path $userRoot '.codex\AGENTS.md'

function Assert-NoReparseEscape {
    param([string]$Root, [string]$Candidate, [string]$Label)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $fullCandidate = [System.IO.Path]::GetFullPath($Candidate)
    $relative = [System.IO.Path]::GetRelativePath($fullRoot, $fullCandidate)
    if ($relative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "$Label escapes the recorded user root: $fullCandidate"
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
                throw "$Label resolves outside the recorded user root: $resolved"
            }
        }
        if ([string]::Equals($cursor.TrimEnd('\', '/'), $fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) {
            throw "$Label did not reach the recorded user root: $fullCandidate"
        }
        $parentRelative = [System.IO.Path]::GetRelativePath($fullRoot, $parent)
        if ($parentRelative.StartsWith('..') -or [System.IO.Path]::IsPathRooted($parentRelative)) {
            throw "$Label ancestor escapes the recorded user root: $parent"
        }
        $cursor = $parent
    }
}

function Get-SourceManifestHash {
    param([string]$Root)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $transientDirectories = @('.git', '.pytest_cache', '__pycache__')
    $entries = [System.Collections.Generic.List[string]]::new()
    foreach ($item in @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force)) {
        $relative = [System.IO.Path]::GetRelativePath($fullRoot, [System.IO.Path]::GetFullPath($item.FullName)).Replace('\', '/')
        $parts = $relative.Split('/')
        if ($parts | Where-Object { $_ -in $transientDirectories }) {
            continue
        }
        if (-not $item.PSIsContainer -and [System.IO.Path]::GetExtension($relative) -eq '.pyc') {
            continue
        }
        Assert-NoReparseEscape $fullRoot $item.FullName 'Source package item'
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

function Get-TreeStateHash {
    param([string]$Root, [string]$Label = 'Tree')
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $fullRoot)) {
        throw "$Label is missing: $fullRoot"
    }
    Assert-NoReparseEscape $userRoot $fullRoot $Label
    $entries = [System.Collections.Generic.List[string]]::new()
    $rootItem = Get-Item -LiteralPath $fullRoot -Force
    $items = @($rootItem) + @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force)
    foreach ($item in $items) {
        Assert-NoReparseEscape $userRoot $item.FullName $Label
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

function Assert-CanaryBoolean {
    param([object]$Value, [string]$Label, [bool]$Expected)
    if ($Value -isnot [bool] -or $Value -ne $Expected) {
        throw "Canary receipt has an invalid $Label value."
    }
}

function Assert-Sha256Value {
    param([object]$Value, [string]$Label)
    if ($Value -isnot [string] -or $Value -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Canary receipt has an invalid $Label SHA-256 value."
    }
    return $Value.ToLowerInvariant()
}

function Assert-NoSensitiveReceiptFields {
    param([object]$Value, [string]$Label)
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        foreach ($property in $Value.PSObject.Properties) {
            if (
                $property.Name -match '(?i)^(prompt|prompt[_-]?text|raw[_-]?prompt)$' -or
                $property.Name -match '(?i)(^|[_-])(secret|password|api[_-]?key|credential|token)$'
            ) {
                throw "$Label contains forbidden raw prompt or secret field: $($property.Name)"
            }
            Assert-NoSensitiveReceiptFields $property.Value $Label
        }
    } elseif ($Value -is [System.Array]) {
        foreach ($item in $Value) {
            Assert-NoSensitiveReceiptFields $item $Label
        }
    }
}

function Assert-OnlyObjectProperties {
    param(
        [object]$Value,
        [string[]]$Allowed,
        [string]$Label
    )
    if ($Value -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$Label must be a JSON object."
    }
    foreach ($property in $Value.PSObject.Properties) {
        if ($Allowed -cnotcontains $property.Name) {
            throw "$Label contains an unexpected field: $($property.Name)"
        }
    }
}

function Convert-JsonBytesToObject {
    param([byte[]]$Bytes, [string]$Label, [string]$Path)
    try {
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($Bytes)
        $value = $text | ConvertFrom-Json
    } catch {
        throw "$Label is malformed or is not strict UTF-8: $Path"
    }
    if ($value -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$Label must be a JSON object: $Path"
    }
    return $value
}

function Assert-CanaryReceipt {
    param(
        [string]$Path,
        [string]$ExpectedPluginVersion,
        [string]$ExpectedSourceManifestHash
    )
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    Assert-NoReparseEscape $userRoot $fullPath 'Canary receipt'
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Canary receipt is missing or is not a file: $fullPath"
    }
    $receiptBytes = [System.IO.File]::ReadAllBytes($fullPath)
    $receiptHash = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($receiptBytes)
    ).ToLowerInvariant()
    $receipt = Convert-JsonBytesToObject $receiptBytes 'Canary receipt' $fullPath
    Assert-NoSensitiveReceiptFields $receipt 'Canary receipt'
    Assert-OnlyObjectProperties $receipt @(
        'schema_version',
        'plugin_name',
        'source_plugin_version',
        'cache_plugin_version',
        'source_manifest_hash',
        'completed_at_utc',
        'command_process',
        'workspace_tree',
        'evidence'
    ) 'Canary receipt'
    if ($receipt.schema_version -isnot [long] -or $receipt.schema_version -ne 2) {
        throw "Canary receipt has an unsupported schema version: $fullPath"
    }
    if ($receipt.plugin_name -isnot [string] -or $receipt.plugin_name -cne 'mochicode-auto') {
        throw "Canary receipt names an unexpected plugin: $fullPath"
    }
    if (
        $receipt.source_plugin_version -isnot [string] -or
        $receipt.source_plugin_version -cne $ExpectedPluginVersion
    ) {
        throw "Canary receipt source plugin version does not match the source package: $fullPath"
    }
    if (
        $receipt.cache_plugin_version -isnot [string] -or
        $receipt.cache_plugin_version -cne $ExpectedPluginVersion
    ) {
        throw "Canary receipt cache plugin version does not match the source package: $fullPath"
    }
    $recordedSourceManifestHash = Assert-Sha256Value $receipt.source_manifest_hash 'source manifest'
    if ($recordedSourceManifestHash -cne $ExpectedSourceManifestHash) {
        throw "Canary receipt source manifest hash does not match the source package: $fullPath"
    }
    $completedAt = $null
    if ($receipt.completed_at_utc -is [DateTime]) {
        if ($receipt.completed_at_utc.Kind -ne [DateTimeKind]::Utc) {
            throw "Canary receipt has no UTC completion timestamp: $fullPath"
        }
        $completedAt = [DateTimeOffset]$receipt.completed_at_utc
    } elseif ($receipt.completed_at_utc -is [string] -and $receipt.completed_at_utc.EndsWith('Z')) {
        try {
            $completedAt = [DateTimeOffset]::Parse(
                $receipt.completed_at_utc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            )
        } catch {
            throw "Canary receipt has an invalid completion timestamp: $fullPath"
        }
    } else {
        throw "Canary receipt has no UTC completion timestamp: $fullPath"
    }
    $now = [DateTimeOffset]::UtcNow
    if ($completedAt -gt $now.AddMinutes(5) -or $completedAt -lt $now.AddHours(-24)) {
        throw "Canary receipt is stale or from the future: $fullPath"
    }

    $command = $receipt.command_process
    if ($command -isnot [System.Management.Automation.PSCustomObject]) {
        throw "Canary receipt has no command/process receipt: $fullPath"
    }
    Assert-OnlyObjectProperties $command @(
        'fresh_task_id',
        'natural_prompt_sha256',
        'model_output_bytes',
        'model_output_sha256',
        'exit_code',
        'timed_out'
    ) 'Canary command/process receipt'
    if (
        $command.fresh_task_id -isnot [string] -or
        $command.fresh_task_id -notmatch '^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$'
    ) {
        throw "Canary receipt has an invalid fresh task ID: $fullPath"
    }
    $promptHash = Assert-Sha256Value $command.natural_prompt_sha256 'natural prompt'
    if ($command.model_output_bytes -isnot [long] -or $command.model_output_bytes -le 0) {
        throw "Canary receipt has an invalid model output byte count: $fullPath"
    }
    $modelOutputHash = Assert-Sha256Value $command.model_output_sha256 'model output'
    if ($command.exit_code -isnot [long] -or $command.exit_code -ne 0) {
        throw "Canary process did not exit successfully: $fullPath"
    }
    Assert-CanaryBoolean $command.timed_out 'command_process.timed_out' $false

    $workspace = $receipt.workspace_tree
    if ($workspace -isnot [System.Management.Automation.PSCustomObject]) {
        throw "Canary receipt has no workspace tree hashes: $fullPath"
    }
    Assert-OnlyObjectProperties $workspace @('before_sha256', 'after_sha256') 'Canary workspace receipt'
    $workspaceBeforeHash = Assert-Sha256Value $workspace.before_sha256 'workspace tree before'
    $workspaceAfterHash = Assert-Sha256Value $workspace.after_sha256 'workspace tree after'
    if ($workspaceBeforeHash -cne $workspaceAfterHash) {
        throw "Canary receipt records workspace file changes: $fullPath"
    }

    $binding = $receipt.evidence
    if ($binding -isnot [System.Management.Automation.PSCustomObject]) {
        throw "Canary receipt has no raw evidence binding: $fullPath"
    }
    Assert-OnlyObjectProperties $binding @('path', 'bytes', 'sha256') 'Canary evidence binding'
    if ($binding.path -isnot [string] -or [string]::IsNullOrWhiteSpace($binding.path)) {
        throw "Canary receipt has an invalid raw evidence path: $fullPath"
    }
    if ($binding.bytes -isnot [long] -or $binding.bytes -le 0) {
        throw "Canary receipt has an invalid raw evidence byte count: $fullPath"
    }
    $boundEvidenceHash = Assert-Sha256Value $binding.sha256 'raw evidence'
    $fullEvidencePath = if ([System.IO.Path]::IsPathRooted($binding.path)) {
        [System.IO.Path]::GetFullPath($binding.path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $userRoot $binding.path))
    }
    Assert-NoReparseEscape $userRoot $fullEvidencePath 'Canary evidence'
    if (-not (Test-Path -LiteralPath $fullEvidencePath -PathType Leaf)) {
        throw "Canary evidence is missing or is not a file: $fullEvidencePath"
    }
    $evidenceBytes = [System.IO.File]::ReadAllBytes($fullEvidencePath)
    $evidenceHash = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($evidenceBytes)
    ).ToLowerInvariant()
    if ($evidenceBytes.LongLength -ne $binding.bytes -or $evidenceHash -cne $boundEvidenceHash) {
        throw "Canary evidence byte count or SHA-256 does not match its receipt: $fullEvidencePath"
    }
    if (
        $command.model_output_bytes -ne $binding.bytes -or
        $modelOutputHash -cne $boundEvidenceHash
    ) {
        throw "Canary model output does not bind the raw evidence file: $fullEvidencePath"
    }

    $evidence = Convert-JsonBytesToObject $evidenceBytes 'Canary evidence' $fullEvidencePath
    Assert-NoSensitiveReceiptFields $evidence 'Canary evidence'
    Assert-OnlyObjectProperties $evidence @(
        'schema_version',
        'success_marker',
        'plugin_name',
        'plugin_version',
        'source_manifest_hash',
        'fresh_task_id',
        'natural_prompt_sha256',
        'workspace_tree_before_sha256',
        'workspace_tree_after_sha256'
    ) 'Canary evidence'
    if ($evidence.schema_version -isnot [long] -or $evidence.schema_version -ne 1) {
        throw "Canary evidence has an unsupported schema version: $fullEvidencePath"
    }
    if (
        $evidence.success_marker -isnot [string] -or
        $evidence.success_marker -cne 'mochicode-auto.fresh-natural-prompt-activation.success'
    ) {
        throw "Canary evidence has no expected structured success marker: $fullEvidencePath"
    }
    if ($evidence.plugin_name -isnot [string] -or $evidence.plugin_name -cne 'mochicode-auto') {
        throw "Canary evidence names an unexpected plugin: $fullEvidencePath"
    }
    if ($evidence.plugin_version -isnot [string] -or $evidence.plugin_version -cne $ExpectedPluginVersion) {
        throw "Canary evidence plugin version does not match the source package: $fullEvidencePath"
    }
    $evidenceSourceHash = Assert-Sha256Value $evidence.source_manifest_hash 'evidence source manifest'
    if ($evidenceSourceHash -cne $ExpectedSourceManifestHash) {
        throw "Canary evidence source manifest hash does not match the source package: $fullEvidencePath"
    }
    if ($evidence.fresh_task_id -isnot [string] -or $evidence.fresh_task_id -cne $command.fresh_task_id) {
        throw "Canary evidence fresh task ID does not match the process receipt: $fullEvidencePath"
    }
    $evidencePromptHash = Assert-Sha256Value $evidence.natural_prompt_sha256 'evidence natural prompt'
    if ($evidencePromptHash -cne $promptHash) {
        throw "Canary evidence natural prompt hash does not match the process receipt: $fullEvidencePath"
    }
    $evidenceWorkspaceBefore = Assert-Sha256Value $evidence.workspace_tree_before_sha256 'evidence workspace tree before'
    $evidenceWorkspaceAfter = Assert-Sha256Value $evidence.workspace_tree_after_sha256 'evidence workspace tree after'
    if (
        $evidenceWorkspaceBefore -cne $workspaceBeforeHash -or
        $evidenceWorkspaceAfter -cne $workspaceAfterHash -or
        $evidenceWorkspaceBefore -cne $evidenceWorkspaceAfter
    ) {
        throw "Canary evidence workspace hashes do not prove zero file changes: $fullEvidencePath"
    }

    return [PSCustomObject]@{
        receipt_path = $fullPath
        receipt_bytes = $receiptBytes.LongLength
        receipt_sha256 = $receiptHash
        evidence_path = $fullEvidencePath
        evidence_bytes = $evidenceBytes.LongLength
        evidence_sha256 = $evidenceHash
        fresh_task_id = $command.fresh_task_id
        natural_prompt_sha256 = $promptHash
    }
}

function Write-JsonAtomically {
    param([string]$Path, [object]$Value, [int]$Depth = 12)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent ('.' + [System.IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = $Value | ConvertTo-Json -Depth $Depth
        [System.IO.File]::WriteAllText(
            $temporary,
            $json + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-Manifest {
    Write-JsonAtomically $manifestPath ([PSCustomObject]@{
        schema_version = 1
        operation = $operation
        installed_at_utc = $installedAtUtc
        user_root = $userRoot
        backup_root = $backupRoot
        plugin_source = $sourceRoot
        plugin_target = $pluginTarget
        marketplace = $marketplacePath
        plugin_registration = [PSCustomObject]@{
            selector = 'mochicode-auto@personal'
            attempted = [bool]$registrationAttempted
            succeeded = [bool]$registrationSucceeded
            config_path = $configPath
            cache_path = $pluginCacheTarget
        }
        routing_canary = $routingCanaryValidation
        adaptive_config = [PSCustomObject]@{
            attempted = [bool]$adaptiveConfigAttempted
            remove_stale_context = [bool]$RemoveStaleContext
            direct_first = [bool]$DirectFirst
            terra_first = [bool]$TerraFirst
        }
        confirm_install = [bool]$ConfirmInstall
        stale_mcp_changes = $staleMcpChanges
        removed_stale_agents = @($removedStaleAgents)
        entries = @($manifestEntries)
    }) 8
}

function Copy-SourceTreeFiltered {
    param([string]$Root, [string]$Destination)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $fullDestination = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
    $transientDirectories = @('.git', '.pytest_cache', '__pycache__')
    New-Item -ItemType Directory -Force -Path $fullDestination | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force)) {
        $relative = [System.IO.Path]::GetRelativePath(
            $fullRoot,
            [System.IO.Path]::GetFullPath($item.FullName)
        )
        $parts = $relative.Replace('\', '/').Split('/')
        if ($parts | Where-Object { $_ -in $transientDirectories }) {
            continue
        }
        if (-not $item.PSIsContainer -and [System.IO.Path]::GetExtension($relative) -eq '.pyc') {
            continue
        }
        Assert-NoReparseEscape $fullRoot $item.FullName 'Source package item'
        $target = Join-Path $fullDestination $relative
        Assert-NoReparseEscape $userRoot $target 'Installed plugin item'
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
        } else {
            $parent = Split-Path -Parent $target
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Add-BackupEntry {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    Assert-NoReparseEscape $userRoot $full 'Activation target'
    if (@($manifestEntries | Where-Object path -eq $full).Count -gt 0) {
        return
    }
    $existed = Test-Path -LiteralPath $full
    $digest = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($full))
    $relative = [Convert]::ToHexString($digest)
    $backup = Join-Path $backupRoot $relative
    if ($existed) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null
        Copy-Item -LiteralPath $full -Destination $backup -Recurse -Force
    }
    $manifestEntries.Add([PSCustomObject]@{
        path = $full
        existed = [bool]$existed
        backup = $backup
    })
    Write-Manifest
}

function Set-ImplicitPolicyFalse {
    param([string]$SkillRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $SkillRoot 'SKILL.md'))) {
        return
    }
    $agentsRoot = Join-Path $SkillRoot 'agents'
    $policyPath = Join-Path $agentsRoot 'openai.yaml'
    Add-BackupEntry $agentsRoot
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $policyPath) | Out-Null
    if (Test-Path -LiteralPath $policyPath) {
        $text = Get-Content -LiteralPath $policyPath -Raw
        if ($text -match '(?m)^\s*allow_implicit_invocation:\s*(true|false)\s*$') {
            $text = [regex]::Replace(
                $text,
                '(?m)^(\s*allow_implicit_invocation:)\s*(true|false)\s*$',
                '$1 false',
                1
            )
        } elseif ($text -match '(?m)^policy:\s*$') {
            $text = [regex]::Replace(
                $text,
                '(?m)^policy:\s*$',
                "policy:`n  allow_implicit_invocation: false",
                1
            )
        } else {
            $text = $text.TrimEnd() + "`n`npolicy:`n  allow_implicit_invocation: false`n"
        }
    } else {
        $text = "policy:`n  allow_implicit_invocation: false`n"
    }
    Set-Content -LiteralPath $policyPath -Value $text -Encoding utf8NoBOM
}

function Get-Utf8TextState {
    param(
        [string]$Path,
        [string]$Label
    )
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $hasBom = (
        $bytes.Length -ge 3 -and
        $bytes[0] -eq 0xef -and
        $bytes[1] -eq 0xbb -and
        $bytes[2] -eq 0xbf
    )
    $offset = if ($hasBom) { 3 } else { 0 }
    try {
        $encoding = [System.Text.UTF8Encoding]::new($false, $true)
        $text = if ($bytes.Length -eq $offset) {
            ''
        } else {
            $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
        }
    } catch {
        throw "$Label is not strict UTF-8: $Path"
    }
    return [PSCustomObject]@{
        text = $text
        has_bom = [bool]$hasBom
    }
}

function Write-Utf8TextAtomically {
    param(
        [string]$Path,
        [string]$Text,
        [bool]$HasBom
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent ('.' + [System.IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $encoding = [System.Text.UTF8Encoding]::new($false)
        $payload = $encoding.GetBytes($Text)
        if ($HasBom) {
            $bytes = [byte[]]::new($payload.Length + 3)
            $bytes[0] = 0xef
            $bytes[1] = 0xbb
            $bytes[2] = 0xbf
            [Array]::Copy($payload, 0, $bytes, 3, $payload.Length)
        } else {
            $bytes = $payload
        }
        [System.IO.File]::WriteAllBytes($temporary, $bytes)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Set-MochiCodeMarkerBlock {
    param(
        [string]$Path,
        [string]$Begin,
        [string]$End
    )
    $state = if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Get-Utf8TextState $Path 'Codex instructions'
    } else {
        [PSCustomObject]@{
            text = "# Global Codex instructions`n"
            has_bom = $false
        }
    }
    $text = [string]$state.text
    $lineEnding = if ($text.Contains("`r`n")) {
        "`r`n"
    } elseif ($text.Contains("`n")) {
        "`n"
    } elseif ($text.Contains("`r")) {
        "`r"
    } else {
        "`n"
    }
    $block = @(
        $Begin
        '## Automatic model and agent routing'
        ''
        '- MochiCode Auto is the only automatic top-level workflow for substantive project work. The user supplies the real goal once; route, model, effort, skills, children, checkpoints, judges, and stop conditions are selected automatically.'
        '- Keep trivial lookups, tiny rewrites, one obvious reversible action, one-step navigation, and ordinary conversation direct in the current parent.'
        '- Sol High is the default substantive parent. It owns product, architecture, curriculum, visual design, UI/UX, interaction, motion, tightly coupled implementation, integration, debugging, live verification, and final judgment. Use Max only for consequential whole-product, architecture, security/release, or repeated quality-failure decisions; Ultra is exceptional.'
        '- Direct Sol is a stock-quality passthrough. Preserve the original goal and repository instructions; do not add planning ceremony, optional skills, workers, ledgers, or critics unless their concrete trigger becomes observable.'
        '- Keep small or sequential routine work direct Sol. Use a real Luna Medium child only for a sizable independent implementation leaf with a concrete expected saving from leaf size, slow verification, external build latency, context isolation, or batch volume. Independence alone is insufficient. Escalate to Luna Max after failed acceptance or proven difficulty.'
        '- Never label parent-executed work as Luna. Report a child model and effort only when current session evidence or a real child receipt proves it.'
        '- Fan out only frozen, disjoint leaves under a Sol parent after predicting a concrete critical-path saving. Independence alone is insufficient. Start with two workers and cap normal live waves at three until a representative benchmark proves a larger wave improves accepted quality per token and wall time.'
        '- Terra is absent from the default native route. The deterministic controller and its Terra contract/review roles are experimental and must never be selected automatically until the published promotion defects are fixed and rebenchmarked.'
        '- For genuinely interruption-prone or multi-context work, keep a compact verified state ledger outside production paths. Do not add ledger ceremony to short tasks.'
        '- Activate systematic debugging or TDD for security, concurrency, or data-integrity consequence, after incomplete diagnosis or failed first-pass acceptance, or when no executable reproduction exists. Do not force it merely because a bug is boundary-sensitive. Activate long-horizon state or critics only when their mechanism matches the task. Never run unbounded perfection loops.'
        '- A warranted quality gate uses three fresh task-relevant read-only judges with distinct lenses, one Sol adjudication, at most one integrated repair pass, then the same hard checks and stop. For mixed UI-and-logic work, default to product hierarchy, accessibility and interaction, and state integration, and preserve unresolved visual preference for human comparison.'
        '- For consequential interactive behavior, verify running, paused, re-entrant, editing-during-execution, keyboard-focus, narrow-layout, and actual reduced-motion states when applicable.'
        '- A prompt beginning with `[MOCHICODE_CHILD]` performs only its assigned role and does not invoke a top-level workflow or spawn descendants.'
        '- Children never spawn grandchildren. Eight live children is a host ceiling, not a target. Preserve one writer per file or shared state, concise scoped context, and successive waves after completed children close.'
        '- A future human test is a readiness state, not a blocker. Prepare it and continue independent reversible work; report blocked only for a real human-only, external, or irreversible gate with no useful work left.'
        '- Internal failures are work, not blockers. Controller or child failure, missing handoff/state, timeout, tool error, red tests, unresolved architecture, or worktree confusion must trigger diagnosis, stronger or fresh routing, route change, prerequisite repair, or packet parking while independent work continues.'
        '- A blocked goal is valid only for a specific human-only action, unavailable external authority/credential/service/hardware, spending, production/deployment approval, destructive permission, or another external condition after every reversible path is exhausted. Name the exact unblock action and preserve resumable state.'
        '- Report milestones only, never repeated waiting updates or empty completed turns. Parent handoffs record each child role, model and effort, owned paths, tests, result, and stop reason.'
        '- Preserve every existing confirmation, privacy, process, secret, repository, and external-effect safety rule. MochiCode never weakens them.'
        '- Routing cleanup may make broad workflow skills explicit-only; `master-status` remains manually available through explicit invocation.'
        $End
    ) -join $lineEnding

    $beginIndex = $text.IndexOf($Begin, [System.StringComparison]::Ordinal)
    $endIndex = if ($beginIndex -ge 0) {
        $text.IndexOf($End, $beginIndex + $Begin.Length, [System.StringComparison]::Ordinal)
    } else {
        -1
    }
    if ($beginIndex -ge 0) {
        if ($endIndex -lt 0) {
            throw "Codex instructions contain an incomplete MochiCode marker block: $Path"
        }
        if ($text.IndexOf($Begin, $beginIndex + $Begin.Length, [System.StringComparison]::Ordinal) -ge 0) {
            throw "Codex instructions contain multiple MochiCode marker blocks: $Path"
        }
        if ($text.IndexOf($End, $endIndex + $End.Length, [System.StringComparison]::Ordinal) -ge 0) {
            throw "Codex instructions contain multiple MochiCode marker blocks: $Path"
        }
        $updated = $text.Substring(0, $beginIndex) + $block + $text.Substring($endIndex + $End.Length)
    } elseif ($text.IndexOf($End, [System.StringComparison]::Ordinal) -ge 0) {
        throw "Codex instructions contain an orphaned MochiCode end marker: $Path"
    } elseif ($text.Length -eq 0) {
        $updated = $block + $lineEnding
    } elseif ($text.EndsWith($lineEnding, [System.StringComparison]::Ordinal)) {
        $updated = $text + $lineEnding + $block + $lineEnding
    } else {
        $updated = $text + $lineEnding + $lineEnding + $block + $lineEnding
    }
    if ($updated -cne $text) {
        Write-Utf8TextAtomically $Path $updated ([bool]$state.has_bom)
    }
}

function Resolve-CodexExecutable {
    foreach ($name in @('codex.cmd', 'codex')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }
        $candidate = if (-not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            [string]$command.Source
        } else {
            [string]$command.Path
        }
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    throw 'Codex CLI is required for the adaptive config audit/merge transaction.'
}

function Invoke-AdaptiveCommand {
    param(
        [string]$PythonPath,
        [string[]]$Arguments
    )
    $output = & $PythonPath @Arguments 2> $null | Out-String
    return [PSCustomObject]@{
        output = [string]$output
        exit_code = [int]$LASTEXITCODE
    }
}

function Assert-AdaptiveAuditReport {
    param(
        [object]$Report,
        [string]$ExpectedConfigPath
    )
    Assert-NoSensitiveReceiptFields $Report 'Adaptive audit report'
    Assert-OnlyObjectProperties $Report @(
        'ok',
        'config_valid',
        'config',
        'capabilities',
        'unsupported_assumptions',
        'preservation',
        'warnings'
    ) 'Adaptive audit report'
    if ($Report.ok -isnot [bool] -or $Report.ok -ne $true) {
        throw 'Adaptive config audit did not prove a usable Codex capability report.'
    }
    if ($Report.config_valid -isnot [bool] -or $Report.config_valid -ne $true) {
        throw 'Adaptive config audit did not validate the existing TOML.'
    }
    $config = $Report.config
    Assert-OnlyObjectProperties $config @(
        'path',
        'root_context_overrides',
        'mcp_server_names',
        'root_key_count',
        'table_count'
    ) 'Adaptive audit config report'
    if ($config.path -isnot [string]) {
        throw 'Adaptive audit report has no config path binding.'
    }
    if (-not [string]::Equals(
        ([System.IO.Path]::GetFullPath($config.path)).TrimEnd('\', '/'),
        ([System.IO.Path]::GetFullPath($ExpectedConfigPath)).TrimEnd('\', '/'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Adaptive audit report does not bind the requested config.'
    }
    if ($config.mcp_server_names -isnot [System.Array]) {
        throw 'Adaptive audit report has no redacted MCP name list.'
    }
    if ($config.root_key_count -isnot [long] -or $config.table_count -isnot [long]) {
        throw 'Adaptive audit report has invalid TOML counts.'
    }
    $capabilities = $Report.capabilities
    Assert-OnlyObjectProperties $capabilities @(
        'executable',
        'version',
        'available',
        'catalog_available',
        'model_catalog',
        'selected_model',
        'selected_model_bounds',
        'agent_defaults_probe',
        'feature_probe',
        'unsupported_assumptions',
        'warnings'
    ) 'Adaptive audit capability report'
    if (
        $capabilities.available -isnot [bool] -or $capabilities.available -ne $true -or
        $capabilities.catalog_available -isnot [bool] -or $capabilities.catalog_available -ne $true
    ) {
        throw 'Adaptive config audit did not prove the Codex executable and model catalog.'
    }
    $preservation = $Report.preservation
    Assert-OnlyObjectProperties $preservation @('unowned_values_and_bytes', 'secrets_emitted') 'Adaptive audit preservation report'
    if (
        $preservation.unowned_values_and_bytes -isnot [string] -or
        $preservation.unowned_values_and_bytes -cne 'preserved' -or
        $preservation.secrets_emitted -isnot [bool] -or
        $preservation.secrets_emitted -ne $false
    ) {
        throw 'Adaptive audit report did not prove redacted preservation.'
    }
}

function Assert-AdaptiveMergeReport {
    param(
        [object]$Report,
        [string]$ExpectedConfigPath,
        [string]$ExpectedOutputPath,
        [string]$ExpectedReportPath
    )
    Assert-NoSensitiveReceiptFields $Report 'Adaptive merge report'
    Assert-OnlyObjectProperties $Report @(
        'ok',
        'command',
        'config_valid',
        'output',
        'report',
        'capabilities',
        'changes',
        'removed_stale_context',
        'warnings',
        'validation',
        'preservation'
    ) 'Adaptive merge report'
    if (
        $Report.ok -isnot [bool] -or $Report.ok -ne $true -or
        $Report.command -isnot [string] -or $Report.command -cne 'merge' -or
        $Report.config_valid -isnot [bool] -or $Report.config_valid -ne $true
    ) {
        throw 'Adaptive config merge report did not prove a valid merge.'
    }
    foreach ($binding in @(@('output', $ExpectedOutputPath), @('report', $ExpectedReportPath))) {
        $value = $Report.($binding[0])
        if ($value -isnot [string] -or -not [string]::Equals(
            ([System.IO.Path]::GetFullPath($value)).TrimEnd('\', '/'),
            ([System.IO.Path]::GetFullPath([string]$binding[1])).TrimEnd('\', '/'),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Adaptive merge report does not bind its $($binding[0]) path."
        }
    }
    $capabilities = $Report.capabilities
    if (
        $capabilities -isnot [System.Management.Automation.PSCustomObject] -or
        $capabilities.available -isnot [bool] -or $capabilities.available -ne $true -or
        $capabilities.catalog_available -isnot [bool] -or $capabilities.catalog_available -ne $true
    ) {
        throw 'Adaptive merge report did not prove the Codex capability audit.'
    }
    $changes = $Report.changes
    Assert-OnlyObjectProperties $changes @(
        'added_root_defaults',
        'added_features',
        'added_agent_defaults',
        'disabled_mcp',
        'already_disabled_mcp',
        'missing_mcp',
        'removed_stale_context',
        'preserved_context',
        'set_direct_first_defaults',
        'set_terra_first_defaults',
        'removed_default_service_tier'
    ) 'Adaptive merge changes report'
    foreach ($name in @(
        'added_root_defaults',
        'added_features',
        'added_agent_defaults',
        'disabled_mcp',
        'already_disabled_mcp',
        'missing_mcp',
        'removed_stale_context',
        'preserved_context',
        'set_direct_first_defaults',
        'set_terra_first_defaults',
        'removed_default_service_tier'
    )) {
        if ($changes.$name -isnot [System.Array]) {
            throw "Adaptive merge report has an invalid changes.$name list."
        }
    }
    if ($Report.removed_stale_context -isnot [System.Array]) {
        throw 'Adaptive merge report has an invalid removed_stale_context list.'
    }
    $validation = $Report.validation
    Assert-OnlyObjectProperties $validation @('input_toml', 'output_toml', 'secrets_emitted') 'Adaptive merge validation report'
    if (
        $validation.input_toml -isnot [bool] -or $validation.input_toml -ne $true -or
        $validation.output_toml -isnot [bool] -or $validation.output_toml -ne $true -or
        $validation.secrets_emitted -isnot [bool] -or $validation.secrets_emitted -ne $false
    ) {
        throw 'Adaptive merge report did not prove redacted TOML validation.'
    }
    $preservation = $Report.preservation
    Assert-OnlyObjectProperties $preservation @('unowned_values_and_bytes', 'secrets_emitted') 'Adaptive merge preservation report'
    if (
        $preservation.unowned_values_and_bytes -isnot [string] -or
        $preservation.unowned_values_and_bytes -cne 'preserved' -or
        $preservation.secrets_emitted -isnot [bool] -or
        $preservation.secrets_emitted -ne $false
    ) {
        throw 'Adaptive merge report did not prove machine-specific preservation.'
    }
}

function Assert-AdaptiveCandidateLoads {
    param(
        [string]$CandidatePath,
        [string]$CodexPath
    )
    $validationRoot = Join-Path $backupRoot ('.adaptive-config-validation-' + [guid]::NewGuid().ToString('N'))
    Assert-NoReparseEscape $userRoot $validationRoot 'Adaptive config validation root'
    New-Item -ItemType Directory -Force -Path $validationRoot | Out-Null
    try {
        $validationConfig = Join-Path $validationRoot 'config.toml'
        $validationTemp = Join-Path $validationRoot 'temp'
        $validationAppData = Join-Path $validationRoot 'appdata'
        $validationLocalAppData = Join-Path $validationRoot 'localappdata'
        New-Item -ItemType Directory -Force -Path $validationTemp, $validationAppData, $validationLocalAppData | Out-Null
        Copy-Item -LiteralPath $CandidatePath -Destination $validationConfig -Force
        $environmentNames = @('CODEX_HOME', 'USERPROFILE', 'HOME', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA')
        $previous = @{}
        foreach ($name in $environmentNames) {
            $previous[$name] = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        }
        try {
            $env:CODEX_HOME = $validationRoot
            $env:USERPROFILE = $validationRoot
            $env:HOME = $validationRoot
            $env:TEMP = $validationTemp
            $env:TMP = $validationTemp
            $env:APPDATA = $validationAppData
            $env:LOCALAPPDATA = $validationLocalAppData
            & $CodexPath features list > $null 2> $null
            $exitCode = $LASTEXITCODE
        } finally {
            foreach ($name in $environmentNames) {
                [System.Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
            }
        }
        if ($exitCode -ne 0) {
            throw "Codex rejected the adaptive config candidate during load validation with exit $exitCode."
        }
    } finally {
        Assert-NoReparseEscape $userRoot $validationRoot 'Adaptive config validation root'
        if (Test-Path -LiteralPath $validationRoot) {
            Remove-Item -LiteralPath $validationRoot -Recurse -Force
        }
    }
}

function Replace-ConfigAtomically {
    param(
        [string]$CandidatePath,
        [string]$DestinationPath
    )
    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        $replacementBackup = Join-Path $backupRoot ('.adaptive-config-replacement-' + [guid]::NewGuid().ToString('N') + '.bak')
        Assert-NoReparseEscape $userRoot $replacementBackup 'Adaptive replacement backup path'
        try {
            [System.IO.File]::Replace($CandidatePath, $DestinationPath, $replacementBackup, $true)
        } finally {
            Remove-OwnedAdaptivePath $replacementBackup
        }
    } else {
        [System.IO.File]::Move($CandidatePath, $DestinationPath)
    }
}

function Remove-OwnedAdaptivePath {
    param([string]$Path)
    Assert-NoReparseEscape $backupRoot $Path 'Adaptive temporary path'
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Adaptive temporary path is not an owned file: $Path"
    }
    Remove-Item -LiteralPath $Path -Force
}

function Invoke-AdaptiveConfigTransaction {
    param(
        [switch]$RemoveStaleContext,
        [switch]$DirectFirst,
        [switch]$TerraFirst,
        [string[]]$DisableMcpNames = @()
    )
    if (-not (Test-Path -LiteralPath $adaptiveConfigScript -PathType Leaf)) {
        if ($RemoveStaleContext -or $DirectFirst -or $TerraFirst -or $DisableMcpNames.Count -gt 0) {
            throw "Adaptive config helper is required for this explicit config operation: $adaptiveConfigScript"
        }
        return $null
    }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        if ($RemoveStaleContext -or $DirectFirst -or $TerraFirst -or $DisableMcpNames.Count -gt 0) {
            throw 'The explicit adaptive config operation requires an existing Codex config.'
        }
        return $null
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($null -eq $pythonCommand) {
        throw 'Python is required for the adaptive config audit/merge transaction.'
    }
    $pythonPath = if (-not [string]::IsNullOrWhiteSpace([string]$pythonCommand.Source)) {
        [string]$pythonCommand.Source
    } else {
        [string]$pythonCommand.Path
    }
    $codexPath = Resolve-CodexExecutable
    $script:adaptiveConfigAttempted = $true
    Add-BackupEntry $configPath
    $transactionId = [guid]::NewGuid().ToString('N')
    $candidatePath = Join-Path $backupRoot ('.adaptive-config-' + $transactionId + '.candidate.toml')
    $reportPath = Join-Path $backupRoot ('.adaptive-config-' + $transactionId + '.report.json')
    Assert-NoReparseEscape $userRoot $candidatePath 'Adaptive candidate path'
    Assert-NoReparseEscape $userRoot $reportPath 'Adaptive report path'
    $priorLocation = (Get-Location).Path
    try {
        Set-Location -LiteralPath $sourceRoot
        $auditArguments = @('-B', $adaptiveConfigScript, 'audit', '--config', $configPath, '--codex-exe', $codexPath, '--json')
        $audit = Invoke-AdaptiveCommand $pythonPath $auditArguments
        if ($audit.exit_code -ne 0) {
            throw "Adaptive config audit failed with exit $($audit.exit_code)."
        }
        $auditReport = Convert-JsonBytesToObject ([System.Text.Encoding]::UTF8.GetBytes($audit.output)) 'Adaptive audit report' $adaptiveConfigScript
        Assert-AdaptiveAuditReport $auditReport $configPath

        $mergeArguments = @(
            '-B',
            $adaptiveConfigScript,
            'merge',
            '--config',
            $configPath,
            '--codex-exe',
            $codexPath,
            '--output',
            $candidatePath,
            '--report',
            $reportPath,
            '--json'
        )
        foreach ($name in @($DisableMcpNames)) {
            $mergeArguments += @('--disable-mcp', $name)
        }
        if ($RemoveStaleContext) {
            $mergeArguments += '--remove-stale-context'
        }
        if ($DirectFirst) {
            $mergeArguments += '--direct-first'
        }
        if ($TerraFirst) {
            $mergeArguments += '--terra-first'
        }
        $merge = Invoke-AdaptiveCommand $pythonPath $mergeArguments
        if ($merge.exit_code -ne 0) {
            throw "Adaptive config merge failed with exit $($merge.exit_code)."
        }
        if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
            throw 'Adaptive config merge did not create its candidate output.'
        }
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw 'Adaptive config merge did not create its redacted report.'
        }
        $reportState = Get-Utf8TextState $reportPath 'Adaptive merge report'
        $mergeReport = Convert-JsonBytesToObject ([System.Text.Encoding]::UTF8.GetBytes([string]$reportState.text)) 'Adaptive merge report' $reportPath
        Assert-AdaptiveMergeReport $mergeReport $configPath $candidatePath $reportPath
        Assert-AdaptiveCandidateLoads $candidatePath $codexPath
        Replace-ConfigAtomically $candidatePath $configPath
        return [PSCustomObject]@{
            attempted = $true
            remove_stale_context = [bool]$RemoveStaleContext
            direct_first = [bool]$DirectFirst
            terra_first = [bool]$TerraFirst
            disabled_mcp = @($mergeReport.changes.disabled_mcp)
            already_disabled_mcp = @($mergeReport.changes.already_disabled_mcp)
            missing_mcp = @($mergeReport.changes.missing_mcp)
            removed_stale_context = @($mergeReport.removed_stale_context)
        }
    } finally {
        Set-Location -LiteralPath $priorLocation
        Remove-OwnedAdaptivePath $candidatePath
        Remove-OwnedAdaptivePath $reportPath
    }
}

function Assert-InstalledPluginMatchesSource {
    param(
        [string]$InstalledRoot,
        [string]$ExpectedPluginVersion,
        [string]$ExpectedSourceManifestHash
    )
    Assert-NoReparseEscape $userRoot $InstalledRoot 'Installed plugin tree'
    if (-not (Test-Path -LiteralPath $InstalledRoot -PathType Container)) {
        throw "Installed plugin tree is missing: $InstalledRoot"
    }
    $installedMetadataPath = Join-Path $InstalledRoot '.codex-plugin\plugin.json'
    Assert-NoReparseEscape $userRoot $installedMetadataPath 'Installed plugin metadata'
    if (-not (Test-Path -LiteralPath $installedMetadataPath -PathType Leaf)) {
        throw "Installed plugin metadata is missing: $installedMetadataPath"
    }
    $installedMetadataBytes = [System.IO.File]::ReadAllBytes($installedMetadataPath)
    $installedMetadata = Convert-JsonBytesToObject $installedMetadataBytes 'Installed plugin metadata' $installedMetadataPath
    if (
        $installedMetadata.name -isnot [string] -or
        $installedMetadata.name -cne 'mochicode-auto' -or
        $installedMetadata.version -isnot [string] -or
        $installedMetadata.version -cne $ExpectedPluginVersion
    ) {
        throw "Installed plugin name or version does not match the source package: $InstalledRoot"
    }
    $installedManifestHash = Get-SourceManifestHash $InstalledRoot
    if ($installedManifestHash -cne $ExpectedSourceManifestHash) {
        throw "Installed plugin tree does not match the source package manifest: $InstalledRoot"
    }
    return $installedManifestHash
}

function Assert-ExactVersionCacheMatchesSource {
    param(
        [string]$CacheRoot,
        [string]$ExpectedPluginVersion,
        [string]$ExpectedSourceManifestHash
    )
    Assert-NoReparseEscape $userRoot $CacheRoot 'Plugin cache root'
    if (-not (Test-Path -LiteralPath $CacheRoot -PathType Container)) {
        throw "Plugin cache root is missing: $CacheRoot"
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $CacheRoot -Force)) {
        Assert-NoReparseEscape $userRoot $entry.FullName 'Plugin cache entry'
        if ($entry.Name -cne $ExpectedPluginVersion) {
            throw "Plugin cache contains an unexpected version: $($entry.FullName)"
        }
    }
    $versionRoot = Join-Path $CacheRoot $ExpectedPluginVersion
    return Assert-InstalledPluginMatchesSource $versionRoot $ExpectedPluginVersion $ExpectedSourceManifestHash
}

function Assert-CodexPluginListMatchesSource {
    param(
        [object]$PluginList,
        [string]$ExpectedPluginVersion,
        [string]$ExpectedSourcePath
    )
    $installed = if (
        $PluginList -is [System.Management.Automation.PSCustomObject] -and
        $null -ne $PluginList.installed
    ) {
        @($PluginList.installed)
    } else {
        @($PluginList)
    }
    $matches = @($installed | Where-Object {
        $_.pluginId -ceq 'mochicode-auto@personal' -or
        ($_.name -ceq 'mochicode-auto' -and $_.marketplaceName -ceq 'personal')
    })
    if ($matches.Count -ne 1) {
        throw "Codex plugin list did not contain exactly one mochicode-auto@personal installation."
    }
    $entry = $matches[0]
    if (
        $entry.name -isnot [string] -or
        $entry.name -cne 'mochicode-auto' -or
        $entry.marketplaceName -isnot [string] -or
        $entry.marketplaceName -cne 'personal' -or
        $entry.version -isnot [string] -or
        $entry.version -cne $ExpectedPluginVersion
    ) {
        throw 'Codex plugin list has an unexpected mochicode-auto name, marketplace, or version.'
    }
    if ($entry.enabled -isnot [bool] -or $entry.enabled -ne $true) {
        throw 'Codex plugin list does not report mochicode-auto as enabled.'
    }
    if ($entry.source -isnot [System.Management.Automation.PSCustomObject]) {
        throw 'Codex plugin list has no mochicode-auto source identity.'
    }
    if ($entry.source.source -isnot [string] -or $entry.source.source -cne 'local') {
        throw 'Codex plugin list does not report a local mochicode-auto source.'
    }
    if ($entry.source.path -isnot [string] -or [string]::IsNullOrWhiteSpace($entry.source.path)) {
        throw 'Codex plugin list has no mochicode-auto source path.'
    }
    $listedSourcePath = if ([System.IO.Path]::IsPathRooted($entry.source.path)) {
        [System.IO.Path]::GetFullPath($entry.source.path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $userRoot $entry.source.path))
    }
    if (
        -not [string]::Equals(
            $listedSourcePath.TrimEnd('\', '/'),
            ([System.IO.Path]::GetFullPath($ExpectedSourcePath)).TrimEnd('\', '/'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Codex plugin list source path does not match the installed plugin: $listedSourcePath"
    }
    Assert-NoReparseEscape $userRoot $listedSourcePath 'Listed plugin source'
}

function Assert-ManifestRestored {
    foreach ($entry in @($manifestEntries)) {
        $target = [System.IO.Path]::GetFullPath([string]$entry.path)
        $backup = [System.IO.Path]::GetFullPath([string]$entry.backup)
        Assert-NoReparseEscape $userRoot $target 'Rollback target'
        if ([bool]$entry.existed) {
            if (-not (Test-Path -LiteralPath $target)) {
                throw "Rollback target is missing: $target"
            }
            if (-not (Test-Path -LiteralPath $backup)) {
                throw "Rollback backup is missing: $backup"
            }
            $targetHash = Get-TreeStateHash $target 'Rollback target'
            $backupHash = Get-TreeStateHash $backup 'Rollback backup'
            if ($targetHash -cne $backupHash) {
                throw "Rollback target is not byte-equivalent to its backup: $target"
            }
        } elseif (Test-Path -LiteralPath $target) {
            throw "Rollback left a newly-created target: $target"
        }
    }
}

function Invoke-CodexPluginList {
    $output = & codex.cmd plugin list --marketplace personal --json 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Codex plugin list failed with exit ${exitCode}: $($output.Trim())"
    }
    try {
        return $output | ConvertFrom-Json
    } catch {
        throw "Codex plugin list returned malformed JSON: $($output.Trim())"
    }
}

$pluginMetadataPath = Join-Path $sourceRoot '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $pluginMetadataPath -PathType Leaf)) {
    throw "Plugin source is missing .codex-plugin/plugin.json: $pluginMetadataPath"
}
$roleCatalogPath = Join-Path $sourceRoot 'config\role-dispositions.json'
if (-not (Test-Path -LiteralPath $roleCatalogPath -PathType Leaf)) {
    throw "Plugin source is missing the canonical role catalog: $roleCatalogPath"
}
$repositoryWorkflowSkillPath = Join-Path $sourceRoot 'skills\repository-workflow-upgrader\SKILL.md'
if (-not (Test-Path -LiteralPath $repositoryWorkflowSkillPath -PathType Leaf)) {
    throw "Plugin source is missing repository-workflow-upgrader: $repositoryWorkflowSkillPath"
}
try {
    $pluginMetadata = Get-Content -LiteralPath $pluginMetadataPath -Raw | ConvertFrom-Json
} catch {
    throw "Plugin metadata is malformed: $pluginMetadataPath"
}
if ($pluginMetadata -isnot [System.Management.Automation.PSCustomObject]) {
    throw "Plugin metadata must be a JSON object: $pluginMetadataPath"
}
$pluginName = [string]$pluginMetadata.name
$pluginVersion = [string]$pluginMetadata.version
if ($pluginName -cne 'mochicode-auto' -or [string]::IsNullOrWhiteSpace($pluginVersion)) {
    throw "Plugin metadata does not identify mochicode-auto with a version: $pluginMetadataPath"
}
$sourceManifestHash = Get-SourceManifestHash $sourceRoot

if ($RoutingCleanupOnly -and $UpdateExisting) {
    throw '-UpdateExisting cannot be combined with -RoutingCleanupOnly.'
}
if ($DisableStaleMcp -and -not $RoutingCleanupOnly) {
    throw '-DisableStaleMcp requires -RoutingCleanupOnly and a canary receipt.'
}
if ($DirectFirst -and $TerraFirst) {
    throw '-DirectFirst cannot be combined with the legacy -TerraFirst switch.'
}
if (($RemoveStaleContext -or $DirectFirst -or $TerraFirst -or $DisableStaleMcp) -and -not (Test-Path -LiteralPath $adaptiveConfigScript -PathType Leaf)) {
    throw "Adaptive config helper is required for the explicit config operation: $adaptiveConfigScript"
}

if ($RoutingCleanupOnly) {
    if ($SkipRoutingCleanup) {
        throw '-SkipRoutingCleanup cannot be combined with -RoutingCleanupOnly.'
    }
    if ([string]::IsNullOrWhiteSpace($CanaryReceipt)) {
        throw 'Routing cleanup requires -CanaryReceipt for a successful fresh-task canary.'
    }
    $installedManifestHash = Assert-InstalledPluginMatchesSource $pluginTarget $pluginVersion $sourceManifestHash
    $routingCanaryValidation = Assert-CanaryReceipt $CanaryReceipt $pluginVersion $sourceManifestHash
    $cleanupTargets = [System.Collections.Generic.List[string]]::new()
    foreach ($target in @($backupRoot, $latestReceipt)) {
        $cleanupTargets.Add($target)
    }
    foreach ($name in $conflictingSkills) {
        $skillRoot = Join-Path $userRoot ".agents\skills\$name"
        if (Test-Path -LiteralPath (Join-Path $skillRoot 'SKILL.md')) {
            $cleanupTargets.Add((Join-Path $skillRoot 'agents'))
        }
    }
    foreach ($name in $staleAgentNames) {
        $cleanupTargets.Add((Join-Path $agentTarget $name))
    }
    if ($DisableStaleMcp -or $RemoveStaleContext -or $DirectFirst -or $TerraFirst) {
        $cleanupTargets.Add($configPath)
    }
    foreach ($target in $cleanupTargets) {
        Assert-NoReparseEscape $userRoot $target 'Routing cleanup target'
    }
    if (-not $ConfirmInstall) {
        Write-Output 'No changes made. This was a preview because -ConfirmInstall was not supplied.'
        Write-Output "Re-run with -ConfirmInstall to apply routing cleanup. Planned backup: $backupRoot"
        return
    }
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    try {
        Add-BackupEntry $latestReceipt
        Write-JsonAtomically $latestReceipt ([PSCustomObject]@{
            operation = 'routing_cleanup'
            backup_manifest = $manifestPath
            plugin_target = $pluginTarget
            plugin_version = $pluginVersion
            source_manifest_hash = $installedManifestHash
            routing_canary = $routingCanaryValidation
            explicit_only_skills = @($conflictingSkills)
            manually_available_skills = @('master-status')
            preserved_implicit_skills = @('claude-state-bridge')
            confirm_install = [bool]$ConfirmInstall
        })
        foreach ($name in $conflictingSkills) {
            Set-ImplicitPolicyFalse (Join-Path $userRoot ".agents\skills\$name")
        }
        if ($RemoveStaleContext -or $DirectFirst -or $TerraFirst -or $DisableStaleMcp) {
            $disableNames = if ($DisableStaleMcp) { $staleMcpNames } else { @() }
            $adaptiveResult = Invoke-AdaptiveConfigTransaction `
                -RemoveStaleContext:$RemoveStaleContext `
                -DirectFirst:$DirectFirst `
                -TerraFirst:$TerraFirst `
                -DisableMcpNames $disableNames
            $staleMcpChanges = @($adaptiveResult.disabled_mcp)
        }
        foreach ($name in $staleAgentNames) {
            $target = Join-Path $agentTarget $name
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                Add-BackupEntry $target
                Remove-Item -LiteralPath $target -Force
                [void]$removedStaleAgents.Add($name)
            } elseif (Test-Path -LiteralPath $target) {
                throw "Stale custom agent target is not a file: $target"
            }
        }
        Write-JsonAtomically $latestReceipt ([PSCustomObject]@{
            operation = 'routing_cleanup'
            backup_manifest = $manifestPath
            plugin_target = $pluginTarget
            plugin_version = $pluginVersion
            source_manifest_hash = $installedManifestHash
            routing_canary = $routingCanaryValidation
            explicit_only_skills = @($conflictingSkills)
            manually_available_skills = @('master-status')
            preserved_implicit_skills = @('claude-state-bridge')
            removed_stale_agents = @($removedStaleAgents)
            disabled_stale_mcp = @($staleMcpChanges)
            adaptive_config = [PSCustomObject]@{
                attempted = [bool]$adaptiveConfigAttempted
                remove_stale_context = [bool]$RemoveStaleContext
                direct_first = [bool]$DirectFirst
                terra_first = [bool]$TerraFirst
            }
            confirm_install = [bool]$ConfirmInstall
        })
        Write-Manifest
    } catch {
        $failure = $_
        $rollback = "Automatic rollback was not completed."
        if (Test-Path -LiteralPath $manifestPath) {
            try {
                & $restoreScript -Manifest $manifestPath | Out-Null
                Assert-ManifestRestored
                $rollback = "Automatic rollback completed."
            } catch {
                $rollback = "Automatic rollback failed: $($_.Exception.Message)"
            }
        }
        throw "MochiCode routing cleanup failed: $($failure.Exception.Message) $rollback Rollback manifest: $manifestPath"
    }
    Write-Output "Updated MochiCode routing policies"
    Write-Output "Plugin: $pluginTarget"
    Write-Output "Backup: $backupRoot"
    return
}

if (-not [string]::IsNullOrWhiteSpace($CanaryReceipt)) {
    throw '-CanaryReceipt is valid only with -RoutingCleanupOnly.'
}

$pluginTargetExists = Test-Path -LiteralPath $pluginTarget
if ($pluginTargetExists -and -not $UpdateExisting) {
    throw "Plugin target already exists. Re-run with -UpdateExisting: $pluginTarget"
}
if (-not $pluginTargetExists -and $UpdateExisting) {
    throw "-UpdateExisting requires an existing plugin target: $pluginTarget"
}
$activationTargets = [System.Collections.Generic.List[string]]::new()
foreach ($target in @($backupRoot, $latestReceipt, $pluginTarget, $marketplacePath, $agentsPath)) {
    $activationTargets.Add($target)
}
foreach ($file in $agentFiles) {
    $activationTargets.Add((Join-Path $agentTarget $file.Name))
}
if (-not $SkipPluginCommand -or $RemoveStaleContext -or $DirectFirst -or $TerraFirst -or (Test-Path -LiteralPath $adaptiveConfigScript -PathType Leaf)) {
    $activationTargets.Add($configPath)
}
if (-not $SkipPluginCommand) {
    $activationTargets.Add($pluginCacheTarget)
}
foreach ($target in $activationTargets) {
    Assert-NoReparseEscape $userRoot $target 'Activation target'
}
if (-not $ConfirmInstall) {
    Write-Output 'No changes made. This was a preview because -ConfirmInstall was not supplied.'
    Write-Output "Re-run with -ConfirmInstall to apply $operation. Planned plugin target: $pluginTarget"
    return
}
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

try {
    Add-BackupEntry $latestReceipt
    Write-JsonAtomically $latestReceipt ([PSCustomObject]@{
        backup_manifest = $manifestPath
        plugin_target = $pluginTarget
        confirm_install = [bool]$ConfirmInstall
    })

    Add-BackupEntry $pluginTarget
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pluginTarget) | Out-Null
    if ($UpdateExisting) {
        Remove-Item -LiteralPath $pluginTarget -Recurse -Force
    }
    Copy-SourceTreeFiltered -Root $sourceRoot -Destination $pluginTarget
    $installedManifestHash = Assert-InstalledPluginMatchesSource $pluginTarget $pluginVersion $sourceManifestHash

    Add-BackupEntry $marketplacePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marketplacePath) | Out-Null
    if (Test-Path -LiteralPath $marketplacePath) {
        $marketplace = Get-Content -LiteralPath $marketplacePath -Raw | ConvertFrom-Json
        if ($marketplace.name -ne 'personal') {
            throw "Existing personal marketplace has unexpected name: $($marketplace.name)"
        }
        $remaining = @($marketplace.plugins | Where-Object name -ne 'mochicode-auto')
        $marketplace.plugins = @($remaining) + @([PSCustomObject]@{
            name = 'mochicode-auto'
            source = [PSCustomObject]@{source = 'local'; path = './plugins/mochicode-auto'}
            policy = [PSCustomObject]@{installation = 'AVAILABLE'; authentication = 'ON_INSTALL'}
            category = 'Productivity'
        })
    } else {
        $marketplace = [PSCustomObject]@{
            name = 'personal'
            interface = [PSCustomObject]@{displayName = 'Personal'}
            plugins = @([PSCustomObject]@{
                name = 'mochicode-auto'
                source = [PSCustomObject]@{source = 'local'; path = './plugins/mochicode-auto'}
                policy = [PSCustomObject]@{installation = 'AVAILABLE'; authentication = 'ON_INSTALL'}
                category = 'Productivity'
            })
        }
    }
    Write-JsonAtomically $marketplacePath $marketplace
    Get-Content -LiteralPath $marketplacePath -Raw | ConvertFrom-Json | Out-Null

    New-Item -ItemType Directory -Force -Path $agentTarget | Out-Null
    foreach ($file in $agentFiles) {
        $target = Join-Path $agentTarget $file.Name
        Add-BackupEntry $target
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }

    Add-BackupEntry $agentsPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $agentsPath) | Out-Null
    $begin = '<!-- MOCHICODE-AUTO:BEGIN -->'
    $end = '<!-- MOCHICODE-AUTO:END -->'
    Set-MochiCodeMarkerBlock $agentsPath $begin $end

    if (Test-Path -LiteralPath $adaptiveConfigScript -PathType Leaf) {
        Invoke-AdaptiveConfigTransaction -RemoveStaleContext:$RemoveStaleContext -DirectFirst:$DirectFirst -TerraFirst:$TerraFirst | Out-Null
    }

    if (-not $SkipPluginCommand) {
        Add-BackupEntry $configPath
        Add-BackupEntry $pluginCacheTarget
        if ($UpdateExisting -and (Test-Path -LiteralPath $pluginCacheTarget)) {
            Remove-Item -LiteralPath $pluginCacheTarget -Recurse -Force
        }
        $registrationAttempted = $true
        Write-Manifest
        $priorCodexHome = $env:CODEX_HOME
        $priorUserProfile = $env:USERPROFILE
        $priorHome = $env:HOME
        try {
            $env:CODEX_HOME = Join-Path $userRoot '.codex'
            $env:USERPROFILE = $userRoot
            $env:HOME = $userRoot
            & codex.cmd plugin add 'mochicode-auto@personal'
            $pluginExitCode = $LASTEXITCODE
            if ($pluginExitCode -ne 0) {
                throw "Codex plugin installation failed with exit $pluginExitCode"
            }
            $cacheManifestHash = Assert-ExactVersionCacheMatchesSource $pluginCacheTarget $pluginVersion $sourceManifestHash
            $pluginList = Invoke-CodexPluginList
            Assert-CodexPluginListMatchesSource $pluginList $pluginVersion $pluginTarget
        } finally {
            if ($null -eq $priorCodexHome) { Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue } else { $env:CODEX_HOME = $priorCodexHome }
            if ($null -eq $priorUserProfile) { Remove-Item Env:USERPROFILE -ErrorAction SilentlyContinue } else { $env:USERPROFILE = $priorUserProfile }
            if ($null -eq $priorHome) { Remove-Item Env:HOME -ErrorAction SilentlyContinue } else { $env:HOME = $priorHome }
        }
        $registrationSucceeded = $true
        Write-Manifest
    }
    Write-Manifest
} catch {
    $failure = $_
    $rollback = "Automatic rollback was not completed."
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            & $restoreScript -Manifest $manifestPath | Out-Null
            Assert-ManifestRestored
            $rollback = "Automatic rollback completed."
        } catch {
            $rollback = "Automatic rollback failed: $($_.Exception.Message)"
        }
    }
    throw "MochiCode installation failed: $($failure.Exception.Message) $rollback Rollback manifest: $manifestPath"
}

Write-Output "Installed MochiCode Auto"
Write-Output "Plugin: $pluginTarget"
Write-Output "Backup: $backupRoot"
Write-Output "Start a fresh Codex task to load the new routing."
