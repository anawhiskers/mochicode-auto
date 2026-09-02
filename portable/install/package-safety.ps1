Set-StrictMode -Version Latest

$script:PackageZipMaxEntryCount = 1024
$script:PackageZipMaxEntryUncompressedBytes = [int64](4MB)
$script:PackageZipMaxTotalUncompressedBytes = [int64](16MB)
$script:PackageZipMaxCompressionRatio = [double]100
$script:PackageZipCopyBufferBytes = 65536

function ConvertTo-PackageFullPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path must not be empty.'
    }
    if ($Path -match '[\x00-\x1f\x7f]') {
        throw "Path contains a control character: $Path"
    }
    if ($Path -match '^(?i:[a-z]):(?:$|[^\\/])') {
        throw "Drive-relative paths are not safe: $Path"
    }
    if ($Path -match '^[\\/]{2}') {
        throw "UNC and device paths are not safe: $Path"
    }
    try {
        return [System.IO.Path]::GetFullPath($Path)
    } catch {
        throw "Path is not valid: $Path"
    }
}

function Test-PackagePathWithin {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Candidate
    )

    $rootFull = (ConvertTo-PackageFullPath $Root).TrimEnd('\', '/')
    $candidateFull = (ConvertTo-PackageFullPath $Candidate).TrimEnd('\', '/')
    if ([string]::Equals($rootFull, $candidateFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-PackageReparsePoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.FileSystemInfo]$Item
    )

    $hasLinkType = @($Item.PSObject.Properties.Name) -contains 'LinkType'
    return (
        (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -or
        ($hasLinkType -and $null -ne $Item.LinkType)
    )
}

function Assert-PackageNotOneDrive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Label = 'Path'
    )

    $full = ConvertTo-PackageFullPath $Path
    $knownRoots = @(
        $env:OneDrive,
        $env:OneDriveCommercial,
        $env:OneDriveConsumer
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }

    foreach ($rawRoot in $knownRoots) {
        try {
            $oneDriveRoot = ConvertTo-PackageFullPath ([string]$rawRoot)
        } catch {
            continue
        }
        if (Test-PackagePathWithin -Root $oneDriveRoot -Candidate $full) {
            throw "$Label is under OneDrive and is refused: $full"
        }
    }

    foreach ($segment in ($full -split '[\\/]')) {
        if ($segment -match '^(?i:onedrive)') {
            throw "$Label contains a OneDrive path component and is refused: $full"
        }
    }
}

function Assert-PackageExistingPathSafe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Label = 'Path',
        [ValidateSet('Any', 'File', 'Directory')]
        [string]$PathType = 'Any'
    )

    $full = ConvertTo-PackageFullPath $Path
    if (-not (Test-Path -LiteralPath $full)) {
        throw "$Label does not exist: $full"
    }
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($PathType -eq 'File' -and $item -isnot [System.IO.FileInfo]) {
        throw "$Label is not a file: $full"
    }
    if ($PathType -eq 'Directory' -and $item -isnot [System.IO.DirectoryInfo]) {
        throw "$Label is not a directory: $full"
    }
    Assert-PackageNotOneDrive -Path $full -Label $Label

    $cursor = $full
    while ($true) {
        $current = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (Test-PackageReparsePoint -Item $current) {
            throw "$Label crosses a reparse point: $cursor"
        }
        [void](Resolve-Path -LiteralPath $cursor -ErrorAction Stop)
        $parent = Split-Path -Path $cursor -Parent
        if (
            [string]::IsNullOrWhiteSpace($parent) -or
            [string]::Equals($parent, $cursor, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            break
        }
        $cursor = $parent
    }
    return $full
}

