"""Gated overheard caption retrieval + isolation from personal packets.

TX-L: overheard ask hits captions and cites ambient.
TX-H: first-person belief abstains (IMB poison — caption opinion is not mine).
TX-E: treatment-only needles are findable only when overheard is admitted.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.overheard import (
    OVERHEARD_SOURCE_ID,
    graph_lane_allows_edge_type,
    is_overheard_query,
    query_admits_overheard,
)
from topos.query.retrieval import _query_tokens, _residual_content_tokens
from topos.query.planner import QueryPlan
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

NEEDLE_ASTRA = "GPT6 Astra discovered new primes"
NEEDLE_REMOTE = "military remote viewing programs"
NEEDLE_FRONTIER = "The new OpenAI model is the frontier and it's called Astra"
NEEDLE_GEMINI = "Gemini 3.8 Flash got 73.7% beating GPT6 Astra"
NEEDLE_PRIME = "Two advances in prime number research. Astra helped lower the bound"
POISON_OPINION = "meadowfoam honey varietal is my favourite stance on Astra"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    c = sqlite3.connect(str(tmp_path / "overheard.db"))
    apply_all_migrations(c)
    ensure_all_tables(c)
    _seed(c)
    yield c
    c.close()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO conversation_messages
            (message_id, conversation_id, dataset_id, sender_type, sender_id,
             content, event_at, source_id, is_from_self)
        VALUES ('msg-owner', 'c1', 'd', 'human', 'self',
                'Ship the Topos installer tomorrow',
                '2026-06-01T10:00:00Z', 'imessage', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO transcripts
            (transcript_id, title, origin_kind, participation_mode, asr_quality,
             source_id, source_record_id)
        VALUES ('yt:tx', 'Fixture podcast', 'youtube', 'ambient', 'generated',
                'youtube_transcripts', 'yt:tx')
        """
    )
    rows = [
        ("yt:tx:1", NEEDLE_ASTRA, 0.0),
        ("yt:tx:2", NEEDLE_REMOTE, 45.0),
        ("yt:tx:3", POISON_OPINION, 90.0),
        ("yt:tx:4", NEEDLE_FRONTIER, 120.0),
        ("yt:tx:5", NEEDLE_GEMINI, 150.0),
        ("yt:tx:6", NEEDLE_PRIME, 180.0),
    ]
    for sid, text, start in rows:
        conn.execute(
            """
            INSERT INTO transcript_segments (
                segment_id, transcript_id, content, start_sec, event_at,
                actor_role, is_from_self, source_id, source_record_id
            ) VALUES (?, 'yt:tx', ?, ?, '2026-06-01T11:00:00Z', 'ambient', 0,
                      'youtube_transcripts', ?)
            """,
            (sid, text, start, sid),
        )
    conn.commit()


def _retrieve(conn, query: str, scope: str, *, plan: QueryPlan | None = None):
    bundle = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(bundle)
    manifest = resolve_scope_manifest(scope)
    req = RetrievalRequest(
        manifest=manifest,
        access_mode="summary",
        query_text=query,
        disclosure_tier="owner_raw",
    )
    if plan is not None:
        # Planner may be skipped in hermetic DBs; stamp flags the retrieve path
        # still reads from request-less plan built inside retrieve(). We set
        # TOPOS_QUERY_PLANNER on and let build_query_plan run on the query text.
        pass
    return adapter.retrieve(req)


def _summaries(bundle) -> list:
    return list((bundle.context_packet or {}).get("summaries") or [])


def _blob(items) -> str:
    return " ".join(
        f"{i.get('summary_text') or ''} {i.get('topic') or ''}" for i in items
    ).lower()


def test_overheard_detector() -> None:
    assert is_overheard_query("What did that podcast say about Astra?")
    assert is_overheard_query("anything", "transcripts:read")
    assert not is_overheard_query("Who works on Topos with me?")
    belief = QueryPlan(query_text="What is my opinion on Astra?", first_person_belief=True)
    assert query_admits_overheard(
        "What did that podcast say about Astra?", "messages:read", belief
    ) is False
    assert graph_lane_allows_edge_type(
        "worked_on", scope_id="relationship_context:read",
        query_text="Who works on Topos with me?",
    )
    assert not graph_lane_allows_edge_type(
        "windowed_with", scope_id="relationship_context:read",
        query_text="Who works on Topos with me?",
    )
    assert graph_lane_allows_edge_type(
        "windowed_with", scope_id="graph:read",
        query_text="What is connected to Topos?",
    )


