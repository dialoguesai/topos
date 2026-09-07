"""SUITE-DENSITY-STRATA — the gate's verdict must be a property of the ask, not of the corpus.

protects: G-ttfa-a2 (first answer on a small node) and SYS-query I1 read two-sided. A one-week-old
node must not refuse a class a three-year node answers.

The abstention gate reads document frequency, and df is a property of the CORPUS. Two thresholds
in `_rare_tokens` / `_veto_for` therefore make the owner's experience depend on how much data they
have rather than on what they asked:

  * the corpus guard — below `rare_token_df_max() * 10` indexed rows (300 by default) the gate is
    switched off entirely and nothing can be vetoed;
  * the veto rule — above it, an unevidenced token with df <= 2 empties the lane.

This file measures both boundaries and pins what each side means for the owner. It is a SWEEP, not
a tuning lane: nothing here moves a threshold, and a threshold may only ever move with a measured
flip curve behind it (research wiki `Harness_Failure_Taxonomy_Eval_Backlog`, option H-02).

THE INSTRUMENT. A veto needs three things at once, and the obvious fixture supplies only two: an
evidence lane must return candidate items (lanes named in `context_sources` do not count), a rare
token must be present, and that token must be unevidenced among the returned items. A fabricated
subject on its own retrieves nothing, so the fusion returns `store_empty` BEFORE the gate runs —
measured 2026-09-06, that is why a seeded-corpus negative reads `store_empty` and never
`gate_vetoed`. The probe below therefore uses a MIXED ask: words that make the goals lane return a
row, plus a rare token that row cannot evidence. That is also the shape of the live incident.

The subject word is invented and checked to be absent from the seeded corpus, because the corpus
already contains a "kayak spend" insight — a first draft of this sweep used "kayaking" and measured
nothing, since `_token_variants` evidenced it through "kayak".
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

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
from transform_eval_cases import ANSWERABLE_CASES  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-corpus-density-strata")]

#: Ordinary working-life filler. No form words, no proper nouns — padding must add density
#: without adding subjects, or the sweep measures the filler instead of the threshold.
_FILLER = [
    "shipped the installer work and reviewed the release notes",
    "meeting with the team about the roadmap and this month's priorities",
    "journal entry about the week and what I finished",
    "tracked progress on goals and updated the status",
]

#: A subject the seeded corpus does not contain, in any morphological variant.
_SUBJECT = "spelunking"

#: An ask that makes an evidence lane return a row AND carries the rare subject. Both halves are
#: load-bearing: without the first the fusion never reaches the gate, without the second there is
#: nothing to veto.
_MIXED_ASK = f"What have I been working on lately and my {_SUBJECT}"

#: A wholly fabricated subject, framed with recency. The recency word is not decoration: the
#: `recent` lane counts as CONTEXT rather than evidence unless the ask carries recency intent, so
#: without it this ask cannot reach the gate either. With it, this is the false-PRESENCE probe —
#: the ask has no honest answer, and what the node does with it is the other half of the ledger.
_FABRICATED_ASK = "What did I do recently about zorblatt tourism"

WORK_CONTEXT_SOURCES = [
    "chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
    "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file",
]

#: The corpus guard's own value, so the strata move if the setting does.
FLOOR = rare_token_df_max() * 10


class _NoSemanticLane:
    """The vector lane stubbed to empty: this sweep observes the canonical and derived lanes plus
    the gate, which is where density enters."""

    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        def _empty(*args, **kwargs):  # noqa: ANN002, ANN003
            return {"items": [], "total": 0}
        return _empty


#: Filler rows carry `chunk_index = 0` and a RECENT `event_at` on purpose. Without them a row
#: raises the FTS count — switching the gate on — while remaining invisible to every evidence lane,
#: so the empty is stamped `store_empty` at the branch ABOVE the gate and the sweep measures
#: nothing. `_load_recent_summary_items` selects on exactly these two columns within the last 14
#: days. This is the difference between a fixture that can observe a veto and one that cannot.
_RECENT_DAYS = 3


def _rows(prefix: str, sentences: List[str], count: int, now: datetime) -> List[tuple]:
    return [
        (
            f"{prefix}{i}", f"{prefix}r{i}", "chatgpt_ingestion", "work", "m", "p", 0,
            sentences[i % len(sentences)], sentences[i % len(sentences)],
            0, (now - timedelta(days=1 + (i % _RECENT_DAYS))).isoformat(),
        )
        for i in range(count)
    ]


_INSERT = """INSERT INTO signal_embeddings
             (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,
              text_preview, search_text, chunk_index, event_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _build(tmp: Path, total_rows: int, subject_rows: int = 0) -> Path:
    """A migrated node with `total_rows` indexed rows, `subject_rows` of which mention the subject."""
    db = tmp / f"n{total_rows}-{subject_rows}.db"
    build_seeded_corpus(db)
    if total_rows:
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(db))
        try:
            sentence = f"went {_SUBJECT} in the caves at the weekend with friends"
            rows = _rows("f", _FILLER, max(0, total_rows - subject_rows), now)
            rows += _rows("s", [sentence], subject_rows, now)
            conn.executemany(_INSERT, rows)
            conn.commit()
        finally:
            conn.close()
    return db


