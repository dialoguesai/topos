"""Execute the real aggregate shell with failures, skips and cancellations."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AggregateTests(unittest.TestCase):
    def test_every_fast_path_is_driven_by_the_validated_plan(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertLess(workflow.index("python -m unittest discover"), workflow.index("id: select"))
        for flag in ("run_public", "run_targeted", "privacy", "package"):
            self.assertIn(f"steps.select.outputs.{flag}", workflow)
            self.assertIn(f"needs.plan.outputs.{flag}", workflow)
        self.assertIn("--force-full", workflow)
        self.assertIn("&& 'full' || 'change'", workflow, "routine pushes must not cancel full backstops")
        self.assertIn("--release-check-plan check-plan.json", workflow)
        upgrades = (ROOT / ".github/workflows/upgrade-matrix.yml").read_text()
        self.assertIn("github.event_name == 'pull_request'", upgrades)
        self.assertIn("steps.select.outputs.upgrade != 'false'", upgrades)

    def test_only_the_selected_successful_jobs_pass(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        job = workflow.split("\n  test-and-package:\n", 1)[1]
        script = "\n".join(line[10:] for line in job.split("        run: |\n", 1)[1].splitlines())
        base = {"PLAN_RESULT": "success", "PUBLIC_REQUIRED": "true", "PUBLIC_RESULT": "success",
                "TARGETED_REQUIRED": "false", "TARGETED_RESULT": "skipped",
                "PRIVACY_REQUIRED": "true", "BUILD_REQUIRED": "true",
                "PACKAGE_REQUIRED": "true", "PACKAGE_RESULT": "success"}
        def succeeds(values):
            return subprocess.run(["bash", "-e", "-c", script], env={**os.environ, **values},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0
        self.assertTrue(succeeds(base))
        self.assertTrue(succeeds({**base, "PUBLIC_REQUIRED": "false", "PUBLIC_RESULT": "skipped",
                                 "PACKAGE_REQUIRED": "false", "PACKAGE_RESULT": "skipped"}))
        self.assertTrue(succeeds({**base, "PUBLIC_REQUIRED": "false", "PUBLIC_RESULT": "skipped",
                                 "TARGETED_REQUIRED": "true", "TARGETED_RESULT": "success"}))
        for name in ("PLAN", "PUBLIC", "PACKAGE"):
            for bad in ("failure", "cancelled", "skipped", ""):
                with self.subTest(name=name, result=bad):
                    self.assertFalse(succeeds({**base, f"{name}_RESULT": bad}))
        self.assertFalse(succeeds({**base, "TARGETED_REQUIRED": "true"}))
        self.assertFalse(succeeds({**base, "TARGETED_RESULT": "failure"}))
        for name in ("PUBLIC_REQUIRED", "TARGETED_REQUIRED", "PRIVACY_REQUIRED", "BUILD_REQUIRED"):
            for bad in ("", "unexpected"):
                with self.subTest(name=name, bad=bad):
                    self.assertFalse(succeeds({**base, name: bad}))

    def test_publish_checks_cumulative_policy_before_uploading(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("statuses: read", workflow)
        self.assertLess(workflow.index("python scripts/node_release_gate.py --verify"),
                        workflow.index("- name: Upload release artifacts"))
        self.assertIn("needs: smoke", workflow)


if __name__ == "__main__":
    unittest.main()
