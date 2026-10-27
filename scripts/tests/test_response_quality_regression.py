"""The gate must detect regressions, lost coverage and incomparable baselines."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("quality_gate", Path(__file__).parents[1] / "run_response_quality_regression.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def baseline(self):
        return {"catalog_hash": "c", "fixture_hash": "f", "model": "mock",
                "toolchain": {"python": "3"}, "errors": [],
                "cases": {"passing": {"status": "passed"}, "broken": {"status": "failed"}}}

    def test_fix_cannot_offset_regression(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after["cases"]["passing"]["status"] = "failed"
        after["cases"]["broken"]["status"] = "passed"
        diff = gate.compare(before, after)
        self.assertEqual(diff["verdict"], "FAIL")
        self.assertEqual(diff["fixes"], ["broken"])
        self.assertEqual(diff["regressions"], ["passing"])

    def test_unchanged_existing_failure_remains_red(self):
        self.assertEqual(gate.compare(self.baseline(), self.baseline())["verdict"], "FAIL")

    def test_skipping_previous_pass_is_regression(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after["cases"]["passing"]["status"] = "skipped"
        self.assertIn("passing", gate.compare(before, after)["regressions"])

    def test_missing_or_added_test_invalidates_comparison(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        del after["cases"]["broken"]
        self.assertEqual(gate.compare(before, after)["verdict"], "NOT_COMPARABLE")

    def test_changed_fixture_or_toolchain_or_model_invalidates_comparison(self):
        for field in ("fixture_hash", "catalog_hash", "model", "toolchain", "runtime_policy"):
            before = self.baseline()
            after = copy.deepcopy(before)
            after[field] = "different"
            self.assertEqual(gate.compare(before, after)["verdict"], "NOT_COMPARABLE")

    def test_runner_failure_is_not_comparable(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after["errors"] = ["web runner crashed"]
        self.assertEqual(gate.compare(before, after)["verdict"], "NOT_COMPARABLE")

    def test_all_passes_with_same_inventory_can_pass(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after["cases"]["broken"]["status"] = "passed"
        self.assertEqual(gate.compare(before, after)["verdict"], "PASS")

    def parse(self, xml, files):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.xml"
            path.write_text(xml)
            return gate.parse_junit(path, "test", files)

    def test_parses_pytest_and_vitest_shapes(self):
        rows = self.parse('''<testsuites><testsuite name="pytest">
          <testcase file="tests/test_a.py" classname="tests.test_a" name="test_one" time="0.2"/>
          <testcase file="tests/test_a.py" classname="tests.test_a" name="test_two"><skipped/></testcase>
          </testsuite><testsuite name="/checkout/lib/a.test.ts">
          <testcase classname="lib/a.test.ts" name="failure"><failure>ignored private output</failure></testcase>
          </testsuite></testsuites>''', {"tests/test_a.py": "node", "lib/a.test.ts": "web"})
        self.assertEqual(sorted(r["status"] for r in rows.values()), ["failed", "passed", "skipped"])
        self.assertNotIn("private", str(rows))

    def test_empty_or_duplicate_reports_are_not_green(self):
        with self.assertRaises(ValueError):
            self.parse('<testsuite/>', {"tests/a.py": "one"})
        with self.assertRaises(ValueError):
            self.parse('''<testsuite name="tests/a.py"><testcase name="a"/><testcase name="a"/></testsuite>''', {"tests/a.py": "one"})


if __name__ == "__main__":
    unittest.main()
