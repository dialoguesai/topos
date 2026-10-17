"""Fast stdlib tests also run in the planner before any CI jobs can be skipped."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.select_release_checks import classify, make_plan, previous_release


def modified(path: str, before: str = "old", after: str = "new") -> tuple:
    return ("M", path, before, after)


class SelectionTests(unittest.TestCase):
    def test_docs_skip_expensive_work(self):
        plan = classify([modified("README.md"), ("D", "docs/old.md", "old", None)])
        self.assertEqual(plan["mode"], "docs")
        for flag in ("run_public", "run_targeted", "privacy", "package", "upgrade", "sharing_rig"):
            self.assertFalse(plan[flag], flag)

    def test_owner_status_selects_regression_tests_and_smoke(self):
        plan = classify([modified("topos/cli/status_cmd.py"), modified("CHANGELOG.md")])
        self.assertEqual(plan["mode"], "targeted")
        self.assertIn("tests/topos/test_status_cmd.py", plan["targets"])
        self.assertTrue(plan["privacy"] and plan["package"])
        self.assertFalse(plan["sharing_rig"] or plan["upgrade"] or plan["run_public"])

    def test_drawing_only_does_not_skip_lifecycle_changes_in_same_file(self):
        before = "import os\ndef _draw_reindex_badge(x):\n return 1\nclass ToposTray:\n def _quit(self):\n  return 0\n"
        drawn = before.replace("return 1", "return 2")
        plan = classify([modified("topos/cli/tray.py", before, drawn)])
        self.assertEqual(plan["mode"], "targeted")
        for changed in (drawn.replace("return 0", "return 3"), "import sys\n" + drawn,
                        drawn + "\ndef new_runtime_code():\n return 1\n",
                        drawn.replace("_draw_reindex_badge(x)", "_draw_reindex_badge(x=os.getcwd())")):
            plan = classify([modified("topos/cli/tray.py", before, changed)])
            self.assertTrue(plan["run_public"] and plan["sharing_rig"])

    def test_unknown_runtime_and_shared_dependencies_require_everything(self):
        for path in ("topos/permissions_v2/new.py", "topos/api/shell.py", "topos/ingestion/foo.py",
                     "topos/core/state.py", "shared/new.py", "uv.lock", "pyproject.toml"):
            with self.subTest(path=path):
                plan = classify([modified(path)])
                self.assertTrue(plan["run_public"] and plan["sharing_rig"] and plan["upgrade"])

    def test_infrastructure_changes_run_full_ci(self):
        for path in (".github/workflows/ci.yml", "scripts/select_release_checks.py",
                     "tests/conftest.py", "tests/topos/conftest.py", "tests/lane_shards.py",
                     "requirements.txt", "requirements-dev.txt", "unknown/file"):
            self.assertTrue(classify([modified(path)])["run_public"], path)

    def test_deleted_renamed_or_new_runtime_never_uses_fast_path(self):
        for change in (("D", "topos/cli/status_cmd.py", "old", None),
                       ("A", "topos/cli/status_cmd.py", None, "new"),
                       ("D", "tests/topos/test_status_cmd.py", "old", None)):
            self.assertTrue(classify([change])["run_public"])
        mixed = classify([modified("topos/cli/status_cmd.py"), modified("topos/owner_key.py")])
        self.assertTrue(mixed["run_public"] and mixed["sharing_rig"])

    def test_isolated_test_changes_select_changed_files(self):
        plan = classify([modified("tests/topos/test_status_cmd.py")])
        self.assertTrue(plan["run_targeted"])
        self.assertFalse(plan["package"] or plan["sharing_rig"])
        self.assertIn("tests/topos/test_status_cmd.py", plan["targets"])

    def test_release_stamping_is_not_a_dependency_or_upgrade_change(self):
        old_project = '[project]\nname = "topos-node"\nversion = "1.5.11"\ndependencies = ["click==8"]\n'
        old_manifest = {"releases": [{"version": "1.5.11", "steps": []},
                                     {"version": "unreleased", "steps": []}]}
        new_manifest = {"releases": [{"version": "1.5.11", "steps": []},
                                     {"version": "1.5.12", "steps": [], "summary": "CLI"},
                                     {"version": "unreleased", "steps": []}]}
        changes = [modified("pyproject.toml", old_project, old_project.replace("1.5.11", "1.5.12")),
                   modified("topos/__version__.py", '__version__ = "1.5.11"\n', '__version__ = "1.5.12"\n'),
                   modified("topos/upgrades/manifests.json", json.dumps(old_manifest), json.dumps(new_manifest))]
        plan = classify(changes)
        self.assertEqual(plan["mode"], "metadata")
        self.assertTrue(plan["package"])
        self.assertFalse(plan["sharing_rig"] or plan["run_public"])
        self.assertTrue(classify([modified("pyproject.toml", old_project,
                                          old_project.replace("click==8", "click==9"))])["sharing_rig"])
        self.assertTrue(classify([modified("topos/__version__.py", '__version__ = "1.5.11"\n',
                                          '__version__ = __import__("os").getcwd()\n')])["sharing_rig"])
        lock = '[[package]]\nname = "topos-node"\nversion = "1.5.11"\n[[package]]\nname = "click"\nversion = "8.0"\n'
        self.assertEqual(classify([modified("uv.lock", lock, lock.replace("1.5.11", "1.5.12"))])["mode"], "metadata")
        self.assertTrue(classify([modified("uv.lock", lock, lock.replace('"8.0"', '"9.0"'))])["sharing_rig"])
        new_manifest["releases"][1]["steps"] = [{"kind": "derived_rebuild"}]
        self.assertTrue(classify([modified("topos/upgrades/manifests.json", json.dumps(old_manifest),
                                          json.dumps(new_manifest))])["sharing_rig"])

    def test_empty_comparison_defaults_full(self):
        self.assertTrue(classify([])["run_public"])


class GitRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE).strip()

    def commit(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        return self.git("rev-parse", "HEAD")

    def test_release_range_includes_risky_change_before_version_bump(self):
        self.commit("README.md", "initial")
        self.git("tag", "v1.0.0")
        self.commit("topos/permissions_v2/new.py", "risky")
        self.commit("CHANGELOG.md", "release notes")
        self.git("tag", "v1.0.1")
        self.git("tag", "app-v9.0.0")
        self.assertEqual(previous_release(root=self.root), "v1.0.0")
        plan = make_plan("previous-release", root=self.root)
        self.assertTrue(plan["sharing_rig"])
        self.assertIn("topos/permissions_v2/new.py", plan["changed_files"])

    def test_rename_cannot_hide_deleted_runtime(self):
        base = self.commit("topos/cli/status_cmd.py", "old")
        (self.root / "docs").mkdir()
        self.git("mv", "topos/cli/status_cmd.py", "docs/status.md")
        self.git("commit", "-qm", "rename")
        self.assertTrue(make_plan(base, root=self.root)["sharing_rig"])

    def test_missing_base_fails_instead_of_skipping_checks(self):
        self.commit("README.md", "initial")
        with self.assertRaises(subprocess.CalledProcessError):
            make_plan("missing", root=self.root)


if __name__ == "__main__":
    unittest.main()
