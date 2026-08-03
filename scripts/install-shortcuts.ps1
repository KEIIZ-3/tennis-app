[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Set-CodexUtf8Environment
    $repoRoot = Get-TennisAppRoot
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shell = New-Object -ComObject WScript.Shell
    $powerShellPath = (Get-Command powershell.exe).Source
    $shortcuts = @(
        @{ Name = "Tennis-App 開発.lnk"; Script = Join-Path $PSScriptRoot "start-codex.ps1" },
        @{ Name = "PRマージ.lnk"; Script = Join-Path $PSScriptRoot "merge-pr.ps1" }
    )
    $quote = [char]34

    foreach ($definition in $shortcuts) {
        $shortcutPath = Join-Path $desktop $definition.Name
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powerShellPath
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File $quote$($definition.Script)$quote"
        $shortcut.WorkingDirectory = $repoRoot
        $shortcut.IconLocation = "$powerShellPath,0"
        $shortcut.Save()
        Write-Host "作成しました: $shortcutPath"
    }
}
catch {
    Write-WorkflowError -ErrorRecord $_
    exit 1
}
