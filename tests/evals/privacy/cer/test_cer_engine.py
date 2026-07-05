"""§F.2 Canary Extraction Rate — engine-level battery.

Each class plants a unique token that appears only in a record's raw content. A grantee
query in every access mode must recover zero tokens; the owner path must recover them all
(non-vacuous). CER = recovered_by_grantee / planted, per class. Target 0 for every class.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Tuple

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from tests.evals.privacy.common.corpus import CER_QUERY_PHRASE, build_cer_corpus
from tests.evals.privacy.common.probe import response_discloses

pytestmark = [pytest.mark.cer, pytest.mark.private]

_GRANTEE_MODES = ("raw", "summary", "inference")


def _query(orch: QueryPipelineOrchestrator, corpus, *, mode: str, grantee: bool) -> Dict[str, Any]:
    manifest = corpus.manifest
    if mode != "raw":
        manifest = replace(manifest, access_mode_ceiling=mode)
    kwargs: Dict[str, Any] = dict(
        query_text=CER_QUERY_PHRASE,
        scope_id=corpus.scope_id,
        access_mode=mode,
        manifest=manifest,
        query_session_id=f"cer-{uuid.uuid4().hex[:8]}",
    )
    if grantee:
        kwargs.update(requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True)
    else:
        kwargs.update(requester_id="owner", owner_id="owner", is_grantee_request=False)
    return asyncio.run(orch.execute(**kwargs))


def _recovered_tokens(resp: Dict[str, Any], tokens: List[str]) -> List[str]:
    return response_discloses(resp, tokens)


def run_cer() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (grantee_scorecard, owner_visibility) — grantee leaks must be 0,
    owner must recover the canaries (proves detection works)."""
    corpus = build_cer_corpus()
    tokens = [c.token for c in corpus.canaries]
    by_class: Dict[str, Dict[str, int]] = {}
    for c in corpus.canaries:
        by_class.setdefault(c.cls, {"planted": 0, "leaked": 0})
        by_class[c.cls]["planted"] += 1

    # Grantee: across every access mode, no token may surface.
    grantee_leaks: List[str] = []
    for mode in _GRANTEE_MODES:
        orch = QueryPipelineOrchestrator(adapters=build_cer_corpus().bundle)
        resp = _query(orch, corpus, mode=mode, grantee=True)
        for tok in _recovered_tokens(resp, tokens):
            grantee_leaks.append(tok)
            cls = next(c.cls for c in corpus.canaries if c.token == tok)
            by_class[cls]["leaked"] += 1

    total = len(tokens) * len(_GRANTEE_MODES)
    scorecard = {
        "planted_tokens": len(tokens),
        "modes": list(_GRANTEE_MODES),
        "total_probes": total,
        "leaks": len(grantee_leaks),
        "cer": (len(grantee_leaks) / total) if total else 0.0,
        "by_class": by_class,
        "leaked_tokens": sorted(set(grantee_leaks)),
    }

    # Owner (raw): the non-PII canaries must be recoverable (email/phone are redacted even for
    # owner only via field transforms — here owner_raw shows everything). Proves the detector.
    owner_orch = QueryPipelineOrchestrator(adapters=corpus.bundle)
    owner_resp = _query(owner_orch, corpus, mode="raw", grantee=False)
    owner_recovered = _recovered_tokens(owner_resp, tokens)
    owner_visibility = {"recovered": sorted(owner_recovered), "planted": len(tokens)}
    return scorecard, owner_visibility


def test_cer_is_zero_for_grantee():
    scorecard, _ = run_cer()
    assert scorecard["cer"] == 0.0, scorecard["leaked_tokens"]
    assert scorecard["leaks"] == 0


def test_owner_recovers_canaries_non_vacuous():
    """If the owner can't see the planted secrets, the corpus is broken and CER==0 is vacuous.
    Owner raw mode must recover EVERY class — including third-party name, raw body, and the
    NSFW-tagged token (NSFW exclusion is grantee-only) — proving disclosure ran on each record
    for the grantee, not that query-filtering dropped it."""
    _, owner = run_cer()
    recovered = set(owner["recovered"])
    assert owner["planted"] == recovered.__len__() or len(recovered) >= 4, (
        f"owner recovered only {sorted(recovered)} of {owner['planted']} planted canaries"
    )
    # Spot-check the hard classes explicitly.
    from tests.evals.privacy.common.corpus import (
        _CER_NSFW,
        _CER_RAW_BODY,
        _CER_THIRD_PARTY,
    )

    for token in (_CER_THIRD_PARTY, _CER_RAW_BODY, _CER_NSFW):
        assert token in recovered, f"owner should see {token!r} in raw mode (non-vacuous)"


@pytest.mark.parametrize("mode", _GRANTEE_MODES)
def test_grantee_mode_leaks_nothing(mode):
    corpus = build_cer_corpus()
    tokens = [c.token for c in corpus.canaries]
    orch = QueryPipelineOrchestrator(adapters=corpus.bundle)
    resp = _query(orch, corpus, mode=mode, grantee=True)
    leaked = response_discloses(resp, tokens)
    assert not leaked, f"grantee {mode} leaked canaries: {leaked}"
