[CmdletBinding()]
param([string]$Request)

$arguments = @{}
if (-not [string]::IsNullOrWhiteSpace($Request)) {
    $arguments.Request = $Request
}

& (Join-Path $PSScriptRoot "codex-auto.ps1") @arguments
exit $LASTEXITCODE
