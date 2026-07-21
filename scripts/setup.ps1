[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Output "Creating virtual environment at $venvPath"
    & py -3 -m venv $venvPath
}

Write-Output 'Installing project dependencies'
& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements.txt')
& $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements-dev.txt')
& $pythonPath -m pip install --no-deps --editable $projectRoot

if (-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)) {
    Write-Output 'Installing PSScriptAnalyzer for the current user'
    Install-Module -Name PSScriptAnalyzer -RequiredVersion '1.25.0' -Scope CurrentUser -Force -SkipPublisherCheck
}

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Output 'Installing Markdown lint dependencies'
    & npm install --prefix $projectRoot
}
else {
    throw 'Node.js/npm is required for Markdown linting.'
}

Write-Output 'Setup complete.'
& $pythonPath --version
