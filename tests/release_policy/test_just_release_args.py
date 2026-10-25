"""`just release-checks --waive "<reason>"` hands the reason to the gate as ONE argument.

Before this, the release recipes interpolated `{{args}}` into the shell line unquoted: a multi-word waiver reason was
split into words, and brackets in it were a shell syntax error (1.5.25's waiver had to call the script directly).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = ROOT / "justfile"


def _recipe(name: str) -> str:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name} "))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith((" ", "\t")):
            break
        body.append(line.strip())
    return "\n".join(body)


class ReleaseRecipeArgumentTests(unittest.TestCase):
    def test_release_recipes_pass_arguments_through_quoted(self):
        text = JUSTFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^set positional-arguments\s*$")
        self.assertEqual(_recipe("release-checks"), 'uv run python scripts/node_release_gate.py "$@"')
        self.assertEqual(_recipe("release-plan"), 'uv run python scripts/select_release_checks.py "$@"')

    @unittest.skipUnless(shutil.which("just"), "just is not installed")
    def test_a_multi_word_waiver_reason_arrives_as_one_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "argv.json"
            stub = Path(directory) / "uv"
            stub.write_text(f"#!/usr/bin/env python3\nimport json, sys\nopen({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
                            encoding="utf-8")
            stub.chmod(0o755)
            reason = "r1+r2 failed refusal timing under host load (owner, 25 Oct)"
            subprocess.run(["just", "--justfile", str(JUSTFILE), "--working-directory", directory,
                            "release-checks", "--waive", reason],
                           check=True, env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(json.loads(record.read_text()),
                             ["run", "python", "scripts/node_release_gate.py", "--waive", reason])


if __name__ == "__main__":
    unittest.main()