function Assert-PackageNewPathSafe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$Label = 'New path'
    )

    $full = ConvertTo-PackageFullPath $Path
    Assert-PackageNotOneDrive -Path $full -Label $Label
    if (Test-Path -LiteralPath $full) {
        throw "$Label already exists and is refused: $full"
    }

    $parent = Split-Path -Path $full -Parent
    if ([string]::IsNullOrWhiteSpace($parent)) {
        throw "$Label has no usable parent directory: $full"
    }
    $cursor = $parent
    while (-not (Test-Path -LiteralPath $cursor)) {
        $next = Split-Path -Path $cursor -Parent
        if (
            [string]::IsNullOrWhiteSpace($next) -or
            [string]::Equals($next, $cursor, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "$Label has no existing safe ancestor: $full"
        }
        $cursor = $next
    }
    Assert-PackageExistingPathSafe -Path $cursor -Label "$Label parent" -PathType Directory | Out-Null
    return $full
}

function Assert-PackageNoReparseInTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [string]$Label = 'Tree'
    )

    $full = Assert-PackageExistingPathSafe -Path $Root -Label $Label -PathType Directory
    foreach ($item in @(Get-ChildItem -LiteralPath $full -Force -Recurse -ErrorAction Stop)) {
        if (Test-PackageReparsePoint -Item $item) {
            throw "$Label contains a reparse point: $($item.FullName)"
        }
    }
    return $full
}

function Get-PackageRelativePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $rootFull = ConvertTo-PackageFullPath $Root
    $pathFull = ConvertTo-PackageFullPath $Path
    if (-not (Test-PackagePathWithin -Root $rootFull -Candidate $pathFull)) {
        throw "Path escapes root: $pathFull"
    }
    $relative = [System.IO.Path]::GetRelativePath($rootFull, $pathFull).Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($relative) -or $relative -eq '.') {
        return ''
    }
    if ($relative -match '^(?:\.\.(?:/|$))' -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "Path escapes root: $pathFull"
    }
    return $relative
}

function Assert-PackageRelativePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    if ([string]::IsNullOrWhiteSpace($RelativePath)) {
        throw 'A package relative path must not be empty.'
    }
    if ($RelativePath -ne $RelativePath.Replace('\', '/')) {
        throw "Package paths must use forward slashes: $RelativePath"
    }
    if ($RelativePath.StartsWith('/') -or $RelativePath -match '^(?i:[a-z]:)') {
        throw "Package path is not relative: $RelativePath"
    }
    if ($RelativePath -match '[\x00-\x1f\x7f]') {
        throw "Package path contains a control character: $RelativePath"
    }

    foreach ($part in @($RelativePath -split '/')) {
        if ([string]::IsNullOrEmpty($part)) {
            throw "Package path contains an empty component: $RelativePath"
        }
        if ($part -eq '.' -or $part -eq '..') {
            throw "Package path contains traversal: $RelativePath"
        }
        if ($part -match '[:*?"<>|]') {
            throw "Package path contains an unsafe Windows character: $RelativePath"
        }
        if ($part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw "Package path contains a non-canonical component: $RelativePath"
        }
        if ($part -match '^(?i:(?:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\..*)?)$') {
            throw "Package path contains a reserved Windows device name: $RelativePath"
        }
    }
}

function Test-PackageExcludedRelativePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $normalized = $RelativePath.Replace('\', '/').TrimStart('/')
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $true
    }
    $parts = @($normalized -split '/')
    $leaf = [string]$parts[$parts.Count - 1]
    $runtimeDirectories = @(
        '.git', '.hg', '.svn', '__pycache__', '.pytest_cache', '.mypy_cache',
        '.venv', 'venv', 'node_modules', 'cache', 'caches', 'benchmark', 'benchmarks',
        'benchmark-data', 'benchmark_work', 'work', 'working', 'runtime',
        'run', 'runs', 'state', 'learning', 'logs', 'log', 'evidence',
        'sessions', 'session', 'auth', 'authentication', 'credentials',
        'secrets', 'private', 'tmp', 'temp'
    )
    foreach ($part in $parts) {
        if ($runtimeDirectories -contains [string]$part) {
            return $true
        }
    }

    if ($leaf -match '(?i)\.py[co]$') {
        return $true
    }
    if ($leaf -match '(?i)\.(?:log|trace|dmp|jsonl)$') {
        return $true
    }
    if ($leaf -match '^(?i:\.env(?:\..*)?)$') {
        return $true
    }
    if ($leaf -match '(?i)(?:^|[-_.])(?:secret|credential|password|token|auth|oauth|cookie|session)(?:[-_.]|$)') {
        return $true
    }
    if ($leaf -match '(?i)(?:^|[-_.])(?:raw|unredacted)(?:[-_.]|$)') {
        return $true
    }
    return $false
}

