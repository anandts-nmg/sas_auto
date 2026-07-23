[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue

if ($null -eq $uvCommand) {
    throw 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun setup.'
}

Push-Location $projectRoot
try {
    Write-Output 'Synchronizing locked Python dependencies with uv'
    & $uvCommand.Source sync --locked --all-groups
    if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' }

    if (-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)) {
        Write-Output 'Installing PSScriptAnalyzer for the current user'
        Install-Module -Name PSScriptAnalyzer -RequiredVersion '1.25.0' -Scope CurrentUser -Force -SkipPublisherCheck
    }

    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Output 'Installing locked Markdown and Pyright dependencies'
        & npm ci --prefix $projectRoot
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    }
    else {
        throw 'Node.js/npm is required for Markdown linting and Pyright.'
    }

    Write-Output 'Setup complete.'
    & $uvCommand.Source run --locked python --version
    if ($LASTEXITCODE -ne 0) { throw 'Unable to run Python through uv.' }
}
finally {
    Pop-Location
}