def _ask(db: Path, query: str, monkeypatch) -> Tuple[int, str]:
    """(items returned, the ledger's empty cause or 'answered')."""
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
                installed_source_ids=list(WORK_CONTEXT_SOURCES),
                ledger=ledger,
            )
        )
        assert not bundle.error, bundle.error
        items = len(bundle.context_packet.get("summaries") or [])
        return items, (ledger.empty_cause or "answered")
    finally:
        conn.close()


# ---------------------------------------------------------------- the instrument


class TestTheFixtureCanMeasureAVeto:
    def test_the_subject_is_absent_from_the_seeded_corpus(self, tmp_path: Path) -> None:
        """A word the fixture already contains would be evidenced through its variants and no
        veto could ever fire — which is how the first draft of this sweep measured nothing."""
        db = _build(tmp_path, 0)
        conn = sqlite3.connect(str(db))
        try:
            for variant in R._token_variants(_SUBJECT):
                row = conn.execute(
                    "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                    (f'"{variant}"',),
                ).fetchone()
                assert not row or int(row[0]) == 0, variant
        finally:
            conn.close()

    def test_the_mixed_ask_really_does_veto_above_the_floor(self, tmp_path: Path, monkeypatch) -> None:
        """The self-check that makes every green below mean something."""
        _, cause = _ask(_build(tmp_path, FLOOR + 100), _MIXED_ASK, monkeypatch)
        assert cause == _N.CAUSE_GATE_VETOED

    def test_a_fabricated_ask_with_no_recency_never_reaches_the_gate(self, tmp_path: Path, monkeypatch) -> None:
        """Recorded because it is the trap, and it is not the one it looks like. The `recent` lane
        is CONTEXT unless the ask carries recency intent, and context lanes are excluded from
        `evidence_items` — so the fusion returns `store_empty` at the branch above the gate. A
        catalogue negative as written therefore cannot exercise this gate at any density."""
        _, cause = _ask(_build(tmp_path, FLOOR + 100), "What did I say about zorblatt tourism", monkeypatch)
        assert cause == _N.CAUSE_STORE_EMPTY

    def test_the_fixture_meets_all_three_preconditions_for_a_veto(self, tmp_path: Path, monkeypatch) -> None:
        """A veto needs an evidence lane with rows, a rare token, and that token unevidenced. The
        FTS count alone pins only the second; a fixture can satisfy it and still measure nothing,
        which is what a count-only self-check misses."""
        db = _build(tmp_path, FLOOR + 100)
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT count(*) FROM signal_embeddings_fts").fetchone()[0] >= FLOOR
            # rows an evidence lane can actually select on
            dated = conn.execute(
                "SELECT count(*) FROM signal_embeddings WHERE chunk_index = 0 AND event_at IS NOT NULL"
            ).fetchone()[0]
            assert dated >= FLOOR, "filler is invisible to the recent lane; the gate cannot be reached"
        finally:
            conn.close()
        # and the whole chain, end to end
        _, cause = _ask(db, _FABRICATED_ASK, monkeypatch)
        assert cause == _N.CAUSE_GATE_VETOED


# ---------------------------------------------------------------- the corpus guard


