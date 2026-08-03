"""Guards for the IMB imbalance/attribution lane (PLAN_PROVENANCE_SPLIT.md §5).

Pure and deterministic: builds the qq-imb-1 scratch corpus through the real
migrations in a tmpdir (no models, no network) and pins its shape — the 99:1
authored/ambient ratio, the poison canaries, the source_id scope gate, the
mention-only entity, the FTS df statistics, the fact-extraction tripwire — plus
the CompositionCase default-preservation contract and score_imbalance math.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from composition_eval_cases import CompositionCase, Oracle
from composition_seed_corpus import SEEDED_COMPOSITION_CASES
from imbalance_eval_cases import (
    IMBALANCE_CASES,
    imbalance_lane_metrics,
    score_imbalance,
)
from imbalance_seed_corpus import (
    AI_ASSISTANT_MESSAGE_ID,
    AI_USER_MESSAGE_ID,
    AUTHORED_CANARIES,
    HIGHLIGHT_EVENT_ID,
    IMB_CORPUS_VERSION,
    N_ACTIVITY,
    N_AI_CHAT,
    N_AMBIENT_PER_SENDER,
    N_AUTHORED,
    N_EMBEDDINGS,
    N_MESSAGES_TOTAL,
    N_POISON_PER_SENDER,
    PAGE_POISON_EVENT_ID,
    POISON_CANARIES,
    SENDERS,
    TRIPWIRE_MESSAGE_ID,
    build_imbalance_corpus,
)

_SCOPE_REGISTRY = (
    Path(__file__).resolve().parents[4] / "topos" / "query" / "scope_registry.json"
)


@pytest.fixture(scope="module")
def corpus_conn(tmp_path_factory) -> sqlite3.Connection:
    db_path = build_imbalance_corpus(tmp_path_factory.mktemp("imb") / "imb.db")
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _one(conn: sqlite3.Connection, sql: str, *params) -> Any:
    return conn.execute(sql, params).fetchone()[0]


# --- Corpus shape ----------------------------------------------------------------------


def test_version_is_pinned():
    assert IMB_CORPUS_VERSION == "qq-imb-1"


def test_exact_row_counts(corpus_conn):
    assert _one(corpus_conn, "SELECT COUNT(*) FROM conversation_messages") == N_MESSAGES_TOTAL == 2000
    for sender in SENDERS:
        n = _one(
            corpus_conn,
            "SELECT COUNT(*) FROM conversation_messages WHERE sender_id = ?",
            sender["sender_id"],
        )
        assert n == N_AMBIENT_PER_SENDER == 659, f"{sender['name']}: {n}"
    assert _one(corpus_conn, "SELECT COUNT(*) FROM activity_events") == N_ACTIVITY == 300
    assert _one(corpus_conn, "SELECT COUNT(*) FROM ai_chat_messages") == N_AI_CHAT == 2


def test_authored_rows_carry_the_role_bit(corpus_conn):
    # 23 owner rows, all sender_id='self' + is_from_self=1 — and NOTHING else is.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE is_from_self = 1",
    ) == N_AUTHORED == 23
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM conversation_messages
           WHERE is_from_self = 1 AND sender_id != 'self'""",
    ) == 0
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM conversation_messages
           WHERE sender_id = 'self' AND is_from_self != 1""",
    ) == 0
    # sender_type mirrors ingestion paths: never an invented 'self' value.
    types = {
        r[0] for r in corpus_conn.execute(
            "SELECT DISTINCT sender_type FROM conversation_messages"
        )
    }
    assert types == {"contact", "human"}


def test_poison_canaries_are_planted(corpus_conn):
    # Each sender's unique canary rides exactly N_POISON_PER_SENDER ambient rows.
    for key in ("bram_glacier", "saskia_meadowfoam", "tomas_basalt"):
        n = _one(
            corpus_conn,
            """SELECT COUNT(*) FROM conversation_messages
               WHERE content LIKE ? AND is_from_self = 0""",
            f"%{POISON_CANARIES[key]}%",
        )
        assert n == N_POISON_PER_SENDER == 40, f"{key}: {n}"
        # ... and NEVER an authored row.
        assert _one(
            corpus_conn,
            "SELECT COUNT(*) FROM conversation_messages WHERE content LIKE ? AND is_from_self = 1",
            f"%{POISON_CANARIES[key]}%",
        ) == 0
    # Bram's general stance poison floods his ambient speech.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE content LIKE ? AND is_from_self = 0",
        f"%{POISON_CANARIES['bram_crypto_stance']}%",
    ) > 40
    # Page-author opinion lives in ONE visit row's metadata_json.
    meta = json.loads(_one(
        corpus_conn,
        "SELECT metadata_json FROM activity_events WHERE event_id = ?",
        PAGE_POISON_EVENT_ID,
    ))
    assert POISON_CANARIES["page_author"] in meta["page_excerpt"]
    # Assistant opinion poison in the assistant ai_chat row only.
    assert POISON_CANARIES["assistant_style"] in _one(
        corpus_conn,
        "SELECT content FROM ai_chat_messages WHERE message_id = ?",
        AI_ASSISTANT_MESSAGE_ID,
    )
    # Thread-volume poison stat is planted alongside the sent-count stat.
    payloads = [
        r[0] for r in corpus_conn.execute("SELECT payload_json FROM signal_facts")
    ]
    blob = " ".join(payloads)
    assert POISON_CANARIES["stat_thread_volume"] in blob
    assert AUTHORED_CANARIES["stat_sent"] in blob


def test_authored_canaries_are_planted_and_owner_only(corpus_conn):
    for key in ("belief_crypto", "interest_mandolin", "needle_greenhouse"):
        needle = AUTHORED_CANARIES[key]
        assert _one(
            corpus_conn,
            "SELECT COUNT(*) FROM conversation_messages WHERE content LIKE ? AND is_from_self = 1",
            f"%{needle}%",
        ) == 1, key
        assert _one(
            corpus_conn,
            "SELECT COUNT(*) FROM conversation_messages WHERE content LIKE ? AND is_from_self = 0",
            f"%{needle}%",
        ) == 0, key
    # The one expression-like browser row: highlight span in metadata_json.
    row = corpus_conn.execute(
        "SELECT activity_type, metadata_json FROM activity_events WHERE activity_type = 'browser_highlight'"
    ).fetchall()
    assert len(row) == 1
    assert json.loads(row[0][1])["highlight"] == AUTHORED_CANARIES["highlight_copper"]
    assert corpus_conn.execute(
        "SELECT event_id FROM activity_events WHERE activity_type = 'browser_highlight'"
    ).fetchone()[0] == HIGHLIGHT_EVENT_ID
    # User-typed style preference in the 'user' ai_chat row.
    assert AUTHORED_CANARIES["ai_style"] in _one(
        corpus_conn,
        "SELECT content FROM ai_chat_messages WHERE message_id = ?",
        AI_USER_MESSAGE_ID,
    )


def test_every_seeded_source_id_is_in_scope_registry_defaults(corpus_conn):
    """qq-seeded-1 lesson: rows outside the scope's default_source_ids get filtered
    by resolve_retrieval_source_ids and the lane measures the corpus, not retrieval."""
    registry = json.loads(_SCOPE_REGISTRY.read_text())
    defaults = {
        s["scope_id"]: set(s.get("default_source_ids") or []) for s in registry["scopes"]
    }
    table_to_scope = {
        "conversation_messages": "messages:read",
        "conversations": "messages:read",
        "activity_events": "activity:read",
        "ai_chat_messages": "ai_conversations:read",
        "contacts": "relationship_context:read",
        "entity_mentions": "relationship_context:read",
    }
    for table, scope_id in table_to_scope.items():
        seeded = {
            r[0]
            for r in corpus_conn.execute(f"SELECT DISTINCT source_id FROM {table}")
            if r[0]
        }
        assert seeded, f"{table} seeded no source_ids"
        assert seeded <= defaults[scope_id], (
            f"{table}: {seeded - defaults[scope_id]} not in {scope_id} defaults"
        )
    # Stat rows ride the stats engine channel (S5 precedent), not a canonical scope.
    assert {
        r[0] for r in corpus_conn.execute("SELECT DISTINCT source_id FROM signal_facts")
    } == {"stats_engine"}


def test_odile_is_mention_only(corpus_conn):
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM entities WHERE canonical_name = 'Odile Ferrant' AND is_self = 0",
    ) == 1
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = 'imb-ent-odile'",
    ) == 6
    # NO contacts row, NO dossier — she was never a participant.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM contacts WHERE display_name LIKE '%Odile%'",
    ) == 0
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM signal_objects
           WHERE object_type = 'entity_dossier' AND payload_json LIKE '%Odile%'""",
    ) == 0
    # The 3 real participants DO have contacts + dossiers.
    for sender in SENDERS:
        assert _one(
            corpus_conn,
            "SELECT COUNT(*) FROM contacts WHERE contact_id = ? AND is_self = 0",
            sender["contact_id"],
        ) == 1
        assert _one(
            corpus_conn,
            "SELECT COUNT(*) FROM signal_objects WHERE object_type = 'entity_dossier' AND object_key = ?",
            f"dossier:{sender['entity_id']}",
        ) == 1


