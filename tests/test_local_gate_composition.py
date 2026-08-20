"""Every pytest lane CI runs must also be reachable from the local gate.

The defect this pins is not a missing check — it is a check nobody runs. The
privacy firewall battery (tests/evals/privacy, 305 tests: UAR/CER zero-leak,
the black-hole probes, minimality, the negotiation ratchet, dense
sparsification, redaction idempotence) has caught the black-hole existence
leak, the SG1 access-mode ceiling gap and both of Q7's roster leaks. Between
its creation and 2026-08-20 it ran in exactly one place: ci.yml, which reaches
it by PATH because every file under that directory is auto-marked `private`
(tests/conftest.py PRIVATE_PATH_HINTS) and both `just test` and `just gate`
select `-m "public and ..."`.

So a developer could run the full local gate, watch it go green, and push a
privacy regression. The signal existed and the loop it belonged in did not
include it — which is the same failure mode as a deleted assertion, minus the
diff that would have shown it.

Wiring it in is one line per recipe. Keeping it wired is this file: a step
added to CI and not to the justfile re-opens the gap silently, and the next
person to notice is whoever reads a red push six commits later.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = REPO_ROOT / "justfile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Recipes a developer is told to run before pushing. Both must reach every
#: lane CI reaches; `gate` is the pre-tag ritual and `test` is the everyday one,
#: and a battery only in `gate` is a battery most changes never see.
_LOCAL_GATE_RECIPES = ("test", "gate")

#: `run:` lines in ci.yml that invoke pytest, minus the YAML key.
_CI_PYTEST_STEP = re.compile(r"^\s*run:\s*(uv run pytest\s.*?)\s*$", re.MULTILINE)


def _recipe_body(name: str) -> str:
    """The indented lines of one just recipe.

    Deliberately textual. Parsing the justfile properly would mean
    reimplementing just's grammar to answer a question about two literal lines,
    and a parser that silently returned "" on a syntax it did not know would
    turn this test green for the wrong reason.
    """
    text = JUSTFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}[^\n]*:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)*)",
                      text, re.MULTILINE)
    assert match is not None, f"no `{name}` recipe in {JUSTFILE}"
    return match.group(1)


def _resolved_recipe_body(name: str, depth: int = 3) -> str:
    """Recipe body with `just <other-recipe>` lines inlined.

    `just test` reaches the battery through `just test-privacy-battery` rather
    than by repeating the pytest line, so a check that only read `test`'s own
    body would report the gap that this file exists to deny.
    """
    body = _recipe_body(name)
    if depth <= 0:
        return body
    for called in re.findall(r"^\s*just\s+([A-Za-z0-9_-]+)", body, re.MULTILINE):
        body += _resolved_recipe_body(called, depth - 1)
    return body


def _ci_pytest_commands() -> list[str]:
    return [
        " ".join(cmd.split())
        for cmd in _CI_PYTEST_STEP.findall(CI_WORKFLOW.read_text(encoding="utf-8"))
    ]


def test_ci_still_has_more_than_one_pytest_lane() -> None:
    """Guards the check below against passing on an empty list.

    If ci.yml is ever restructured so no `run:` line matches, every assertion
    that iterates those commands passes vacuously — the failure shape that let
    the battery drift out of the local gate in the first place.
    """
    commands = _ci_pytest_commands()
    assert len(commands) >= 2, commands


@pytest.mark.parametrize("recipe", _LOCAL_GATE_RECIPES)
def test_the_local_gate_runs_the_privacy_battery(recipe: str) -> None:
    """The specific lane that was missing, named so its removal is a red diff."""
    body = _resolved_recipe_body(recipe)
    assert "tests/evals/privacy" in body, (
        f"`just {recipe}` no longer reaches tests/evals/privacy. That battery is "
        "`private`, so no marker selection will pick it up — it has to be named "
        "by path, and CI naming it is not enough: the point is to fail here "
        "rather than on push."
    )


@pytest.mark.parametrize("recipe", _LOCAL_GATE_RECIPES)
def test_every_ci_pytest_target_is_reachable_from_the_local_gate(recipe: str) -> None:
    """Compare TARGETS, not whole command lines.

    Matching the full command would make this fail on a `-p no:randomly` or a
    `--maxfail` that CI wants and a laptop does not. The invariant is coverage:
    whatever body of tests CI selects, the local gate selects too.
    """
    body = _resolved_recipe_body(recipe)
    missing = []
    for command in _ci_pytest_commands():
        # `uv run pytest <target> ...` — the first non-flag word after pytest.
        words = command.split()[3:]
        target = next((w for w in words if not w.startswith("-")), None)
        if target is None:
            continue
        if target.rstrip("/") not in body:
            missing.append(f"{target}  (from ci.yml: {command})")
    assert not missing, (
        f"ci.yml runs pytest against targets `just {recipe}` never reaches: "
        f"{missing}. A lane that only exists in CI is a lane developers "
        "discover by breaking it."
    )
