[CmdletBinding()]
param(
    [Alias('PluginSource')]
    [string]$Source = $PSScriptRoot,
    [Parameter(Mandatory = $true)]
    [Alias('OutputDirectory', 'PackageRoot')]
    [string]$Destination,
    [Alias('OutputZip', 'ArchivePath')]
    [string]$ZipPath,
    [string]$Version,
    [string]$GeneratedTimestampUtc,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$helperPath = Join-Path $PSScriptRoot 'portable\install\package-safety.ps1'
if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Package safety helper is missing: $helperPath"
}
. $helperPath

function Read-PackageJsonObject {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Label = 'JSON file'
    )

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    try {
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        $value = $text | ConvertFrom-Json
    } catch {
        throw "$Label is not valid strict UTF-8 JSON: $Path"
    }
    if ($null -eq $value -or $value -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$Label must be a JSON object: $Path"
    }
    return $value
}

function Test-PackageSourceExcluded {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $normalized = $RelativePath.Replace('\', '/').TrimStart('/')
    $parts = @($normalized -split '/')
    $leaf = [string]$parts[$parts.Count - 1]
    if ($parts.Count -gt 0 -and [string]$parts[0] -in @('portable', 'chatgpt', 'templates', 'tests')) {
        return $true
    }
    if ($normalized -eq 'package.ps1' -or $normalized -eq 'verify-package.ps1') {
        return $true
    }
    if ($normalized -eq 'schemas/package-manifest.schema.json') {
        return $true
    }
    if ($leaf -match '^(?i:(?:CODEX-WORKFLOW-MIGRATION|TRUTH-TABLE|VERIFICATION)\.md)$') {
        return $true
    }
    return Test-PackageExcludedRelativePath -RelativePath $normalized
}

function Sort-PackageRecords {
    param(
        [object[]]$Records
    )

    $list = [System.Collections.Generic.List[object]]::new()
    foreach ($record in @($Records)) {
        [void]$list.Add($record)
    }
    for ($left = 0; $left -lt $list.Count; $left++) {
        for ($right = $left + 1; $right -lt $list.Count; $right++) {
            if (
                [System.StringComparer]::Ordinal.Compare(
                    [string]$list[$left].RelativePath,
                    [string]$list[$right].RelativePath
                ) -gt 0
            ) {
                $temporary = $list[$left]
                $list[$left] = $list[$right]
                $list[$right] = $temporary
            }
        }
    }
    return @($list.ToArray())
}

function Get-PackageSourceRecords {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse -File -ErrorAction Stop)) {
        $relative = Get-PackageRelativePath -Root $Root -Path $item.FullName
        if (Test-PackageSourceExcluded -RelativePath $relative) {
            continue
        }
        Assert-PackageRelativePath -RelativePath $relative
        $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
        Assert-PackageSafeTextBytes -Bytes $bytes -Path $relative
        [void]$records.Add([PSCustomObject]@{
            RelativePath = $relative
            SourcePath = $item.FullName
        })
    }
    return @(Sort-PackageRecords -Records $records.ToArray())
}

function Get-PortableAssetRecords {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceRoot,
        [Parameter(Mandatory = $true)]
        [string]$Category
    )

    $candidates = @(
        (Join-Path $SourceRoot (Join-Path 'portable' $Category))
        (Join-Path $SourceRoot $Category)
    )
    $assetRoot = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $assetRoot = Assert-PackageExistingPathSafe -Path $candidate -Label "Portable $Category assets" -PathType Directory
            break
        }
    }
    if ($null -eq $assetRoot) {
        throw "Required portable asset category is absent: $Category. Expected portable\$Category or $Category."
    }

    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @(Get-ChildItem -LiteralPath $assetRoot -Force -Recurse -File -ErrorAction Stop)) {
        $relative = Get-PackageRelativePath -Root $assetRoot -Path $item.FullName
        if (Test-PackageExcludedRelativePath -RelativePath $relative) {
            continue
        }
        $targetRelative = ('portable/{0}/{1}' -f $Category, $relative)
        Assert-PackageRelativePath -RelativePath $targetRelative
        $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
        Assert-PackageSafeTextBytes -Bytes $bytes -Path $targetRelative -PortableAsset
        [void]$records.Add([PSCustomObject]@{
            RelativePath = $targetRelative
            SourcePath = $item.FullName
        })
    }
    if ($records.Count -eq 0) {
        throw "Required portable asset category has no safe files: $Category"
    }
    return [PSCustomObject]@{
        Category = $Category
        SourceRoot = $assetRoot
        Records = @(Sort-PackageRecords -Records $records.ToArray())
    }
}

