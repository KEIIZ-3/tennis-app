[CmdletBinding()]
param([string]$Request)

$arguments = @{}
if (-not [string]::IsNullOrWhiteSpace($Request)) {
    $arguments.Request = $Request
}

& (Join-Path $PSScriptRoot "codex-auto.ps1") @arguments
$codexExitCode = $LASTEXITCODE
if ($codexExitCode -ne 0) {
    exit $codexExitCode
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (Test-Path -LiteralPath (Join-Path $repoRoot "handoff.json") -PathType Leaf) {
    & (Join-Path $PSScriptRoot "publish-from-handoff.ps1")
    exit $LASTEXITCODE
}

exit 0