def test_tx_l1_overheard_hit_and_ambient_cite(conn) -> None:
    bundle = _retrieve(
        conn,
        "What did that podcast say about GPT6 Astra?",
        "messages:read",
    )
    items = _summaries(bundle)
    blob = _blob(items)
    assert "gpt6 astra" in blob
    overheard = [
        i
        for i in items
        if "transcript" in str(i.get("retrieval_source") or "")
        or i.get("source_id") == OVERHEARD_SOURCE_ID
    ]
    assert overheard, f"no caption items in packet: {_blob(items)[:200]}"
    assert all(i.get("actor_role") == "ambient" for i in overheard)
    assert all(i.get("source_id") == OVERHEARD_SOURCE_ID for i in overheard)


def test_tx_l2_explicit_transcripts_scope(conn) -> None:
    bundle = _retrieve(
        conn,
        "According to the transcript, what did they claim about prime number research?",
        "transcripts:read",
    )
    items = _summaries(bundle)
    blob = _blob(items)
    assert "prime number" in blob
    assert all(
        i.get("source_id") == OVERHEARD_SOURCE_ID
        or "transcript" in str(i.get("retrieval_source") or "")
        for i in items
    )


def test_tx_l2_residual_drops_speech_act_framing() -> None:
    tokens = _query_tokens(
        "According to the transcript, what did they claim about prime number research?"
    )
    residual = _residual_content_tokens(tokens, tables=["transcript_segments"])
    assert residual == ["prime", "number", "research"]
    messages_residual = _residual_content_tokens(tokens, tables=["conversation_messages"])
    assert "they" in messages_residual
    assert "prime" in messages_residual


def test_tx_h1_first_person_opinion_abstains(conn) -> None:
    bundle = _retrieve(conn, "What is my opinion on Astra?", "messages:read")
    blob = _blob(_summaries(bundle))
    assert "meadowfoam honey varietal" not in blob
    assert not any(
        i.get("source_id") == OVERHEARD_SOURCE_ID for i in _summaries(bundle)
    )


def test_tx_e1_treatment_needle_on_overheard_ask(conn) -> None:
    bundle = _retrieve(
        conn,
        "According to the transcript, what did they claim about meadowfoam honey varietal?",
        "messages:read",
    )
    assert "meadowfoam honey varietal" in _blob(_summaries(bundle))


def test_tx_e2_personal_messages_miss_treatment_needle(conn) -> None:
    bundle = _retrieve(
        conn,
        "Find the messages about meadowfoam honey varietal",
        "messages:read",
    )
    items = _summaries(bundle)
    assert "meadowfoam honey varietal" not in _blob(items)
    assert not any(i.get("source_id") == OVERHEARD_SOURCE_ID for i in items)


def test_tx_l3_paraphrase_without_needle_word(conn) -> None:
    bundle = _retrieve(
        conn,
        "What did that podcast say the new OpenAI frontier model is called?",
        "messages:read",
    )
    blob = _blob(_summaries(bundle))
    assert "astra" in blob
    assert any(i.get("source_id") == OVERHEARD_SOURCE_ID for i in _summaries(bundle))


def test_tx_l4_numeric_score_ask(conn) -> None:
    bundle = _retrieve(
        conn,
        "According to the recording, what score did Gemini get beating GPT6 Astra?",
        "transcripts:read",
    )
    blob = _blob(_summaries(bundle))
    assert "73.7" in blob or "73" in blob


def test_tx_l6_transcripts_scope_without_media_noun(conn) -> None:
    bundle = _retrieve(
        conn,
        "What did they claim about prime number research?",
        "transcripts:read",
    )
    assert "prime number" in _blob(_summaries(bundle))


def test_tx_l7_bound_after_astra(conn) -> None:
    bundle = _retrieve(
        conn,
        "What did the transcript say Astra helped lower the bound on?",
        "transcripts:read",
    )
    blob = _blob(_summaries(bundle))
    assert "prime" in blob or "bound" in blob


def test_tx_h2_first_person_leech_opinion_abstains(conn) -> None:
    bundle = _retrieve(conn, "What is my opinion on apps being leeches?", "messages:read")
    items = _summaries(bundle)
    assert not any(i.get("source_id") == OVERHEARD_SOURCE_ID for i in items)


def test_tx_remote_viewing_still_hits_on_transcripts_scope(conn) -> None:
    bundle = _retrieve(
        conn,
        "What did the recording mention about remote viewing?",
        "transcripts:read",
    )
    assert "remote viewing" in _blob(_summaries(bundle))


def test_caption_rows_never_enter_derivation_history(conn) -> None:
    from topos.enrichment.jobs.canonical.derivation_job import _iter_history
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    history = _iter_history(conn, limit=50)
    assert not any(row["table"] == "transcript_segments" for row in history)
    packs = load_packs(bundled_pack_dir(), only=["interests.taste", "behavior.habits"])
    assert packs
    for pack in packs.values():
        assert "ambient" in pack.allowed_roles()
    # Isolation is the history skip: any_with_label would have admitted ambient.