function Get-PackageSha256Hex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $full = Assert-PackageExistingPathSafe -Path $Path -Label 'Hashed file' -PathType File
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($full)
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Write-PackageUtf8NoBom {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Content
    )

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText((ConvertTo-PackageFullPath $Path), $Content, $encoding)
}

function Assert-PackageVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value,
        [string]$Label = 'Version'
    )

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._+~\-]{0,127}$') {
        throw "$Label is missing or invalid."
    }
}

function ConvertTo-PackageTimestamp {
    [CmdletBinding()]
    param(
        [string]$Value
    )

    $styles = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
    try {
        $parsed = if ([string]::IsNullOrWhiteSpace($Value)) {
            [DateTimeOffset]::UtcNow
        } else {
            [DateTimeOffset]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture, $styles)
        }
    } catch {
        throw 'GeneratedTimestampUtc must be a valid UTC-compatible timestamp.'
    }
    return $parsed.ToUniversalTime().ToString(
        'yyyy-MM-ddTHH:mm:ss.fffZ',
        [Globalization.CultureInfo]::InvariantCulture
    )
}

function Assert-PackageSafeTextBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$PortableAsset
    )

    try {
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($Bytes)
    } catch {
        throw "Package file is not valid UTF-8 text: $Path"
    }

    $credentialPatterns = @(
        '(?im)-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----',
        '(?im)\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,})\b',
        '(?im)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}',
        '(?im)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|secret|credential)\b\s*[:=]\s*["'']?(?!<[^>]+>|\$\{[^}]+\}|YOUR[_ -]?|REDACTED\b|REPLACE[_ -]?)[A-Za-z0-9+/_=.:-]{8,}'
    )
    foreach ($pattern in $credentialPatterns) {
        if ($text -match $pattern) {
            throw "Package file contains a secret or credential value: $Path"
        }
    }

    $privateAddressPattern = '(?i)(?<![0-9])(?:(?:10|127)\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}|192\.168\.(?:[0-9]{1,3}\.)[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[0-1])\.(?:[0-9]{1,3}\.)[0-9]{1,3})(?![0-9])'
    if ($text -match $privateAddressPattern) {
        throw "Package file contains a private or loopback endpoint: $Path"
    }

    $urlPattern = '(?i)\b(?:https?|wss?)://(?:(?:[^/\s:@]+(?::[^/\s@]*)?@)?)(?<host>\[[^\]]+\]|[^/\s:]+)'
    foreach ($match in [regex]::Matches($text, $urlPattern)) {
        $urlHost = ([string]$match.Groups['host'].Value).Trim('[', ']').ToLowerInvariant()
        if (
            $urlHost -eq 'localhost' -or
            $urlHost -eq '0.0.0.0' -or
            $urlHost -eq '::1' -or
            $urlHost.EndsWith('.local') -or
            $urlHost.EndsWith('.internal') -or
            $urlHost.EndsWith('.lan') -or
            $urlHost.EndsWith('.corp')
        ) {
            throw "Package file contains a private endpoint: $Path"
        }
    }

    if ($PortableAsset -and $text -match '(?im)(?:^|[\s`])(?:[A-Z]:[\\/]|/(?:Users|home|private|var)/)') {
        throw "Portable asset contains a computer-specific private path: $Path"
    }
}

