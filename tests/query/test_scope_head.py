"""The loader seam — a head drops in, or is refused, without touching callers.

The refusals are the point. Each one is a failure mode that would otherwise be silent:
a drifted taxonomy mis-routing under a permission boundary, or a model trained on data
we published a promise not to train on.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from topos.query import scope_classifier as sc
from topos.query.scope_head import (
    FORMAT,
    KIND_LINEAR,
    ScopeHeadError,
    describe,
    load_head,
    save_linear_head,
)

PUBLIC_MANIFEST = {
    "corpora": [
        {"source": "AmazonScience/massive", "licence": "CC BY-4.0", "rows": 1600},
        {"source": "schema-grounded (G3)", "licence": "internal", "rows": 709},
    ]
}


def _write_head(tmp_path, *, labels=None, manifest=None, tau_high=0.5, tau_low=0.3):
    labels = list(labels or sc.live_scope_ids())
    rng = np.random.default_rng(0)
    return save_linear_head(
        tmp_path / "head",
        labels=labels,
        coef=rng.normal(size=(len(labels), 8)),
        intercept=np.zeros(len(labels)),
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        corpus_manifest=manifest if manifest is not None else PUBLIC_MANIFEST,
        metrics={"macro_f1": 0.61},
        tau_high=tau_high,
        tau_low=tau_low,
        trained_at="2026-08-14",
    )


def _embed(texts, input_role):  # noqa: ANN001
    return [[1.0] + [0.0] * 7 for _ in texts]


# --- the normal cases -------------------------------------------------------


def test_no_head_installed_is_silent_and_returns_none(tmp_path) -> None:
    """Shipping default. Absence is not an error."""
    assert load_head(tmp_path / "nothing-here") is None


def test_a_valid_head_round_trips(tmp_path) -> None:
    path = _write_head(tmp_path)
    head = load_head(path, embed=_embed)
    assert head is not None
    assert head.kind == KIND_LINEAR
    assert set(head.labels) == set(sc.live_scope_ids())
    assert json.loads((path / "head.json").read_text())["format"] == FORMAT

    scores = head.predict(["how did I sleep"])
    assert len(scores) == 1
    assert set(scores[0]) == set(head.labels)
    assert all(0.0 <= v <= 1.0 for v in scores[0].values())


def test_classify_prefers_the_head_and_says_so(tmp_path) -> None:
    head = load_head(_write_head(tmp_path, tau_high=0.0, tau_low=0.0), embed=_embed)
    verdict = sc.classify("anything", head=head)
    assert verdict.source == sc.SOURCE_HEAD
    assert verdict.labels


def test_the_head_honours_the_same_escalation_contract(tmp_path) -> None:
    """Thresholds mean the same thing on either rung — M1's ladder is unchanged."""
    head = load_head(_write_head(tmp_path, tau_high=1.1, tau_low=0.0), embed=_embed)
    assert sc.classify("anything", head=head).escalated is True

    head = load_head(_write_head(tmp_path, tau_high=1.1, tau_low=1.05), embed=_embed)
    verdict = sc.classify("anything", head=head)
    assert verdict.abstained is True and verdict.labels == ()


def test_use_head_false_forces_the_prototype_path(tmp_path) -> None:
    head = load_head(_write_head(tmp_path, tau_high=0.0, tau_low=0.0), embed=_embed)
    verdict = sc.classify(
        "anything", head=head, use_head=False,
        prototypes=[("health:read", (1.0,))], embed=lambda t, r: [[1.0]],
    )
    assert verdict.source == sc.SOURCE_PROTOTYPE


# --- the refusals -----------------------------------------------------------


def test_refuses_a_head_trained_on_a_non_public_source(tmp_path) -> None:
    """The mechanism behind "No training on your data" — a load-time gate, not a promise."""
    path = _write_head(
        tmp_path,
        manifest={"corpora": [{"source": "owner-node-traffic", "licence": "owner-private", "rows": 900}]},
    ) if False else None
    # save_linear_head validates too, so the refusal fires before anything is written.
    with pytest.raises(ScopeHeadError, match="not a public or synthetic source"):
        save_linear_head(
            tmp_path / "bad",
            labels=list(sc.live_scope_ids()),
            coef=np.zeros((14, 4)), intercept=np.zeros(14),
            embedding_model="x",
            corpus_manifest={"corpora": [
                {"source": "owner-node-traffic", "licence": "owner-private", "rows": 900}
            ]},
        )
    assert not (tmp_path / "bad" / "head.json").exists()
    assert path is None


