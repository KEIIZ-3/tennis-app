[CmdletBinding()]
param(
    [ValidateSet("dev", "hotfix", "refactor", "review")]
    [string]$TaskType = "dev",
    [string]$Request
)

. (Join-Path $PSScriptRoot "codex-common.ps1")

try {
    $repoRoot = Initialize-CodexWorkflow -RequireCodex
    Assert-CleanWorktree
    Sync-MainBranch

    if ([string]::IsNullOrWhiteSpace($Request)) {
        $Request = Read-ImprovementRequest
    }

    $basePrompt = Get-PromptTemplate -Name "prompt-base.txt"
    $taskPrompt = Get-PromptTemplate -Name "prompt-$TaskType.txt"
    $prompt = @"
$basePrompt

$taskPrompt

## 今回の改善要求

$($Request.Trim())
"@

    Write-Host "Codexへ改善要求を送信しました。完了までこの画面を閉じないでください。"
    $prompt | & codex exec -C $repoRoot --sandbox workspace-write -c 'approval_policy="on-request"' -
    if ($LASTEXITCODE -ne 0) {
        throw "Codexの自動作業が失敗しました。表示されたログを確認してください。"
    }
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
