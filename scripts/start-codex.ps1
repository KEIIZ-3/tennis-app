[CmdletBinding()]
param(
    [string]$Request,
    [ValidateRange(1, [int]::MaxValue)][int]$PrNumber
)

$arguments = @{}
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$controlRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "tennis-app-control-{0}" -f [Guid]::NewGuid().ToString("N")
)

if (-not [string]::IsNullOrWhiteSpace($Request)) {
    $arguments.Request = $Request
}
if ($PSBoundParameters.ContainsKey("PrNumber")) {
    $arguments.PrNumber = $PrNumber
}
$arguments.RepositoryRoot = $repoRoot

try {
    # Keep the complete control bundle from workflow start. An existing-PR
    # checkout may replace every file under scripts with an older revision.
    $fixedScriptsRoot = Join-Path $controlRoot (Split-Path -Leaf $PSScriptRoot)
    [IO.Directory]::CreateDirectory($controlRoot) | Out-Null
    Copy-Item -LiteralPath $PSScriptRoot -Destination $fixedScriptsRoot -Recurse

    & (Join-Path $fixedScriptsRoot "codex-auto.ps1") @arguments
    $codexExitCode = $LASTEXITCODE
    if ($codexExitCode -ne 0) {
        exit $codexExitCode
    }

    if (Test-Path -LiteralPath (Join-Path $repoRoot "handoff.json") -PathType Leaf) {
        $publishArguments = @{ RepositoryRoot = $repoRoot }
        if ($PSBoundParameters.ContainsKey("PrNumber")) {
            $publishArguments.PrNumber = $PrNumber
        }
        & (Join-Path $fixedScriptsRoot "publish-from-handoff.ps1") @publishArguments
        exit $LASTEXITCODE
    }
}
finally {
    if (Test-Path -LiteralPath $controlRoot) {
        Remove-Item -LiteralPath $controlRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit 0
