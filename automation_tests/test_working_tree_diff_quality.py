from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMMON_SCRIPT = ROOT / "scripts" / "common.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
ANSI_CSI_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text):
    return ANSI_CSI_SEQUENCE.sub("", text)


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "PowerShell and Git are required")
class WorkingTreeDiffQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tennis-working-diff-")
        self.root = Path(self.temporary.name)
        self.git("init")
        self.git("config", "user.name", "Diff Quality Test")
        self.git("config", "user.email", "diff-quality@example.invalid")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        result = subprocess.run(["git", *arguments], cwd=self.root, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def commit_bytes(self, content):
        (self.root / "selected.txt").write_bytes(content)
        self.git("add", "selected.txt")
        self.git("commit", "-m", "base")

    def check(self, files=("selected.txt",)):
        script = str(COMMON_SCRIPT).replace("'", "''")
        repository = str(self.root).replace("'", "''")
        paths = ",".join(f"'{path}'" for path in files)
        command = (f". '{script}'; Assert-WorkingTreeDiffQuality "
                   f"-RepositoryRoot '{repository}' -Files @({paths})")
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-Command", command], cwd=self.root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", check=False)

    def assert_passes(self, files=("selected.txt",)):
        result = self.check(files)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_fails(self, expected, files=("selected.txt",)):
        result = self.check(files)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        output = strip_ansi(result.stdout + result.stderr)
        self.assertIn(expected, output)

    def test_small_change_passes(self):
        self.commit_bytes(b"before\n")
        (self.root / "selected.txt").write_bytes(b"after\n")
        self.assert_passes()

    def test_trailing_whitespace_fails(self):
        self.commit_bytes(b"before\n")
        (self.root / "selected.txt").write_bytes(b"after \n")
        self.assert_fails("trailing whitespace or conflict marker")

    def test_conflict_marker_fails(self):
        self.commit_bytes(b"before\n")
        (self.root / "selected.txt").write_bytes(b"<<<<<<< ours\nleft\n=======\nright\n>>>>>>> theirs\n")
        self.assert_fails("trailing whitespace or conflict marker")

    def test_crlf_to_lf_full_conversion_fails(self):
        content = b"".join(f"line {number}\r\n".encode() for number in range(150))
        self.commit_bytes(content)
        (self.root / "selected.txt").write_bytes(content.replace(b"\r\n", b"\n"))
        self.assert_fails("EOL normalization suspected")

    def test_lf_to_crlf_full_conversion_fails(self):
        content = b"".join(f"line {number}\n".encode() for number in range(150))
        self.commit_bytes(content)
        (self.root / "selected.txt").write_bytes(content.replace(b"\n", b"\r\n"))
        self.assert_fails("EOL normalization suspected")

    def test_mixed_eol_file_with_small_code_change_passes(self):
        content = b"alpha\r\nbeta\ngamma\r\ndelta\n"
        self.commit_bytes(content)
        (self.root / "selected.txt").write_bytes(content.replace(b"beta", b"changed"))
        self.assert_passes()

    def test_new_file_passes(self):
        (self.root / "new.txt").write_bytes(b"new content\r\n")
        self.assert_passes(("new.txt",))

    def test_new_file_trailing_whitespace_fails(self):
        (self.root / "new.txt").write_bytes(b"bad \n")
        self.assert_fails("trailing whitespace or conflict marker", ("new.txt",))


if __name__ == "__main__":
    unittest.main()
