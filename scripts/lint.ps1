[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Virtual environment not found. Run .\scripts\setup.ps1 first."
}

Push-Location $projectRoot
try {
    Write-Output 'Ruff lint'
    & $pythonPath -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }

    Write-Output 'Ruff format check'
    & $pythonPath -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed.' }

    Write-Output 'mypy'
    & $pythonPath -m mypy
    if ($LASTEXITCODE -ne 0) { throw 'mypy failed.' }

    Write-Output 'Pyright/Pylance type check'
    & npm run typecheck:pyright --silent
    if ($LASTEXITCODE -ne 0) { throw 'Pyright failed.' }

    Write-Output 'yamllint'
    & $pythonPath -m yamllint config.yaml config.example.yaml .yamllint.yml
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
