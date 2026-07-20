[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "Creating virtual environment at $venvPath"
    & py -3 -m venv $venvPath
}

Write-Host 'Installing project dependencies'
& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements.txt')
& $pythonPath -m pip install --no-deps --editable $projectRoot

Write-Host 'Setup complete.'
& $pythonPath --version