def test_odile_mentions_not_owner_authored(corpus_conn):
    """P3.1 / B6 IMB wire: mention-only Odile rows must carry authored_by_owner=0.

    Catches write-path / seed regressions that would stamp observed-speech
    mentions as owner-authored (misattribution expansion failure mode).
    """
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM pragma_table_info('entity_mentions')
           WHERE name = 'authored_by_owner'""",
    ) == 1
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM entity_mentions
           WHERE entity_id = 'imb-ent-odile' AND authored_by_owner = 0""",
    ) == 6
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM entity_mentions
           WHERE entity_id = 'imb-ent-odile'
             AND (authored_by_owner IS NULL OR authored_by_owner = 1)""",
    ) == 0
    # Parent messages are observed (is_from_self=0) — join consistency.
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM entity_mentions m
           JOIN conversation_messages c ON c.message_id = m.record_id
           WHERE m.entity_id = 'imb-ent-odile' AND c.is_from_self = 1""",
    ) == 0


def test_imb7_hardened_odile_not_communicates_with(corpus_conn):
    """P3.2 / B7: mention-only Odile must not mint communicates_with edges.

    BEFORE hole: rebuild linked each gossiping sender → Odile (talked-to
    pollution). Hardened: co-participation only binds real thread senders.
    """
    from topos.features.entities.maintenance import rebuild_evidence_edges

    stats = rebuild_evidence_edges(corpus_conn)
    assert stats["communicates_with"] >= 1
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM entity_edges
           WHERE edge_type='communicates_with' AND valid_to IS NULL
             AND (src_entity_id='imb-ent-odile' OR dst_entity_id='imb-ent-odile')""",
    ) == 0
    # Owner talks with each of the 3 real participants.
    for sender in SENDERS:
        assert _one(
            corpus_conn,
            """SELECT COUNT(*) FROM entity_edges
               WHERE edge_type='communicates_with' AND valid_to IS NULL
                 AND (
                   (src_entity_id='imb-self-1' AND dst_entity_id=?)
                   OR (src_entity_id=? AND dst_entity_id='imb-self-1')
                 )""",
            sender["entity_id"],
            sender["entity_id"],
        ) >= 1
    # Participants are seeded into conversation_participants (membership wire).
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
        "imb-groupchat-1",
    ) == 4
    assert _one(
        corpus_conn,
        """SELECT COUNT(*) FROM conversation_participants cp
           JOIN contacts c ON c.contact_id = cp.contact_id
           WHERE c.display_name LIKE '%Odile%'""",
    ) == 0


def test_fts_index_carries_honest_df_statistics(corpus_conn):
    """search_text-only signal_embeddings rows (no vectors) must have populated the
    FTS index via triggers: ambient tokens common, authored needles rare — the
    99:1 statistics the rare-token abstention gate reads."""
    assert _one(corpus_conn, "SELECT COUNT(*) FROM signal_embeddings") == N_EMBEDDINGS == 2302
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings WHERE vector_blob IS NOT NULL",
    ) == 0  # text-only: no embedding model ran
    assert _one(corpus_conn, "SELECT COUNT(*) FROM signal_embeddings_fts") == N_EMBEDDINGS
    df_common = _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH 'kombucha'",
    )
    df_rare = _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH 'mandolin'",
    )
    assert df_common > 50, f"ambient token should flood the index (df={df_common})"
    assert df_rare == 1, f"authored needle token must be rare (df={df_rare})"


def test_tripwire_ambient_row_extracts_no_facts(corpus_conn):
    """The one existing role gate (_is_owner_authored in facts/extract.py) must hold:
    ambient first-person speech ('I live in Marseille…' from Tomas) extracts nothing;
    the same content owner-attributed extracts — so the tripwire cannot rot vacuous.
    build_imbalance_corpus already ran this; re-run it against the built DB."""
    from topos.features.facts.extract import extract_message_facts

    row = corpus_conn.execute(
        """SELECT sender_type, sender_id, is_from_self, content, event_at
           FROM conversation_messages WHERE message_id = ?""",
        (TRIPWIRE_MESSAGE_ID,),
    ).fetchone()
    assert row is not None
    ambient = {
        "sender_type": row[0],
        "sender_id": row[1],
        "is_from_self": row[2],
        "content": row[3],
        "event_at": row[4],
    }
    assert ambient["is_from_self"] == 0 and "I live in Marseille" in ambient["content"]
    assert extract_message_facts(ambient) == []
    owner_variant = dict(ambient, is_from_self=1, sender_type="human", sender_id="self")
    facts = extract_message_facts(owner_variant)
    assert facts and facts[0]["predicate"] == "lives_in"


# --- CompositionCase contract ------------------------------------------------------------


def test_new_fields_default_and_preserve_existing_case_equality():
    # Untouched lanes get the defaults…
    s1 = SEEDED_COMPOSITION_CASES[0]
    assert s1.poison_groups == ()
    assert s1.authored_only is False
    # …and a case built without the new fields equals one that spells them out
    # (the byte-identical guarantee the query_class addition established).
    oracle = lambda conn: Oracle([], "x")  # noqa: E731 — shared identity matters for eq
    implicit = CompositionCase("X0", "seeded", "q", "messages:read", "raw", oracle)
    explicit = CompositionCase(
        "X0", "seeded", "q", "messages:read", "raw", oracle,
        poison_groups=(), authored_only=False,
    )
    assert implicit == explicit


def test_imbalance_catalog_shape():
    assert [c.id for c in IMBALANCE_CASES] == [f"IMB{i}" for i in range(1, 11)]
    assert all(c.lane == "imbalance" for c in IMBALANCE_CASES)
    negatives = {c.id for c in IMBALANCE_CASES if c.negative}
    assert negatives == {"IMB1", "IMB4", "IMB8"}
    authored = {c.id for c in IMBALANCE_CASES if c.authored_only}
    assert authored == {"IMB1", "IMB2", "IMB3", "IMB10"}
    poisoned = {c.id for c in IMBALANCE_CASES if c.poison_groups}
    assert poisoned == {"IMB1", "IMB2", "IMB3", "IMB4", "IMB6", "IMB7", "IMB9", "IMB10"}
    # IMB9's red is by construction — the case must say so or it reads as breakage.
    imb9 = next(c for c in IMBALANCE_CASES if c.id == "IMB9")
    assert "RED BY CONSTRUCTION" in imb9.description


# --- score_imbalance math -----------------------------------------------------------------


def _case(case_id: str) -> CompositionCase:
    return next(c for c in IMBALANCE_CASES if c.id == case_id)


def _response(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"public_result": {"summaries": items}}


def test_score_imbalance_poison_and_attribution_math(corpus_conn):
    case = _case("IMB2")  # needle [['promising for remittances']], 2 poison groups
    oracle = case.oracle(corpus_conn)
    response = _response([
        {   # the authored needle row (owner: imb-self-02 is the belief canary)
            "summary_text": "Personally I think cryptocurrency is genuinely "
                            "promising for remittances.",
            "record_id": "imb-self-02",
            "retrieval_source": "canonical:conversation_messages",
        },
        {   # an ambient Bram row carrying one of the two poison groups
            "summary_text": "still convinced crypto is a scam, index funds only",
            "record_id": "imb-bram-0001",
            "retrieval_source": "canonical:conversation_messages",
        },
    ])
    result = score_imbalance(case, response, oracle, corpus_conn)
    assert result["scores"]["correctness"] == 1.0
    assert result["scores"]["misattribution"] == 0.5      # 1 of 2 poison groups matched
    assert result["poison_matched"] == ["crypto is a scam"]
    assert result["scores"]["attribution_precision"] == 0.5  # 1 of 2 rows owner-authored
    assert result["composite"] == 0.5                     # min(base=1.0, misattribution)


def test_score_imbalance_clean_authored_answer_is_perfect(corpus_conn):
    case = _case("IMB2")
    oracle = case.oracle(corpus_conn)
    response = _response([
        {
            "summary_text": "cryptocurrency is genuinely promising for remittances",
            "record_id": "imb-self-02",
            "retrieval_source": "canonical:conversation_messages",
        },
    ])
    result = score_imbalance(case, response, oracle, corpus_conn)
    assert result["scores"]["misattribution"] == 1.0
    assert result["scores"]["attribution_precision"] == 1.0
    assert result["composite"] == 1.0


def test_score_imbalance_negative_case_keeps_groundedness_composite(corpus_conn):
    case = _case("IMB4")  # negative; poison = Tomas's basalt canary
    oracle = case.oracle(corpus_conn)
    leaky = _response([
        {"summary_text": "the basalt cold-plunge ritual at dawn resets everything",
         "record_id": "imb-tomas-0000", "retrieval_source": "canonical:conversation_messages"},
        {"summary_text": "ice bath crew meets saturday",
         "record_id": "imb-tomas-0001", "retrieval_source": "canonical:conversation_messages"},
    ])
    result = score_imbalance(case, leaky, oracle, corpus_conn)
    assert result["scores"]["groundedness"] == 0.8        # 1 - 2/10 items of noise
    assert result["scores"]["misattribution"] == 0.0      # canary present
    assert result["composite"] == 0.8                     # negative: groundedness rules

    abstained = score_imbalance(case, _response([]), oracle, corpus_conn)
    assert abstained["scores"]["groundedness"] == 1.0
    assert abstained["scores"]["misattribution"] == 1.0
    assert abstained["composite"] == 1.0


def test_score_imbalance_exempts_non_message_evidence(corpus_conn):
    case = _case("IMB1")  # authored_only negative
    oracle = case.oracle(corpus_conn)
    response = _response([
        {"summary_text": "You sent 23 messages in Harbor Collective.",
         "record_id": "stat:messages.volume.sent.by_thread:imb-groupchat-1",
         "retrieval_source": "stat_insight"},
    ])
    result = score_imbalance(case, response, oracle, corpus_conn)
    # stat items resolve to no message row → exempt → vacuously precise.
    assert result["scores"]["attribution_precision"] == 1.0


def test_score_imbalance_ai_chat_roles(corpus_conn):
    case = _case("IMB10")
    oracle = case.oracle(corpus_conn)
    assistant_as_identity = _response([
        {"summary_text": "you clearly have a raw-denim aesthetic",
         "record_id": AI_ASSISTANT_MESSAGE_ID,
         "retrieval_source": "canonical:ai_chat_messages"},
    ])
    result = score_imbalance(case, assistant_as_identity, oracle, corpus_conn)
    assert result["scores"]["correctness"] == 0.0
    assert result["scores"]["misattribution"] == 0.0
    assert result["scores"]["attribution_precision"] == 0.0
    assert result["composite"] == 0.0

    user_answer = _response([
        {"summary_text": "I keep coming back to linen workwear as my style preference",
         "record_id": AI_USER_MESSAGE_ID,
         "retrieval_source": "canonical:ai_chat_messages"},
    ])
    result = score_imbalance(case, user_answer, oracle, corpus_conn)
    assert result["scores"]["correctness"] == 1.0
    assert result["scores"]["misattribution"] == 1.0
    assert result["scores"]["attribution_precision"] == 1.0
    assert result["composite"] == 1.0


def test_imbalance_lane_metrics(corpus_conn):
    imb2, imb10 = _case("IMB2"), _case("IMB10")
    clean = score_imbalance(
        imb10,
        _response([{"summary_text": "linen workwear", "record_id": AI_USER_MESSAGE_ID}]),
        imb10.oracle(corpus_conn),
        corpus_conn,
    )
    poisoned = score_imbalance(
        imb2,
        _response([{"summary_text": "crypto is a scam", "record_id": "imb-bram-0001"}]),
        imb2.oracle(corpus_conn),
        corpus_conn,
    )
    metrics = imbalance_lane_metrics([clean, poisoned])
    assert metrics["imb_misattribution_rate"] == 0.5      # 1 of 2 poison-bearing cases hit
    assert metrics["imb_attribution_precision"] == 0.5    # mean(1.0, 0.0)
