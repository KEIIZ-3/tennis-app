from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / "scripts" / "start-codex.ps1").read_text(encoding="utf-8-sig")
AUTO = (ROOT / "scripts" / "codex-auto.ps1").read_text(encoding="utf-8-sig")
COMMON = (ROOT / "scripts" / "common.ps1").read_text(encoding="utf-8-sig")
PUBLISH = (ROOT / "scripts" / "publish-from-handoff.ps1").read_text(encoding="utf-8-sig")


class ExistingPullRequestWorkflowTests(unittest.TestCase):
    def test_pr_number_is_explicitly_forwarded(self):
        self.assertIn('[int]$PrNumber', START)
        self.assertIn('$arguments.PrNumber = $PrNumber', START)
        self.assertIn('$publishArguments.PrNumber = $PrNumber', START)
        self.assertIn('Sync-PullRequestBranch -Number $PrNumber', AUTO)

    def test_normal_mode_remains_intact(self):
        self.assertIn('else {\n        Sync-MainBranch', AUTO)
        self.assertIn('@("switch", "-c", $branch)', PUBLISH)
        self.assertIn('gh pr create --draft', PUBLISH)
        self.assertIn('"pr", "merge", $prUrl, "--auto", "--squash"', PUBLISH)

    def test_pr_validation_and_head_sync_are_safe(self):
        for value in ('state -ne "OPEN"', 'headRefName,headRefOid,headRepository',
                      'headRepository.nameWithOwner -ne $repository',
                      '@("fetch", "origin", $branch)', 'merge-base --is-ancestor',
                      '@("merge", "--ff-only", "origin/$branch")'):
            self.assertIn(value, COMMON)
        function = COMMON[COMMON.index('function Sync-PullRequestBranch'):]
        self.assertNotIn('@("switch", "main")', function)
        self.assertNotIn('reset --hard', function)

    def test_local_branch_paths_are_both_supported(self):
        self.assertIn('"switch", "-c", $branch, "--track", "origin/$branch"', COMMON)
        self.assertIn('refs/remotes/origin/$branch', COMMON)

    def test_existing_publish_reuses_branch_and_validates_updated_sha(self):
        self.assertIn('@("push", "origin", $branch)', PUBLISH)
        self.assertIn('head SHA was not updated', PUBLISH)
        self.assertIn('$handoff.publish_mode -ne "existing_pr"', PUBLISH)
        self.assertIn('[int]$handoff.pr_number -ne $PrNumber', PUBLISH)
        self.assertIn('Existing PR handoff requires the explicit -PrNumber', PUBLISH)

    def test_allowlist_staging_is_shared(self):
        self.assertIn('Invoke-HandoffStaging -RepositoryRoot $repoRoot', PUBLISH)
        self.assertIn('@("add", "-A", "--") + $validatedFiles.ToArray()', PUBLISH)

    def test_artifacts_are_initialized_and_report_is_preserved(self):
        self.assertIn('Move-Item -LiteralPath $reportPath -Destination $previousReportPath', COMMON)
        for name in ('"handoff.json"', '".pr-body.md"', '".codex-prompt.tmp"'):
            self.assertIn(name, COMMON)
        self.assertIn('Initialize-WorkflowArtifacts -RepositoryRoot $repoRoot', AUTO)


if __name__ == "__main__":
    unittest.main()
