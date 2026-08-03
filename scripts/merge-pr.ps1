[CmdletBinding()]
param([int]$PrNumber)

. (Join-Path $PSScriptRoot "common.ps1")

try {
    $repoRoot = Initialize-Workflow
    Assert-CleanWorktree

    if ($PrNumber -le 0) {
        $rawNumber = Read-Host "PR番号を入力してください"
        if (-not [int]::TryParse($rawNumber, [ref]$PrNumber) -or $PrNumber -le 0) {
            throw "正しいPR番号を入力してください。"
        }
    }

    $repository = Get-GitHubRepository
    $owner, $repo = $repository.nameWithOwner -split "/", 2
    $pr = Get-PullRequest -Number $PrNumber -Repository $repository.nameWithOwner

    if ($pr.state -ne "OPEN") { throw "PR #$($pr.number) はOPENではありません。" }
    if ($pr.mergeable -ne "MERGEABLE" -or $pr.mergeStateStatus -ne "CLEAN") {
        throw "PR #$($pr.number) は現在マージできません。mergeable=$($pr.mergeable), status=$($pr.mergeStateStatus)"
    }
    if ($pr.reviewDecision -eq "CHANGES_REQUESTED") { throw "PR #$($pr.number) には変更要求が残っています。" }

    $failedChecks = @($pr.statusCheckRollup | Where-Object {
        ($_.conclusion -and $_.conclusion -notin @("SUCCESS", "NEUTRAL", "SKIPPED")) -or
        ($_.status -and $_.status -ne "COMPLETED") -or
        ($_.state -and $_.state -ne "SUCCESS")
    })
    if ($failedChecks.Count -gt 0) { throw "PR #$($pr.number) に未完了または失敗したステータスチェックがあります。" }

    $query = @'
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { isResolved isOutdated }
        pageInfo { hasNextPage }
      }
    }
  }
}
'@
    $threadJson = Invoke-NativeChecked -FilePath "gh" -Arguments @(
        "api", "graphql", "-f", "query=$query", "-F", "owner=$owner",
        "-F", "repo=$repo", "-F", "number=$($pr.number)"
    ) -FailureMessage "レビューコメントの確認に失敗しました。" -Quiet
    $threadData = (($threadJson -join [Environment]::NewLine) | ConvertFrom-Json)
    $threads = $threadData.data.repository.pullRequest.reviewThreads
    if ($threads.pageInfo.hasNextPage) { throw "レビューコメントが100件を超えています。GitHub上で確認してください。" }
    $unresolved = @($threads.nodes | Where-Object { -not $_.isResolved })
    if ($unresolved.Count -gt 0) { throw "PR #$($pr.number) に未解決レビューコメントが$($unresolved.Count)件あります。" }

    Write-Host "PR #$($pr.number) の検証に成功しました: $($pr.url)"
    if ($pr.isDraft) {
        Invoke-NativeChecked -FilePath "gh" -Arguments @("pr", "ready", [string]$pr.number, "--repo", $repository.nameWithOwner) -FailureMessage "PRをReady for reviewへ変更できませんでした。"
    }
    Invoke-NativeChecked -FilePath "gh" -Arguments @("pr", "merge", [string]$pr.number, "--repo", $repository.nameWithOwner, "--merge") -FailureMessage "PRのマージに失敗しました。"
    Invoke-NativeChecked -FilePath "git" -Arguments @("fetch", "origin", "main") -FailureMessage "マージ後のorigin/mainを取得できませんでした。"
    Invoke-NativeChecked -FilePath "git" -Arguments @("switch", "main") -FailureMessage "mainへ切り替えられませんでした。"
    Invoke-NativeChecked -FilePath "git" -Arguments @("pull", "--ff-only", "origin", "main") -FailureMessage "ローカルmainを同期できませんでした。"
    Assert-CleanWorktree

    $sha = Invoke-NativeChecked -FilePath "git" -Arguments @("rev-parse", "HEAD") -FailureMessage "mainの最新SHAを取得できませんでした。" -Quiet
    Write-Host "マージ完了: $($pr.url)"
    Write-Host "main: $(($sha | Select-Object -First 1).Trim())"
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
