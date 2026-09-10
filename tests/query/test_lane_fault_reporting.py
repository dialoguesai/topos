"""SUITE-LANE-FAULT-REPORTING — a lane that broke says it broke.

protects: SYS-query I1. Fourteen handlers in `retrieval.py` end in `except Exception: return []`.
Measured 2026-09-09 (`test_lane_fault_injection.py`): a swallowed crash and an honest empty were
the same value at the seam, so with every lane faulted, the state a shared dependency going down
produces, the owner was told their data held nothing and the ledger said nothing about a failure.

Now every handler records its fault, `retrieve()` writes one `lane_error` entry per faulted lane,
and an EMPTY result is stamped `engine_failed`, which the app already turns into "try again". Two
things are deliberately NOT faults: a store that is simply not on this node (`no such table`, `no
such module`) stays an honest absence, and a lane that failed while others still answered is
recorded without overriding the answer.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
_ASK = "What have I been working on lately"

LANE_FOR = {
    "_load_user_goal_summaries": "goals", "_entity_thread_entities": "entity_thread",
    "_goal_entity_ids": "commitment_goal_entities", "_load_emotion_summary_items": "emotions",
    "_load_complexity_summary_items": "complexity", "_load_attention_summary_items": "attention",
    "_load_time_summary_items": "time", "_load_brief_summary_items": "briefs",
    "_semantic_hits": "vector", "_load_ranked_clusters_unfiltered": "clusters",
    "_load_fact_store_items": "facts_store", "_load_stat_insight_items": "stat_insights",
    "_load_recent_summary_items": "recent",
}


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("lane-report") / "n.db"
    build_seeded_corpus(db)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            """INSERT INTO signal_embeddings
               (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,
                text_preview, search_text, chunk_index, event_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(f"f{i}", f"fr{i}", SOURCES[0], "work", "m", "p", 0, _FILLER, _FILLER, 0,
              (now - timedelta(days=1 + i % 3)).isoformat()) for i in range(FLOOR + 100)],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _faulting(lane: str, exc: BaseException):
    """A loader whose own handler has just swallowed `exc`: exactly what the real handlers do."""
    def _loader(*args, **kwargs):  # noqa: ANN002, ANN003
        R._note_lane_fault(lane, exc)
        return ([], str(exc)) if lane == "vector" else []
    return _loader


def _ask(db: Path, monkeypatch):
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
    conn = sqlite3.connect(str(db))
    try:
        ledger = NarrowingLedger()
        bundle = R.DefaultSignalRetrievalAdapter(
            AdapterFactory.create("local_database", conn=conn)
        ).retrieve(RetrievalRequest(
            manifest=resolve_scope_manifest("work_context:read"), access_mode="summary",
            query_text=_ASK, installed_source_ids=list(SOURCES), ledger=ledger,
        ))
        packet = bundle.context_packet if isinstance(bundle.context_packet, dict) else {}
        return sum(len(v) for v in packet.values() if isinstance(v, list)), ledger
    finally:
        conn.close()


class TestTheClassifier:
    @pytest.mark.parametrize("message", ["no such table: message_emotions", "no such module: vec0"])
    def test_a_store_that_is_not_on_this_node_is_an_absence(self, message: str) -> None:
        assert R._lane_absent(sqlite3.OperationalError(message))

    @pytest.mark.parametrize("exc", [
        sqlite3.OperationalError("database is locked"),
        sqlite3.ProgrammingError("Cannot operate on a closed database."),
        sqlite3.OperationalError("no such column: event_at"),
        RuntimeError("database_unavailable"),
        KeyError("summary_text"),
    ], ids=["locked", "closed", "schema-mismatch", "unavailable", "bug"])
    def test_everything_else_is_a_fault(self, exc: BaseException) -> None:
        assert not R._lane_absent(exc)


