"""SUITE-LEDGER-CONSERVATION — the narrowing ledger has to add up.

protects: every measurement built on the ledger, which by now is most of them. A ledger entry says
a stage dropped N things. That number means nothing on its own: dropped out of how many? Without
the denominator, "the gate dropped 1" and "the gate dropped 63" are the same sentence about a lane
that might have held two candidates or two hundred, and a run whose lanes returned nothing at all
looks identical to a run whose lanes were never asked.

Backlog option H-13 states the invariant as: the sum of per-stage `dropped`, plus what was
returned, equals the candidate count. This audits the ledger against what actually happened by
watching the fusion directly, so the test's own truth does not come from the thing under test.

What it found on 2026-09-09 is written into the assertions: a turn that ANSWERS records nothing at
all — no entries, no candidate count — so conservation is not merely violated, it is unanswerable.
That is the finding, and it is why the checks I have had to hand-build all week (does this fixture
observe anything? did the lane look?) could not be structural.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from topos.features.signal import service as signal_service
from topos.features.signal.vector_settings import rare_token_df_max
from topos.query import narrowing as _N
from topos.query import retrieval as R
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"))
from composition_seed_corpus import build_seeded_corpus  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-ledger-conservation")]

FLOOR = rare_token_df_max() * 10
SOURCES = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
           "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
_FILLER = "shipped the installer work and reviewed the release notes for the project"

#: Asks that reach the fusion on this fixture: one that answers, and ones the gate empties.
_ANSWERING = "What have I been working on lately"
_VETOED = "What did I do recently about zorblatt tourism"


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("conservation") / "n.db"
    build_seeded_corpus(db)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            """INSERT INTO signal_embeddings
               (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,
                text_preview, search_text, chunk_index, event_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (f"f{i}", f"fr{i}", SOURCES[0], "work", "m", "p", 0, _FILLER, _FILLER, 0,
                 (now - timedelta(days=1 + i % 3)).isoformat())
                for i in range(FLOOR + 100)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _audit(db: Path, monkeypatch, query: str) -> Dict[str, Any]:
    """Run one ask and return BOTH stories: what the fusion actually saw, and what the ledger says.

    The candidate count is taken by watching `_rrf_fuse_summary_lists` rather than by reading the
    ledger, so the audit's truth is independent of the record it audits.
    """
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
    seen: Dict[str, int] = {}
    original = R._rrf_fuse_summary_lists

    def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        lists = kwargs.get("lists", args[0] if args else None) or []
        context = kwargs.get("context_sources") or (args[1] if len(args) > 1 else None) or set()
        candidates = [item for name, _, ordered in lists if name not in context for item in ordered]
        seen["candidates"] = seen.get("candidates", 0) + len(candidates)
        out = original(*args, **kwargs)
        seen["returned_by_fusion"] = seen.get("returned_by_fusion", 0) + len(out)
        return out

    monkeypatch.setattr(R, "_rrf_fuse_summary_lists", _spy)
    conn = sqlite3.connect(str(db))
    try:
        ledger = NarrowingLedger()
        bundle = R.DefaultSignalRetrievalAdapter(
            AdapterFactory.create("local_database", conn=conn)
        ).retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("work_context:read"),
                access_mode="summary",
                query_text=query,
                installed_source_ids=list(SOURCES),
                ledger=ledger,
            )
        )
    finally:
        conn.close()
    packet = bundle.context_packet if isinstance(bundle.context_packet, dict) else {}
    entries = [
        {"stage": e.stage, "action": e.action, "reason": e.reason, "dropped": e.dropped}
        for e in ledger.entries
    ]
    return {
        "candidates": seen.get("candidates", 0),
        "returned_by_fusion": seen.get("returned_by_fusion", 0),
        "returned_in_packet": sum(len(v) for v in packet.values() if isinstance(v, list)),
        "entries": entries,
        "dropped_total": sum(e["dropped"] for e in entries if isinstance(e["dropped"], int)),
        "cause": ledger.empty_cause or "answered",
    }


class TestWhatTheLedgerCanAccountFor:
    def test_an_emptied_lane_accounts_for_every_candidate_it_dropped(self, node: Path, monkeypatch) -> None:
        """Where the invariant DOES hold today. The gate empties a lane and says how many it took,
        and that number matches the candidates the fusion actually saw."""
        audit = _audit(node, monkeypatch, _VETOED)
        assert audit["cause"] == _N.CAUSE_GATE_VETOED
        assert audit["candidates"] > 0, "no candidates reached the fusion; this asserts nothing"
        assert audit["dropped_total"] + audit["returned_by_fusion"] == audit["candidates"]

    @pytest.mark.xfail(
        strict=True,
        reason="a turn that ANSWERS records no ledger entries at all, so there is no candidate "
               "count to conserve against — measured 2026-09-09, filed against H-13",
    )
    def test_an_answering_turn_accounts_for_its_candidates_too(self, node: Path, monkeypatch) -> None:
        """What the invariant should mean. `dropped` is a numerator with no denominator anywhere in
        the record: nothing says how many candidates a stage was given. So a lane that returned
        three of two hundred and a lane that returned three of three produce identical ledgers, and
        no consumer can tell a working retrieval from a starved one."""
        audit = _audit(node, monkeypatch, _ANSWERING)
        assert audit["candidates"] > 0
        assert audit["entries"], "an answering turn records nothing at all"
        assert audit["dropped_total"] + audit["returned_by_fusion"] == audit["candidates"]


class TestWhatTheRecordOmits:
    """The gap stated as measurements rather than as a complaint, so it can be tracked."""

    def test_an_answering_turn_leaves_no_trace(self, node: Path, monkeypatch) -> None:
        audit = _audit(node, monkeypatch, _ANSWERING)
        assert audit["returned_in_packet"] > 0
        assert audit["entries"] == [], (
            "an answering turn now records something — good, and this measurement needs rewriting"
        )

    def test_no_entry_anywhere_carries_a_candidate_count(self, node: Path, monkeypatch) -> None:
        """`dropped` is the only quantity the vocabulary has, and it is a loss. There is no field
        for "considered", which is what conservation would need."""
        for query in (_ANSWERING, _VETOED):
            for entry in _audit(node, monkeypatch, query)["entries"]:
                assert set(entry) <= {"stage", "action", "reason", "dropped"}

    def test_the_dropped_number_is_unanchored(self, node: Path, monkeypatch) -> None:
        """The concrete consequence. Two asks on the SAME corpus: one is answered from twelve
        candidates, the other refused after dropping a handful. Read from the ledger alone, the
        second says only "1 dropped" — a number that could describe a lane holding one candidate or
        a thousand."""
        answered = _audit(node, monkeypatch, _ANSWERING)
        vetoed = _audit(node, monkeypatch, "What did I work on this week")
        assert answered["returned_in_packet"] > 0 and not answered["entries"]
        assert vetoed["cause"] == _N.CAUSE_GATE_VETOED
        drops = [e["dropped"] for e in vetoed["entries"] if isinstance(e["dropped"], int)]
        assert drops, "the veto records no count at all"
        # Nothing in either record says how many candidates existed. That is the whole finding.
        assert all("considered" not in e for e in vetoed["entries"])
