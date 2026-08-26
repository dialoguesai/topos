"""L5-22 — the lens contract: `synthesis[]`, validated.

A `predicates[]` entry declares an extraction from ONE record. A `synthesis[]` entry
declares a LENS: a named computation over accumulated structure. The block was grown in
place rather than given a sibling key (D8, 2026-08-26) — it already meant "derive from
accumulated evidence", it already held 55 declarations across the 25 packs, and nothing
consumed it yet, so widening it was free then and a migration later.

The two things this file exists to pin down are the two a validator gets wrong first:

  * there are TWO shapes, not one — producers emit onto predicates, reconcilers open a
    review and assert nothing; and
  * the shipped corpus is far messier than a clean design would assume, so the contract
    has to fit the data rather than legislate it.
"""

from __future__ import annotations

import pytest
import yaml

from topos.features.derivation.packs import (
    MinEvidence,
    PackValidationError,
    load_pack,
    load_packs,
)
from topos.features.derivation.registry import bundled_pack_dir


# --- the shipped corpus must keep parsing ---

def test_every_shipped_lens_parses():
    """The compatibility floor: widening the block may not invalidate one declaration."""
    packs = load_packs(bundled_pack_dir())
    lenses = [l for p in packs.values() for l in p.lenses]
    assert len(packs) >= 25, "the shipped catalog should not shrink"
    assert len(lenses) >= 55, "widening the block must not invalidate a declaration"


def test_the_corpus_splits_into_producers_and_reconcilers():
    lenses = [l for p in load_packs(bundled_pack_dir()).values() for l in p.lenses]
    assert sum(1 for l in lenses if not l.is_producer) == 5, "the reconciler set is enumerable"
    assert sum(1 for l in lenses if l.is_producer) >= 50


def test_a_non_owner_lens_exists_only_inside_an_outward_pack():
    """The two ends of the outward lane must agree.

    A lens with `subject: person` is refused at load unless its pack declares
    net_subject: allow — so this asserts the shipped catalog actually satisfies the rule
    rather than merely that the rule exists.
    """
    packs = load_packs(bundled_pack_dir())
    for p in packs.values():
        for l in p.lenses:
            if l.subject != "owner":
                assert p.net_subject == "allow", (
                    f"{p.pack} has a {l.subject!r} lens without declaring net_subject: allow")


# --- min_evidence: 21 spellings, three families ---

@pytest.mark.parametrize("raw,count,days,unit", [
    (3, 3, None, ""),                       # bare count
    (10, 10, None, ""),
    ("21d", None, 21, ""),                  # duration
    ("6w", None, 42, ""),
    ("90d", None, 90, ""),
    ("200_messages", 200, None, "messages"),  # count carrying a unit
    ("1_dated_event", 1, None, "dated_event"),
    (None, None, None, ""),                 # reconcilers have no floor
])
def test_every_shipped_spelling_of_min_evidence_parses(raw, count, days, unit):
    """Measured across the 55 declarations: 21 distinct spellings. A validator that picked
    one family would have rejected 40 of them, so this parses all three."""
    m = MinEvidence.parse(raw)
    assert (m.count, m.days, m.unit) == (count, days, unit)


def test_an_unparseable_floor_is_refused():
    with pytest.raises(ValueError):
        MinEvidence.parse("whenever it feels ready")


def test_a_boolean_is_not_a_floor():
    """bool subclasses int, so `min_evidence: true` would silently become a floor of 1."""
    with pytest.raises(ValueError):
        MinEvidence.parse(True)


# --- validation ---

def _spec(synthesis, **over):
    d = {
        "pack": "t.test", "version": "0.1.0", "title": "T",
        "sensitivity_class": "personal", "role_policy": "authored_addressed",
        "disclosure_default": "owner_only", "routing": {}, "guidance": {},
        "consumers": ["x"],
        "eval": {"gold": [{"a": 1}], "negative_controls": [{"b": 2}]},
        "predicates": [
            {"name": "t.thing", "value_type": "string", "cardinality": "single",
             "temporal": "interval", "altitude": "stated"},
            {"name": "t.other", "value_type": "string", "cardinality": "single",
             "temporal": "interval", "altitude": "stated"},
        ],
        "synthesis": synthesis,
    }
    d.update(over)
    return d


