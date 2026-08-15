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

            if (
                $relativePath.StartsWith(":") -or
                $relativePath.IndexOfAny(@('*', '?', '[')) -ge 0
            ) {
                throw "Git pathspec syntax is not allowed: $file"
            }

            if ($forbiddenArtifacts -contains $relativePath) {
                throw "Local workflow artifacts cannot be published: $file"
            }

            $candidatePath = [IO.Path]::GetFullPath(
                (Join-Path $repoRoot $relativePath)
            )

            if (
                -not $candidatePath.StartsWith(
                    $repoPrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )
            ) {
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

        $temporaryIndex = Join-Path ([IO.Path]::GetTempPath()) (
            "tennis-handoff-index-{0}" -f [Guid]::NewGuid().ToString("N")
        )

        try {
            [Environment]::SetEnvironmentVariable(
                "GIT_INDEX_FILE",
                $temporaryIndex,
                "Process"
            )

            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @("read-tree", "HEAD") `
                -FailureMessage "The temporary Git index could not be initialized."

            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @("add", "-A", "--", ".") `
                -FailureMessage "The working tree could not be inspected for renames."

            $workingTreeChanges = @(
                & git diff --cached --name-status --find-renames --
            )

            if ($LASTEXITCODE -ne 0) {
                throw "The working tree rename list could not be read."
            }

            foreach ($change in $workingTreeChanges) {
                $fields = @($change -split "`t")

                if (
                    $fields.Count -eq 3 -and
                    $fields[0].StartsWith("R")
                ) {
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

        Invoke-NativeChecked `
            -FilePath "git" `
            -Arguments $addArguments `
            -FailureMessage "The allowed files could not be staged."

        $stagedFiles = @(
            & git diff --cached --name-only --no-renames --
        )

        if ($LASTEXITCODE -ne 0) {
            throw "The staged file list could not be read."
        }

        $expectedFiles = @(
            $validatedFiles |
                Sort-Object
        )

        $actualFiles = @(
            $stagedFiles |
                ForEach-Object { $_.Replace('\', '/') } |
                Sort-Object
        )

        if (
            ($expectedFiles -join "`n") -cne
            ($actualFiles -join "`n")
        ) {
            throw "The staged files do not match the handoff allowlist."
        }

        return $validatedFiles.ToArray()
    }
    finally {
        Pop-Location

        if ($null -ne $previousIndex) {
            [Environment]::SetEnvironmentVariable(
                "GIT_INDEX_FILE",
                $previousIndex,
                "Process"
            )
        }
        else {
            Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-SafeBranchSwitchWithHandoffFiles {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string]$Branch,
        [Parameter(Mandatory = $true)][object[]]$Files
    )

    $repoRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $currentBranch = (& git branch --show-current).Trim()

    if ($LASTEXITCODE -ne 0) {
        throw "The current Git branch could not be determined."
    }

    if ($currentBranch -eq $Branch) {
        return
    }

    $backupRoot = Join-Path ([IO.Path]::GetTempPath()) (
        "tennis-handoff-files-{0}" -f [Guid]::NewGuid().ToString("N")
    )

    $existingFiles = New-Object System.Collections.Generic.List[string]
    $missingFiles = New-Object System.Collections.Generic.List[string]

    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

    try {
        foreach ($file in $Files) {
            $relativePath = ([string]$file).Trim().Replace('\', '/')
            $sourcePath = Join-Path $repoRoot $relativePath

            if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
                $backupPath = Join-Path $backupRoot $relativePath
                $backupDirectory = Split-Path -Parent $backupPath

                if (-not (Test-Path -LiteralPath $backupDirectory)) {
                    New-Item `
                        -ItemType Directory `
                        -Path $backupDirectory `
                        -Force |
                        Out-Null
                }

                Copy-Item `
                    -LiteralPath $sourcePath `
                    -Destination $backupPath `
                    -Force

                $existingFiles.Add($relativePath)
            }
            else {
                $missingFiles.Add($relativePath)
            }
        }

        #
        # Temporarily restore only the handoff files to the current HEAD.
        # This leaves unrelated working-tree changes untouched.
        #
        foreach ($file in $Files) {
            $relativePath = ([string]$file).Trim().Replace('\', '/')

            $trackedPaths = @(
                & git ls-files -- $relativePath
            )

            if ($LASTEXITCODE -ne 0) {
                throw "The tracked state could not be inspected before switching branch: $relativePath"
            }

            if ($trackedPaths.Count -gt 0) {
                Invoke-NativeChecked `
                    -FilePath "git" `
                    -Arguments @(
                        "restore",
                        "--worktree",
                        "--source=HEAD",
                        "--",
                        $relativePath
                    ) `
                    -FailureMessage "The handoff file could not be temporarily restored: $relativePath"
            }
            else {
                $candidatePath = Join-Path $repoRoot $relativePath

                if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
                    Remove-Item -LiteralPath $candidatePath -Force
                }
            }
        }

        #
        # Determine whether the branch exists locally and/or remotely.
        #
        & git show-ref --verify --quiet "refs/heads/$Branch"
        $localBranchExists = $LASTEXITCODE -eq 0

        & git ls-remote --exit-code --heads origin $Branch *> $null
        $remoteBranchExists = $LASTEXITCODE -eq 0

        if ($remoteBranchExists) {
            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @("fetch", "origin", $Branch) `
                -FailureMessage "The existing remote work branch could not be fetched."
        }

        if ($localBranchExists) {
            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @("switch", $Branch) `
                -FailureMessage "The existing work branch could not be checked out."

            if ($remoteBranchExists) {
                $comparison = (
                    & git rev-list `
                        --left-right `
                        --count `
                        "$Branch...origin/$Branch"
                ).Trim()

                if ($LASTEXITCODE -ne 0) {
                    throw "The local and remote work branches could not be compared."
                }

                $parts = @(
                    $comparison -split '\s+'
                )

                if ($parts.Count -ne 2) {
                    throw "The local and remote work branch comparison returned an unexpected result."
                }

                $localOnly = [int]$parts[0]
                $remoteOnly = [int]$parts[1]

                if ($localOnly -gt 0 -and $remoteOnly -gt 0) {
                    throw "The local and remote work branches have diverged. Automatic publishing was stopped."
                }

                if ($localOnly -gt 0 -and $remoteOnly -eq 0) {
                    throw "The local work branch contains commits that are not on origin. Automatic publishing was stopped."
                }

                if ($localOnly -eq 0 -and $remoteOnly -gt 0) {
                    Invoke-NativeChecked `
                        -FilePath "git" `
                        -Arguments @(
                            "merge",
                            "--ff-only",
                            "origin/$Branch"
                        ) `
                        -FailureMessage "The local work branch could not be fast-forwarded to origin."
                }
            }
        }
        elseif ($remoteBranchExists) {
            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @(
                    "switch",
                    "-c",
                    $Branch,
                    "--track",
                    "origin/$Branch"
                ) `
                -FailureMessage "The existing remote work branch could not be checked out."
        }
        else {
            Invoke-NativeChecked `
                -FilePath "git" `
                -Arguments @(
                    "switch",
                    "-c",
                    $Branch
                ) `
                -FailureMessage "The work branch could not be created."
        }

        #
        # Restore exactly the Codex-produced handoff files onto the target
        # branch. No synthetic stash commit and no reset is used.
        #
        foreach ($relativePath in $existingFiles) {
            $backupPath = Join-Path $backupRoot $relativePath
            $destinationPath = Join-Path $repoRoot $relativePath
            $destinationDirectory = Split-Path -Parent $destinationPath

            if (-not (Test-Path -LiteralPath $destinationDirectory)) {
                New-Item `
                    -ItemType Directory `
                    -Path $destinationDirectory `
                    -Force |
                    Out-Null
            }

            Copy-Item `
                -LiteralPath $backupPath `
                -Destination $destinationPath `
                -Force
        }

        foreach ($relativePath in $missingFiles) {
            $destinationPath = Join-Path $repoRoot $relativePath

            if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
                Remove-Item -LiteralPath $destinationPath -Force
            }
        }
    }
    catch {
        #
        # If anything fails after the temporary cleanup, restore the handoff
        # files wherever we currently are before returning the error.
        #
        foreach ($relativePath in $existingFiles) {
            $backupPath = Join-Path $backupRoot $relativePath
            $destinationPath = Join-Path $repoRoot $relativePath

            if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
                $destinationDirectory = Split-Path -Parent $destinationPath

                if (-not (Test-Path -LiteralPath $destinationDirectory)) {
                    New-Item `
                        -ItemType Directory `
                        -Path $destinationDirectory `
                        -Force |
                        Out-Null
                }

                Copy-Item `
                    -LiteralPath $backupPath `
                    -Destination $destinationPath `
                    -Force
            }
        }

        foreach ($relativePath in $missingFiles) {
            $destinationPath = Join-Path $repoRoot $relativePath

            if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
                Remove-Item -LiteralPath $destinationPath -Force
            }
        }

        throw
    }
    finally {
        if (Test-Path -LiteralPath $backupRoot) {
            Remove-Item `
                -LiteralPath $backupRoot `
                -Recurse `
                -Force
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

    Assert-CommandAvailable `
        -Name "git" `
        -InstallMessage "Git was not found. Check the installation and PATH."

    $repoRoot = Get-RepositoryRoot
    Set-Location -LiteralPath $repoRoot

    Invoke-NativeChecked `
        -FilePath "gh" `
        -Arguments @("--version") `
        -FailureMessage "GitHub CLI validation failed."

    Invoke-NativeChecked `
        -FilePath "gh" `
        -Arguments @("auth", "status") `
        -FailureMessage "GitHub CLI authentication failed. Run gh auth login."

    $handoffPath = Join-Path $repoRoot "handoff.json"
    $prBodyPath = Join-Path $repoRoot ".pr-body.md"

    if (-not (Test-Path -LiteralPath $handoffPath -PathType Leaf)) {
        throw "handoff.json was not found."
    }

    try {
        $handoff = Get-Content `
            -Raw `
            -Encoding utf8 `
            -LiteralPath $handoffPath |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "handoff.json is not valid JSON: $($_.Exception.Message)"
    }

    foreach (
        $name in @(
            "branch",
            "files",
            "commit_message",
            "pr_title",
            "pr_body"
        )
    ) {
        if ($handoff.PSObject.Properties.Name -notcontains $name) {
            throw "handoff.json is missing a required property: $name"
        }
    }

    foreach (
        $name in @(
            "branch",
            "commit_message",
            "pr_title",
            "pr_body"
        )
    ) {
        if (
            $handoff.$name -isnot [string] -or
            [string]::IsNullOrWhiteSpace($handoff.$name)
        ) {
            throw "handoff.json property $name must be a non-empty string."
        }
    }

    if (
        $handoff.files -isnot [System.Array] -or
        $handoff.files.Count -eq 0
    ) {
        throw "handoff.json property files must be a non-empty array."
    }

    $branch = $handoff.branch.Trim()

    if (
        $branch -notmatch
        '^agent/[a-z0-9]+(?:-[a-z0-9]+)*$'
    ) {
        throw "branch must start with agent/ and use lowercase kebab-case."
    }

    Invoke-NativeChecked `
        -FilePath "git" `
        -Arguments @(
            "check-ref-format",
            "--branch",
            $branch
        ) `
        -FailureMessage "The branch name is not valid for Git."

    #
    # Find an already-open PR for this branch before changing branches.
    #
    $existingPrJson = & gh pr list `
        --head $branch `
        --state open `
        --json number,url,isDraft,headRefName `
        --limit 1

    if ($LASTEXITCODE -ne 0) {
        throw "Existing pull requests for the handoff branch could not be inspected."
    }

    try {
        $existingPrList = @(
            $existingPrJson |
                ConvertFrom-Json -ErrorAction Stop
        )
    }
    catch {
        throw "The existing pull request information returned by GitHub CLI was not valid JSON."
    }

    $existingPr = $null

    if ($existingPrList.Count -gt 0) {
        $existingPr = $existingPrList[0]
    }

    [void](
        Invoke-HandoffStaging `
            -RepositoryRoot $repoRoot `
            -Files @($handoff.files) `
            -BeforeStage {
                Invoke-SafeBranchSwitchWithHandoffFiles `
                    -RepositoryRoot $repoRoot `
                    -Branch $branch `
                    -Files @($handoff.files)
            }
    )

    Invoke-NativeChecked `
        -FilePath "git" `
        -Arguments @(
            "commit",
            "-m",
            $handoff.commit_message.Trim()
        ) `
        -FailureMessage "The commit failed."

    Invoke-NativeChecked `
        -FilePath "git" `
        -Arguments @(
            "push",
            "-u",
            "origin",
            $branch
        ) `
        -FailureMessage "The push failed."

    $commitSha = (
        & git rev-parse HEAD
    ).Trim()

    if (
        $LASTEXITCODE -ne 0 -or
        [string]::IsNullOrWhiteSpace($commitSha)
    ) {
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

        Invoke-NativeChecked `
            -FilePath "gh" `
            -Arguments @(
                "pr",
                "edit",
                $prUrl,
                "--title",
                $handoff.pr_title.Trim(),
                "--body-file",
                $prBodyPath
            ) `
            -FailureMessage "The existing PR metadata could not be updated."

        if ($existingPr.isDraft) {
            Invoke-NativeChecked `
                -FilePath "gh" `
                -Arguments @(
                    "pr",
                    "ready",
                    $prUrl
                ) `
                -FailureMessage "The existing Draft PR could not be marked ready for review."
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

        $prUrl = (
            $prOutput |
                Select-Object -Last 1
        ).Trim()

        if ([string]::IsNullOrWhiteSpace($prUrl)) {
            throw "The Draft PR URL was not returned."
        }

        Invoke-NativeChecked `
            -FilePath "gh" `
            -Arguments @(
                "pr",
                "ready",
                $prUrl
            ) `
            -FailureMessage "The Draft PR could not be marked ready for review."
    }

    Invoke-NativeChecked `
        -FilePath "gh" `
        -Arguments @(
            "pr",
            "merge",
            $prUrl,
            "--auto",
            "--squash",
            "--match-head-commit",
            $commitSha
        ) `
        -FailureMessage "Auto-merge could not be enabled for the PR."

    $prNumber = [IO.Path]::GetFileName(
        $prUrl.TrimEnd('/')
    )

    $reportPath = Join-Path $repoRoot "report.md"

    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        $report = Get-Content `
            -Raw `
            -Encoding utf8 `
            -LiteralPath $reportPath

        $report = $report.Replace(
            "{{PR_NUMBER}}",
            $prNumber
        )

        $report = $report.Replace(
            "{{PR_URL}}",
            $prUrl
        )

        $report = $report.Replace(
            "{{COMMIT_SHA}}",
            $commitSha
        )

        [IO.File]::WriteAllText(
            $reportPath,
            $report,
            [Text.UTF8Encoding]::new($false)
        )
    }

    Write-Host `
        "Auto-merge enabled: $prUrl" `
        -ForegroundColor Green

    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        Write-Host ""
        Get-Content `
            -Raw `
            -Encoding utf8 `
            -LiteralPath $reportPath
    }

    $published = $true
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
finally {
    if (
        $published -and
        $prBodyPath -and
        (Test-Path -LiteralPath $prBodyPath)
    ) {
        Remove-Item `
            -LiteralPath $prBodyPath `
            -Force
    }

    if (
        $published -and
        $handoffPath -and
        (Test-Path -LiteralPath $handoffPath)
    ) {
        Remove-Item `
            -LiteralPath $handoffPath `
            -Force
    }
}
