from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = ROOT / "scripts" / "publish-from-handoff.ps1"


class PublishWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PUBLISH_SCRIPT.read_text(encoding="utf-8")

    def test_pr_is_created_as_draft_then_marked_ready(self):
        create = self.text.index("gh pr create --draft")
        ready = self.text.index('@("pr", "ready", $prUrl)')
        merge = self.text.index('"pr", "merge", $prUrl')
        self.assertLess(create, ready)
        self.assertLess(ready, merge)

    def test_auto_merge_is_squashed_and_pinned_to_pushed_head(self):
        self.assertIn('git rev-parse HEAD', self.text)
        self.assertIn('"--auto", "--squash", "--match-head-commit", $commitSha', self.text)

    def test_existing_handoff_and_staging_safety_checks_remain(self):
        required = (
            "handoff.json is missing a required property",
            "Local workflow artifacts cannot be published",
            'git diff --cached --quiet --',
            'The staged files do not match the handoff allowlist.',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, self.text)

    def test_staging_is_path_scoped_and_includes_deletions(self):
        self.assertIn('@("add", "-A", "--") + $validatedFiles.ToArray()', self.text)
        self.assertNotIn('git add -A .', self.text)
        self.assertNotIn('@("add", "-A", ".")', self.text)

    def test_tracked_deletion_is_allowed_but_missing_unknown_path_is_rejected(self):
        tracked = self.text.index("git ls-files --error-unmatch -- $relativePath")
        missing = self.text.index(
            "The specified path is neither a file nor a tracked deletion"
        )
        self.assertLess(tracked, missing)
        self.assertNotIn("The specified file does not exist", self.text)

    def test_each_allowlisted_path_must_have_a_worktree_change(self):
        self.assertIn("git diff --quiet -- $relativePath", self.text)
        self.assertIn("The specified tracked file has no working tree change", self.text)

    def test_rename_requires_old_and_new_paths(self):
        self.assertIn("git diff --cached --name-only --no-renames --", self.text)
        self.assertIn("the old and new path", self.text)

    def test_local_artifacts_remain_forbidden_and_cleanup_requires_success(self):
        for artifact in ("report.md", "handoff.json", ".pr-body.md", ".codex-prompt.tmp"):
            self.assertIn(artifact, self.text)
        self.assertIn("if ($published -and $prBodyPath", self.text)
        self.assertIn("if ($published -and $handoffPath", self.text)

    def test_new_pull_request_flow_remains_intact(self):
        self.assertIn('@("switch", "-c", $branch)', self.text)
        self.assertIn("gh pr create --draft", self.text)


if __name__ == "__main__":
    unittest.main()
