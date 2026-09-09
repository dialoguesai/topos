"""SUITE-INDEX-LAG — a node that is still indexing says so, instead of saying you never asked.

protects: G-ttfa-a2. An empty lane has several honest explanations and they are not
interchangeable. "Nothing in your data mentions that" (`gate_vetoed`, `no_match`) and "connect a
source" (`store_empty`) are both wrong when the rows are already on the node and the embeddings
are not — the true answer is "ask again shortly".

This is not an edge case. Embeddings are written by the enrichment path AFTER ingestion, so a node
sits in exactly this state between its first import and the end of its first enrichment pass, which
is when someone asks their first question. The corpus-density sweep measured what that window does
today: below the index floor the gate is off entirely, and above it a subject whose rows exist but
are not yet embedded is refused as absent.

`index_incomplete` is the typed cause for it, and precedence does the arbitration: it outranks the
three explanations above and yields to `scope_denied` and `engine_failed`, which are about whether
the question ran at all rather than about what the node holds.
"""

from __future__ import annotations

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

pytestmark = [pytest.mark.check("C-quality-index-lag-honesty")]

FLOOR = rare_token_df_max() * 10
SOURCES = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
           "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
_FILLER = "shipped the installer work and reviewed the release notes for the project"
_ASK = "What did I do recently about zorblatt tourism"


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


def _node(tmp: Path, *, job: tuple[str, str] | None, tag: str) -> Path:
    """A migrated node above the index floor, optionally with a pipeline job in `job` state."""
    db = tmp / f"lag-{tag}.db"
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
        if job is not None:
            status, source_id = job
            conn.execute(
                """INSERT INTO pipeline_jobs (job_id, kind, status, source_id, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"job-{tag}", "embeddings", status, source_id, "{}"),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _cause(db: Path, monkeypatch, query: str = _ASK) -> str:
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
    conn = sqlite3.connect(str(db))
    try:
        ledger = NarrowingLedger()
        R.DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn)).retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("work_context:read"),
                access_mode="summary",
                query_text=query,
                installed_source_ids=list(SOURCES),
                ledger=ledger,
            )
        )
        return ledger.empty_cause or "answered"
    finally:
        conn.close()


class TestTheVocabulary:
    def test_the_cause_outranks_the_three_it_explains(self) -> None:
        order = _N._CAUSE_PRECEDENCE
        for weaker in (_N.CAUSE_GATE_VETOED, _N.CAUSE_NO_MATCH, _N.CAUSE_STORE_EMPTY):
            assert order.index(_N.CAUSE_INDEX_INCOMPLETE) < order.index(weaker)

    def test_it_yields_to_a_denial_and_a_relay_failure(self) -> None:
        """Those two are about whether the question ran; this one is about what the node holds."""
        order = _N._CAUSE_PRECEDENCE
        for stronger in (_N.CAUSE_SCOPE_DENIED, _N.CAUSE_ENGINE_FAILED):
            assert order.index(stronger) < order.index(_N.CAUSE_INDEX_INCOMPLETE)

    def test_a_ledger_upgrades_a_weaker_cause_but_not_a_stronger_one(self) -> None:
        weak = NarrowingLedger()
        weak.empty(_N.CAUSE_GATE_VETOED, stage=_N.STAGE_RARE_GATE, reason="rare_token_unevidenced")
        weak.empty(_N.CAUSE_INDEX_INCOMPLETE, stage=_N.STAGE_RETRIEVAL, reason="index_job_in_flight")
        assert weak.empty_cause == _N.CAUSE_INDEX_INCOMPLETE
        strong = NarrowingLedger()
        strong.empty(_N.CAUSE_SCOPE_DENIED, stage=_N.STAGE_GRANT, reason="scope_not_granted")
        strong.empty(_N.CAUSE_INDEX_INCOMPLETE, stage=_N.STAGE_RETRIEVAL, reason="index_job_in_flight")
        assert strong.empty_cause == _N.CAUSE_SCOPE_DENIED


class TestTheNodeSaysWhenItIsStillIndexing:
    @pytest.mark.parametrize("status", ["queued", "running"])
    def test_an_unfinished_job_for_this_scopes_source_explains_the_empty(
        self, tmp_path: Path, monkeypatch, status: str
    ) -> None:
        db = _node(tmp_path, job=(status, SOURCES[0]), tag=status)
        assert _cause(db, monkeypatch) == _N.CAUSE_INDEX_INCOMPLETE

    @pytest.mark.parametrize("status", ["done", "failed"])
    def test_a_finished_job_explains_nothing(self, tmp_path: Path, monkeypatch, status: str) -> None:
        """A job that has stopped is not a reason to promise the answer will improve."""
        db = _node(tmp_path, job=(status, SOURCES[0]), tag=status)
        assert _cause(db, monkeypatch) == _N.CAUSE_GATE_VETOED

    def test_a_job_on_a_source_this_scope_cannot_read_explains_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The mistake the density sweep found elsewhere in the gate, refused here: work happening
        on a source outside the scope cannot explain this scope's empty."""
        db = _node(tmp_path, job=("running", "demo_messenger_file"), tag="out-of-scope")
        assert _cause(db, monkeypatch) == _N.CAUSE_GATE_VETOED

    def test_no_job_at_all_leaves_the_cause_alone(self, tmp_path: Path, monkeypatch) -> None:
        db = _node(tmp_path, job=None, tag="none")
        assert _cause(db, monkeypatch) == _N.CAUSE_GATE_VETOED

    def test_an_answered_ask_is_not_relabelled(self, tmp_path: Path, monkeypatch) -> None:
        """The stamp reads the result, not just the ledger: a lane that returned rows has no empty
        to explain, however much indexing is in flight."""
        db = _node(tmp_path, job=("running", SOURCES[0]), tag="answered")
        assert _cause(db, monkeypatch, "What have I been working on lately") == "answered"


class TestItCannotBreakARetrieval:
    def test_a_node_with_no_pipeline_jobs_table_is_unaffected(self, tmp_path: Path) -> None:
        """Older databases and every fixture that predates the table: nothing in flight."""
        conn = sqlite3.connect(":memory:")
        assert R._index_jobs_in_flight(conn, list(SOURCES)) is False
        assert R._index_jobs_in_flight(None, list(SOURCES)) is False
        assert R._index_jobs_in_flight(conn, []) is False
