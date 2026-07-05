"""§F.2 CER over the seeded-SQLite FULL-INGEST corpus.

Canaries traverse the real platform privacy layer (disclosure columns written by
`run_privacy_disclosure_layer`) and grantee reads go through the real SQL disclosure spec.
This is the deep-path complement to the in-memory CER battery: it proves the actual
column-write + SQL-read plumbing, not just the read-path transform. The pending-disclosure
record (never processed) must fail closed to the placeholder for a grantee.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any, Dict, List

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from tests.evals.privacy.common.probe import response_discloses
from tests.evals.privacy.common.sqlite_corpus import (
    SQLITE_CER_QUERY_PHRASE,
    build_sqlite_cer_corpus,
)

pytestmark = [pytest.mark.cer, pytest.mark.private]

_GRANTEE_MODES = ("raw", "summary", "inference")


def _query(orch, corpus, *, mode: str, grantee: bool) -> Dict[str, Any]:
    manifest = corpus.manifest
    if mode != "raw":
        manifest = replace(manifest, access_mode_ceiling=mode)
    kwargs: Dict[str, Any] = dict(
        query_text=SQLITE_CER_QUERY_PHRASE,
        scope_id=corpus.scope_id,
        access_mode=mode,
        manifest=manifest,
        query_session_id=f"sqlcer-{uuid.uuid4().hex[:8]}",
    )
    if grantee:
        kwargs.update(requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True)
    else:
        kwargs.update(requester_id="owner", owner_id="owner", is_grantee_request=False)
    return asyncio.run(orch.execute(**kwargs))


def test_full_ingest_grantee_cer_is_zero():
    corpus = build_sqlite_cer_corpus()
    tokens = [c.token for c in corpus.canaries]
    leaks: List[str] = []
    for mode in _GRANTEE_MODES:
        resp = _query(QueryPipelineOrchestrator(adapters=corpus.bundle), corpus, mode=mode, grantee=True)
        leaks.extend(response_discloses(resp, tokens))
    assert not leaks, f"grantee leaked canaries through full-ingest path: {sorted(set(leaks))}"


def test_full_ingest_owner_recovers_non_pii_canaries():
    """Non-vacuous: owner raw mode recovers the raw-body and email/phone canaries (proves the
    records were retrieved and disclosure genuinely ran, not that they were filtered out)."""
    corpus = build_sqlite_cer_corpus()
    from tests.evals.privacy.common.sqlite_corpus import _S_EMAIL, _S_RAW

    resp = _query(QueryPipelineOrchestrator(adapters=corpus.bundle), corpus, mode="raw", grantee=False)
    recovered = response_discloses(resp, [c.token for c in corpus.canaries])
    assert _S_RAW in recovered, "owner should see the raw-body canary"
    assert _S_EMAIL in recovered, "owner should see the email canary"


def test_pending_disclosure_record_fails_closed_for_grantee():
    """The record never run through the privacy layer has a NULL disclosure column; a grantee
    read must surface the placeholder, never the raw pending secret."""
    corpus = build_sqlite_cer_corpus()
    from tests.evals.privacy.common.sqlite_corpus import _S_PENDING

    resp = _query(QueryPipelineOrchestrator(adapters=corpus.bundle), corpus, mode="raw", grantee=True)
    rows = (resp.get("public_result") or {}).get("rows") or []
    pending = [r for r in rows if r.get("record_id") == "sq-pending" or r.get("message_id") == "sq-pending"]
    # It must not leak the secret; if present at all it is the placeholder.
    assert not response_discloses(resp, [_S_PENDING])
    if pending:
        assert pending[0].get("content") == "[disclosure pending]"


@pytest.mark.parametrize("mode", _GRANTEE_MODES)
def test_full_ingest_mode_leaks_nothing(mode):
    corpus = build_sqlite_cer_corpus()
    tokens = [c.token for c in corpus.canaries]
    resp = _query(QueryPipelineOrchestrator(adapters=corpus.bundle), corpus, mode=mode, grantee=True)
    leaked = response_discloses(resp, tokens)
    assert not leaked, f"grantee {mode} leaked: {leaked}"