function Test-PackageZipSymlinkEntry {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.Compression.ZipArchiveEntry]$Entry
    )

    $attributes = [int64]$Entry.ExternalAttributes
    $unixType = ($attributes -shr 16) -band 0xF000
    return ($unixType -eq 0xA000 -or ($attributes -band 0x400) -ne 0)
}

function Assert-PackageZipPathConflicts {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.List[object]]$Entries
    )

    $files = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $directories = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Entries) {
        if ([bool]$entry.IsDirectory) {
            [void]$directories.Add([string]$entry.RelativePath)
        } else {
            [void]$files.Add([string]$entry.RelativePath)
        }
    }
    foreach ($file in $files) {
        $parts = @($file -split '/')
        $prefix = [System.Collections.Generic.List[string]]::new()
        for ($index = 0; $index -lt ($parts.Count - 1); $index++) {
            [void]$prefix.Add([string]$parts[$index])
            $ancestor = $prefix -join '/'
            if ($files.Contains($ancestor)) {
                throw "ZIP contains a file and a descendant path: $file"
            }
        }
    }
}

function Expand-PackageZipSafely {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ZipPath,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $zipFull = Assert-PackageExistingPathSafe -Path $ZipPath -Label 'ZIP archive' -PathType File
    $destinationFull = Assert-PackageNewPathSafe -Path $Destination -Label 'ZIP extraction directory'
    $destinationParent = Split-Path -Path $destinationFull -Parent
    $stagingRoot = Join-Path $destinationParent ('.mochicode-package-extract-' + [guid]::NewGuid().ToString('N'))
    Assert-PackageNewPathSafe -Path $stagingRoot -Label 'ZIP extraction staging directory' | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipFull)
    $entries = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    [int64]$declaredTotalBytes = 0
    $committed = $false
    try {
        foreach ($entry in $archive.Entries) {
            if (($entries.Count + 1) -gt $script:PackageZipMaxEntryCount) {
                throw "ZIP entry count exceeds limit of $($script:PackageZipMaxEntryCount)."
            }
            if (Test-PackageZipSymlinkEntry -Entry $entry) {
                throw "ZIP contains a symlink or reparse-point entry: $($entry.FullName)"
            }
            $isDirectory = $entry.FullName.EndsWith('/')
            $relative = if ($isDirectory) {
                $entry.FullName.TrimEnd('/')
            } else {
                $entry.FullName
            }
            Assert-PackageRelativePath -RelativePath $relative
            if (-not $seen.Add($relative)) {
                throw "ZIP contains a duplicate path: $relative"
            }
            [int64]$uncompressedBytes = $entry.Length
            [int64]$compressedBytes = $entry.CompressedLength
            if ($uncompressedBytes -lt 0 -or $compressedBytes -lt 0) {
                throw "ZIP entry has invalid byte counts: $relative"
            }
            if ($isDirectory -and ($uncompressedBytes -ne 0 -or $compressedBytes -ne 0)) {
                throw "ZIP directory entry contains payload bytes: $relative"
            }
            if ($uncompressedBytes -gt $script:PackageZipMaxEntryUncompressedBytes) {
                throw "ZIP entry exceeds uncompressed byte limit of $($script:PackageZipMaxEntryUncompressedBytes): $relative"
            }
            if ($declaredTotalBytes -gt ($script:PackageZipMaxTotalUncompressedBytes - $uncompressedBytes)) {
                throw "ZIP total uncompressed bytes exceed limit of $($script:PackageZipMaxTotalUncompressedBytes)."
            }
            $declaredTotalBytes += $uncompressedBytes
            if ($uncompressedBytes -gt 0) {
                if ($compressedBytes -le 0) {
                    throw "ZIP entry has an invalid zero-byte compressed payload: $relative"
                }
                $compressionRatio = [double]$uncompressedBytes / [double]$compressedBytes
                if ($compressionRatio -gt $script:PackageZipMaxCompressionRatio) {
                    throw "ZIP entry compression ratio exceeds limit of $($script:PackageZipMaxCompressionRatio): $relative"
                }
            }

            $candidate = Join-Path $stagingRoot ($relative.Replace('/', '\'))
            if (-not (Test-PackagePathWithin -Root $stagingRoot -Candidate $candidate)) {
                throw "ZIP entry escapes the extraction directory: $relative"
            }
            [void]$entries.Add([PSCustomObject]@{
                RelativePath = $relative
                FullPath = [System.IO.Path]::GetFullPath($candidate)
                IsDirectory = $isDirectory
                ExpectedLength = $uncompressedBytes
                Entry = $entry
            })
        }
        if ($entries.Count -eq 0) {
            throw 'ZIP archive is empty.'
        }
        Assert-PackageZipPathConflicts -Entries $entries

        New-Item -ItemType Directory -Path $stagingRoot -ErrorAction Stop | Out-Null
        foreach ($record in @($entries | Where-Object IsDirectory | Sort-Object RelativePath)) {
            New-Item -ItemType Directory -Path $record.FullPath -Force -ErrorAction Stop | Out-Null
            Assert-PackageExistingPathSafe -Path $record.FullPath -Label "ZIP directory $($record.RelativePath)" -PathType Directory | Out-Null
        }
        $buffer = [byte[]]::new($script:PackageZipCopyBufferBytes)
        [int64]$copiedTotalBytes = 0
        foreach ($record in @($entries | Where-Object { -not $_.IsDirectory } | Sort-Object RelativePath)) {
            $parent = Split-Path -Path $record.FullPath -Parent
            New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
            Assert-PackageExistingPathSafe -Path $parent -Label "ZIP parent for $($record.RelativePath)" -PathType Directory | Out-Null
            $input = $null
            $output = $null
            [int64]$entryCopiedBytes = 0
            try {
                $input = $record.Entry.Open()
                $output = [System.IO.File]::Open(
                    $record.FullPath,
                    [System.IO.FileMode]::CreateNew,
                    [System.IO.FileAccess]::Write,
                    [System.IO.FileShare]::None
                )
                while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    [int64]$readBytes = $read
                    if ($entryCopiedBytes -gt ([int64]$record.ExpectedLength - $readBytes)) {
                        throw "ZIP entry expanded beyond its declared byte count: $($record.RelativePath)"
                    }
                    if ($entryCopiedBytes -gt ($script:PackageZipMaxEntryUncompressedBytes - $readBytes)) {
                        throw "ZIP entry exceeded the bounded copy limit: $($record.RelativePath)"
                    }
                    if ($copiedTotalBytes -gt ($script:PackageZipMaxTotalUncompressedBytes - $readBytes)) {
                        throw 'ZIP extraction exceeded the bounded total copy limit.'
                    }
                    $output.Write($buffer, 0, $read)
                    $entryCopiedBytes += $readBytes
                    $copiedTotalBytes += $readBytes
                }
                if ($entryCopiedBytes -ne [int64]$record.ExpectedLength) {
                    throw "ZIP entry byte count changed during extraction: $($record.RelativePath)"
                }
            } finally {
                if ($null -ne $output) {
                    $output.Dispose()
                }
                if ($null -ne $input) {
                    $input.Dispose()
                }
            }
        }
        if ($copiedTotalBytes -ne $declaredTotalBytes) {
            throw 'ZIP total byte count changed during extraction.'
        }
        Assert-PackageNoReparseInTree -Root $stagingRoot -Label 'Extracted ZIP staging tree' | Out-Null
        [System.IO.Directory]::Move($stagingRoot, $destinationFull)
        $committed = $true
    } finally {
        $archive.Dispose()
        if (-not $committed -and (Test-Path -LiteralPath $stagingRoot)) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Assert-PackageNoReparseInTree -Root $destinationFull -Label 'Extracted ZIP tree' | Out-Null
    return $destinationFull
}
