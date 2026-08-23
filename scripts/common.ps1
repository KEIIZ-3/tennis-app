Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Set-WorkflowUtf8 {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $global:OutputEncoding = [Console]::OutputEncoding
}

function Get-RepositoryRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Add-GitHubCliToPath {
    $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
    if ($null -eq $ghCommand) {
        $candidates = @(
            (Join-Path $env:ProgramFiles "GitHub CLI"),
            (Join-Path $env:LOCALAPPDATA "Programs\GitHub CLI")
        )
        foreach ($candidate in $candidates) {
            if (Test-Path (Join-Path $candidate "gh.exe")) {
                $env:PATH = "$candidate;$env:PATH"
                $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
                break
            }
        }
    }
    if ($null -eq $ghCommand) {
        throw "GitHub CLI (gh) が見つかりません。インストールとPATHを確認してください。"
    }

    $ghDirectory = Split-Path -Parent $ghCommand.Source
    if (($env:PATH -split ";") -notcontains $ghDirectory) {
        $env:PATH = "$ghDirectory;$env:PATH"
    }
}

function Assert-CommandAvailable {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$InstallMessage
    )
    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw $InstallMessage
    }
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$FailureMessage,
        [switch]$Quiet
    )

    if ($Quiet) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $output = & $FilePath @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -ne 0) {
            $details = ($output | Out-String).Trim()
            throw "$FailureMessage $details".Trim()
        }
        return $output
    }

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Initialize-Workflow {
    param([switch]$RequireCodex)

    Set-WorkflowUtf8
    Add-GitHubCliToPath
    Assert-CommandAvailable -Name "git" -InstallMessage "Gitが見つかりません。インストールとPATHを確認してください。"
    if ($RequireCodex) {
        Assert-CommandAvailable -Name "codex" -InstallMessage "Codex CLIが見つかりません。インストールとPATHを確認してください。"
    }

    $repoRoot = Get-RepositoryRoot
    Set-Location -LiteralPath $repoRoot
    Invoke-NativeChecked -FilePath "gh" -Arguments @("--version") -FailureMessage "GitHub CLIの確認に失敗しました。" -Quiet | Out-Null
    Invoke-NativeChecked -FilePath "gh" -Arguments @("auth", "status") -FailureMessage "GitHub CLIの認証に失敗しました。gh auth loginを実行してください。" -Quiet | Out-Null
    return $repoRoot
}

function Assert-CleanWorktree {
    $status = Invoke-NativeChecked -FilePath "git" -Arguments @("status", "--porcelain") -FailureMessage "Git状態の確認に失敗しました。" -Quiet
    if ($status) {
        throw "未コミット変更があります。変更を整理してから再実行してください。"
    }
}

function Sync-MainBranch {
    Invoke-NativeChecked -FilePath "git" -Arguments @("switch", "main") -FailureMessage "mainへの切り替えに失敗しました。" -Quiet | Out-Null
    Invoke-NativeChecked -FilePath "git" -Arguments @("fetch", "origin", "main") -FailureMessage "origin/mainの取得に失敗しました。" -Quiet | Out-Null
    Invoke-NativeChecked -FilePath "git" -Arguments @("pull", "--ff-only", "origin", "main") -FailureMessage "mainをfast-forward同期できませんでした。" -Quiet | Out-Null
}

