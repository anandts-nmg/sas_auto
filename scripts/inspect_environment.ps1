[CmdletBinding()]
param(
    [switch] $AsJson,
    [string] $ConfigPath,
    [string] $InputKmz,
    [string] $SasPlanetExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot 'config.yaml'
}
elseif (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot $ConfigPath
}

function Get-TopLevelYamlScalar {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Key
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $text = Get-Content -LiteralPath $Path -Raw
    $pattern = '(?m)^' + [regex]::Escape($Key) + ':\s*(.*?)\s*$'
    $match = [regex]::Match($text, $pattern)
    if (-not $match.Success) {
        return $null
    }
    return $match.Groups[1].Value.Trim().Trim("'`"")
}

if ([string]::IsNullOrWhiteSpace($InputKmz)) {
    $InputKmz = Get-TopLevelYamlScalar -Path $ConfigPath -Key 'input_kmz'
}
if ([string]::IsNullOrWhiteSpace($SasPlanetExe)) {
    $SasPlanetExe = Get-TopLevelYamlScalar -Path $ConfigPath -Key 'sasplanet_exe'
}

$kmzPath = $null
if (-not [string]::IsNullOrWhiteSpace($InputKmz)) {
    $kmzPath = if ([System.IO.Path]::IsPathRooted($InputKmz)) {
        $InputKmz
    }
    else {
        Join-Path $projectRoot $InputKmz
    }
}

$sasPath = $null
if (-not [string]::IsNullOrWhiteSpace($SasPlanetExe)) {
    $sasPath = if ([System.IO.Path]::IsPathRooted($SasPlanetExe)) {
        $SasPlanetExe
    }
    else {
        Join-Path $projectRoot $SasPlanetExe
    }
}

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
$pythonVersion = if ($pythonCommand) { (& py -3 --version 2>&1 | Out-String).Trim() } else { $null }

$kmzInfo = [ordered]@{
    path = $kmzPath
    readable = $false
    length = $null
    sha256 = $null
    entries = @()
    contains_doc_kml = $false
}
if ($kmzPath -and (Test-Path -LiteralPath $kmzPath -PathType Leaf)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($kmzPath)
    try {
        $entries = @($archive.Entries | ForEach-Object FullName)
    }
    finally {
        $archive.Dispose()
    }
    $kmzItem = Get-Item -LiteralPath $kmzPath
    $kmzInfo = [ordered]@{
        path = $kmzItem.FullName
        readable = $true
        length = $kmzItem.Length
        sha256 = (Get-FileHash -LiteralPath $kmzPath -Algorithm SHA256).Hash
        entries = $entries
        contains_doc_kml = $entries -contains 'doc.kml'
    }
}

$sasInfo = [ordered]@{
    path = $sasPath
    exists = $false
    version = $null
    length = $null
    maps_directory = $null
    sls_autostart = $false
}
$esriDefinitions = @()
if ($sasPath -and (Test-Path -LiteralPath $sasPath -PathType Leaf)) {
    $sasItem = Get-Item -LiteralPath $sasPath
    $binary = [System.IO.File]::ReadAllBytes($sasPath)
    $asciiText = [System.Text.Encoding]::ASCII.GetString($binary)
    $unicodeText = [System.Text.Encoding]::Unicode.GetString($binary)
    $mapsRoot = Join-Path $sasItem.DirectoryName 'Maps'
    if (Test-Path -LiteralPath $mapsRoot -PathType Container) {
        $esriDefinitions = @(
            Get-ChildItem -LiteralPath $mapsRoot -Recurse -File -Filter 'params.txt' -ErrorAction SilentlyContinue |
                ForEach-Object {
                    $text = Get-Content -LiteralPath $_.FullName -Raw
                    $nameMatch = [regex]::Match(
                        $text,
                        '(?im)^\s*name\s*=\s*(ESRI ArcGIS\.Imagery|Esri World Imagery)\s*$'
                    )
                    if ($nameMatch.Success) {
                        $guidMatch = [regex]::Match($text, '(?im)^\s*guid\s*=\s*(\{[0-9a-f-]{36}\})\s*$')
                        if ($guidMatch.Success) {
                            [ordered]@{
                                name = $nameMatch.Groups[1].Value
                                guid = $guidMatch.Groups[1].Value.ToUpperInvariant()
                                params_path = $_.FullName
                            }
                        }
                    }
                }
        )
    }
    $sasInfo = [ordered]@{
        path = $sasItem.FullName
        exists = $true
        version = $sasItem.VersionInfo.FileVersion
        length = $sasItem.Length
        maps_directory = $mapsRoot
        sls_autostart = $asciiText.Contains('--sls-autostart') -or $unicodeText.Contains('--sls-autostart')
    }
}

$report = [ordered]@{
    inspected_at = (Get-Date).ToUniversalTime().ToString('o')
    project_root = $projectRoot
    config_path = $ConfigPath
    python = [ordered]@{
        launcher = if ($pythonCommand) { $pythonCommand.Source } else { $null }
        version = $pythonVersion
    }
    kmz = $kmzInfo
    sasplanet = $sasInfo
    esri_imagery_definitions = $esriDefinitions
    applications_launched = $false
}

if ($AsJson) {
    $report | ConvertTo-Json -Depth 8
}
else {
    $report | Format-List
    Write-Output "Python: $pythonVersion"
    Write-Output "KMZ: $($kmzInfo.path) (readable=$($kmzInfo.readable), doc.kml=$($kmzInfo.contains_doc_kml))"
    Write-Output "SAS.Planet: $($sasInfo.path) (exists=$($sasInfo.exists), --sls-autostart=$($sasInfo.sls_autostart))"
    Write-Output "ESRI ArcGIS.Imagery definitions: $($esriDefinitions.Count)"
    Write-Output 'No application was launched or modified.'
}