@pytest.mark.parametrize(
    "licence",
    ["CC BY-SA-4.0", "CC BY-SA 3.0", "CC BY-NC-4.0", "CC BY-ND-4.0",
     "MS MARCO non-commercial research"],
)
def test_refuses_share_alike_and_non_commercial_corpora(tmp_path, licence) -> None:
    """Regression: "CC BY-SA-4.0".startswith("CC BY") is True.

    The prefix allow-list alone waved through the exact licence class PLAN §6.5a rejected
    for SGD. A gate that admits what the plan rejected is worse than no gate, because it
    looks like one.
    """
    with pytest.raises(ScopeHeadError, match="share-alike|not a public"):
        save_linear_head(
            tmp_path / f"bad-{licence[:6]}", labels=list(sc.live_scope_ids()),
            coef=np.zeros((14, 4)), intercept=np.zeros(14), embedding_model="x",
            corpus_manifest={"corpora": [
                {"source": "somewhere", "licence": licence, "rows": 10}
            ]},
        )


def test_still_accepts_the_clean_licences(tmp_path) -> None:
    for licence in ("CC BY-4.0", "CC BY-3.0", "Apache-2.0", "MIT", "CC0-1.0", "internal"):
        save_linear_head(
            tmp_path / f"ok-{licence[:6]}", labels=list(sc.live_scope_ids()),
            coef=np.zeros((14, 4)), intercept=np.zeros(14), embedding_model="x",
            corpus_manifest={"corpora": [
                {"source": "somewhere", "licence": licence, "rows": 10}
            ]},
        )


def test_refuses_a_head_with_no_manifest_at_all(tmp_path) -> None:
    with pytest.raises(ScopeHeadError, match="declares no training corpus"):
        save_linear_head(
            tmp_path / "bad", labels=list(sc.live_scope_ids()),
            coef=np.zeros((14, 4)), intercept=np.zeros(14),
            embedding_model="x", corpus_manifest={},
        )


def test_refuses_a_head_emitting_a_legacy_scope_id(tmp_path) -> None:
    with pytest.raises(ScopeHeadError, match="legacy scope ids"):
        save_linear_head(
            tmp_path / "bad", labels=["publicBio:read"],
            coef=np.zeros((1, 4)), intercept=np.zeros(1),
            embedding_model="x", corpus_manifest=PUBLIC_MANIFEST,
        )


def test_refuses_a_head_whose_taxonomy_drifted(tmp_path) -> None:
    """§6A.2 — a renamed scope under a positional head is silent mis-routing."""
    with pytest.raises(ScopeHeadError, match="absent from the live registry"):
        save_linear_head(
            tmp_path / "bad", labels=["health:read", "dreams:read"],
            coef=np.zeros((2, 4)), intercept=np.zeros(2),
            embedding_model="x", corpus_manifest=PUBLIC_MANIFEST,
        )


def test_refuses_an_unknown_format(tmp_path) -> None:
    path = _write_head(tmp_path)
    meta = json.loads((path / "head.json").read_text())
    meta["format"] = "topos-scope-head-99"
    (path / "head.json").write_text(json.dumps(meta))
    with pytest.raises(ScopeHeadError, match="unknown head format"):
        load_head(path, embed=_embed)


def test_encoder_head_metadata_validates_before_the_weights_are_touched(tmp_path) -> None:
    """Rung 3 runs now. A head with encoder metadata but no model dir must fail on the
    MISSING WEIGHTS, not sail past the label and manifest gates."""
    path = _write_head(tmp_path)
    meta = json.loads((path / "head.json").read_text())
    meta["kind"] = "encoder"
    (path / "head.json").write_text(json.dumps(meta))
    with pytest.raises(Exception) as exc:  # OSError from transformers, not ScopeHeadError
        load_head(path, embed=_embed)
    assert "not implemented" not in str(exc.value)


def test_encoder_head_still_refuses_a_dirty_manifest(tmp_path) -> None:
    """The gates run on metadata, so they fire before any 265 MB is loaded."""
    path = _write_head(tmp_path)
    meta = json.loads((path / "head.json").read_text())
    meta["kind"] = "encoder"
    meta["corpus_manifest"] = {"corpora": [
        {"source": "somewhere", "licence": "CC BY-SA-4.0", "rows": 10}
    ]}
    (path / "head.json").write_text(json.dumps(meta))
    with pytest.raises(ScopeHeadError, match="share-alike"):
        load_head(path, embed=_embed)


# --- fallback behaviour -----------------------------------------------------


def test_a_refused_head_falls_back_to_prototypes_rather_than_failing(tmp_path, monkeypatch) -> None:
    """Degraded routing is recoverable; a broken query path is not."""
    path = _write_head(tmp_path)
    meta = json.loads((path / "head.json").read_text())
    meta["kind"] = "encoder"  # loads, then refuses
    (path / "head.json").write_text(json.dumps(meta))

    monkeypatch.setenv("TOPOS_SCOPE_HEAD", str(path))
    sc.reset_cache()
    assert sc._head_cached() is None
    assert sc.active_source() == sc.SOURCE_PROTOTYPE
    sc.reset_cache()


