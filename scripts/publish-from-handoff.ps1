[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")

$handoffPath = $null
$prBodyPath = $null
$published = $false

try {
    Set-WorkflowUtf8
    Add-GitHubCliToPath
    Assert-CommandAvailable -Name "git" -InstallMessage "Git was not found. Check the installation and PATH."
    $repoRoot = Get-RepositoryRoot
    Set-Location -LiteralPath $repoRoot
    Invoke-NativeChecked -FilePath "gh" -Arguments @("--version") `
        -FailureMessage "GitHub CLI validation failed."
    Invoke-NativeChecked -FilePath "gh" -Arguments @("auth", "status") `
        -FailureMessage "GitHub CLI authentication failed. Run gh auth login."
    $handoffPath = Join-Path $repoRoot "handoff.json"
    $prBodyPath = Join-Path $repoRoot ".pr-body.md"

    if (-not (Test-Path -LiteralPath $handoffPath -PathType Leaf)) {
        throw "handoff.json was not found."
    }

    try {
        $handoff = Get-Content -Raw -Encoding utf8 -LiteralPath $handoffPath |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "handoff.json is not valid JSON: $($_.Exception.Message)"
    }

    foreach ($name in @("branch", "files", "commit_message", "pr_title", "pr_body")) {
        if ($handoff.PSObject.Properties.Name -notcontains $name) {
            throw "handoff.json is missing a required property: $name"
        }
    }

    foreach ($name in @("branch", "commit_message", "pr_title", "pr_body")) {
        if ($handoff.$name -isnot [string] -or [string]::IsNullOrWhiteSpace($handoff.$name)) {
            throw "handoff.json property $name must be a non-empty string."
        }
    }

    if ($handoff.files -isnot [System.Array] -or $handoff.files.Count -eq 0) {
        throw "handoff.json property files must be a non-empty array."
    }

    $branch = $handoff.branch.Trim()
    if ($branch -notmatch '^agent/[a-z0-9]+(?:-[a-z0-9]+)*$') {
        throw "branch must start with agent/ and use lowercase kebab-case."
    }
    Invoke-NativeChecked -FilePath "git" -Arguments @("check-ref-format", "--branch", $branch) `
        -FailureMessage "The branch name is not valid for Git."

    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $forbiddenArtifacts = @("report.md", "handoff.json", ".pr-body.md", ".codex-prompt.tmp")
    $validatedFiles = New-Object System.Collections.Generic.List[string]
    $seenFiles = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

    foreach ($file in $handoff.files) {
        if ($file -isnot [string] -or [string]::IsNullOrWhiteSpace($file)) {
            throw "Every files entry must be a non-empty string."
        }

        $relativePath = $file.Trim().Replace('\', '/')
        if ([IO.Path]::IsPathRooted($relativePath)) {
            throw "Absolute paths are not allowed: $file"
        }
        if (($relativePath -split '/') -contains "..") {
            throw "Parent path segments are not allowed: $file"
        }
        if ($relativePath.StartsWith(":") -or $relativePath.IndexOfAny(@('*', '?', '[')) -ge 0) {
            throw "Git pathspec syntax is not allowed: $file"
        }
        if ($forbiddenArtifacts -contains $relativePath) {
            throw "Local workflow artifacts cannot be published: $file"
        }

        $candidatePath = [IO.Path]::GetFullPath((Join-Path $repoRoot $relativePath))
        if (-not $candidatePath.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Files outside the repository are not allowed: $file"
        }
        if (Test-Path -LiteralPath $candidatePath -PathType Container) {
            throw "Directory entries are not allowed: $file"
        }
        if (-not $seenFiles.Add($relativePath)) {
            throw "Duplicate files entry: $file"
        }

        & git ls-files --error-unmatch -- $relativePath 2>$null
        $isTracked = $LASTEXITCODE -eq 0
        if ($isTracked) {
            & git diff --quiet -- $relativePath
            if ($LASTEXITCODE -eq 0) {
                throw "The specified tracked file has no working tree change: $file"
            }
            if ($LASTEXITCODE -ne 1) {
                throw "The working tree change could not be inspected: $file"
            }
        }
        elseif (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
            throw "The specified path is neither a file nor a tracked deletion: $file"
        }
        $validatedFiles.Add($relativePath)
    }

    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "The index already contains staged changes. Clear them before publishing."
    }

    Invoke-NativeChecked -FilePath "git" -Arguments @("switch", "-c", $branch) `
        -FailureMessage "The work branch could not be created."

    $addArguments = @("add", "-A", "--") + $validatedFiles.ToArray()
    Invoke-NativeChecked -FilePath "git" -Arguments $addArguments `
        -FailureMessage "The allowed files could not be staged."

    # --no-renames exposes both sides of a rename so the allowlist must contain
    # the old and new path. This prevents an unlisted rename source from being staged.
    $stagedFiles = @(& git diff --cached --name-only --no-renames --)
    if ($LASTEXITCODE -ne 0) {
        throw "The staged file list could not be read."
    }
    $expectedFiles = @($validatedFiles | Sort-Object)
    $actualFiles = @($stagedFiles | ForEach-Object { $_.Replace('\', '/') } | Sort-Object)
    if (($expectedFiles -join "`n") -cne ($actualFiles -join "`n")) {
        throw "The staged files do not match the handoff allowlist."
    }

    Invoke-NativeChecked -FilePath "git" -Arguments @("commit", "-m", $handoff.commit_message.Trim()) `
        -FailureMessage "The commit failed."
    Invoke-NativeChecked -FilePath "git" -Arguments @("push", "-u", "origin", $branch) `
        -FailureMessage "The push failed."

    $commitSha = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commitSha)) {
        throw "The latest commit SHA could not be read."
    }

    [IO.File]::WriteAllText(
        $prBodyPath,
        $handoff.pr_body,
        [Text.UTF8Encoding]::new($false)
    )
    $prOutput = & gh pr create --draft --title $handoff.pr_title.Trim() --body-file $prBodyPath
    if ($LASTEXITCODE -ne 0) {
        throw "Draft PR creation failed."
    }

    $prUrl = ($prOutput | Select-Object -Last 1).Trim()
    if ([string]::IsNullOrWhiteSpace($prUrl)) {
        throw "The Draft PR URL was not returned."
    }

    Invoke-NativeChecked -FilePath "gh" -Arguments @("pr", "ready", $prUrl) `
        -FailureMessage "The Draft PR could not be marked ready for review."
    Invoke-NativeChecked -FilePath "gh" -Arguments @(
        "pr", "merge", $prUrl, "--auto", "--squash", "--match-head-commit", $commitSha
    ) -FailureMessage "Auto-merge could not be enabled for the PR."

    $prNumber = [IO.Path]::GetFileName($prUrl.TrimEnd('/'))
    $reportPath = Join-Path $repoRoot "report.md"
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        $report = Get-Content -Raw -Encoding utf8 -LiteralPath $reportPath
        $report = $report.Replace("{{PR_NUMBER}}", $prNumber)
        $report = $report.Replace("{{PR_URL}}", $prUrl)
        $report = $report.Replace("{{COMMIT_SHA}}", $commitSha)
        [IO.File]::WriteAllText($reportPath, $report, [Text.UTF8Encoding]::new($false))
    }

    Write-Host "Auto-merge enabled: $prUrl" -ForegroundColor Green
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        Write-Host ""
        Get-Content -Raw -Encoding utf8 -LiteralPath $reportPath
    }
    $published = $true
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
finally {
    if ($published -and $prBodyPath -and (Test-Path -LiteralPath $prBodyPath)) {
        Remove-Item -LiteralPath $prBodyPath -Force
    }
    if ($published -and $handoffPath -and (Test-Path -LiteralPath $handoffPath)) {
        Remove-Item -LiteralPath $handoffPath -Force
    }
}