function Copy-PackageRecord {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DestinationRoot,
        [Parameter(Mandatory = $true)]
        [string]$RelativePath,
        [Parameter(Mandatory = $true)]
        [string]$SourcePath
    )

    Assert-PackageRelativePath -RelativePath $RelativePath
    $target = Join-Path $DestinationRoot ($RelativePath.Replace('/', '\'))
    if (-not (Test-PackagePathWithin -Root $DestinationRoot -Candidate $target)) {
        throw "Package copy target escapes the staging root: $RelativePath"
    }
    $parent = Split-Path -Path $target -Parent
    New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
    Assert-PackageExistingPathSafe -Path $parent -Label "Package copy parent for $RelativePath" -PathType Directory | Out-Null
    [System.IO.File]::Copy($SourcePath, $target, $false)
}

function New-PackageManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$PackageVersion,
        [Parameter(Mandatory = $true)]
        [string]$SourceVersion,
        [Parameter(Mandatory = $true)]
        [string]$Timestamp
    )

    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse -File -ErrorAction Stop)) {
        $relative = Get-PackageRelativePath -Root $Root -Path $item.FullName
        if ($relative -eq 'MANIFEST.json') {
            continue
        }
        Assert-PackageRelativePath -RelativePath $relative
        [void]$records.Add([PSCustomObject]@{
            RelativePath = $relative
            Item = $item
        })
    }

    $entries = [System.Collections.Generic.List[object]]::new()
    [int64]$totalBytes = 0
    foreach ($record in @(Sort-PackageRecords -Records $records.ToArray())) {
        $item = Get-Item -LiteralPath $record.Item.FullName -Force -ErrorAction Stop
        $bytes = [int64]$item.Length
        [void]$entries.Add([ordered]@{
            path = [string]$record.RelativePath
            bytes = $bytes
            sha256 = Get-PackageSha256Hex -Path $item.FullName
        })
        $totalBytes += $bytes
    }

    return [ordered]@{
        schema_version = 1
        package_name = 'ana-codex-portable-ultimate'
        version = $PackageVersion
        source_plugin_version = $SourceVersion
        generated_at_utc = $Timestamp
        manifest_scope = 'all package files except MANIFEST.json'
        file_count = [int64]$entries.Count
        total_bytes = $totalBytes
        files = @($entries.ToArray())
    }
}

