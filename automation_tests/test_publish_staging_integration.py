import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = ROOT / "scripts" / "publish-from-handoff.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


class IsolatedGitRepository:
    def __init__(self, test_case):
        self.test_case = test_case
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="tennis-publish-staging-"
        )
        self.root = Path(self.temporary_directory.name)
        self.git("init")
        self.git("config", "user.name", "Tennis App Integration Test")
        self.git("config", "user.email", "integration-test@example.invalid")

    def close(self):
        self.temporary_directory.cleanup()

    def git(self, *arguments, check=True):
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.test_case.fail(
                f"git {' '.join(arguments)} failed ({result.returncode})\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def write(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")

    def remove(self, relative_path):
        (self.root / relative_path).unlink()

    def commit_files(self, files):
        for relative_path, content in files.items():
            self.write(relative_path, content)
        self.git("add", "--", *files)
        self.git("commit", "-m", "initial test state")

    def staged(self):
        result = self.git("diff", "--cached", "--name-status", "--no-renames", "--")
        return [line for line in result.stdout.splitlines() if line]

    def invoke_staging(self, files):
        handoff_path = self.root / "handoff.json"
        handoff_path.write_text(
            json.dumps({"files": files}, ensure_ascii=False), encoding="utf-8"
        )
        script = str(PUBLISH_SCRIPT).replace("'", "''")
        repository = str(self.root).replace("'", "''")
        handoff = str(handoff_path).replace("'", "''")
        command = (
            f". '{script}' -FunctionsOnly; "
            f"$data = Get-Content -Raw -Encoding utf8 -LiteralPath '{handoff}' | ConvertFrom-Json; "
            f"Invoke-HandoffStaging -RepositoryRoot '{repository}' -Files @($data.files)"
        )
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "PowerShell and Git are required")
class PublishStagingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_status_before = cls.main_status()

    @classmethod
    def tearDownClass(cls):
        cls_status_after = cls.main_status()
        if cls_status_after != cls.main_status_before:
            raise AssertionError(
                "The tennis-app Git status changed during isolated integration tests.\n"
                f"before: {cls.main_status_before!r}\nafter: {cls_status_after!r}"
            )

    @staticmethod
    def main_status():
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout

    def setUp(self):
        self.repository = IsolatedGitRepository(self)

    def tearDown(self):
        self.repository.close()

    def assert_staging_succeeds(self, files):
        result = self.repository.invoke_staging(files)
        self.assertEqual(
            result.returncode,
            0,
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
        return result

    def assert_staging_fails(self, files, message):
        result = self.repository.invoke_staging(files)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertTrue((self.repository.root / "handoff.json").is_file())
        return result

    def test_modified_file_is_the_only_staged_path(self):
        self.repository.commit_files({"chosen file.txt": "before\r\n", "other.txt": "before\n"})
        self.repository.write("chosen file.txt", "after\n")
        self.repository.write("other.txt", "not selected\n")

        self.assert_staging_succeeds(["chosen file.txt"])

        self.assertEqual(self.repository.staged(), ["M\tchosen file.txt"])

    def test_untracked_file_is_staged_as_an_addition(self):
        self.repository.commit_files({"tracked.txt": "base\n"})
        self.repository.write("new file.txt", "new\n")

        self.assert_staging_succeeds(["new file.txt"])

        self.assertEqual(self.repository.staged(), ["A\tnew file.txt"])

    def test_tracked_deletion_is_staged(self):
        self.repository.commit_files({"deleted file.txt": "base\n"})
        self.repository.remove("deleted file.txt")

        self.assert_staging_succeeds(["deleted file.txt"])

        self.assertEqual(self.repository.staged(), ["D\tdeleted file.txt"])

    def test_add_modify_delete_mix_is_staged(self):
        self.repository.commit_files({"modify.txt": "before\n", "delete.txt": "before\n"})
        self.repository.write("modify.txt", "after\n")
        self.repository.remove("delete.txt")
        self.repository.write("add.txt", "new\n")

        self.assert_staging_succeeds(["modify.txt", "delete.txt", "add.txt"])

        self.assertEqual(
            sorted(self.repository.staged()),
            ["A\tadd.txt", "D\tdelete.txt", "M\tmodify.txt"],
        )

    def test_unlisted_modify_add_delete_and_local_artifacts_are_not_staged(self):
        self.repository.commit_files(
            {"selected.txt": "before\n", "unlisted.txt": "before\n", "gone.txt": "before\n"}
        )
        self.repository.write("selected.txt", "after\n")
        self.repository.write("unlisted.txt", "after\n")
        self.repository.remove("gone.txt")
        for path in ("untracked.txt", "report.md", ".pr-body.md", ".codex-prompt.tmp"):
            self.repository.write(path, "local\n")

        self.assert_staging_succeeds(["selected.txt"])

        self.assertEqual(self.repository.staged(), ["M\tselected.txt"])
        self.assertTrue((self.repository.root / "handoff.json").is_file())

    def test_unknown_missing_path_is_rejected(self):
        self.repository.commit_files({"tracked.txt": "base\n"})

        self.assert_staging_fails(
            ["never-existed.txt"],
            "The specified path is neither a file nor a tracked deletion",
        )
        self.assertEqual(self.repository.staged(), [])

    def test_rename_requires_both_paths(self):
        cases = (
            (["old name.txt", "new name.txt"], True),
            (["old name.txt"], False),
            (["new name.txt"], False),
        )
        for files, should_succeed in cases:
            with self.subTest(files=files):
                repository = IsolatedGitRepository(self)
                try:
                    repository.commit_files({"old name.txt": "same content\n"})
                    (repository.root / "old name.txt").rename(repository.root / "new name.txt")
                    result = repository.invoke_staging(files)
                    if should_succeed:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(
                            sorted(repository.staged()),
                            ["A\tnew name.txt", "D\told name.txt"],
                        )
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(
                            "Both the old and new path of a rename must be listed",
                            result.stdout + result.stderr,
                        )
                        self.assertTrue((repository.root / "handoff.json").is_file())
                finally:
                    repository.close()

    def test_forbidden_local_artifact_is_rejected_and_handoff_remains(self):
        self.repository.commit_files({"tracked.txt": "base\n"})
        self.repository.write("report.md", "local\n")

        self.assert_staging_fails(
            ["report.md"], "Local workflow artifacts cannot be published"
        )
        self.assertEqual(self.repository.staged(), [])


if __name__ == "__main__":
    unittest.main()
