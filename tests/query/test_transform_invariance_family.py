"""SUITE-TRANSFORM-INVARIANCE — tx-catalog-1 as families: the plain ask and its transform
siblings must reach the same evidence, on a corpus where the rare gate is LIVE.

protects: A2 first answer over own data; SYS-query I1 read two-sided — "answer when you
should" beside the negatives' "abstain when you should".

`test_output_shape_not_content.py` pins the MECHANISM: no output-shape word can veto a
lane. This file pins the RELATION the 2026-09-05 incident violated: on the same node,
scope and window, "Take the work I have been doing lately, and put it into a 3 stanza
iambic pentameter poem" retrieved nothing while "what have I been working on lately"
retrieved 25 items. A relation between two executions is not visible as any one case's
wrong answer, so the unit here is a family — a control ask, its transform siblings, and
NON-LOSS of evidence between them (research wiki `Metamorphic_Harness_Evaluation`; the
public metric is topos-eval `metrics/metamorphic.py`, kept out of this lane so engine
tests stay engine-only).

Three properties keep the instrument honest:

  * The gate is live. The fixture indexes >= df_max * 10 rows of ordinary working-life
    vocabulary and NO form words, so a veto here is a veto on a form word by
    construction — corpus independence engineered, not hoped for. A self-check pins it.
  * Two-sided. Every control must retrieve something, or invariance is satisfied by
    retrieving nothing everywhere.
  * Sensitivity kept. The catalog's fabricated-subject negatives must still be vetoed in
    the SAME fixture, so the loosening that fixed the incident cannot silence absence
    honesty.

Non-loss, not equality: a sibling may retrieve MORE (a kanban ask reaching rows the plain
ask did not); it may not lose what the plain ask found. Jaccard is reported in the failure
message, not gated — a tolerance is chosen on a held-out family, never on the one being
reported. The semantic lane is stubbed to empty so the lane runs hermetically; what this
family observes is the canonical/derived lanes and the gate, which is where the incident
lived.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from topos.features.signal import service as signal_service
from topos.features.signal.vector_settings import rare_token_df_max
from topos.query import narrowing as _N
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"))
from composition_seed_corpus import build_seeded_corpus  # noqa: E402
from transform_eval_cases import ABSTAIN_CASES, ANSWERABLE_CASES, TRANSFORM_CASES  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-transform-invariance")]

#: An ordinary working life, as the FTS index would hold it. Subjects only — no "poem",
#: no "summarize", no "three". A veto against this corpus is a veto on a form word.
_SUBJECT_SENTENCES = [
    "shipped the installer work and reviewed the release notes for the project",
    "meeting with the team about the roadmap and this month's priorities",
    "journal entry about the week, entries on what I worked on and finished",
    "tracked progress on goals, updated the status and sent the executive note",
    "standup: current projects, blockers, impact, next steps and the timeline",
    "talked with the team, said thank you and shared it on linkedin",
    "listened to a song by the sea, read a detective novel and the newspaper",
    "opened a fortune cookie at dinner and kept the slip",
]

#: The scope's registry default sources (topos/query/scope_registry.json), so the seeded
#: goal row (source `chatgpt_ingestion`) is admitted the way the app admits it.
WORK_CONTEXT_SOURCES = [
    "chatgpt_ingestion",
    "chatgpt_file_ingestion",
    "chatgpt_ui_conversation",
    "demo_resume_file",
    "demo_journal_file",
    "grow_journal",
    "grow_data_file",
]

#: Families: a control ask and the transform siblings that name the SAME subject. The
#: catalog's other answerable cases (journal entries, meetings, people talked with) have
#: different subjects and are held to the weaker property only — never gate-vetoed.
FAMILIES: Dict[str, List[str]] = {
    # "what have I been working on lately" / "my recent work"
    "CTL-1": ["TX-A1", "TX-B1", "TX-B2", "TX-B5", "TX-C2", "TX-C5",
              "TX-D1", "TX-D3", "TX-D5", "TX-E3", "TX-F4"],
    # "summarize my week" — see CONTROL_EMPTY_HERE
    "CTL-2": ["TX-A4", "TX-B3", "TX-B6", "TX-C1", "TX-C4", "TX-D4", "TX-E1", "TX-E4", "TX-F3"],
    # "what did I work on this week"
    "CTL-3": ["TX-A2", "TX-B4"],
}

#: Transform siblings whose embedded subject is ALSO a paraphrase of the control's —
#: "what I've been focused on", "what I've been doing" for "what I've been working on".
#: Two axes moved at once, so they cannot sit in an invariance family (a loss could be
#: either axis). They are held to the weaker property with every answerable case, and
#: their evidence loss is RECORDED in TestKnownLosses, not graded here. Seeded-corpus
#: finding, 2026-09-05: the goals lane admits a goal on token overlap with the ask or on
#: a work-surface word; "focused on" and "been doing" carry neither, so the goal the plain
#: ask retrieves is lost. That is a paraphrase-recall gap on one lane, not the incident's
#: form-word class — it is left for a paraphrase family with its own held-out tuning.
PARAPHRASED_TRANSFORMS: Dict[str, str] = {"TX-B7": "CTL-1", "TX-E2": "CTL-1"}

#: Controls that retrieve nothing on the seeded corpus, so their family cannot be graded
#: here. "Summarize my week" names no work surface and shares no token with the seeded
#: goal, and the semantic lane is stubbed off; in the app this ask fans out across scopes
#: and is answered from the timeline lanes, not from work_context alone. The floor test is
#: an xfail(strict) — when a corpus or lane change makes the control answer, the marker
#: must come off and the family starts being graded.
CONTROL_EMPTY_HERE = {"CTL-2"}

_BY_ID = {c.case_id: c for c in TRANSFORM_CASES}


class _NoSemanticLane:
    """The vector lane, stubbed to empty: this family observes the canonical and derived
    lanes plus the gate. Everything else the retrieval adapter asks the service for
    answers empty too, loudly typed as a dict so a new call site fails on shape, not
    on a phantom result."""

    def search_vectors(self, **kwargs):  # noqa: ANN003
        return {"items": [], "total": 0, "query": kwargs.get("query"), "model": "stub",
                "limit": kwargs.get("limit"), "mode": "stub"}

    def __getattr__(self, name: str):
        def _empty(*args, **kwargs):  # noqa: ANN002, ANN003
            return {"items": [], "total": 0}
        return _empty


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("tx-family") / "seed.db"
    build_seeded_corpus(db)
    conn = sqlite3.connect(str(db))
    try:
        # Make the gate LIVE: `_rare_tokens` trusts the frequency signal only above
        # df_max * 10 indexed rows. The trigger on signal_embeddings fills the FTS index.
        floor = rare_token_df_max() * 10
        now = datetime.now(timezone.utc)
        # `chunk_index` and a recent `event_at` are load-bearing, not decoration: without them a
        # row raises the FTS count — switching the gate ON — while staying invisible to every
        # evidence lane, so the fusion returns `store_empty` at the branch ABOVE the gate and no
        # veto can ever be observed. `_load_recent_summary_items` selects on exactly these two
        # columns. A first version of this fixture omitted them, and its "sensitivity kept" check
        # below passed without a single veto ever firing.
        rows = [
            (f"tx-fts-{i}", f"tx-rec-{i}", "chatgpt_ingestion", "work", "fixture", "fixture",
             0, _SUBJECT_SENTENCES[i % len(_SUBJECT_SENTENCES)],
             _SUBJECT_SENTENCES[i % len(_SUBJECT_SENTENCES)],
             0, (now - timedelta(days=1 + (i % 3))).isoformat())
            for i in range(floor + 100)
        ]
        conn.executemany(
            """INSERT INTO signal_embeddings
               (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,
                text_preview, search_text, chunk_index, event_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def run(seeded_db: Path, monkeypatch):
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _NoSemanticLane())
    conn = sqlite3.connect(str(seeded_db))
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))

    def _run(query: str) -> Tuple[Set[str], NarrowingLedger]:
        ledger = NarrowingLedger()
        bundle = adapter.retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("work_context:read"),
                access_mode="summary",
                query_text=query,
                installed_source_ids=list(WORK_CONTEXT_SOURCES),
                ledger=ledger,
            )
        )
        assert not bundle.error, f"retrieval errored: {bundle.error}"
        return _evidence_ids(bundle.context_packet), ledger

    yield _run
    conn.close()


