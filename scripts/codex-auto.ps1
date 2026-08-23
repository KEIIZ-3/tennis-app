[CmdletBinding()]
param(
    [string]$Request,
    [ValidateRange(1, [int]::MaxValue)][int]$PrNumber
)

. (Join-Path $PSScriptRoot "common.ps1")

$promptPath = $null
try {
    $repoRoot = Initialize-Workflow -RequireCodex
    Assert-CleanWorktree
    Assert-LocalArtifactsIgnored
    Initialize-WorkflowArtifacts -RepositoryRoot $repoRoot
    if ($PSBoundParameters.ContainsKey("PrNumber")) {
        $pullRequest = Sync-PullRequestBranch -Number $PrNumber
    }
    else {
        Sync-MainBranch
    }

    if ([string]::IsNullOrWhiteSpace($Request)) {
        $Request = Read-ImprovementRequest
    }

    $template = Get-PromptTemplate -Name "prompt-dev.txt"
    if (-not $template.Contains("{{IMPROVEMENT}}")) {
        throw "prompt-dev.txtに{{IMPROVEMENT}}がありません。"
    }

    $prompt = $template.Replace("{{IMPROVEMENT}}", $Request.Trim())
    if ($PSBoundParameters.ContainsKey("PrNumber")) {
        $prompt += @"

## 既存PR継続モード

親PowerShellが検証・同期した既存PR #$PrNumber のheadで作業しています。
handoff.jsonには `"publish_mode": "existing_pr"`、`"pr_number": $PrNumber` を追加し、branchには現在のPR head branch `"$($pullRequest.headRefName)"` を記載してください。
新規PR用branchを考案しないでください。
"@
    }
    $promptPath = Join-Path $repoRoot ".codex-prompt.tmp"
    [System.IO.File]::WriteAllText(
        $promptPath,
        $prompt,
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host "Codexへ改善要求を送信しました。完了までこの画面を閉じないでください。"
    Get-Content -Raw -Encoding utf8 -LiteralPath $promptPath |
        & codex exec -C $repoRoot --sandbox workspace-write -c 'approval_policy="on-request"' -
    if ($LASTEXITCODE -ne 0) {
        throw "Codexの自動作業が失敗しました。表示されたログを確認してください。"
    }
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
finally {
    if ($promptPath -and (Test-Path -LiteralPath $promptPath)) {
        Remove-Item -LiteralPath $promptPath -Force
    }
}
