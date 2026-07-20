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

$shortcutRoots = @(
    [Environment]::GetFolderPath('Desktop'),
    [Environment]::GetFolderPath('CommonDesktopDirectory'),
    [Environment]::GetFolderPath('StartMenu'),
    [Environment]::GetFolderPath('CommonStartMenu')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$shell = New-Object -ComObject WScript.Shell
$shortcutRecords = @(
    Get-ChildItem -LiteralPath $shortcutRoots -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '(?i)google.*earth|earth.*pro' } |
        ForEach-Object {
            $shortcut = $shell.CreateShortcut($_.FullName)
            [ordered]@{ shortcut = $_.FullName; target = $shortcut.TargetPath; arguments = $shortcut.Arguments }
        }
)

$earthCandidates = @(
    'C:\Program Files\Google\Google Earth Pro\client\googleearth.exe',
    'C:\Program Files (x86)\Google\Google Earth Pro\client\googleearth.exe',
    (Join-Path $env:LOCALAPPDATA 'Google\Google Earth Pro\client\googleearth.exe')
) + @($shortcutRecords | ForEach-Object target)
$earthPath = $earthCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1

$sasInfo = if (Test-Path -LiteralPath $sasPath -PathType Leaf) {
    $item = Get-Item -LiteralPath $sasPath
    [ordered]@{ path = $item.FullName; version = $item.VersionInfo.FileVersion; length = $item.Length }
} else { $null }
$earthInfo = if ($earthPath) {
    $item = Get-Item -LiteralPath $earthPath
    [ordered]@{ path = $item.FullName; version = $item.VersionInfo.FileVersion; length = $item.Length }
} else { $null }

$report = [ordered]@{
    inspected_at = (Get-Date).ToUniversalTime().ToString('o')
    project_root = $projectRoot
    directory_entries = @(Get-ChildItem -LiteralPath $projectRoot -Force | ForEach-Object Name)
    python = [ordered]@{ launcher = $pythonCommand.Source; version = $pythonVersion }
    kmz = $kmzInfo
    sasplanet = $sasInfo
    google_earth = $earthInfo
    google_earth_shortcuts = $shortcutRecords
    applications_launched = $false
}

if ($AsJson) {
    $report | ConvertTo-Json -Depth 8
} else {
    $report | Format-List
    Write-Host "Python: $pythonVersion"
    Write-Host "KMZ: $($kmzInfo.path) (doc.kml=$($kmzInfo.contains_doc_kml))"
    Write-Host "SAS.Planet: $($sasInfo.path)"
    Write-Host "Google Earth Pro: $($earthInfo.path)"
    Write-Host 'No application was launched or modified.'
}
