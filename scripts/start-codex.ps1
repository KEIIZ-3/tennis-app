[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "codex-common.ps1")

try {
    $repoRoot = Initialize-CodexWorkflow -RequireCodex
    Assert-CleanWorktree
    Sync-MainBranch
    Write-Host "Codexを安全なワークスペース書き込みモードで起動します。"
    & codex --cd $repoRoot --sandbox workspace-write --ask-for-approval on-request
    if ($LASTEXITCODE -ne 0) {
        throw "Codexの起動または実行に失敗しました。"
    }
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