class TestTheCorpusGuardIsADiscontinuity:
    """Below the floor the gate cannot fire; at the floor it can. Both sides are pinned so the
    step is a recorded property of the product rather than a surprise in someone's session."""

    @pytest.mark.parametrize("rows", [0, 50, FLOOR - 1])
    def test_below_the_floor_nothing_can_be_vetoed(self, tmp_path: Path, monkeypatch, rows: int) -> None:
        items, cause = _ask(_build(tmp_path, rows), _MIXED_ASK, monkeypatch)
        assert cause != _N.CAUSE_GATE_VETOED
        # The owner-visible consequence: on the smallest nodes an ask naming something absent is
        # ANSWERED from whatever the lane held. That is the answer-when-you-should-abstain side.
        assert items > 0

    @pytest.mark.parametrize("rows", [FLOOR - 1, FLOOR])
    def test_the_false_presence_side_of_the_same_step(self, tmp_path: Path, monkeypatch, rows: int) -> None:
        """The other half of the ledger, which the abstention discipline says must be reported
        separately. A wholly fabricated subject is ANSWERED below the floor — from rows that have
        nothing to do with it — and refused at the floor. A new owner is therefore the one most
        likely to be handed confident noise, and the same step that fixes it is the one that starts
        refusing real subjects mentioned once."""
        items, cause = _ask(_build(tmp_path, rows), _FABRICATED_ASK, monkeypatch)
        if rows < FLOOR:
            assert cause != _N.CAUSE_GATE_VETOED and items > 0
        else:
            assert cause == _N.CAUSE_GATE_VETOED

    @pytest.mark.parametrize("rows", [FLOOR, FLOOR + 1, FLOOR + 100])
    def test_at_and_above_the_floor_the_gate_is_live(self, tmp_path: Path, monkeypatch, rows: int) -> None:
        _, cause = _ask(_build(tmp_path, rows), _MIXED_ASK, monkeypatch)
        assert cause == _N.CAUSE_GATE_VETOED

    def test_the_step_is_one_row_wide(self, tmp_path: Path, monkeypatch) -> None:
        """Named explicitly: adding a single indexed row flips the same ask from answered to
        refused. Nothing about the ask changed."""
        _, below = _ask(_build(tmp_path, FLOOR - 1), _MIXED_ASK, monkeypatch)
        _, at = _ask(_build(tmp_path, FLOOR), _MIXED_ASK, monkeypatch)
        assert below != at


# ---------------------------------------------------------------- the df boundary


#: What the sweep found on 2026-09-06, kept as the expected curve. df 0 is an honest refusal (the
#: word really is absent); df 1 and 2 refuse a subject the node HAS INDEXED; df 3 and above answer.
_DF_CURVE: Dict[int, str] = {0: "vetoed", 1: "vetoed", 2: "vetoed", 3: "answered", 4: "answered", 10: "answered"}


class TestTheDfBoundary:
    @pytest.mark.parametrize("df", sorted(_DF_CURVE))
    def test_the_curve_at_a_mature_density(self, tmp_path: Path, monkeypatch, df: int) -> None:
        _, cause = _ask(_build(tmp_path, FLOOR + 100, subject_rows=df), _MIXED_ASK, monkeypatch)
        got = "vetoed" if cause == _N.CAUSE_GATE_VETOED else "answered"
        assert got == _DF_CURVE[df], f"df={df}: expected {_DF_CURVE[df]}, got {got} ({cause})"

    @pytest.mark.parametrize("rows", [FLOOR + 1, FLOOR + 100, 2000])
    def test_the_curve_does_not_move_with_corpus_size(self, tmp_path: Path, monkeypatch, rows: int) -> None:
        """The reframing this sweep produced. The `df <= 2` cliff is NOT a small-node problem: it
        bites identically at 301 rows and at 2000. What makes it a fresh-node problem is that on a
        new node almost every subject the owner names has been mentioned once or twice."""
        _, few = _ask(_build(tmp_path, rows, subject_rows=2), _MIXED_ASK, monkeypatch)
        _, many = _ask(_build(tmp_path, rows, subject_rows=3), _MIXED_ASK, monkeypatch)
        assert few == _N.CAUSE_GATE_VETOED and many != _N.CAUSE_GATE_VETOED

    @pytest.mark.xfail(
        strict=True,
        reason="FALSE ABSENCE: a subject indexed in 1-2 rows is refused as if the owner never "
               "mentioned it. Measured 2026-09-06; filed against H-02, not fixed here — H-02 is a "
               "sweep and a threshold moves only with a decision behind it.",
    )
    @pytest.mark.parametrize("df", [1, 2])
    def test_a_subject_the_node_has_indexed_is_not_refused(self, tmp_path: Path, monkeypatch, df: int) -> None:
        """df 0 means absent and refusing is honest. df >= 1 means the word IS in the index: the
        node holds rows mentioning it and did not surface them. Telling the owner their data does
        not mention it is a false absence, and it is the failure a new user meets on almost every
        specific ask, because everything they have is mentioned once."""
        _, cause = _ask(_build(tmp_path, FLOOR + 100, subject_rows=df), _MIXED_ASK, monkeypatch)
        assert cause != _N.CAUSE_GATE_VETOED


# ---------------------------------------------------------------- fresh-node parity


