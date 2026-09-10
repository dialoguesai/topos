"""SUITE-LANE-FAULT — when a retrieval lane throws, the owner must not be told "no match".

protects: SYS-query I1. A lane that raises has produced no evidence, which is a different thing
from a lane that looked and found nothing. Thirteen loaders in `retrieval.py` end in
`except Exception: return []`, and a swallowed exception is indistinguishable at the seam from an
honest empty — the severed-wire pattern from 1.3.23, where a broken lane read as absent data.

This is backlog option H-15's measurement half: inject a fault into every one of those loaders in
turn and record what the owner would be told. Nothing here changes the product. The numbers say
whether a mis-caused state exists and therefore whether a change is licensed at all.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

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

pytestmark = [pytest.mark.check("C-quality-lane-fault-injection")]

FLOOR = rare_token_df_max() * 10
SOURCES = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
           "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
_FILLER = "shipped the installer work and reviewed the release notes for the project"

#: Every loader in `retrieval.py` whose exception handler returns `[]`. Enumerated by hand from the
#: source so that a NEW swallowing loader has to be added here deliberately; the first test below
#: fails when the source grows one this list does not name.
SWALLOWING_LOADERS: List[str] = [
    "_load_user_goal_summaries",
    "_entity_thread_entities",
    "_goal_entity_ids",
    "_load_emotion_summary_items",
    "_load_complexity_summary_items",
    "_load_attention_summary_items",
    "_load_time_summary_items",
    "_load_brief_summary_items",
    "_semantic_hits",
    "_load_ranked_clusters_unfiltered",
    "_load_fact_store_items",
    "_load_stat_insight_items",
    "_load_recent_summary_items",
]

#: An ordinary ask that a working node answers, so a difference is the fault and not the question.
_ASK = "What have I been working on lately"


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("lane-fault") / "n.db"
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


def _ask(db: Path, monkeypatch, loader: str | None, mode: str = "raise") -> Dict[str, object]:
    """Run the ask with one loader faulted.

    `mode="raise"` replaces the loader with one that throws, which removes ITS OWN handler and so
    measures whether anything upstream catches it. `mode="empty"` replaces it with one that returns
    `[]`, which is exactly what its own `except Exception: return []` does — and is therefore the
    faithful simulation of a swallowed fault. The second is the one H-15 is about: the lane crashed
    and the seam cannot tell.
    """
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
    called = {"n": 0}
    if loader is not None:
        if mode == "raise":
            def _fault(*args, **kwargs):  # noqa: ANN002, ANN003
                called["n"] += 1
                raise RuntimeError(f"injected fault in {loader}")
        else:
            def _fault(*args, **kwargs):  # noqa: ANN002, ANN003
                called["n"] += 1
                return []
        monkeypatch.setattr(R, loader, _fault)
    conn = sqlite3.connect(str(db))
    try:
        ledger = NarrowingLedger()
        try:
            bundle = R.DefaultSignalRetrievalAdapter(
                AdapterFactory.create("local_database", conn=conn)
            ).retrieve(
                RetrievalRequest(
                    manifest=resolve_scope_manifest("work_context:read"),
                    access_mode="summary",
                    query_text=_ASK,
                    installed_source_ids=list(SOURCES),
                    ledger=ledger,
                )
            )
        except Exception as exc:  # noqa: BLE001 — that IS the measurement
            return {"escaped": True, "detail": f"{type(exc).__name__}", "called": called["n"],
                    "items": 0, "cause": "(exception escaped retrieve)", "reasons": []}
        packet = bundle.context_packet if isinstance(bundle.context_packet, dict) else {}
        return {
            "escaped": False,
            "called": called["n"],
            "items": sum(len(v) for v in packet.values() if isinstance(v, list)),
            "cause": ledger.empty_cause or "answered",
            "error": bool(bundle.error),
            "reasons": [e.reason for e in ledger.entries],
        }
    finally:
        conn.close()


class TestTheInventoryIsHonest:
    def test_every_swallowing_loader_in_the_source_is_named_here(self) -> None:
        """The list above is the test's own coverage claim. A loader that starts swallowing and is
        not listed would be silently unmeasured, which is the failure this file is about."""
        import re

        src = Path(R.__file__).read_text(encoding="utf-8").splitlines()
        found = []
        for i, line in enumerate(src):
            if not re.match(r"\s+except Exception", line):
                continue
            if not any(re.match(r"\s+return \[\]", src[j]) for j in range(i + 1, min(i + 6, len(src)))):
                continue
            for k in range(i, -1, -1):
                m = re.match(r"(?:def|    def) (_?\w+)", src[k])
                if m:
                    found.append(m.group(1))
                    break
        missing = sorted(set(found) - set(SWALLOWING_LOADERS))
        assert not missing, f"loaders that swallow but are not measured here: {missing}"

    def test_the_control_answers(self, node: Path, monkeypatch) -> None:
        """Without a fault the ask is answered, so every difference below is the fault."""
        result = _ask(node, monkeypatch, None)
        assert result["items"] > 0 and result["cause"] == "answered"


class TestAWholesaleEmptyLaneIsStillAnAbsence:
    """These replace a loader WHOLESALE with one returning `[]`, so no exception is ever raised:
    that is a lane that genuinely found nothing, and it must still read as an absence. Until
    2026-09-10 a real fault read exactly the same way; since then every handler records its fault,
    and `test_lane_fault_reporting.py` pins what the owner is told when a lane actually breaks."""

    @pytest.mark.parametrize("loader", SWALLOWING_LOADERS)
    def test_the_ledger_says_nothing_about_the_failure(
        self, node: Path, monkeypatch, loader: str
    ) -> None:
        result = _ask(node, monkeypatch, loader, mode="empty")
        if not result["called"]:
            pytest.skip(f"{loader} is not on this ask's path")
        reasons = " ".join(str(r) for r in result["reasons"])
        assert "error" not in reasons and "fail" not in reasons, (
            f"{loader}: something already records lane failure — {result['reasons']}"
        )

    def test_when_every_lane_swallows_the_owner_is_told_an_absence(
        self, node: Path, monkeypatch
    ) -> None:
        """When every lane returns nothing WITHOUT faulting, the honest answer is an absence and
        the ledger has no failure to report. (The same shape with real faults was the 2026-09-09
        harm, and is now reported: `test_lane_fault_reporting.py`.)"""
        monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
        for loader in SWALLOWING_LOADERS:
            monkeypatch.setattr(R, loader, lambda *a, **k: [])
        conn = sqlite3.connect(str(node))
        try:
            ledger = NarrowingLedger()
            bundle = R.DefaultSignalRetrievalAdapter(
                AdapterFactory.create("local_database", conn=conn)
            ).retrieve(
                RetrievalRequest(
                    manifest=resolve_scope_manifest("work_context:read"),
                    access_mode="summary",
                    query_text=_ASK,
                    installed_source_ids=list(SOURCES),
                    ledger=ledger,
                )
            )
        finally:
            conn.close()
        packet = bundle.context_packet if isinstance(bundle.context_packet, dict) else {}
        assert sum(len(v) for v in packet.values() if isinstance(v, list)) == 0
        assert ledger.empty_cause in (_N.CAUSE_STORE_EMPTY, _N.CAUSE_NO_MATCH, _N.CAUSE_GATE_VETOED)
        reasons = " ".join(str(e.reason) for e in ledger.entries)
        assert "error" not in reasons and "fail" not in reasons, (
            f"a lane failure IS recorded after all: {reasons}"
        )


class TestAFaultThatEscapesItsOwnHandler:
    """The other half: with the loader's own handler out of the way, does anything upstream catch
    it? Recorded per loader, because "the query 500s" and "the query quietly empties" are different
    products and the difference is currently an accident of which lane broke."""

    @pytest.mark.parametrize("loader", SWALLOWING_LOADERS)
    def test_recorded(self, node: Path, monkeypatch, loader: str, record_property) -> None:
        result = _ask(node, monkeypatch, loader, mode="raise")
        state = ("escaped-retrieve" if result["escaped"]
                 else "not-called" if not result["called"]
                 else "caught-upstream")
        record_property(loader, state)
        print(f"\n  {loader:36} {state}  (cause={result['cause']})")
        assert state in ("escaped-retrieve", "not-called", "caught-upstream")
