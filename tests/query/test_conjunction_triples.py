"""SUITE-CONJUNCTION — asking two things at once must not lose the half that has an answer.

protects: SYS-query I1, and the per-part gate specifically. The triple is A, B, and "A and B": if A
answers and B does not, the conjunction must still answer A's half. A gate that reads the whole
request as one bag of words does the opposite — one unanswerable clause empties the lane for the
one that was fine, which is the multi-section weekly report failing because one of its six sections
named something the store has never heard of.

Backlog option H-05. Two typographies, because the option asks for both and because they turn out
not to be the variable that matters.

WHAT THIS MEASURES, stated plainly because it is the whole result: the per-part gate works, and it
is entirely dependent on the CALLER sending `retrieval_parts`. The engine does not segment a
request on its own. Send the parts and a conjunction answers its answerable half while recording a
partial veto for the other; send nothing and the same sentence, in any typography, is emptied. So
the protection is real and it is opt-in, and a client that has not been taught to segment loses
half of every mixed question its owner asks.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

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

pytestmark = [pytest.mark.check("C-quality-conjunction-triples")]

FLOOR = rare_token_df_max() * 10
SOURCES = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
           "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
_FILLER = "shipped the installer work and reviewed the release notes for the project"

#: The two halves. A is answerable on this corpus; B names something it has never held.
A = "What have I been working on lately"
B = "What did I do recently about zorblatt tourism"

#: The same conjunction, written three ways.
TYPOGRAPHIES = {
    "one sentence": f"{A} and what I did recently about zorblatt tourism",
    "numbered": f"1) {A}\n2) {B}",
    "bulleted": f"- {A}\n- {B}",
}


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("conjunction") / "n.db"
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


def _ask(db: Path, monkeypatch, query: str, parts: Optional[List[str]] = None) -> Tuple[int, str, List[str]]:
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
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
                needle_parts=parts,
                installed_source_ids=list(SOURCES),
                ledger=ledger,
            )
        )
        packet = bundle.context_packet if isinstance(bundle.context_packet, dict) else {}
        items = sum(len(v) for v in packet.values() if isinstance(v, list))
        return items, (ledger.empty_cause or "answered"), [e.reason for e in ledger.entries]
    finally:
        conn.close()


class TestTheTwoHalves:
    """The floor. Without these the triple below proves nothing."""

    def test_a_answers(self, node: Path, monkeypatch) -> None:
        items, cause, _ = _ask(node, monkeypatch, A)
        assert items > 0 and cause == "answered"

    def test_b_does_not(self, node: Path, monkeypatch) -> None:
        items, cause, _ = _ask(node, monkeypatch, B)
        assert items == 0 and cause == _N.CAUSE_GATE_VETOED


class TestWithoutPartsTheAnswerableHalfIsLost:
    """The failure, in every typography. This is what a client that does not segment produces."""

    @pytest.mark.parametrize("typography", sorted(TYPOGRAPHIES))
    def test_the_conjunction_is_emptied(self, node: Path, monkeypatch, typography: str) -> None:
        items, cause, _ = _ask(node, monkeypatch, TYPOGRAPHIES[typography])
        assert items == 0 and cause == _N.CAUSE_GATE_VETOED

    def test_the_typography_is_not_the_variable(self, node: Path, monkeypatch) -> None:
        """Numbered sections do not rescue it. The engine does not segment on its own, so a
        beautifully structured request gates exactly like a run-on sentence."""
        causes = {t: _ask(node, monkeypatch, q)[1] for t, q in TYPOGRAPHIES.items()}
        assert len(set(causes.values())) == 1, causes


class TestWithPartsTheGateProtectsTheHalfThatWorks:
    """The mechanism, doing its job. `_veto_for` runs per part and empties the lane only when EVERY
    part is unanswerable — so the conjunction keeps A's evidence and says B's half was vetoed."""

    @pytest.mark.parametrize("typography", ["numbered", "bulleted"])
    def test_the_conjunction_answers_and_records_a_partial_veto(
        self, node: Path, monkeypatch, typography: str
    ) -> None:
        items, cause, reasons = _ask(node, monkeypatch, TYPOGRAPHIES[typography], parts=[A, B])
        assert items > 0 and cause == "answered"
        assert "rare_gate_partial_veto" in reasons, reasons

    def test_it_keeps_exactly_what_the_answerable_half_had(self, node: Path, monkeypatch) -> None:
        """Not merely non-empty: the conjunction returns what A alone returned. A gate that kept
        'something' would pass a weaker test while still losing evidence."""
        alone, _, _ = _ask(node, monkeypatch, A)
        together, _, _ = _ask(node, monkeypatch, TYPOGRAPHIES["numbered"], parts=[A, B])
        assert together == alone

    def test_two_unanswerable_parts_still_empty_the_lane(self, node: Path, monkeypatch) -> None:
        """The other side of the rule, so the protection is not just 'never veto a multi-part ask'.
        When no part can be answered, the honest empty survives."""
        items, cause, _ = _ask(node, monkeypatch, f"1) {B}\n2) {B}", parts=[B, B])
        assert items == 0 and cause == _N.CAUSE_GATE_VETOED


class TestTheProtectionIsOptIn:
    def test_the_same_request_differs_only_by_whether_parts_were_sent(
        self, node: Path, monkeypatch
    ) -> None:
        """The finding, as one assertion. Identical text, identical corpus, identical scope — and
        the verdict turns entirely on whether the caller segmented the request. The engine does not
        do it for them, so this is a property of the client, not of the question."""
        query = TYPOGRAPHIES["numbered"]
        without = _ask(node, monkeypatch, query)
        with_parts = _ask(node, monkeypatch, query, parts=[A, B])
        assert without[1] == _N.CAUSE_GATE_VETOED and without[0] == 0
        assert with_parts[1] == "answered" and with_parts[0] > 0
