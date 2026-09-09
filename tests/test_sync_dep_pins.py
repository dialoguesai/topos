"""The dependency-pin writer must survive the comments it writes around.

`scripts/sync-dep-pins.py` is the only supported way to move a pin: CI runs it
with `--check`, and when that fails it tells you to run the script for real.
That write path was unreachable-by-accident for its whole life — `main()`
returns early when the pins already match, so the only case it ever ran was the
no-op one, and its block regex could not match a dependency list containing
comments. The lists here carry comments that are load-bearing (`grand-cypher`'s
ceiling records that 1.0.0+ breaks `test_entity_cypher`; `torch`'s records a
reproduced Apple-silicon SIGSEGV), so the two facts combined meant the script
raised `RuntimeError` for every real edit and would have eaten those notes if
the regex had matched.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync-dep-pins.py"


def _load():
    spec = importlib.util.spec_from_file_location("_topos_sync_dep_pins", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the script's `DepSection` is a dataclass under
    # `from __future__ import annotations`, and resolving those deferred
    # annotations needs the module findable in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pins():
    return _load()


@pytest.fixture(scope="module")
def versions(pins):
    return pins.parse_lock_versions(pins.LOCKFILE.read_text(encoding="utf-8"))


def test_write_path_runs_against_the_real_pyproject(pins, versions):
    """The regression itself: this raised RuntimeError for every real edit."""
    rendered = pins.expected_content(versions)
    assert rendered.strip(), "writer produced empty content"


def test_rendered_content_satisfies_the_check_ci_runs(pins, versions):
    """`--check` and the writer must agree, or the loop CI sends you into never ends."""
    assert pins.check_pins(pins.expected_content(versions), versions) == []


def _dependency_comments(text: str) -> set[str]:
    """Comment lines sitting inside a `... = [` list, which is what got eaten."""
    found: set[str] = set()
    for block in re.findall(r"^\w[\w-]* = \[$(.*?)^\]$", text, re.MULTILINE | re.DOTALL):
        found.update(
            line.strip() for line in block.splitlines() if line.strip().startswith("#")
        )
    return found


def test_comments_inside_the_dependency_lists_survive(pins, versions):
    original = pins.PYPROJECT.read_text(encoding="utf-8")
    before = _dependency_comments(original)
    assert before, "fixture is inert — the real pyproject has no in-list comments to lose"

    after = _dependency_comments(pins.expected_content(versions))
    assert before <= after, f"writer dropped in-list comments: {sorted(before - after)}"


def test_deliberate_ceilings_are_not_floated_to_the_lock(pins, versions):
    """A cap in CUSTOM_SPECS outranks the locked version — that is the whole point.

    torch is capped `<2.13` because 2.13.0 segfaults on Apple silicon under the
    node's concurrent first-query load. If the writer ever re-derived this pin
    from uv.lock, a routine `uv lock` would silently reopen that crash.
    """
    rendered = pins.expected_content(versions)
    for dep, spec in pins.CUSTOM_SPECS.items():
        assert f'"{dep}{spec}"' in rendered, f"{dep} lost its deliberate spec {spec}"
        assert pins.resolve_spec(dep, versions) == spec


def test_a_managed_name_does_not_eat_a_longer_neighbour(pins):
    """`pydantic` must not rewrite the `pydantic-settings` entry sitting above it.

    The per-entry regex ends in `[^"]*`, which without a right boundary on the
    name swallows the rest of a longer package name: rewriting `pydantic`
    against a block whose *first* entry is `pydantic-settings` turned that entry
    into a second `pydantic` line and returned a match, so the script exited 0
    having silently deleted a dependency from a file that ships to PyPI.

    The real pyproject happens to list `pydantic` before `pydantic-settings`, so
    the shipped file never hits this — which is exactly why the check has to be
    driven from a synthetic block in the dangerous order rather than from
    `expected_content`, and why asserting against the real file would pass with
    the bug fully present.
    """
    content = (
        "dependencies = [\n"
        '  "pydantic-settings~=2.15.0",\n'
        '  "pydantic~=2.13.2",\n'
        "]\n"
    )
    section = pins.DepSection("dependencies", ("pydantic",))
    out = pins.replace_section(content, section, {"pydantic": "~=9.9.9"})

    assert '"pydantic-settings~=2.15.0",' in out, (
        "the pydantic rewrite consumed the longer pydantic-settings entry"
    )
    assert '"pydantic~=9.9.9",' in out
    assert out.count("pydantic-settings") == 1
