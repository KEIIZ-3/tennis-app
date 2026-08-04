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


if __name__ == "__main__":
    unittest.main()