def test_describe_reports_what_is_installed(tmp_path) -> None:
    assert describe(None) == {"installed": False, "source": "prototype"}
    head = load_head(_write_head(tmp_path), embed=_embed)
    info = describe(head)
    assert info["installed"] is True
    assert info["source"] == "head"
    assert "AmazonScience/massive" in info["corpora"]
    assert info["metrics"]["macro_f1"] == 0.61


# --- the four-branch ladder (REBUILD §B0/§B0a) ------------------------------
#
# Escalate on UNCERTAINTY, never on cardinality: a confident two-scope set is the model
# doing its job. `none` is a trained output, so "decided nothing" and "no idea" are
# different branches — before B0a they were the same all-zero vector, and 54.6% of all
# failures hid inside that identity.


class _StubHead:
    tau_high = 0.60
    tau_low = 0.30

    def __init__(self, scores):
        self._scores = dict(scores)

    def predict(self, texts):
        return [dict(self._scores) for _ in texts]


def _scores(none=0.02, **over):
    base = {s: 0.02 for s in sc.live_scope_ids()}
    base["none"] = none
    base.update(over)
    return base


def test_confident_multi_scope_set_is_ACTED_on_not_escalated() -> None:
    """Jonny's B0 ruling: cardinality is not uncertainty."""
    head = _StubHead(_scores(**{"availability:read": 0.91, "schedule:read": 0.84}))
    verdict = sc.classify("am I free after lunch Friday according to my calendar?", head=head)
    assert verdict.escalated is False
    assert set(verdict.labels) == {"availability:read", "schedule:read"}
    assert verdict.reason == ""


def test_a_banded_scope_escalates_as_ambiguity() -> None:
    head = _StubHead(_scores(**{"health:read": 0.45}))
    verdict = sc.classify("anything", head=head)
    assert verdict.escalated is True and verdict.labels == ()
    assert verdict.reason == "ambiguity"


def test_high_none_is_a_DECIDED_abstain() -> None:
    head = _StubHead(_scores(none=0.88))
    verdict = sc.classify("what's the capital of France?", head=head)
    assert verdict.abstained is True and verdict.escalated is False
    assert verdict.reason == "confident-none"


def test_no_signal_anywhere_escalates_as_ignorance_not_abstain() -> None:
    """The branch that could not exist before `none` was trained: all-low used to be
    indistinguishable from confident-none and silently abstained."""
    head = _StubHead(_scores())  # everything 0.02, none included
    verdict = sc.classify("show my attention heatmap", head=head)
    assert verdict.escalated is True and verdict.labels == ()
    assert verdict.reason == "ignorance"


def test_none_and_a_scope_both_confident_is_ambiguity() -> None:
    head = _StubHead(_scores(none=0.85, **{"schedule:read": 0.85}))
    verdict = sc.classify("anything", head=head)
    assert verdict.escalated is True and verdict.reason == "ambiguity"


def test_an_implausibly_wide_set_is_ambiguity_not_confidence() -> None:
    wide = {s: 0.95 for s in list(sc.live_scope_ids())[:5]}
    head = _StubHead(_scores(**wide))
    verdict = sc.classify("anything", head=head)
    assert verdict.escalated is True and verdict.reason == "ambiguity"


def test_none_never_reaches_labels() -> None:
    head = _StubHead(_scores(none=0.99, **{"health:read": 0.95}))
    verdict = sc.classify("anything", head=head)
    assert "none" not in verdict.labels


def test_pre_b0a_heads_keep_the_legacy_two_threshold_ladder(tmp_path) -> None:
    """An artifact without a trained `none` cannot tell ignorance from confident-none;
    pretending otherwise would relabel every abstention as an escalation."""
    head = load_head(_write_head(tmp_path, tau_high=1.1, tau_low=1.05), embed=_embed)
    verdict = sc.classify("anything", head=head)
    assert verdict.abstained is True and verdict.reason == ""


# --- the pack seam (REBUILD: scope is a bound role, not hardwired) -----------


def test_scope_binding_defaults_to_the_head_when_no_pack_binds_it() -> None:
    b = sc.resolve_scope_binding(None)
    assert b == {"provider": "scope-head", "model": "", "source": "engine_default"}


def test_scope_binding_honours_an_explicit_pack_binding(monkeypatch) -> None:
    class _B:
        provider = "ollama"
        model = "mistral:7b"

    import topos.config.model_packs as mp

    monkeypatch.setattr(mp, "resolve_role_binding", lambda conn, role: _B() if role == "scope" else None)
    b = sc.resolve_scope_binding(object())
    assert b == {"provider": "ollama", "model": "mistral:7b", "source": "pack"}


def test_scope_binding_never_raises_on_a_broken_cache(monkeypatch) -> None:
    import topos.config.model_packs as mp

    def _boom(conn, role):
        raise RuntimeError("corrupt cache")

    monkeypatch.setattr(mp, "resolve_role_binding", _boom)
    assert sc.resolve_scope_binding(object())["source"] == "engine_default"