function Initialize-WorkflowArtifacts {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $reportPath = Join-Path $RepositoryRoot "report.md"
    $previousReportPath = Join-Path $RepositoryRoot "report.previous.md"
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        Move-Item -LiteralPath $reportPath -Destination $previousReportPath -Force
    }
    foreach ($name in @("handoff.json", ".pr-body.md", ".codex-prompt.tmp")) {
        $path = Join-Path $RepositoryRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Get-OpenPullRequestHead {
    param([Parameter(Mandatory = $true)][int]$Number)

    $repository = (Get-GitHubRepository).nameWithOwner
    $json = Invoke-NativeChecked -FilePath "gh" -Arguments @(
        "pr", "view", [string]$Number, "--repo", $repository, "--json",
        "number,url,state,headRefName,headRefOid,headRepository"
    ) -FailureMessage "PR #${Number}を取得できませんでした。" -Quiet
    $pullRequest = (($json -join [Environment]::NewLine) | ConvertFrom-Json)
    if ($pullRequest.state -ne "OPEN") {
        throw "PR #${Number}はOPENではありません: $($pullRequest.state)"
    }
    if ([string]::IsNullOrWhiteSpace($pullRequest.headRefName)) {
        throw "PR #${Number}のhead branchを取得できませんでした。"
    }
    if ($null -eq $pullRequest.headRepository -or
        $pullRequest.headRepository.nameWithOwner -ne $repository) {
        throw "PR #${Number}のhead repositoryが現在のrepositoryと一致しません。"
    }
    return $pullRequest
}

function Sync-PullRequestBranch {
    param([Parameter(Mandatory = $true)][int]$Number)

    $pullRequest = Get-OpenPullRequestHead -Number $Number
    $branch = $pullRequest.headRefName
    Invoke-NativeChecked -FilePath "git" -Arguments @("check-ref-format", "--branch", $branch) `
        -FailureMessage "PR head branch名が不正です。"
    Invoke-NativeChecked -FilePath "git" -Arguments @("fetch", "origin", $branch) `
        -FailureMessage "origin/$branch の取得に失敗しました。" -Quiet | Out-Null

    & git show-ref --verify --quiet "refs/heads/$branch"
    $localExists = $LASTEXITCODE -eq 0
    if ($localExists) {
        $upstream = @(& git for-each-ref --format="%(upstream)" "refs/heads/$branch")
        if ($LASTEXITCODE -ne 0 -or ($upstream -join "").Trim() -ne "refs/remotes/origin/$branch") {
            throw "ローカルbranch $branch のupstreamがorigin/$branchではありません。"
        }
        & git merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch"
        if ($LASTEXITCODE -ne 0) {
            throw "ローカルbranch $branch はorigin/$branchへfast-forward同期できません。"
        }
        Invoke-NativeChecked -FilePath "git" -Arguments @("switch", $branch) `
            -FailureMessage "$branch への切り替えに失敗しました。" -Quiet | Out-Null
        Invoke-NativeChecked -FilePath "git" -Arguments @("merge", "--ff-only", "origin/$branch") `
            -FailureMessage "$branch をfast-forward同期できませんでした。" -Quiet | Out-Null
    }
    else {
        Invoke-NativeChecked -FilePath "git" -Arguments @(
            "switch", "-c", $branch, "--track", "origin/$branch"
        ) -FailureMessage "origin/$branch からローカルbranchを作成できませんでした。" -Quiet | Out-Null
    }

    $localHead = (& git rev-parse HEAD).Trim()
    $remoteHead = (& git rev-parse "refs/remotes/origin/$branch").Trim()
    if ($LASTEXITCODE -ne 0 -or $localHead -ne $remoteHead -or $localHead -ne $pullRequest.headRefOid) {
        throw "同期後のheadがPR #${Number}のheadと一致しません。"
    }
    return $pullRequest
}

function Get-PromptTemplate {
    param([Parameter(Mandatory = $true)][string]$Name)
    $path = Join-Path (Join-Path $PSScriptRoot "prompts") $Name
    if (-not (Test-Path -LiteralPath $path)) {
        throw "プロンプトテンプレートが見つかりません: $Name"
    }
    return Get-Content -Raw -Encoding utf8 -LiteralPath $path
}

function Read-ImprovementRequest {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Tennis-App 開発"
    $form.StartPosition = "CenterScreen"
    $form.Size = New-Object System.Drawing.Size(760, 560)
    $form.MinimumSize = New-Object System.Drawing.Size(600, 420)

    $label = New-Object System.Windows.Forms.Label
    $label.Text = "改善内容を入力してください"
    $label.AutoSize = $true
    $label.Location = New-Object System.Drawing.Point(12, 12)

    $textBox = New-Object System.Windows.Forms.TextBox
    $textBox.Multiline = $true
    $textBox.AcceptsReturn = $true
    $textBox.AcceptsTab = $true
    $textBox.ScrollBars = "Both"
    $textBox.WordWrap = $false
    $textBox.Font = New-Object System.Drawing.Font("Meiryo UI", 10)
    $textBox.Anchor = "Top,Bottom,Left,Right"
    $textBox.Location = New-Object System.Drawing.Point(12, 40)
    $textBox.Size = New-Object System.Drawing.Size(718, 425)

    $okButton = New-Object System.Windows.Forms.Button
    $okButton.Text = "開始"
    $okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $okButton.Anchor = "Bottom,Right"
    $okButton.Location = New-Object System.Drawing.Point(574, 478)
    $okButton.Size = New-Object System.Drawing.Size(75, 30)

    $cancelButton = New-Object System.Windows.Forms.Button
    $cancelButton.Text = "キャンセル"
    $cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $cancelButton.Anchor = "Bottom,Right"
    $cancelButton.Location = New-Object System.Drawing.Point(655, 478)
    $cancelButton.Size = New-Object System.Drawing.Size(75, 30)

    $form.Controls.AddRange(@($label, $textBox, $okButton, $cancelButton))
    $form.AcceptButton = $okButton
    $form.CancelButton = $cancelButton
    $form.Add_Shown({ $textBox.Focus() })
    $result = $form.ShowDialog()
    $request = $textBox.Text
    $form.Dispose()

    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        throw "作業をキャンセルしました。"
    }
    if ([string]::IsNullOrWhiteSpace($request)) {
        throw "改善内容が入力されていません。"
    }
    return $request.Trim()
}

function Get-GitHubRepository {
    $json = Invoke-NativeChecked -FilePath "gh" -Arguments @("repo", "view", "--json", "nameWithOwner") -FailureMessage "GitHubリポジトリ情報を取得できませんでした。" -Quiet
    return (($json -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Get-PullRequest {
    param([Parameter(Mandatory = $true)][int]$Number, [Parameter(Mandatory = $true)][string]$Repository)
    $json = Invoke-NativeChecked -FilePath "gh" -Arguments @(
        "pr", "view", [string]$Number, "--repo", $Repository, "--json",
        "number,url,state,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup"
    ) -FailureMessage "PR #${Number}を取得できませんでした。" -Quiet
    return (($json -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Assert-LocalArtifactsIgnored {
    foreach ($name in @("report.md", "report.previous.md", "handoff.json", ".pr-body.md", ".codex-prompt.tmp")) {
        & git check-ignore --quiet -- $name
        if ($LASTEXITCODE -ne 0) {
            throw "$name が.gitignoreに登録されていません。"
        }
    }
}

function Write-WorkflowError {
    param([Parameter(Mandatory = $true)]$ErrorRecord)
    Write-Host "エラー: $($ErrorRecord.Exception.Message)" -ForegroundColor Red
}
