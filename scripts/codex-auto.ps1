[CmdletBinding()]
param([string]$Request)

. (Join-Path $PSScriptRoot "common.ps1")

$promptPath = $null
try {
    $repoRoot = Initialize-Workflow -RequireCodex
    Assert-CleanWorktree
    Assert-LocalArtifactsIgnored
    Sync-MainBranch

    if ([string]::IsNullOrWhiteSpace($Request)) {
        $Request = Read-ImprovementRequest
    }

    $template = Get-PromptTemplate -Name "prompt-dev.txt"
    if (-not $template.Contains("{{IMPROVEMENT}}")) {
        throw "prompt-dev.txtに{{IMPROVEMENT}}がありません。"
    }

    $prompt = $template.Replace("{{IMPROVEMENT}}", $Request.Trim())
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
