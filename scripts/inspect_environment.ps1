[CmdletBinding()]
param(
    [switch] $AsJson
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$kmzPath = Join-Path $projectRoot 'Selection_91_All_Areas.kmz'
$sasPath = 'C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe'

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
$pythonVersion = if ($pythonCommand) { (& py -3 --version 2>&1 | Out-String).Trim() } else { $null }

$kmzInfo = $null
if (Test-Path -LiteralPath $kmzPath -PathType Leaf) {
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

$sasInfo = $null
$esriDefinitions = @()
if (Test-Path -LiteralPath $sasPath -PathType Leaf) {
    $sasItem = Get-Item -LiteralPath $sasPath
    $binary = [System.IO.File]::ReadAllBytes($sasPath)
    $asciiText = [System.Text.Encoding]::ASCII.GetString($binary)
    $unicodeText = [System.Text.Encoding]::Unicode.GetString($binary)
    $mapsRoot = Join-Path $sasItem.DirectoryName 'Maps'
    $esriDefinitions = @(
        Get-ChildItem -LiteralPath $mapsRoot -Recurse -File -Filter 'params.txt' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $text = Get-Content -LiteralPath $_.FullName -Raw
                if ($text -match '(?im)^\s*name\s*=\s*(ESRI ArcGIS\.Imagery|Esri World Imagery)\s*$') {
                    $guidMatch = [regex]::Match($text, '(?im)^\s*guid\s*=\s*(\{[0-9a-f-]{36}\})\s*$')
                    if ($guidMatch.Success) {
                        [ordered]@{
                            name = $Matches[1]
                            guid = $guidMatch.Groups[1].Value.ToUpperInvariant()
                            params_path = $_.FullName
                        }
                    }
                }
            }
    )
    $sasInfo = [ordered]@{
        path = $sasItem.FullName
        version = $sasItem.VersionInfo.FileVersion
        length = $sasItem.Length
        maps_directory = $mapsRoot
        sls_autostart = $asciiText.Contains('--sls-autostart') -or $unicodeText.Contains('--sls-autostart')
    }
}

$report = [ordered]@{
    inspected_at = (Get-Date).ToUniversalTime().ToString('o')
    project_root = $projectRoot
    python = [ordered]@{ launcher = $pythonCommand.Source; version = $pythonVersion }
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
    Write-Output "KMZ: $($kmzInfo.path) (doc.kml=$($kmzInfo.contains_doc_kml))"
    Write-Output "SAS.Planet: $($sasInfo.path) (--sls-autostart=$($sasInfo.sls_autostart))"
    Write-Output "ESRI ArcGIS.Imagery definitions: $($esriDefinitions.Count)"
    Write-Output 'No application was launched or modified.'
}