class TestEveryHandlerNotesItsFault:
    def test_no_swallowing_handler_in_retrieval_skips_the_note(self) -> None:
        """Structural, so the next loader that learns to swallow cannot forget to say so."""
        src = Path(R.__file__).read_text(encoding="utf-8").splitlines()
        missing = []
        for i, line in enumerate(src):
            if not re.match(r"\s+except Exception", line):
                continue
            ret = next((j for j in range(i + 1, min(i + 7, len(src)))
                        if re.match(r"\s+return \[\]", src[j])), None)
            if ret is None:
                continue
            if not any("_note_lane_fault(" in src[k] for k in range(i + 1, ret)):
                missing.append(i + 1)
        assert not missing, f"swallowing handlers that do not note their fault, at lines {missing}"


class TestARealLoaderNotesARealFault:
    def test_a_closed_connection_is_recorded_as_a_fault(self) -> None:
        closed = sqlite3.connect(":memory:"); closed.close()
        token = R._LANE_FAULTS.set({})
        try:
            assert R._load_user_goal_summaries("work", conn=closed) == []
            assert R._LANE_FAULTS.get() == {"goals": "ProgrammingError"}
        finally:
            R._LANE_FAULTS.reset(token)

    def test_a_node_without_the_table_is_not(self) -> None:
        token = R._LANE_FAULTS.set({})
        try:
            assert R._load_user_goal_summaries("work", conn=sqlite3.connect(":memory:")) == []
            assert R._LANE_FAULTS.get() == {}
        finally:
            R._LANE_FAULTS.reset(token)

    def test_outside_a_retrieval_nothing_is_recorded_and_nothing_raises(self) -> None:
        closed = sqlite3.connect(":memory:"); closed.close()
        assert R._LANE_FAULTS.get() is None
        assert R._load_user_goal_summaries("work", conn=closed) == []


class TestWhatTheOwnerIsTold:
    def test_when_every_lane_faults_the_empty_is_a_failure_not_an_absence(
        self, node: Path, monkeypatch
    ) -> None:
        """The 2026-09-09 harm, now reported. The app turns `engine_failed` into "try again"."""
        for fn, lane in LANE_FOR.items():
            monkeypatch.setattr(R, fn, _faulting(lane, sqlite3.OperationalError("database is locked")))
        items, ledger = _ask(node, monkeypatch)
        assert items == 0
        assert ledger.empty_cause == _N.CAUSE_ENGINE_FAILED
        assert "lane_error" in [e.reason for e in ledger.entries]

    def test_a_lane_that_fails_while_others_answer_is_recorded_not_claimed(
        self, node: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(R, "_load_stat_insight_items",
                            _faulting("stat_insights", RuntimeError("database_unavailable")))
        items, ledger = _ask(node, monkeypatch)
        assert items > 0 and ledger.empty_cause is None
        assert [e.reason for e in ledger.entries].count("lane_error") == 1

    def test_stores_that_are_simply_absent_stay_an_absence(self, node: Path, monkeypatch) -> None:
        """The over-correction this guards against: a young node without optional stores must not
        be told "try again" about stores it will never have."""
        for fn, lane in LANE_FOR.items():
            monkeypatch.setattr(R, fn, _faulting(lane, sqlite3.OperationalError("no such table: x")))
        items, ledger = _ask(node, monkeypatch)
        assert items == 0
        assert ledger.empty_cause in (_N.CAUSE_STORE_EMPTY, _N.CAUSE_NO_MATCH, _N.CAUSE_GATE_VETOED)
        assert "lane_error" not in [e.reason for e in ledger.entries]

    def test_nothing_about_the_failure_leaves_the_node_but_the_closed_reason(
        self, node: Path, monkeypatch
    ) -> None:
        for fn, lane in LANE_FOR.items():
            monkeypatch.setattr(R, fn, _faulting(lane, RuntimeError("row summary_text='a private note'")))
        _, ledger = _ask(node, monkeypatch)
        public = json.dumps(ledger.as_public())
        assert "lane_error" in public
        assert "private note" not in public and "RuntimeError" not in public and "detail" not in public
