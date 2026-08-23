import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "PowerShell and Git are required")
class ControlBundleIntegrationTests(unittest.TestCase):
    def run_command(self, arguments, cwd, env=None):
        result = subprocess.run(
            arguments,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, repository, *arguments):
        return self.run_command(["git", *arguments], repository)

    def write(self, repository, relative_path, content):
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")

    def make_repository(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="tennis-control-bundle-", dir=ROOT
        )
        repository = Path(temporary.name)
        self.git(repository, "init")
        self.git(repository, "config", "user.name", "Control Bundle Test")
        self.git(repository, "config", "user.email", "control@example.invalid")

        self.write(repository, "scripts/start-codex.ps1", "# old entry\n")
        self.write(repository, "scripts/codex-auto.ps1", "# old runner\n")
        self.write(repository, "scripts/common.ps1", "# old common\n")
        self.write(
            repository,
            "scripts/publish-from-handoff.ps1",
            "param()\nthrow 'old publisher must not run'\n",
        )
        self.git(repository, "add", "scripts")
        self.git(repository, "commit", "-m", "old PR control scripts")
        self.git(repository, "branch", "old-pr")
        self.git(repository, "switch", "-c", "main")

        shutil.copy2(ROOT / "scripts" / "start-codex.ps1", repository / "scripts/start-codex.ps1")
        self.write(
            repository,
            "scripts/codex-auto.ps1",
            """param([string]$Request, [int]$PrNumber, [string]$RepositoryRoot)
if ($PSBoundParameters.ContainsKey('PrNumber')) {
    & git -C $RepositoryRoot switch old-pr | Out-Null
}
[IO.File]::WriteAllText((Join-Path $RepositoryRoot 'handoff.json'), '{}')
""",
        )
        self.write(repository, "scripts/common.ps1", "# fixed common marker\n")
        self.write(
            repository,
            "scripts/publish-from-handoff.ps1",
            """param([int]$PrNumber, [string]$RepositoryRoot)
$mode = if ($PSBoundParameters.ContainsKey('PrNumber')) { "existing:$PrNumber" } else { 'new' }
[IO.File]::WriteAllText((Join-Path $RepositoryRoot 'publisher.marker'), $mode)
""",
        )
        self.git(repository, "add", "scripts")
        self.git(repository, "commit", "-m", "current main control scripts")
        return temporary, repository

    def invoke_start(self, repository, *arguments):
        temp_root = repository / "workflow-temp"
        temp_root.mkdir()
        environment = os.environ.copy()
        environment["TEMP"] = str(temp_root)
        environment["TMP"] = str(temp_root)
        command = [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "scripts/start-codex.ps1"),
            *arguments,
        ]
        self.run_command(command, repository, environment)
        self.assertEqual(list(temp_root.glob("tennis-app-control-*")), [])

    def test_existing_pr_switch_keeps_main_control_bundle(self):
        temporary, repository = self.make_repository()
        try:
            self.invoke_start(repository, "-PrNumber", "264", "-Request", "test")
            self.assertEqual((repository / "publisher.marker").read_text(), "existing:264")
            current = self.git(repository, "branch", "--show-current").stdout.strip()
            self.assertEqual(current, "old-pr")
        finally:
            temporary.cleanup()

    def test_normal_mode_still_uses_new_pr_publisher(self):
        temporary, repository = self.make_repository()
        try:
            self.invoke_start(repository, "-Request", "test")
            self.assertEqual((repository / "publisher.marker").read_text(), "new")
            current = self.git(repository, "branch", "--show-current").stdout.strip()
            self.assertEqual(current, "main")
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
