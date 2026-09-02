[CmdletBinding()]
param(
    [Alias('BundleRoot')]
    [string]$PackageRoot = $PSScriptRoot,
    [string]$ZipPath,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$sourceHelperPath = Join-Path $PSScriptRoot 'portable\install\package-safety.ps1'
$helperPath = if (Test-Path -LiteralPath $sourceHelperPath -PathType Leaf) {
    $sourceHelperPath
} else {
    $rawPackageRoot = [System.IO.Path]::GetFullPath($PackageRoot)
    $packageHelperPath = Join-Path $rawPackageRoot 'portable\install\package-safety.ps1'
    if (Test-Path -LiteralPath $packageHelperPath -PathType Leaf) {
        $packageHelperPath
    } else {
        $null
    }
}
if ($null -eq $helperPath) {
    throw 'Package safety helper is missing from the verifier source or package.'
}
. $helperPath
$packageRootFull = ConvertTo-PackageFullPath $PackageRoot

function Assert-PackageJsonProperties {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,
        [Parameter(Mandatory = $true)]
        [string[]]$Allowed,
        [Parameter(Mandatory = $true)]
        [string[]]$Required,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($Value -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$Label must be a JSON object."
    }
    $allowedSet = [System.Collections.Generic.HashSet[string]]::new(
        $Allowed,
        [System.StringComparer]::Ordinal
    )
    $actualSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($property in $Value.PSObject.Properties) {
        if (-not $allowedSet.Contains($property.Name)) {
            throw "$Label contains an unsupported property: $($property.Name)"
        }
        [void]$actualSet.Add($property.Name)
    }
    foreach ($propertyName in $Required) {
        if (-not $actualSet.Contains($propertyName)) {
            throw "$Label is missing required property: $propertyName"
        }
    }
}

function Assert-PackageUniqueJsonProperties {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Element,
        [string]$Location = 'JSON document'
    )

    if ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
        $names = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
        foreach ($property in $Element.EnumerateObject()) {
            if (-not $names.Add($property.Name)) {
                throw "JSON document contains a duplicate property at $Location.$($property.Name)."
            }
            Assert-PackageUniqueJsonProperties -Element $property.Value -Location "$Location.$($property.Name)"
        }
    } elseif ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
        $index = 0
        foreach ($child in $Element.EnumerateArray()) {
            Assert-PackageUniqueJsonProperties -Element $child -Location "$Location[$index]"
            $index++
        }
    }
}

function Read-PackageJsonObject {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Label = 'JSON file'
    )

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    try {
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes)
    } catch {
        throw "$Label is not strict UTF-8: $Path"
    }

    try {
        $jsonType = 'System.Text.Json.JsonDocument' -as [type]
        if ($null -ne $jsonType) {
            $document = [System.Text.Json.JsonDocument]::Parse($text)
            try {
                if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) {
                    throw "$Label root must be a JSON object: $Path"
                }
                Assert-PackageUniqueJsonProperties -Element $document.RootElement -Location $Label
            } finally {
                $document.Dispose()
            }
        }
        $value = $text | ConvertFrom-Json
    } catch {
        throw "$Label is malformed JSON: $Path"
    }
    if ($null -eq $value -or $value -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$Label must be a JSON object: $Path"
    }
    return [PSCustomObject]@{
        Value = $value
        Text = $text
    }
}

function Assert-PackageString {
    param(
        [object]$Value,
        [string]$Label
    )
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        throw "$Label must be a non-empty string."
    }
}

