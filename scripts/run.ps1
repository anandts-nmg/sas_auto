[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw 'uv is required. Run .\scripts\setup.ps1 after installing uv.'
}

Push-Location $projectRoot
try {
    & $uvCommand.Source run --locked python -m sas_auto.cli @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
