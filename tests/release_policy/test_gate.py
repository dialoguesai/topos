"""A publish cannot substitute stale, failed, or mismatched-tree rig evidence."""
import unittest
import json
from pathlib import Path
import tempfile
import subprocess
from unittest.mock import patch

from scripts.node_release_gate import (CONTEXT, MAX_WAIVER_REASON, description, run_gate, validate_evidence,
                                       verify_statuses, waive_gate, waiver_description)


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



TREE = "a" * 40


class WaiverTests(unittest.TestCase):
    """The owner may waive Rig D for one exact tree; the status says "waived", never "passed"."""

    def test_waiver_records_its_own_text_and_runs_no_rig(self):
        with patch("scripts.node_release_gate.git", return_value=""), \
                patch("scripts.node_release_gate.subprocess.run") as command:
            waive_gate({"sharing_rig": True, "tree": TREE, "head": "candidate-sha"}, "rig timing under host load",
                       "dialoguesai/topos")
        command.assert_called_once()
        args = command.call_args.args[0]
        self.assertIn("repos/dialoguesai/topos/statuses/candidate-sha", args)
        self.assertIn("state=success", args)
        self.assertIn(f"description=Rig D waived by owner: rig timing under host load; tree={TREE}", args)
        self.assertNotIn(f"description={description(TREE)}", args)

    def test_waiver_needs_a_clean_tree_a_reason_and_an_impact(self):
        with patch("scripts.node_release_gate.subprocess.run") as command:
            with patch("scripts.node_release_gate.git", return_value="dirty"):
                with self.assertRaises(ValueError):
                    waive_gate({"sharing_rig": True, "tree": TREE, "head": "sha"}, "reason", "dialoguesai/topos")
            with patch("scripts.node_release_gate.git", return_value=""):
                for reason in ("", "   ", "a;b", "x" * (MAX_WAIVER_REASON + 1)):
                    with self.assertRaises(ValueError):
                        waive_gate({"sharing_rig": True, "tree": TREE, "head": "sha"}, reason, "dialoguesai/topos")
                waive_gate({"sharing_rig": False}, "reason", "dialoguesai/topos")
            command.assert_not_called()

    def test_a_full_length_waiver_fits_githubs_limit(self):
        self.assertEqual(len(waiver_description(TREE, "x" * MAX_WAIVER_REASON)), 140)

    def test_verify_accepts_a_waiver_for_this_exact_tree_only(self):
        waived = {"context": CONTEXT, "state": "success", "description": waiver_description(TREE, "host load")}
        self.assertEqual(verify_statuses([waived], TREE), "waived: host load")
        self.assertEqual(verify_statuses([{**waived, "description": description(TREE)}], TREE), "passed")
        other = "b" * 40
        for statuses in ([{**waived, "description": waiver_description(other, "host load")}],
                         [{**waived, "state": "pending"}],
                         [{**waived, "description": f"Rig D waived by owner: ; tree={TREE}"}],
                         [{**waived, "description": f"Rig D waived by someone: host load; tree={TREE}"}],
                         [{**waived, "state": "failure"}, waived]):
            with self.assertRaises(ValueError):
                verify_statuses(statuses, TREE)


if __name__ == "__main__":
    unittest.main()