function Assert-PackageManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $manifestPath = Join-Path $Root 'MANIFEST.json'
    Assert-PackageExistingPathSafe -Path $manifestPath -Label 'Package manifest' -PathType File | Out-Null
    $manifestDocument = Read-PackageJsonObject -Path $manifestPath -Label 'Package manifest'
    $manifest = $manifestDocument.Value
    Assert-PackageJsonProperties `
        -Value $manifest `
        -Allowed @(
            'schema_version',
            'package_name',
            'version',
            'source_plugin_version',
            'generated_at_utc',
            'manifest_scope',
            'file_count',
            'total_bytes',
            'files'
        ) `
        -Required @(
            'schema_version',
            'package_name',
            'version',
            'source_plugin_version',
            'generated_at_utc',
            'manifest_scope',
            'file_count',
            'total_bytes',
            'files'
        ) `
        -Label 'Package manifest'

    if ($manifest.schema_version -isnot [int] -and $manifest.schema_version -isnot [long]) {
        throw 'Package manifest schema_version must be an integer.'
    }
    if ([int64]$manifest.schema_version -ne 1) {
        throw 'Package manifest schema_version must be 1.'
    }
    if ([string]$manifest.package_name -cne 'ana-codex-portable-ultimate') {
        throw 'Package manifest package_name is unsupported.'
    }
    Assert-PackageString -Value $manifest.version -Label 'Package manifest version'
    Assert-PackageVersion -Value ([string]$manifest.version) -Label 'Package manifest version'
    Assert-PackageString -Value $manifest.source_plugin_version -Label 'Source plugin version'
    Assert-PackageVersion -Value ([string]$manifest.source_plugin_version) -Label 'Source plugin version'
    if ([string]$manifest.manifest_scope -cne 'all package files except MANIFEST.json') {
        throw 'Package manifest manifest_scope is unsupported.'
    }

    $timestamp = [regex]::Match(
        $manifestDocument.Text,
        '"generated_at_utc"\s*:\s*"([^"]*)"'
    )
    if (-not $timestamp.Success) {
        throw 'Package manifest generated_at_utc is missing.'
    }
    $timestampValue = $timestamp.Groups[1].Value
    if ($timestampValue -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$') {
        throw 'Package manifest generated_at_utc must be canonical UTC with millisecond precision.'
    }
    try {
        [void][DateTimeOffset]::ParseExact(
            $timestampValue,
            'yyyy-MM-ddTHH:mm:ss.fffZ',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal
        )
    } catch {
        throw "Package manifest generated_at_utc is not a real timestamp: $timestampValue"
    }

    if ($manifest.file_count -isnot [int] -and $manifest.file_count -isnot [long]) {
        throw 'Package manifest file_count must be an integer.'
    }
    if ($manifest.total_bytes -isnot [int] -and $manifest.total_bytes -isnot [long]) {
        throw 'Package manifest total_bytes must be an integer.'
    }
    [int64]$fileCount = $manifest.file_count
    [int64]$totalBytes = $manifest.total_bytes
    if ($fileCount -lt 1 -or $totalBytes -lt 0) {
        throw 'Package manifest counts are invalid.'
    }
    if ($manifest.files -isnot [System.Array]) {
        throw 'Package manifest files must be an array.'
    }

    $entries = @($manifest.files)
    if ($entries.Count -eq 0 -or $entries.Count -ne $fileCount) {
        throw 'Package manifest file_count does not match its files array.'
    }
    $paths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    [int64]$listedBytes = 0
    foreach ($entry in $entries) {
        Assert-PackageJsonProperties `
            -Value $entry `
            -Allowed @('path', 'bytes', 'sha256') `
            -Required @('path', 'bytes', 'sha256') `
            -Label 'Package manifest file entry'
        if ($entry.path -isnot [string]) {
            throw 'Package manifest file path must be a string.'
        }
        $relative = [string]$entry.path
        Assert-PackageRelativePath -RelativePath $relative
        if ($relative -eq 'MANIFEST.json') {
            throw 'Package manifest must not list itself.'
        }
        if (Test-PackageExcludedRelativePath -RelativePath $relative) {
            throw "Package manifest contains a forbidden path: $relative"
        }
        if (-not $paths.Add($relative)) {
            throw "Package manifest contains a duplicate path: $relative"
        }
        if ($entry.bytes -isnot [int] -and $entry.bytes -isnot [long]) {
            throw "Package manifest byte count must be an integer: $relative"
        }
        [int64]$bytes = $entry.bytes
        if ($bytes -lt 0) {
            throw "Package manifest byte count is negative: $relative"
        }
        if ($entry.sha256 -isnot [string] -or [string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Package manifest SHA-256 is invalid: $relative"
        }

        $payloadPath = Join-Path $Root ($relative.Replace('/', '\'))
        if (-not (Test-PackagePathWithin -Root $Root -Candidate $payloadPath)) {
            throw "Manifest path escapes the package root: $relative"
        }
        Assert-PackageExistingPathSafe -Path $payloadPath -Label "Payload $relative" -PathType File | Out-Null
        $item = Get-Item -LiteralPath $payloadPath -Force -ErrorAction Stop
        if ([int64]$item.Length -ne $bytes) {
            throw "Payload byte count mismatch for ${relative}: expected $bytes, actual $($item.Length)"
        }
        $portableAsset = $relative -match '^(?i:portable/(?:docs|templates|chatgpt)/)'
        Assert-PackageSafeTextBytes `
            -Bytes ([System.IO.File]::ReadAllBytes($payloadPath)) `
            -Path $relative `
            -PortableAsset:$portableAsset
        $actualHash = Get-PackageSha256Hex -Path $payloadPath
        if ($actualHash -cne [string]$entry.sha256) {
            throw "Payload SHA-256 mismatch for ${relative}: expected $($entry.sha256), actual $actualHash"
        }
        $listedBytes += $bytes
    }

    $actualPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse -File -ErrorAction Stop)) {
        $relative = Get-PackageRelativePath -Root $Root -Path $item.FullName
        Assert-PackageRelativePath -RelativePath $relative
        if ($relative -eq 'MANIFEST.json') {
            continue
        }
        if (Test-PackageExcludedRelativePath -RelativePath $relative) {
            throw "Package contains a forbidden path: $relative"
        }
        if (-not $actualPaths.Add($relative)) {
            throw "Package contains duplicate case-insensitive paths: $relative"
        }
        if (-not $paths.Contains($relative)) {
            throw "Package contains an unmanifested file: $relative"
        }
    }
    if ($actualPaths.Count -ne $paths.Count) {
        throw "Package file count mismatch: manifest lists $($paths.Count), actual payload has $($actualPaths.Count)"
    }
    if ($totalBytes -ne $listedBytes) {
        throw "Package manifest total_bytes does not match its entries: expected $listedBytes, actual $totalBytes"
    }

    foreach ($required in @(
        'verify-package.ps1',
        'install.ps1',
        'update.ps1',
        'doctor.ps1',
        'restore.ps1',
        'easy-install.ps1',
        'schemas/package-manifest.schema.json',
        'portable/install/package-safety.ps1',
        'portable/install/install.ps1',
        'portable/install/update.ps1',
        'portable/install/doctor.ps1',
        'portable/install/restore.ps1',
        'portable/install/easy-install.ps1',
        'plugin/.codex-plugin/plugin.json',
        'plugin/install.ps1',
        'plugin/restore.ps1',
        'plugin/scripts/mochicode.py',
        'plugin/scripts/mochicode_core/cli.py'
    )) {
        if (-not $paths.Contains($required)) {
            throw "Package is missing required file: $required"
        }
    }
    foreach ($category in @('docs', 'templates', 'chatgpt')) {
        $prefix = "portable/$category/"
        $hasCategory = $false
        foreach ($path in $paths) {
            if ([string]$path -like "$prefix*") {
                $hasCategory = $true
                break
            }
        }
        if (-not $hasCategory) {
            throw "Package is missing required portable asset category: $category"
        }
    }

    $metadataPath = Join-Path $Root 'plugin\.codex-plugin\plugin.json'
    $metadataDocument = Read-PackageJsonObject -Path $metadataPath -Label 'Bundled plugin metadata'
    $metadata = $metadataDocument.Value
    if ([string]$metadata.name -cne 'mochicode-auto') {
        throw "Bundled plugin metadata has unexpected name: $($metadata.name)"
    }
    if ([string]$metadata.version -cne [string]$manifest.source_plugin_version) {
        throw 'Package source_plugin_version does not match bundled plugin metadata.'
    }

    return [PSCustomObject]@{
        Manifest = $manifest
        FileCount = $paths.Count
        TotalBytes = $listedBytes
        GeneratedAtUtc = $timestampValue
    }
}

if (-not [string]::IsNullOrWhiteSpace($ZipPath)) {
    $zipFull = Assert-PackageExistingPathSafe -Path $ZipPath -Label 'Package ZIP' -PathType File
    $zipParent = Split-Path -Path $zipFull -Parent
    $temporaryRoot = Join-Path $zipParent ('.mochicode-package-verify-' + [guid]::NewGuid().ToString('N'))
    Assert-PackageNewPathSafe -Path $temporaryRoot -Label 'ZIP verification directory' | Out-Null
    try {
        Expand-PackageZipSafely -ZipPath $zipFull -Destination $temporaryRoot | Out-Null
        $result = Assert-PackageManifest -Root $temporaryRoot
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $Quiet) {
        Write-Output "Verified package ZIP: $zipFull"
        Write-Output "Payload files: $($result.FileCount)"
        Write-Output "Payload bytes: $($result.TotalBytes)"
    }
    exit 0
}

$result = Assert-PackageManifest -Root $packageRootFull
if (-not $Quiet) {
    Write-Output "Verified portable package: $packageRootFull"
    Write-Output "Payload files: $($result.FileCount)"
    Write-Output "Payload bytes: $($result.TotalBytes)"
    Write-Output "Generated UTC: $($result.GeneratedAtUtc)"
}
