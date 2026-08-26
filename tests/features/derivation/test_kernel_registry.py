"""L5-22 second half — the `kind` → kernel dispatcher.

55 lens declarations across 25 packs, three implementations, and — until this — **zero
dispatch**. The only caller was a pilot script that hardcoded two kernels by name. The
ontology could say what to derive from accumulated structure and nothing could execute it.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation.kernels import (
    KernelResult,
    kernel_version,
    register_kernel,
    registered_kinds,
    run_lens,
    run_pack_lenses,
    unimplemented_kinds,
)
from topos.features.derivation.packs import Lens, load_packs
from topos.features.derivation.registry import bundled_pack_dir


class _Pack:
    def __init__(self, lenses=()):
        self.pack = "t.test"
        self.lenses = list(lenses)


def _lens(kind="pattern", predicate="t.thing"):
    return Lens(kind=kind, predicates=[predicate])


def test_the_three_existing_synthesizers_are_reachable_by_declaration():
    """They existed before; nothing could dispatch to them."""
    assert {"rhythm", "graph_labeling", "trajectory"} <= registered_kinds()


def test_a_kernel_carries_its_own_version():
    """Separate from the pack's, because they change for different reasons: a pack bump means
    the ontology changed and triggers re-extraction, a kernel bump means the maths changed and
    should trigger re-computation. Conflating them re-runs an LLM to fix arithmetic."""
    assert kernel_version("rhythm") == "1"
    assert kernel_version("no_such_kind") is None


def test_an_unregistered_kind_abstains_with_a_reason_rather_than_raising(tmp_path):
    c = sqlite3.connect(":memory:")
    res = run_lens(c, _Pack(), _lens(kind="pattern"), "ent_owner")
    assert res.abstained and res.reason == "no_kernel_registered"
    c.close()


def test_a_node_without_an_owner_abstains(tmp_path):
    c = sqlite3.connect(":memory:")
    res = run_lens(c, _Pack(), _lens(kind="rhythm"), "")
    assert res.abstained and res.reason == "no_owner_entity"
    c.close()


def test_one_failing_kernel_does_not_stop_the_pass():
    """Abstention is a RESULT, never an exception. A lens below its evidence floor has given
    a legitimate answer — 'not enough to say' — and raising would make every honest
    abstention look like a broken pipeline."""
    @register_kernel("t_explodes", version="9")
    def _boom(conn, pack, lens, owner):
        raise RuntimeError("kaboom")

    c = sqlite3.connect(":memory:")
    res = run_lens(c, _Pack(), _lens(kind="t_explodes"), "ent_owner")
    assert res.abstained
    assert res.reason.startswith("kernel_error:")
    assert res.kernel_version == "9"
    c.close()


def test_a_kind_cannot_be_registered_twice():
    """Two implementations of one kind means the answer depends on import order."""
    @register_kernel("t_once")
    def _a(conn, pack, lens, owner):
        return []

    with pytest.raises(ValueError):
        @register_kernel("t_once")
        def _b(conn, pack, lens, owner):
            return []


def test_a_pack_runs_all_of_its_lenses():
    @register_kernel("t_counts")
    def _k(conn, pack, lens, owner):
        return [{"predicate": lens.predicate}]

    c = sqlite3.connect(":memory:")
    p = _Pack([_lens(kind="t_counts", predicate="t.a"), _lens(kind="t_counts", predicate="t.b")])
    out = run_pack_lenses(c, p, "ent_owner")
    assert [r.rows[0]["predicate"] for r in out] == ["t.a", "t.b"]
    c.close()


def test_the_unimplemented_gap_is_a_number_not_a_feeling():
    """The distance between what the ontology asks for and what the engine can compute is
    the actual size of the remaining work."""
    gap = unimplemented_kinds(load_packs(bundled_pack_dir()))
    assert gap, "there is still a gap; if this is empty the accounting broke"
    assert "rhythm" not in gap and "graph_labeling" not in gap
    assert sum(gap.values()) >= 30


def test_no_pack_ships_a_kernel():
    """The pack contract's own rule, applied to maths. A pack NAMES a kernel; the engine owns
    the implementation. That property is what makes opening the format to third parties mean
    'accept YAML' rather than 'accept someone's algorithm over your message history'."""
    import pathlib

    import yaml

    for f in sorted(bundled_pack_dir().glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        raw = yaml.safe_load(f.read_text()) or {}
        for entry in raw.get("synthesis") or []:
            assert set(entry) <= {
                "kind", "predicate", "inputs", "min_evidence", "subject", "over",
                "calibration", "null_model", "coverage", "disclosure", "description",
                "note", "window",
            }, f"{f.name}: a synthesis entry carries an unexpected key"