function New-PackageZip {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::Open(
        $ArchivePath,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        $records = [System.Collections.Generic.List[object]]::new()
        foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse -File -ErrorAction Stop)) {
            [void]$records.Add([PSCustomObject]@{
                RelativePath = Get-PackageRelativePath -Root $Root -Path $item.FullName
                Item = $item
            })
        }
        foreach ($record in @(Sort-PackageRecords -Records $records.ToArray())) {
            $entry = $archive.CreateEntry(
                [string]$record.RelativePath,
                [System.IO.Compression.CompressionLevel]::Optimal
            )
            $entry.LastWriteTime = [DateTimeOffset]::Parse('1980-01-01T00:00:00Z')
            $input = [System.IO.File]::OpenRead($record.Item.FullName)
            $output = $entry.Open()
            try {
                $input.CopyTo($output)
            } finally {
                $output.Dispose()
                $input.Dispose()
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function Invoke-PackageExtractedSelfTest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,
        [Parameter(Mandatory = $true)]
        [string]$Parent,
        [Parameter(Mandatory = $true)]
        [string]$VerifierPath
    )

    $selfTestRoot = Join-Path $Parent ('.mochicode-package-self-test-' + [guid]::NewGuid().ToString('N'))
    Assert-PackageNewPathSafe -Path $selfTestRoot -Label 'Package self-test root' | Out-Null
    $profileRoot = Join-Path $selfTestRoot 'profile'
    $tempRoot = Join-Path $profileRoot 'temp'
    New-Item -ItemType Directory -Path $tempRoot -Force -ErrorAction Stop | Out-Null

    $environmentNames = @('USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'CODEX_HOME', 'TEMP', 'TMP')
    $previousEnvironment = @{}
    foreach ($name in $environmentNames) {
        $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        [System.Environment]::SetEnvironmentVariable('USERPROFILE', $profileRoot, 'Process')
        [System.Environment]::SetEnvironmentVariable('HOME', $profileRoot, 'Process')
        [System.Environment]::SetEnvironmentVariable('APPDATA', (Join-Path $profileRoot 'appdata'), 'Process')
        [System.Environment]::SetEnvironmentVariable('LOCALAPPDATA', (Join-Path $profileRoot 'localappdata'), 'Process')
        [System.Environment]::SetEnvironmentVariable('CODEX_HOME', (Join-Path $profileRoot '.codex'), 'Process')
        [System.Environment]::SetEnvironmentVariable('TEMP', $tempRoot, 'Process')
        [System.Environment]::SetEnvironmentVariable('TMP', $tempRoot, 'Process')

        $extractRoot = Join-Path $profileRoot 'extracted'
        Expand-PackageZipSafely -ZipPath $ArchivePath -Destination $extractRoot | Out-Null
        $extractedVerifier = Join-Path $extractRoot 'verify-package.ps1'
        try {
            & $extractedVerifier -PackageRoot $extractRoot -Quiet | Out-Null
        } catch {
            throw "Extracted ZIP package verification failed: $($_.Exception.Message)"
        }
        $extractedDoctor = Join-Path $extractRoot 'doctor.ps1'
        try {
            & $extractedDoctor -PackageOnly | Out-Null
        } catch {
            throw "Extracted ZIP doctor self-test failed: $($_.Exception.Message)"
        }
    } finally {
        foreach ($name in $environmentNames) {
            [System.Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
        if (Test-Path -LiteralPath $selfTestRoot) {
            Remove-Item -LiteralPath $selfTestRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$sourceRoot = Assert-PackageNoReparseInTree -Root $Source -Label 'Plugin source'
$sourceMetadataPath = Join-Path $sourceRoot '.codex-plugin\plugin.json'
Assert-PackageExistingPathSafe -Path $sourceMetadataPath -Label 'Plugin metadata' -PathType File | Out-Null
$sourceMetadata = Read-PackageJsonObject -Path $sourceMetadataPath -Label 'Plugin metadata'
if ([string]$sourceMetadata.name -cne 'mochicode-auto') {
    throw "Plugin metadata has unexpected name: $($sourceMetadata.name)"
}
$sourceVersion = [string]$sourceMetadata.version
Assert-PackageVersion -Value $sourceVersion -Label 'Source plugin version'
$packageVersion = if ([string]::IsNullOrWhiteSpace($Version)) { $sourceVersion } else { $Version }
Assert-PackageVersion -Value $packageVersion -Label 'Package version'
$timestamp = ConvertTo-PackageTimestamp -Value $GeneratedTimestampUtc

foreach ($requiredSource in @(
    '.codex-plugin\plugin.json',
    'install.ps1',
    'restore.ps1',
    'scripts\mochicode.py',
    'scripts\mochicode_core\cli.py'
)) {
    $requiredPath = Join-Path $sourceRoot $requiredSource
    Assert-PackageExistingPathSafe -Path $requiredPath -Label "Required plugin file $requiredSource" -PathType File | Out-Null
}

$sourceRecords = @(Get-PackageSourceRecords -Root $sourceRoot)
$assetCategories = @('docs', 'templates', 'chatgpt')
$assetBundles = [System.Collections.Generic.List[object]]::new()
foreach ($category in $assetCategories) {
    [void]$assetBundles.Add((Get-PortableAssetRecords -SourceRoot $sourceRoot -Category $category))
}

$installRoot = Assert-PackageExistingPathSafe -Path (Join-Path $PSScriptRoot 'portable\install') -Label 'Portable install wrappers' -PathType Directory
$installRecords = [System.Collections.Generic.List[object]]::new()
foreach ($item in @(Get-ChildItem -LiteralPath $installRoot -Force -Recurse -File -ErrorAction Stop)) {
    $relative = Get-PackageRelativePath -Root $installRoot -Path $item.FullName
    if (Test-PackageExcludedRelativePath -RelativePath $relative) {
        continue
    }
    $targetRelative = ('portable/install/{0}' -f $relative)
    Assert-PackageRelativePath -RelativePath $targetRelative
    $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
    Assert-PackageSafeTextBytes -Bytes $bytes -Path $targetRelative
    [void]$installRecords.Add([PSCustomObject]@{
        RelativePath = $targetRelative
        SourcePath = $item.FullName
    })
}
if ($installRecords.Count -eq 0) {
    throw 'Portable install wrapper directory has no safe files.'
}
$installRecords = @(Sort-PackageRecords -Records $installRecords.ToArray())
$requiredWrapperNames = @('install.ps1', 'update.ps1', 'doctor.ps1', 'restore.ps1', 'easy-install.ps1', 'agent-sync.ps1')
foreach ($wrapperName in $requiredWrapperNames) {
    if (-not (@($installRecords | Where-Object RelativePath -eq "portable/install/$wrapperName").Count -eq 1)) {
        throw "Portable install wrapper is missing: $wrapperName"
    }
}

$destinationFull = ConvertTo-PackageFullPath $Destination
$zipFull = if ([string]::IsNullOrWhiteSpace($ZipPath)) {
    $destinationFull + '.zip'
} else {
    ConvertTo-PackageFullPath $ZipPath
}
Assert-PackageNewPathSafe -Path $destinationFull -Label 'Package destination' | Out-Null
Assert-PackageNewPathSafe -Path $zipFull -Label 'Package ZIP destination' | Out-Null
if (Test-PackagePathWithin -Root $sourceRoot -Candidate $destinationFull) {
    throw 'Package destination may not be inside the plugin source.'
}
if (Test-PackagePathWithin -Root $sourceRoot -Candidate $zipFull) {
    throw 'Package ZIP destination may not be inside the plugin source.'
}
if (Test-PackagePathWithin -Root $destinationFull -Candidate $sourceRoot) {
    throw 'Package destination may not contain the plugin source.'
}
if (
    (Test-PackagePathWithin -Root $destinationFull -Candidate $zipFull) -or
    (Test-PackagePathWithin -Root $zipFull -Candidate $destinationFull)
) {
    throw 'Package destination and ZIP destination must be separate paths.'
}
$destinationParent = Split-Path -Path $destinationFull -Parent
$zipParent = Split-Path -Path $zipFull -Parent
Assert-PackageExistingPathSafe -Path $destinationParent -Label 'Package destination parent' -PathType Directory | Out-Null
Assert-PackageExistingPathSafe -Path $zipParent -Label 'Package ZIP destination parent' -PathType Directory | Out-Null

if ($DryRun) {
    Write-Output 'Portable package dry run passed.'
    Write-Output "Source: $sourceRoot"
    Write-Output "Destination: $destinationFull"
    Write-Output "ZIP: $zipFull"
    Write-Output "Version: $packageVersion"
    Write-Output "Required asset categories: $($assetCategories -join ', ')"
    Write-Output "Plugin source files: $($sourceRecords.Count)"
    exit 0
}

$stagingRoot = Join-Path $destinationParent ('.mochicode-package-staging-' + [guid]::NewGuid().ToString('N'))
$stagingZip = Join-Path $zipParent ('.mochicode-package-zip-' + [guid]::NewGuid().ToString('N') + '.zip')
Assert-PackageNewPathSafe -Path $stagingRoot -Label 'Package staging directory' | Out-Null
Assert-PackageNewPathSafe -Path $stagingZip -Label 'Package staging ZIP' | Out-Null
$committedDestination = $false
$committedZip = $false
try {
    New-Item -ItemType Directory -Path $stagingRoot -ErrorAction Stop | Out-Null

    foreach ($record in $sourceRecords) {
        Copy-PackageRecord -DestinationRoot (Join-Path $stagingRoot 'plugin') -RelativePath $record.RelativePath -SourcePath $record.SourcePath
    }
    foreach ($bundle in $assetBundles) {
        foreach ($record in $bundle.Records) {
            Copy-PackageRecord -DestinationRoot $stagingRoot -RelativePath $record.RelativePath -SourcePath $record.SourcePath
        }
    }
    foreach ($record in $installRecords) {
        Copy-PackageRecord -DestinationRoot $stagingRoot -RelativePath $record.RelativePath -SourcePath $record.SourcePath
    }

    Copy-PackageRecord -DestinationRoot $stagingRoot -RelativePath 'verify-package.ps1' -SourcePath (Join-Path $PSScriptRoot 'verify-package.ps1')
    Copy-PackageRecord -DestinationRoot $stagingRoot -RelativePath 'schemas/package-manifest.schema.json' -SourcePath (Join-Path $PSScriptRoot 'schemas\package-manifest.schema.json')
    foreach ($wrapperName in $requiredWrapperNames) {
        Copy-PackageRecord `
            -DestinationRoot $stagingRoot `
            -RelativePath $wrapperName `
            -SourcePath (Join-Path $installRoot $wrapperName)
    }

    Assert-PackageNoReparseInTree -Root $stagingRoot -Label 'Package staging tree' | Out-Null
    $manifest = New-PackageManifest -Root $stagingRoot -PackageVersion $packageVersion -SourceVersion $sourceVersion -Timestamp $timestamp
    $manifestPath = Join-Path $stagingRoot 'MANIFEST.json'
    $manifestJson = $manifest | ConvertTo-Json -Depth 12
    Write-PackageUtf8NoBom -Path $manifestPath -Content ($manifestJson.TrimEnd() + [Environment]::NewLine)

    & (Join-Path $PSScriptRoot 'verify-package.ps1') -PackageRoot $stagingRoot -Quiet | Out-Null
    if (-not $?) {
        throw 'Staging package verification failed.'
    }

    New-PackageZip -Root $stagingRoot -ArchivePath $stagingZip
    Invoke-PackageExtractedSelfTest -ArchivePath $stagingZip -Parent $zipParent -VerifierPath (Join-Path $PSScriptRoot 'verify-package.ps1')

    [System.IO.Directory]::Move($stagingRoot, $destinationFull)
    $committedDestination = $true
    & (Join-Path $destinationFull 'verify-package.ps1') -PackageRoot $destinationFull -Quiet | Out-Null
    if (-not $?) {
        throw 'Committed package verification failed.'
    }
    [System.IO.File]::Move($stagingZip, $zipFull)
    $committedZip = $true
} catch {
    if (-not $committedDestination -and (Test-Path -LiteralPath $stagingRoot)) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $committedZip -and (Test-Path -LiteralPath $stagingZip)) {
        Remove-Item -LiteralPath $stagingZip -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Output "Created verified portable package: $destinationFull"
Write-Output "Created verified ZIP: $zipFull"
Write-Output "Version: $packageVersion"
Write-Output "Payload files: $($manifest.file_count)"
Write-Output "Payload bytes: $($manifest.total_bytes)"
