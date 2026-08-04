from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_has_expected_top_level_structure(self):
        self.assertRegex(self.text, r"(?m)^name:\s*CI\s*$")
        self.assertRegex(self.text, r"(?m)^on:\s*$")
        self.assertRegex(self.text, r"(?m)^jobs:\s*$")
        self.assertRegex(self.text, r"(?m)^  test:\s*$")
        self.assertRegex(self.text, r"(?m)^    steps:\s*$")

    def test_ci_uses_render_python_and_project_requirements(self):
        self.assertIn('python-version: "3.13.4"', self.text)
        self.assertIn("python -m pip install -r requirements.txt", self.text)
        self.assertNotIn("requirements-ci.txt", self.text)
        self.assertNotIn("PyYAML", self.text)

    def test_ci_contains_required_checks(self):
        required = (
            "python manage.py check",
            "python manage.py test",
            "git diff --check",
            "git diff --name-only",
            "git grep",
            "git ls-files",
            "Management.Automation.Language.Parser",
        )
        for command in required:
            with self.subTest(command=command):
                self.assertIn(command, self.text)


if __name__ == "__main__":
    unittest.main()
