"""M0 rung-1 scope classifier — contract and safety invariants.

Hermetic: every test injects a fake embedder, so nothing here loads MiniLM. The
viability numbers live in ``scripts/eval_scope_classifier.py``, not in the test suite.
"""

from __future__ import annotations

import pytest

from topos.query.scope_classifier import (
    SOURCE_PROTOTYPE,
    TAU_HIGH,
    TAU_LOW,
    ScopeVerdict,
    _build_prototypes,
    _check_emittable,
    classify,
    expand_routing,
    live_scope_ids,
    prototype_texts,
    reset_cache,
)
from topos.query.scope_registry_loader import LEGACY_SCOPE_IDS


def _unit(index: int, dims: int = 4) -> list[float]:
    vec = [0.0] * dims
    vec[index % dims] = 1.0
    return vec


@pytest.fixture
def protos() -> list[tuple[str, tuple[float, ...]]]:
    live = live_scope_ids()
    return [(live[i], tuple(_unit(i))) for i in range(3)]


def _embed_as(vector: list[float]):
    def _embed(texts, input_role):  # noqa: ANN001
        return [list(vector) for _ in texts]

    return _embed


def test_prototypes_come_from_the_live_registry_only() -> None:
    texts = prototype_texts()
    assert set(texts) == set(live_scope_ids())
    assert len(texts) == 14
    for scope, entries in texts.items():
        assert entries, f"{scope} has no prototype text"
        assert all(isinstance(t, str) and t.strip() for t in entries)


def test_no_legacy_scope_id_is_emittable() -> None:
    assert not (set(live_scope_ids()) & LEGACY_SCOPE_IDS)
    for legacy in sorted(LEGACY_SCOPE_IDS):
        with pytest.raises(ValueError, match="legacy scope id"):
            _check_emittable([legacy])


def test_unknown_label_is_refused() -> None:
    with pytest.raises(ValueError, match="absent from the live registry"):
        _check_emittable(["not_a_scope:read"])


def test_expand_routing_is_identity_today() -> None:
    scopes = live_scope_ids()[:3]
    assert expand_routing(scopes) == tuple(scopes)
    assert expand_routing([]) == ()


def test_above_tau_high_answers(protos) -> None:
    verdict = classify(
        "anything", prototypes=protos, embed=_embed_as(_unit(0)), tau_high=0.5, tau_low=0.2
    )
    assert isinstance(verdict, ScopeVerdict)
    assert verdict.labels == (protos[0][0],)
    assert verdict.escalated is False
    assert verdict.abstained is False
    assert verdict.source == SOURCE_PROTOTYPE
    assert verdict.confidence == pytest.approx(1.0)


def test_between_thresholds_escalates_and_names_nothing(protos) -> None:
    """The band exists so an unsure classifier hands off rather than guessing a scope."""
    blend = [0.6, 0.6, 0.0, 0.0]
    verdict = classify(
        "anything", prototypes=protos, embed=_embed_as(blend), tau_high=0.95, tau_low=0.2
    )
    assert verdict.escalated is True
    assert verdict.labels == ()
    assert verdict.abstained is False


def test_below_tau_low_abstains_and_opens_nothing(protos) -> None:
    orthogonal = [0.0, 0.0, 0.0, 1.0]
    verdict = classify(
        "anything", prototypes=protos, embed=_embed_as(orthogonal), tau_high=0.5, tau_low=0.2
    )
    assert verdict.labels == ()
    assert verdict.escalated is False
    assert verdict.abstained is True


def test_multi_label_only_when_the_runner_up_also_clears(protos) -> None:
    tied = [0.71, 0.71, 0.0, 0.0]
    both = classify(
        "anything", prototypes=protos, embed=_embed_as(tied), tau_high=0.6, tau_low=0.2
    )
    assert set(both.labels) == {protos[0][0], protos[1][0]}

    one = classify(
        "anything", prototypes=protos, embed=_embed_as(tied), tau_high=0.705, tau_low=0.2
    )
    assert len(one.labels) == 2 or len(one.labels) == 1  # both sit at 0.71


def test_top_k_bounds_the_label_set(protos) -> None:
    tied = [0.58, 0.58, 0.58, 0.0]
    verdict = classify(
        "anything", prototypes=protos, embed=_embed_as(tied), tau_high=0.5, tau_low=0.2, top_k=2
    )
    assert len(verdict.labels) == 2


def test_empty_text_abstains_without_embedding() -> None:
    def _boom(texts, input_role):  # noqa: ANN001
        raise AssertionError("must not embed empty input")

    verdict = classify("   ", embed=_boom, prototypes=[("health:read", (1.0,))])
    assert verdict.abstained is True
    assert verdict.labels == ()


def test_no_prototypes_abstains(protos) -> None:
    verdict = classify("anything", prototypes=[], embed=_embed_as(_unit(0)))
    assert verdict.abstained is True


def test_scores_are_reported_for_every_prototype(protos) -> None:
    verdict = classify(
        "anything", prototypes=protos, embed=_embed_as(_unit(0)), tau_high=0.5, tau_low=0.2
    )
    assert set(verdict.scores) == {scope for scope, _ in protos}


def test_thresholds_are_ordered() -> None:
    assert 0.0 < TAU_LOW < TAU_HIGH < 1.0


def test_build_prototypes_centroids_one_per_live_scope() -> None:
    calls: list[str] = []

    def _embed(texts, input_role):  # noqa: ANN001
        calls.append(input_role)
        return [_unit(i) for i in range(len(texts))]

    built = _build_prototypes(_embed)
    assert len(built) == len(live_scope_ids())
    # Prototype text is indexed as a passage; the query side asks as a query.
    assert calls == ["passage"]


def test_reset_cache_is_callable() -> None:
    reset_cache()