def _load(tmp_path, synthesis, **over):
    f = tmp_path / "t.yaml"
    f.write_text(yaml.safe_dump(_spec(synthesis, **over)))
    return load_pack(f)


def test_a_producer_must_name_a_predicate_the_pack_declares(tmp_path):
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "pattern", "predicate": "t.nonexistent"}])
    assert "not declared by this pack" in str(e.value)


def test_a_producer_with_no_predicate_is_refused(tmp_path):
    with pytest.raises(PackValidationError):
        _load(tmp_path, [{"kind": "pattern"}])


def test_a_lens_may_name_several_predicates(tmp_path):
    """9 of the 55 do — one stylometry pass fills six at once."""
    p = _load(tmp_path, [{"kind": "stylometry", "predicate": ["t.thing", "t.other"]}])
    assert p.lenses[0].predicates == ["t.thing", "t.other"]
    assert p.lenses[0].predicate == "t.thing", "the convenience accessor is the first"


def test_a_reconciler_needs_no_predicate(tmp_path):
    """It opens a review rather than asserting, so there is nothing to name."""
    p = _load(tmp_path, [{"kind": "consistency_check", "description": "x vs y"}])
    assert p.lenses[0].predicates == []
    assert not p.lenses[0].is_producer


def test_an_unknown_kind_is_refused(tmp_path):
    """Packs name an engine kernel; they never ship one. An unknown kind is a lens with
    no implementation, which would fail silently at dispatch instead of loudly at load."""
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "vibes", "predicate": "t.thing"}])
    assert "unknown kind" in str(e.value)


# --- the rule that ties the contract to the consent plane ---

def test_a_non_owner_subject_requires_the_pack_to_allow_it(tmp_path):
    """F10's fix, at the contract level.

    Authorisation for an outward write is derived from a DECLARATION, not from a routing
    string an extractor produced per assertion. A lens claiming to describe dyads cannot
    sit in a pack that never claimed the right to describe anyone but the owner.
    """
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "graph_labeling", "predicate": "t.thing",
                          "subject": "dyad"}])
    assert "net_subject" in str(e.value)


def test_a_non_owner_subject_loads_when_the_pack_allows_it(tmp_path, monkeypatch):
    from topos.features.derivation import packs as m

    monkeypatch.setattr(m, "first_party_pack_dirs", lambda: (tmp_path.resolve(),))
    p = _load(tmp_path, [{"kind": "graph_labeling", "predicate": "t.thing",
                          "subject": "dyad"}], net_subject="allow")
    assert p.lenses[0].subject == "dyad"


def test_an_owner_subject_needs_no_permission(tmp_path):
    assert _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing"}]).lenses[0].subject == "owner"


def test_a_nonsense_subject_axis_is_refused(tmp_path):
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing", "subject": "vibes"}])
    assert "bad subject" in str(e.value)


# --- ceilings ---

def test_a_lens_may_narrow_disclosure(tmp_path):
    p = _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing",
                          "disclosure": "owner_only"}], disclosure_default="scoped")
    assert p.lenses[0].disclosure == "owner_only"


def test_a_lens_may_not_widen_disclosure(tmp_path):
    """The same ceiling rule the pack itself lives under."""
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing",
                          "disclosure": "public"}], disclosure_default="owner_only")
    assert "wider than" in str(e.value)


def test_a_bad_calibration_method_is_refused(tmp_path):
    with pytest.raises(PackValidationError) as e:
        _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing",
                          "calibration": {"method": "eyeballing"}}])
    assert "calibration.method" in str(e.value)


def test_undecided_keys_are_recorded_but_not_required(tmp_path):
    """`over`, `null_model` and `coverage` carry through without being mandatory — making
    them required today would reject all 55 shipped declarations on the day they acquire
    a meaning."""
    p = _load(tmp_path, [{"kind": "graph_labeling", "predicate": "t.thing",
                          "over": "directed_edges@period",
                          "null_model": "degree_preserving_shuffle",
                          "coverage": {"degrade_to": "messaging_only"}}])
    l = p.lenses[0]
    assert l.over == "directed_edges@period"
    assert l.null_model == "degree_preserving_shuffle"
    assert l.coverage == {"degrade_to": "messaging_only"}
    assert _load(tmp_path, [{"kind": "pattern", "predicate": "t.thing"}]).lenses[0].over == ""
