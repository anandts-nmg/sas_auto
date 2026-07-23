[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue

if ($null -eq $uvCommand) {
    throw 'uv is required. Run .\scripts\setup.ps1 after installing uv.'
}

Push-Location $projectRoot
try {
    Write-Output 'Ruff lint'
    & $uvCommand.Source run --locked ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }

    Write-Output 'Ruff format check'
    & $uvCommand.Source run --locked ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed.' }

    Write-Output 'mypy'
    & $uvCommand.Source run --locked mypy
    if ($LASTEXITCODE -ne 0) { throw 'mypy failed.' }

    Write-Output 'Pyright/Pylance type check'
    & npm run typecheck:pyright --silent
    if ($LASTEXITCODE -ne 0) { throw 'Pyright failed.' }

    Write-Output 'yamllint'
    & $uvCommand.Source run --locked yamllint config.yaml config.example.yaml .yamllint.yml
    if ($LASTEXITCODE -ne 0) { throw 'yamllint failed.' }

    Write-Output 'PSScriptAnalyzer'
    if (-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)) {
        throw 'PSScriptAnalyzer is not installed. Run .\scripts\setup.ps1 first.'
    }
    Import-Module PSScriptAnalyzer -ErrorAction Stop
    $analysis = @(Invoke-ScriptAnalyzer -Path $PSScriptRoot -Recurse -Settings (Join-Path $projectRoot 'PSScriptAnalyzerSettings.psd1'))
    if ($analysis.Count -gt 0) {
        $analysis | Format-Table -AutoSize
        throw "PSScriptAnalyzer reported $($analysis.Count) issue(s)."
    }

    Write-Output 'markdownlint'
    & npm run lint:markdown --silent
    if ($LASTEXITCODE -ne 0) { throw 'markdownlint failed.' }

    Write-Output 'All lint and type checks passed.'
}
finally {
    Pop-Location
}
