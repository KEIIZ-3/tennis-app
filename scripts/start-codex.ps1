[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding

    $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
    if ($null -eq $ghCommand) {
        $ghCandidates = @(
            (Join-Path $env:ProgramFiles "GitHub CLI"),
            (Join-Path $env:LOCALAPPDATA "Programs\GitHub CLI")
        )
        foreach ($candidate in $ghCandidates) {
            if (Test-Path (Join-Path $candidate "gh.exe")) {
                $env:PATH = "$candidate;$env:PATH"
                $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
                break
            }
        }
    }
    if ($null -eq $ghCommand) {
        throw "GitHub CLI (gh) が見つかりません。GitHub CLIをインストールし、PATHを確認してください。"
    }

    $ghDirectory = Split-Path -Parent $ghCommand.Source
    if (($env:PATH -split ";") -notcontains $ghDirectory) {
        $env:PATH = "$ghDirectory;$env:PATH"
    }

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $repoRoot

    Invoke-CheckedCommand -Command { gh --version } `
        -FailureMessage "GitHub CLIのバージョン確認に失敗しました。"
    Invoke-CheckedCommand -Command { gh auth status } `
        -FailureMessage "GitHub CLIの認証確認に失敗しました。gh auth loginを実行してください。"

    Write-Host "現在のGit状態:"
    Invoke-CheckedCommand -Command { git status --short --branch } `
        -FailureMessage "git statusの取得に失敗しました。"

    $pendingChanges = git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "未コミット変更の確認に失敗しました。"
    }
    if ($pendingChanges) {
        throw "未コミット変更があります。安全のためmainへの切り替えとpullを行わず終了します。"
    }

    Invoke-CheckedCommand -Command { git switch main } `
        -FailureMessage "mainブランチへの切り替えに失敗しました。"
    Invoke-CheckedCommand -Command { git fetch origin main } `
        -FailureMessage "origin/mainの取得に失敗しました。ネットワークとGit認証を確認してください。"
    Invoke-CheckedCommand -Command { git pull --ff-only origin main } `
        -FailureMessage "mainをfast-forwardできませんでした。履歴の分岐状態を確認してください。"

    if ($null -eq (Get-Command codex -ErrorAction SilentlyContinue)) {
        throw "Codex CLIが見つかりません。インストールとPATHを確認してください。"
    }

    Write-Host "Codexを安全なワークスペース書き込みモードで起動します。"
    Invoke-CheckedCommand -Command {
        codex --cd $repoRoot --sandbox workspace-write --ask-for-approval on-request
    } -FailureMessage "Codexの起動または実行に失敗しました。"
}
catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