_ID_KEYS = ("record_id", "goal_id", "object_id", "fact_id", "message_id", "brief_id", "cluster_id", "id")


def _evidence_ids(packet) -> Set[str]:
    """Every retrieved thing that carries an identity, from every list-valued lane."""
    ids: Set[str] = set()
    if not isinstance(packet, dict):
        return ids
    for lane, value in packet.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            for key in _ID_KEYS:
                v = item.get(key)
                if v:
                    ids.add(f"{lane}:{key}:{v}")
                    break
    return ids


def _jaccard(a: Set[str], b: Set[str]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


# ---------------------------------------------------------------- the instrument itself


class TestTheFixtureIsAnInstrument:
    def test_the_gate_is_live_here(self, seeded_db: Path, run) -> None:
        """The self-check that makes a green board mean something — and it has to pin all THREE of
        the veto's preconditions, not just the frequency one. A veto needs an evidence lane holding
        rows, a rare token, and that token unevidenced among the rows. The first version of this
        check counted FTS rows only; the fixture met that and still could not produce a single
        veto, because filler with no `chunk_index` / `event_at` is invisible to every evidence lane
        (found by the corpus-density sweep, 2026-09-06). So the check now ends by DEMONSTRATING a
        veto end to end."""
        conn = sqlite3.connect(str(seeded_db))
        try:
            assert conn.execute("SELECT count(*) FROM signal_embeddings_fts").fetchone()[0] >= rare_token_df_max() * 10
            assert conn.execute(
                "SELECT count(*) FROM signal_embeddings WHERE chunk_index = 0 AND event_at IS NOT NULL"
            ).fetchone()[0] >= rare_token_df_max() * 10
        finally:
            conn.close()
        _, ledger = run("What did I do recently about zorblatt tourism")
        assert ledger.empty_cause == _N.CAUSE_GATE_VETOED, (
            "the fixture cannot produce a veto, so every invariance relation below is vacuous"
        )

    def test_every_family_member_is_a_catalog_case(self) -> None:
        for control, siblings in FAMILIES.items():
            assert control in _BY_ID and _BY_ID[control].subclass == "control"
            for sid in siblings:
                assert sid in _BY_ID and _BY_ID[sid].expect == "answerable", sid

    def test_the_absence_negatives_still_veto_in_this_fixture(self, run) -> None:
        """Sensitivity kept: the fabricated subjects the catalog carries must empty the lane
        with the gate's own cause — in the same corpus where the transforms must not."""
        # These catalogue negatives carry no recency framing, so the `recent` lane stays CONTEXT
        # and they never reach the gate at all — they empty one branch earlier, as `store_empty`.
        # That is why this accepts any honest empty rather than demanding `gate_vetoed`: the
        # sensitivity it pins is "a fabricated subject retrieves nothing", and the gate's own
        # sensitivity is pinned by `test_the_gate_is_live_here` above, which forces a real veto.
        honest_empty = {_N.CAUSE_GATE_VETOED, _N.CAUSE_NO_MATCH, _N.CAUSE_STORE_EMPTY}
        for case in ABSTAIN_CASES:
            ids, ledger = run(case.query)
            assert ids == set(), (
                f"{case.case_id}: a fabricated subject {case.absent_subject!r} retrieved "
                f"{sorted(ids)} — absence honesty lost"
            )
            # Which honest cause depends on whether any candidate reached the gate: with
            # candidates it is `gate_vetoed` (the incident's 63 dropped items); with none,
            # the engine attributes the empty to the store or the match. On this seeded
            # corpus a real store with no matching rows is stamped `store_empty` — the
            # same attribution quirk the live abstention matrix recorded (a pre-data
            # window read as store_empty where no_match is truer). Observed, not gated.
            assert ledger.empty_cause in honest_empty, (
                f"{case.case_id}: empty with cause {ledger.empty_cause!r}, which is not an "
                f"honest absence"
            )


# ---------------------------------------------------------------- the families


class TestTransformInvariance:
    @pytest.mark.parametrize(
        "control",
        [
            pytest.param(
                c,
                marks=pytest.mark.xfail(
                    strict=True, reason="control retrieves nothing on the seeded corpus; see CONTROL_EMPTY_HERE"
                ),
            )
            if c in CONTROL_EMPTY_HERE
            else c
            for c in sorted(FAMILIES)
        ],
        ids=lambda c: c,
    )
    def test_the_control_retrieves_something(self, run, control: str) -> None:
        """Two-sided floor. An empty control makes every relation below vacuous."""
        ids, ledger = run(_BY_ID[control].query)
        assert ids, f"{control} retrieved nothing (cause={ledger.empty_cause}); the family cannot be graded"

    @pytest.mark.parametrize(
        "control,sibling",
        [(c, s) for c, sibs in FAMILIES.items() for s in sibs],
        ids=lambda x: x,
    )
    def test_a_transform_sibling_keeps_the_controls_evidence(self, run, control: str, sibling: str) -> None:
        base, _ = run(_BY_ID[control].query)
        if not base:
            pytest.skip(f"{control} retrieves nothing on this corpus; the floor test carries that")
        got, ledger = run(_BY_ID[sibling].query)
        lost = sorted(base - got)
        assert ledger.empty_cause != _N.CAUSE_GATE_VETOED, (
            f"{sibling} was gate-vetoed — the incident's class — on {_BY_ID[sibling].query!r}"
        )
        assert not lost, (
            f"{sibling} lost {len(lost)} of the control's {len(base)} evidence items "
            f"(jaccard={_jaccard(base, got):.2f}); lost: {lost[:5]}"
        )

    @pytest.mark.parametrize("case", ANSWERABLE_CASES, ids=lambda c: c.case_id)
    def test_no_answerable_transform_is_gate_vetoed(self, run, case) -> None:
        """The weaker property, for every answerable case whatever its subject: the form
        words never empty the lane. Subjects the seeded corpus does not hold may return
        empty for an honest reason — that is `no_match`, never `gate_vetoed`."""
        _, ledger = run(case.query)
        assert ledger.empty_cause != _N.CAUSE_GATE_VETOED, (
            f"{case.case_id}: {case.query!r} was gate-vetoed; ledger={ledger.as_public()}"
        )


class TestKnownLosses:
    """Losses the family FOUND and the lane does not yet repair. Each is an xfail(strict):
    the board shows the loss as a known one instead of a red that reads as noise, and the
    day a lane change repairs it the cell XPASSes and the marker must come off — the same
    discipline as the floor. Nothing here is tuned to pass."""

    @pytest.mark.parametrize(
        "sibling,control",
        [
            pytest.param(
                s,
                c,
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="paraphrase-recall gap on the goals lane (token overlap or work-surface word)",
                ),
            )
            for s, c in PARAPHRASED_TRANSFORMS.items()
        ],
        ids=lambda x: x,
    )
    def test_a_paraphrased_transform_keeps_the_controls_evidence(self, run, sibling: str, control: str) -> None:
        base, _ = run(_BY_ID[control].query)
        got, ledger = run(_BY_ID[sibling].query)
        assert ledger.empty_cause != _N.CAUSE_GATE_VETOED, sibling
        assert not (base - got), f"{sibling} lost {sorted(base - got)} (jaccard={_jaccard(base, got):.2f})"
