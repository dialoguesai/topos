"""SUITE-RESIDUAL-VETO — how many form words the exemption list does not know about.

protects: SYS-query I1. `_OUTPUT_SHAPE_TOKENS` exempts words that name the FORM of an answer from
the abstention gate, because an instruction is not a subject. It is a hand-curated closed list, and
the density sweep already found seven catalogue asks refused on words it does not hold —
`linkedin`, `executive`, `detective`, `fortune`, `timeline`, `shanty`, `impact`. This measures the
rate rather than collecting anecdotes: template plausible form words onto a control ask that is
known to answer, and count how many empty the lane.

That number is the argument about mechanism. A low rate says the list is nearly complete and
curation is working; a high rate says the list is the wrong shape for the job, and the answer is a
compiler that knows an instruction from a subject rather than a longer list of words. Backlog
option H-07 exists to turn that argument into a figure, and H-26 is the option it gates.

The words below are hand-written, not drawn from a corpus: this repository is public and a wordlist
dependency is not worth a network fetch. They are ordinary English nouns and verbs for the form of
an answer, chosen before any of them was run, and every one is checked to be outside the exemption
list at test time — so the sample cannot be quietly tuned by the list growing underneath it.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

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

pytestmark = [pytest.mark.check("C-quality-residual-veto-fuzz")]

FLOOR = rare_token_df_max() * 10
SOURCES = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
           "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
_FILLER = "shipped the installer work and reviewed the release notes for the project"

#: The control. Known to answer on this fixture, so a refusal below is the templated word.
_CONTROL = "What have I been working on lately"
#: The instruction template. Deliberately the plainest possible wrapper, so nothing but the word
#: under test differs from the control.
_TEMPLATE = "Take what I have been working on lately and put it into a {word}"

#: Form and genre nouns a person might plausibly ask for. Written by hand, before running any.
_FORM_WORDS: List[str] = [
    "eulogy", "sermon", "haiku", "playlist", "epitaph", "crossword", "libretto",
    "telegram", "voicemail", "postcard", "billboard", "jingle", "horoscope",
    "obituary", "pamphlet", "broadsheet", "leaflet", "brochure", "prospectus",
    "invoice", "receipt", "ledger", "docket", "affidavit", "indictment",
    "recipe", "menu", "playbill", "programme", "syllabus", "curriculum",
    "dossier", "brief", "communique", "dispatch", "bulletin", "gazette",
    "sonnet", "villanelle", "clerihew", "ballad", "aria", "shanty",
    "storyboard", "screenplay", "treatment", "logline", "synopsis", "blurb",
]

#: Directive verbs for the same job.
_DIRECTIVE_VERBS: List[str] = [
    "condense", "distil", "paraphrase", "annotate", "caption", "dramatise",
    "versify", "abridge", "recast", "transpose", "lampoon", "parody",
]


class _NoSemanticLane:
    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0}

    def __getattr__(self, name: str):
        return lambda *a, **k: {"items": [], "total": 0}


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("fuzz") / "n.db"
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


def _cause(db: Path, monkeypatch, query: str) -> str:
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


def _sample() -> List[Tuple[str, str]]:
    """(kind, word) for every word the exemption list does not already hold. Words it DOES hold are
    dropped rather than counted as passes — including them would flatter the rate with words the
    curation already covers."""
    return [
        (kind, w)
        for kind, words in (("form", _FORM_WORDS), ("verb", _DIRECTIVE_VERBS))
        for w in words
        if w not in R._OUTPUT_SHAPE_TOKENS
    ]


class TestTheInstrument:
    def test_the_control_answers(self, node: Path, monkeypatch) -> None:
        assert _cause(node, monkeypatch, _CONTROL) == "answered"

    def test_the_gate_is_live_on_this_fixture(self, node: Path, monkeypatch) -> None:
        """Without this the rate below would be zero for the wrong reason."""
        assert _cause(node, monkeypatch, "What did I do recently about zorblatt tourism") == _N.CAUSE_GATE_VETOED

    def test_the_sample_is_outside_the_exemption_list(self) -> None:
        sample = _sample()
        assert len(sample) >= 30, f"only {len(sample)} words are outside the list; widen the sample"
        assert not [w for _, w in sample if w in R._OUTPUT_SHAPE_TOKENS]


class TestTheResidualVetoRate:
    def test_measured(self, node: Path, monkeypatch, record_property) -> None:
        """The figure H-07 exists for, with the offenders named so the next reader can judge
        whether curation or a compiler is the answer."""
        sample = _sample()
        vetoed = [(k, w) for k, w in sample if _cause(node, monkeypatch, _TEMPLATE.format(word=w)) == _N.CAUSE_GATE_VETOED]
        rate = len(vetoed) / len(sample)
        record_property("residual_veto_rate", round(rate, 3))
        record_property("sample_size", len(sample))
        print(f"\n  residual veto rate: {len(vetoed)}/{len(sample)} = {rate:.0%}")
        print(f"  refused on: {sorted(w for _, w in vetoed)}")
        # No threshold is asserted. A rate is not a pass/fail, and picking a bar here would be
        # tuning the argument the measurement is supposed to inform.
        assert 0.0 <= rate <= 1.0

    def test_a_word_the_list_holds_is_not_refused(self, node: Path, monkeypatch) -> None:
        """The control for the measurement: the exemption does work for what it covers, so the
        rate above is about the list's REACH, not about whether exempting works at all."""
        for word in ("haiku", "memo", "kanban"):
            assert word in R._OUTPUT_SHAPE_TOKENS
            assert _cause(node, monkeypatch, _TEMPLATE.format(word=word)) != _N.CAUSE_GATE_VETOED
