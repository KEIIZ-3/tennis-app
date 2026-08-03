[CmdletBinding()]
param([string]$Request)

$arguments = @{ TaskType = "dev" }
if (-not [string]::IsNullOrWhiteSpace($Request)) {
    $arguments.Request = $Request
}

& (Join-Path $PSScriptRoot "codex-auto.ps1") @arguments
exit $LASTEXITCODE
