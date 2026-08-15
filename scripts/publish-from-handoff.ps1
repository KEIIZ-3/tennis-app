[CmdletBinding()]
param([switch]$FunctionsOnly)

. (Join-Path $PSScriptRoot "common.ps1")

function Invoke-HandoffStaging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][object[]]$Files,
        [scriptblock]$BeforeStage
    )

    $repoRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    $forbiddenArtifacts = @("report.md", "handoff.json", ".pr-body.md", ".codex-prompt.tmp")
    $validatedFiles = New-Object System.Collections.Generic.List[string]
    $seenFiles = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $previousIndex = [Environment]::GetEnvironmentVariable("GIT_INDEX_FILE", "Process")

    # Never inherit an index belonging to the caller's repository. This is
    # especially important when tests invoke this function for a temporary
    # repository from a checked-out GitHub Actions workspace.
    Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    Push-Location -LiteralPath $repoRoot
    try {
        foreach ($file in $Files) {
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

            $trackedPaths = @(& git ls-files -- $relativePath)
            if ($LASTEXITCODE -ne 0) {
                throw "The tracked file state could not be inspected: $file"
            }
            $isTracked = $trackedPaths.Count -gt 0
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

        # A path-scoped git add can stage only one side of a rename. Inspect the
        # complete working tree through a disposable index so both rename paths
        # are visible without changing the repository's real index.
        $temporaryIndex = Join-Path ([IO.Path]::GetTempPath()) (
            "tennis-handoff-index-{0}" -f [Guid]::NewGuid().ToString("N")
        )
        try {
            [Environment]::SetEnvironmentVariable("GIT_INDEX_FILE", $temporaryIndex, "Process")
            Invoke-NativeChecked -FilePath "git" -Arguments @("read-tree", "HEAD") `
                -FailureMessage "The temporary Git index could not be initialized."
            Invoke-NativeChecked -FilePath "git" -Arguments @("add", "-A", "--", ".") `
                -FailureMessage "The working tree could not be inspected for renames."
            $workingTreeChanges = @(& git diff --cached --name-status --find-renames --)
            if ($LASTEXITCODE -ne 0) {
                throw "The working tree rename list could not be read."
            }
            foreach ($change in $workingTreeChanges) {
                $fields = @($change -split "`t")
                if ($fields.Count -eq 3 -and $fields[0].StartsWith("R")) {
                    $oldListed = $seenFiles.Contains($fields[1])
                    $newListed = $seenFiles.Contains($fields[2])
                    if ($oldListed -xor $newListed) {
                        throw "Both the old and new path of a rename must be listed in handoff files."
                    }
                }
            }
        }
        finally {
            Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $temporaryIndex -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryIndex -Force
            }
        }

        if ($BeforeStage) {
            & $BeforeStage
        }

        $addArguments = @("add", "-A", "--") + $validatedFiles.ToArray()
        Invoke-NativeChecked -FilePath "git" -Arguments $addArguments `
            -FailureMessage "The allowed files could not be staged."

        # --no-renames keeps the final allowlist comparison path-for-path after
        # the complete-working-tree rename validation above.
        $stagedFiles = @(& git diff --cached --name-only --no-renames --)
        if ($LASTEXITCODE -ne 0) {
            throw "The staged file list could not be read."
        }
        $expectedFiles = @($validatedFiles | Sort-Object)
        $actualFiles = @($stagedFiles | ForEach-Object { $_.Replace('\', '/') } | Sort-Object)
        if (($expectedFiles -join "`n") -cne ($actualFiles -join "`n")) {
            throw "The staged files do not match the handoff allowlist."
        }

        return $validatedFiles.ToArray()
    }
    finally {
        Pop-Location
        if ($null -ne $previousIndex) {
            [Environment]::SetEnvironmentVariable("GIT_INDEX_FILE", $previousIndex, "Process")
        }
        else {
            Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
        }
    }
}

if ($FunctionsOnly) {
    return
}

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

    # Determine whether this handoff belongs to an already existing PR.
    # An existing PR must be updated instead of creating a duplicate PR.
    $existingPrJson = & gh pr list `
        --head $branch `
        --state open `
        --json number,url,isDraft,headRefName `
        --limit 1

    if ($LASTEXITCODE -ne 0) {
        throw "Existing pull requests for the handoff branch could not be inspected."
    }

    try {
        $existingPrList = @($existingPrJson | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "The existing pull request information returned by GitHub CLI was not valid JSON."
    }

    $existingPr = $null
    if ($existingPrList.Count -gt 0) {
        $existingPr = $existingPrList[0]
    }

    [void](Invoke-HandoffStaging -RepositoryRoot $repoRoot -Files @($handoff.files) -BeforeStage {
        # The working tree already contains the Codex changes, so switch without
        # discarding them. Reuse an existing local branch when present.
        & git show-ref --verify --quiet "refs/heads/$branch"
        $localBranchExists = $LASTEXITCODE -eq 0

        if ($localBranchExists) {
            Invoke-NativeChecked -FilePath "git" -Arguments @("switch", $branch) `
                -FailureMessage "The existing work branch could not be checked out."
        }
        else {
            # If the branch exists only on origin, create a local tracking
            # branch from it. Otherwise this is a genuinely new branch.
            & git ls-remote --exit-code --heads origin $branch *> $null
            $remoteBranchExists = $LASTEXITCODE -eq 0

            if ($remoteBranchExists) {
                Invoke-NativeChecked -FilePath "git" -Arguments @("fetch", "origin", $branch) `
                    -FailureMessage "The existing remote work branch could not be fetched."
                Invoke-NativeChecked -FilePath "git" -Arguments @(
                    "switch", "-c", $branch, "--track", "origin/$branch"
                ) -FailureMessage "The existing remote work branch could not be checked out."
            }
            else {
                Invoke-NativeChecked -FilePath "git" -Arguments @("switch", "-c", $branch) `
                    -FailureMessage "The work branch could not be created."
            }
        }
    })

    Invoke-NativeChecked -FilePath "git" -Arguments @(
        "commit", "-m", $handoff.commit_message.Trim()
    ) -FailureMessage "The commit failed."

    Invoke-NativeChecked -FilePath "git" -Arguments @(
        "push", "-u", "origin", $branch
    ) -FailureMessage "The push failed."

    $commitSha = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commitSha)) {
        throw "The latest commit SHA could not be read."
    }

    [IO.File]::WriteAllText(
        $prBodyPath,
        $handoff.pr_body,
        [Text.UTF8Encoding]::new($false)
    )

    if ($null -ne $existingPr) {
        $prUrl = [string]$existingPr.url

        if ([string]::IsNullOrWhiteSpace($prUrl)) {
            throw "The existing pull request URL was not returned."
        }

        # Keep the existing PR, but refresh its title and body from the latest
        # handoff so the PR accurately describes the new head commit.
        Invoke-NativeChecked -FilePath "gh" -Arguments @(
            "pr", "edit", $prUrl,
            "--title", $handoff.pr_title.Trim(),
            "--body-file", $prBodyPath
        ) -FailureMessage "The existing PR metadata could not be updated."

        if ($existingPr.isDraft) {
            Invoke-NativeChecked -FilePath "gh" -Arguments @(
                "pr", "ready", $prUrl
            ) -FailureMessage "The existing Draft PR could not be marked ready for review."
        }
    }
    else {
        $prOutput = & gh pr create `
            --draft `
            --title $handoff.pr_title.Trim() `
            --body-file $prBodyPath

        if ($LASTEXITCODE -ne 0) {
            throw "Draft PR creation failed."
        }

        $prUrl = ($prOutput | Select-Object -Last 1).Trim()
        if ([string]::IsNullOrWhiteSpace($prUrl)) {
            throw "The Draft PR URL was not returned."
        }

        Invoke-NativeChecked -FilePath "gh" -Arguments @(
            "pr", "ready", $prUrl
        ) -FailureMessage "The Draft PR could not be marked ready for review."
    }

    # Whether this was a new PR or an update to an existing PR, enable
    # auto-merge against the exact commit that was just pushed.
    Invoke-NativeChecked -FilePath "gh" -Arguments @(
        "pr", "merge", $prUrl,
        "--auto",
        "--squash",
        "--match-head-commit", $commitSha
    ) -FailureMessage "Auto-merge could not be enabled for the PR."

    $prNumber = [IO.Path]::GetFileName($prUrl.TrimEnd('/'))
    $reportPath = Join-Path $repoRoot "report.md"

    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        $report = Get-Content -Raw -Encoding utf8 -LiteralPath $reportPath
        $report = $report.Replace("{{PR_NUMBER}}", $prNumber)
        $report = $report.Replace("{{PR_URL}}", $prUrl)
        $report = $report.Replace("{{COMMIT_SHA}}", $commitSha)
        [IO.File]::WriteAllText(
            $reportPath,
            $report,
            [Text.UTF8Encoding]::new($false)
        )
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
