[CmdletBinding()]
param(
    [string]$Request,
    [ValidateRange(1, [int]::MaxValue)][int]$PrNumber
)

$arguments = @{}
if (-not [string]::IsNullOrWhiteSpace($Request)) {
    $arguments.Request = $Request
}
if ($PSBoundParameters.ContainsKey("PrNumber")) {
    $arguments.PrNumber = $PrNumber
}

& (Join-Path $PSScriptRoot "codex-auto.ps1") @arguments
$codexExitCode = $LASTEXITCODE
if ($codexExitCode -ne 0) {
    exit $codexExitCode
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (Test-Path -LiteralPath (Join-Path $repoRoot "handoff.json") -PathType Leaf) {
    $publishArguments = @{}
    if ($PSBoundParameters.ContainsKey("PrNumber")) {
        $publishArguments.PrNumber = $PrNumber
    }
    & (Join-Path $PSScriptRoot "publish-from-handoff.ps1") @publishArguments
    exit $LASTEXITCODE
}

exit 0
