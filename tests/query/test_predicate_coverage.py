"""Every pack-declared predicate resolves to a question shape — forever.

protects: a derivation is shipped only when something can ask for it.

This is the "arrives unreachable" tripwire from the query-loop plan: before
S7, 12 hand-curated aliases reached 16 of 231 declared predicates and the
other ~93% of the derived layer rode token luck. Any FUTURE pack predicate
whose leaf phrase cannot be matched by the generic index reds this battery
the day it is declared, not the day an owner asks and gets silence.
"""

from __future__ import annotations

import re

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.query.facts_direct import match_known_item


def _all_predicates():
    packs = load_packs(bundled_pack_dir())
    pack_iter = packs.values() if isinstance(packs, dict) else packs
    for pack in pack_iter:
        for name in pack.predicates:
            yield pack, name


def test_every_declared_predicate_resolves_from_its_leaf_phrase():
    missed = []
    total = 0
    for pack, name in _all_predicates():
        total += 1
        leaf = name.split(".", 1)[1] if "." in name else name
        phrase = " ".join(t for t in re.split(r"[._]+", leaf) if t)
        question = f"What is my {phrase}?"
        m = match_known_item(question)
        if not m or name not in m["predicates"]:
            missed.append((name, question))
    assert total >= 231, f"pack catalog shrank to {total} predicates"
    assert not missed, (
        f"{len(missed)}/{total} declared predicates are unreachable: "
        + "; ".join(f"{n} (asked: {q!r})" for n, q in missed[:10])
    )


def test_special_class_comes_from_the_pack_not_a_hand_list():
    """beliefs.* and admin.legal are special-class packs that had NO alias —
    the generic layer must carry their sensitivity, or widening the match
    widens disclosure."""
    special_hits = 0
    for pack, name in _all_predicates():
        if pack.effective_sensitivity(name) != "special":
            continue
        leaf = name.split(".", 1)[1] if "." in name else name
        phrase = " ".join(t for t in re.split(r"[._]+", leaf) if t)
        m = match_known_item(f"What is my {phrase}?")
        assert m is not None and name in m["predicates"], name
        assert m["special"] is True, (
            f"{name} is special-class in {pack.pack} but matched non-special"
        )
        special_hits += 1
    assert special_hits >= 40  # the five special packs declare 42 predicates


def test_owner_frame_still_gates_the_generic_layer():
    """A question about someone else must never fire the owner's fact sheet,
    generic layer included."""
    assert match_known_item("What is Sarah's medication?") is None


def test_curated_aliases_survive_unchanged():
    m = match_known_item("What medications am I taking?")
    assert m is not None
    assert "health.medication" in m["predicates"]
    assert m["special"] is True
