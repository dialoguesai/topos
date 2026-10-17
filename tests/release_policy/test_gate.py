"""A publish cannot substitute stale, failed, or mismatched-tree rig evidence."""
import unittest
import json
from pathlib import Path
import tempfile
import subprocess
from unittest.mock import patch

from scripts.node_release_gate import CONTEXT, description, run_gate, validate_evidence, verify_statuses


class EvidenceTests(unittest.TestCase):
    def test_failed_rig_replaces_prior_success_with_failure(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[1].endswith("run_any_to_any_release.py"):
                raise subprocess.CalledProcessError(1, args)
        with patch.dict("os.environ", {"A2A_RIG_OUT": "/unused/test-output"}), \
                patch("scripts.node_release_gate.git", return_value=""), \
                patch("scripts.node_release_gate.subprocess.run", side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError):
                run_gate({"sharing_rig": True, "tree": "tree", "head": "sha"}, None, "dialoguesai/topos")
        posted = [c for c in calls if c[0] == "gh"]
        self.assertEqual(len(posted), 2)
        self.assertIn("state=pending", posted[0])
        self.assertIn("state=failure", posted[1])

    def test_only_exact_tree_clean_verdict_is_reusable(self):
        good = {"schema": 1, "pass": True, "failures": [], "tree": "abc"}
        validate_evidence(good, "abc")
        for bad in ({**good, "tree": "old"}, {**good, "failures": ["failed"]},
                    {**good, "pass": False}, {**good, "schema": 2}, {}):
            with self.assertRaises(ValueError):
                validate_evidence(bad, "abc")

    def test_newer_failure_overrides_older_success(self):
        good = {"context": CONTEXT, "state": "success", "description": description("abc")}
        verify_statuses([good], "abc")
        for statuses in ([], [{**good, "state": "pending"}], [{**good, "description": description("old")}],
                         [{**good, "state": "failure"}, good], [{**good, "context": "other"}]):
            with self.assertRaises(ValueError):
                verify_statuses(statuses, "abc")

    def test_no_rig_for_fast_release_and_no_status_for_dirty_or_stale_evidence(self):
        with patch("scripts.node_release_gate.subprocess.run") as command:
            run_gate({"sharing_rig": False}, None, "dialoguesai/topos")
            command.assert_not_called()
            with patch("scripts.node_release_gate.git", return_value="dirty"):
                with self.assertRaises(ValueError):
                    run_gate({"sharing_rig": True}, None, "dialoguesai/topos")
            with tempfile.TemporaryDirectory() as directory:
                evidence = Path(directory) / "proof.json"
                evidence.write_text(json.dumps({"schema": 1, "pass": True, "failures": [], "tree": "old"}))
                with patch("scripts.node_release_gate.git", return_value=""):
                    with self.assertRaises(ValueError):
                        run_gate({"sharing_rig": True, "tree": "new"}, evidence, "dialoguesai/topos")
            command.assert_not_called()

    def test_identical_tree_evidence_records_merged_sha_without_rerunning_rig(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "proof.json"
            evidence.write_text(json.dumps({"schema": 1, "pass": True, "failures": [], "tree": "same-tree"}))
            with patch("scripts.node_release_gate.git", return_value=""), \
                    patch("scripts.node_release_gate.subprocess.run") as command:
                run_gate({"sharing_rig": True, "tree": "same-tree", "head": "merged-sha"},
                         evidence, "dialoguesai/topos")
                command.assert_called_once()
                args = command.call_args.args[0]
                self.assertIn("repos/dialoguesai/topos/statuses/merged-sha", args)
                self.assertIn(f"description={description('same-tree')}", args)


if __name__ == "__main__":
    unittest.main()