#: The two corpora the same asks are graded against. Both are ordinary working life; they differ
#: only in whether the owner happens to have written the words a transform ask uses for the FORM of
#: its answer ("a LinkedIn post", "an executive summary", "a noir detective monologue"). Nothing
#: about the questions changes between them.
_FILLER_WITHOUT_FORM_WORDS = _FILLER
_FILLER_WITH_FORM_WORDS = _FILLER + [
    "talked with the team, said thank you and shared it on linkedin",
    "listened to a song by the sea, read a detective novel and the newspaper",
    "opened a fortune cookie at dinner and kept the slip",
    "standup: current projects, blockers, impact, next steps and the timeline",
    "tracked progress on goals, updated the status and sent the executive note",
]

#: Measured 2026-09-06. Each of these answerable asks is REFUSED on a corpus that does not happen
#: to contain the word naming its output form, and ANSWERED on one that does. The word is the
#: whole difference; it is unevidenced with df 0, and `_veto_for` empties the lane for any such
#: token even though the rest of the ask is perfectly answerable.
_CORPUS_DEPENDENT_CASES: Dict[str, str] = {
    "TX-A5": "shanty",
    "TX-B1": "executive",
    "TX-B2": "linkedin",
    "TX-C3": "detective",
    "TX-D3": "impact",
    "TX-D5": "timeline",
    "TX-F4": "fortune / cookie",
}


def _build_with(tmp: Path, filler: List[str], total_rows: int, tag: str) -> Path:
    db = tmp / f"{tag}-{total_rows}.db"
    build_seeded_corpus(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(_INSERT, _rows(tag, filler, total_rows, datetime.now(timezone.utc)))
        conn.commit()
    finally:
        conn.close()
    return db


class TestTheVerdictIsAPropertyOfTheCorpusNotTheAsk:
    """The headline measurement. Same question, same code, same density — two corpora that differ
    only in ordinary vocabulary, and seven answerable asks change verdict."""

    @pytest.mark.parametrize("case_id", sorted(_CORPUS_DEPENDENT_CASES))
    def test_the_same_ask_flips_on_vocabulary_alone(self, tmp_path: Path, monkeypatch, case_id: str) -> None:
        query = {c.case_id: c.query for c in ANSWERABLE_CASES}[case_id]
        without = _build_with(tmp_path, _FILLER_WITHOUT_FORM_WORDS, FLOOR + 100, "wo")
        with_ = _build_with(tmp_path, _FILLER_WITH_FORM_WORDS, FLOOR + 100, "wi")
        _, cause_without = _ask(without, query, monkeypatch)
        _, cause_with = _ask(with_, query, monkeypatch)
        assert cause_without == _N.CAUSE_GATE_VETOED, (
            f"{case_id} was expected to be refused on a corpus lacking "
            f"{_CORPUS_DEPENDENT_CASES[case_id]!r}; got {cause_without}"
        )
        assert cause_with != _N.CAUSE_GATE_VETOED, (
            f"{case_id} is refused even where the word is ordinary — the flip is no longer about "
            f"vocabulary and this measurement needs rebuilding"
        )


class TestFreshNodeParity:
    """The property the bundle is named for: an ask a mature node answers, a small node must not
    refuse. Graded per case so the board reads as a flip-rate rather than one red line."""

    @pytest.mark.parametrize(
        "case_id",
        [
            pytest.param(
                c.case_id,
                marks=pytest.mark.xfail(
                    strict=True,
                    reason=(
                        "corpus-dependent veto on a word naming the answer's form "
                        "(see _CORPUS_DEPENDENT_CASES); filed against H-02, not fixed here"
                    ),
                ),
            )
            if c.case_id in _CORPUS_DEPENDENT_CASES
            else c.case_id
            for c in ANSWERABLE_CASES
        ],
    )
    def test_an_answerable_ask_is_not_refused_above_the_floor(
        self, tmp_path: Path, monkeypatch, case_id: str
    ) -> None:
        query = {c.case_id: c.query for c in ANSWERABLE_CASES}[case_id]
        _, cause = _ask(_build(tmp_path, FLOOR + 100), query, monkeypatch)
        assert cause != _N.CAUSE_GATE_VETOED

    @pytest.mark.parametrize("rows", [0, 50, FLOOR - 1])
    def test_below_the_floor_no_answerable_ask_is_refused(
        self, tmp_path: Path, monkeypatch, rows: int
    ) -> None:
        """The one thing the smallest nodes get right for free: with the gate off, nothing is
        refused. The cost is on the other side of the ledger, pinned above."""
        db = _build(tmp_path, rows)
        vetoed = [c.case_id for c in ANSWERABLE_CASES if _ask(db, c.query, monkeypatch)[1] == _N.CAUSE_GATE_VETOED]
        assert not vetoed, vetoed
