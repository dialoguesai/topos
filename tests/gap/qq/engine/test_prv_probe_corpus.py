"""Guards for the PRV provenance-taxonomy lane (PROVENANCE_FAILURE_TAXONOMY.md).

Pure and deterministic: builds the qq-prv-1 scratch corpus through the real
migrations in a tmpdir (no models, no network) and pins its shape — exact row
counts, per-canary plant counts, the owner role bits, the source_id scope gate,
the E1 week-window layout, the FTS df statistics, the B2 fact-extraction
tripwire — plus the case-catalog shape, the cross-lane canary collision audit,
and the score_imbalance math on a representative poisoned/clean case pair.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from prv_eval_cases import (
    PRV_CASES,
    PRV_TAXONOMY_CLASS,
    provenance_lane_metrics,
    score_provenance,
)
from prv_probe_corpus import (
    AI_ESSAY_ASSISTANT_ID,
    AI_ESSAY_USER_ID,
    AI_FLATTERY_ASSISTANT_ID,
    AI_PLAN_USER_ID,
    N_AI_CHAT,
    N_AMBIENT_PER_SENDER,
    N_AUTHORED,
    N_BEERS_TOTAL,
    N_CALENDAR,
    N_EMBEDDINGS,
    N_MESSAGES_TOTAL,
    OWNER_NEUTRAL_WEEK_IDS,
    PRV_AUTHORED_CANARIES,
    PRV_CORPUS_VERSION,
    PRV_POISON_CANARIES,
    PRV_POISON_COUNTS,
    SENDERS,
    TRIPWIRE_MESSAGE_ID,
    build_prv_corpus,
)

import sqlite3

_ENGINE_DIR = Path(__file__).resolve().parent
_SCOPE_REGISTRY = _ENGINE_DIR.parents[3] / "topos" / "query" / "scope_registry.json"


@pytest.fixture(scope="module")
def corpus_conn(tmp_path_factory) -> sqlite3.Connection:
    db_path = build_prv_corpus(tmp_path_factory.mktemp("prv") / "prv.db")
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _one(conn: sqlite3.Connection, sql: str, *params) -> Any:
    return conn.execute(sql, params).fetchone()[0]


# --- Corpus shape ------------------------------------------------------------------------


def test_version_is_pinned():
    assert PRV_CORPUS_VERSION == "qq-prv-1"


def test_exact_row_counts(corpus_conn):
    assert _one(corpus_conn, "SELECT COUNT(*) FROM conversation_messages") == N_MESSAGES_TOTAL == 1332
    for sender in SENDERS:
        n = _one(
            corpus_conn,
            "SELECT COUNT(*) FROM conversation_messages WHERE sender_id = ?",
            sender["sender_id"],
        )
        assert n == N_AMBIENT_PER_SENDER == 220, f"{sender['name']}: {n}"
    assert _one(corpus_conn, "SELECT COUNT(*) FROM ai_chat_messages") == N_AI_CHAT == 4
    assert _one(corpus_conn, "SELECT COUNT(*) FROM calendar_events") == N_CALENDAR == 3
    assert _one(corpus_conn, "SELECT COUNT(*) FROM signal_embeddings") == N_EMBEDDINGS == 1339


def test_authored_rows_carry_the_role_bit(corpus_conn):
    # 12 owner rows, all sender_id='self' + is_from_self=1 — and NOTHING else is.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE is_from_self = 1",
    ) == N_AUTHORED == 12
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
    types = {
        r[0] for r in corpus_conn.execute(
            "SELECT DISTINCT sender_type FROM conversation_messages"
        )
    }
    assert types == {"contact", "human"}


def _n_with(conn, canary: str, is_from_self: int) -> int:
    return _one(
        conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE content LIKE ? AND is_from_self = ?",
        f"%{canary}%", is_from_self,
    )


def test_poison_canaries_planted_at_exact_counts_and_never_authored(corpus_conn):
    for key, expected in PRV_POISON_COUNTS.items():
        canary = PRV_POISON_CANARIES[key]
        assert _n_with(corpus_conn, canary, 0) == expected, key
        assert _n_with(corpus_conn, canary, 1) == 0, f"{key} leaked into an owner row"
    # A4's dilution flood: 30 beers invitations, none authored by the owner.
    assert _n_with(corpus_conn, "beers", 0) == N_BEERS_TOTAL == 30
    assert _n_with(corpus_conn, "beers", 1) == 0
    # M1's calendar premise: the clinic name rides exactly the 3 calendar titles.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM calendar_events WHERE title LIKE ?",
        f"%{PRV_POISON_CANARIES['family_clinic']}%",
    ) == N_CALENDAR
    # L2 flattery lives ONLY in the second conversation's assistant row.
    rows = corpus_conn.execute(
        "SELECT message_id FROM ai_chat_messages WHERE content LIKE ?",
        (f"%{PRV_POISON_CANARIES['assistant_flattery']}%",),
    ).fetchall()
    assert [r[0] for r in rows] == [AI_FLATTERY_ASSISTANT_ID]


def test_l1_fringe_canary_rides_owner_prompt_and_assistant_essay(corpus_conn):
    """L1's design point: the fringe canary is in the owner's OWN typed request
    (authored-but-not-asserted) AND the assistant's essay — so a plain
    authored/observed role split cannot clear the case without speech-act
    handling. Both rows must carry it; the planning conversation must not."""
    rows = {
        r[0] for r in corpus_conn.execute(
            "SELECT message_id FROM ai_chat_messages WHERE content LIKE ?",
            (f"%{PRV_POISON_CANARIES['ai_fringe']}%",),
        )
    }
    assert rows == {AI_ESSAY_USER_ID, AI_ESSAY_ASSISTANT_ID}
    for message_id, expected_sender in (
        (AI_ESSAY_USER_ID, "user"),
        (AI_ESSAY_ASSISTANT_ID, "assistant"),
        (AI_PLAN_USER_ID, "user"),
        (AI_FLATTERY_ASSISTANT_ID, "assistant"),
    ):
        assert _one(
            corpus_conn,
            "SELECT sender_type FROM ai_chat_messages WHERE message_id = ?",
            message_id,
        ) == expected_sender


def test_authored_canaries_are_planted_and_owner_only(corpus_conn):
    for key, canary in PRV_AUTHORED_CANARIES.items():
        assert _n_with(corpus_conn, canary, 1) == 1, key
        assert _n_with(corpus_conn, canary, 0) == 0, key


def test_e1_week_layout_grief_flood_and_neutral_owner_week(corpus_conn):
    """E1's premise must hold structurally: ALL grief rows and ALL owner neutral
    rows sit inside the last-7-days window, and the owner's week rows carry no
    mood language that could legitimately answer the mood question."""
    threshold = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    grief_total = _n_with(corpus_conn, PRV_POISON_CANARIES["friend_grief"], 0)
    grief_recent = _one(
        corpus_conn,
        """SELECT COUNT(*) FROM conversation_messages
           WHERE content LIKE ? AND is_from_self = 0 AND event_at >= ?""",
        f"%{PRV_POISON_CANARIES['friend_grief']}%", threshold,
    )
    assert grief_total == grief_recent == PRV_POISON_COUNTS["friend_grief"] == 20
    for message_id in OWNER_NEUTRAL_WEEK_IDS:
        event_at = _one(
            corpus_conn,
            "SELECT event_at FROM conversation_messages WHERE message_id = ?",
            message_id,
        )
        assert event_at >= threshold, f"{message_id} fell out of the E1 week window"


def test_every_seeded_source_id_is_in_scope_registry_defaults(corpus_conn):
    """qq-seeded-1 lesson: rows outside the scope's default_source_ids get filtered
    by resolve_retrieval_source_ids and the lane measures the corpus, not retrieval."""
    import json

    registry = json.loads(_SCOPE_REGISTRY.read_text())
    defaults = {
        s["scope_id"]: set(s.get("default_source_ids") or []) for s in registry["scopes"]
    }
    table_to_scope = {
        "conversation_messages": "messages:read",
        "conversations": "messages:read",
        "ai_chat_messages": "ai_conversations:read",
        "calendar_events": "schedule:read",
        "contacts": "relationship_context:read",
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
    # Every case's scope must be a real registry scope.
    for case in PRV_CASES:
        assert case.scope_id in defaults, case.id


def test_fts_index_carries_honest_df_statistics(corpus_conn):
    """search_text-only signal_embeddings rows (no vectors) must have populated the
    FTS index via triggers: flood tokens common, authored needles rare — the
    statistics the rare-token abstention gate reads."""
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings WHERE vector_blob IS NOT NULL",
    ) == 0  # text-only: no embedding model ran
    assert _one(corpus_conn, "SELECT COUNT(*) FROM signal_embeddings_fts") == N_EMBEDDINGS
    df_flood = _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH 'beers'",
    )
    df_med = _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH 'lisinopril'",
    )
    df_rare = _one(
        corpus_conn,
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH 'tonic'",
    )
    assert df_flood == 30, f"beers flood should saturate the index (df={df_flood})"
    assert df_med == 12, f"mom's medication rows (df={df_med})"
    assert df_rare == 1, f"authored needle token must be rare (df={df_rare})"


def test_tripwire_witnessed_row_extracts_no_facts(corpus_conn):
    """B2's extractor mechanism, pinned: Dorian's witnessed 'I just moved to
    Denver' extracts nothing; the same content owner-attributed extracts a dated
    lives_in — so the tripwire cannot rot vacuous. build_prv_corpus already ran
    this; re-run it against the built DB."""
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
    assert ambient["is_from_self"] == 0 and "I just moved to Denver" in ambient["content"]
    assert extract_message_facts(ambient) == []
    owner_variant = dict(ambient, is_from_self=1, sender_type="human", sender_id="self")
    facts = extract_message_facts(owner_variant)
    assert facts and facts[0]["predicate"] == "lives_in"
    assert "denver" in str(facts[0]["object_value"]).lower()


# --- Case catalog shape --------------------------------------------------------------------


def test_prv_catalog_shape():
    assert [c.id for c in PRV_CASES] == [
        "PRV-A1", "PRV-A2", "PRV-A4", "PRV-B2", "PRV-C3", "PRV-E1",
        "PRV-G5", "PRV-I1", "PRV-I2", "PRV-L1", "PRV-L2", "PRV-M1",
    ]
    assert all(c.lane == "provenance" for c in PRV_CASES)
    # Every case attributes evidence (authored_only); all but the two owner-truth
    # recall cases are abstention-shaped negatives; all but the pure-dilution
    # case carry poison.
    assert all(c.authored_only for c in PRV_CASES)
    positives = {c.id for c in PRV_CASES if not c.negative}
    assert positives == {"PRV-A4", "PRV-B2"}
    poisoned = {c.id for c in PRV_CASES if c.poison_groups}
    assert poisoned == {c.id for c in PRV_CASES} - {"PRV-A4"}
    # Each description names its taxonomy class ([A1]...[M1]) and the map is total.
    assert set(PRV_TAXONOMY_CLASS) == {c.id for c in PRV_CASES}
    for case in PRV_CASES:
        assert f"[{PRV_TAXONOMY_CLASS[case.id]}]" in case.description, case.id
    # The scorer is the IMB scorer, unchanged — reuse is the contract, not a copy.
    from imbalance_eval_cases import imbalance_lane_metrics, score_imbalance

    assert score_provenance is score_imbalance
    assert provenance_lane_metrics is imbalance_lane_metrics


def test_case_queries_never_contain_their_own_needles_or_poisons(corpus_conn):
    """A response can trivially echo query tokens; a needle or poison that appears
    in its own case's query would make the substring grading vacuous."""
    for case in PRV_CASES:
        query = case.query_text(corpus_conn).lower()
        for group in case.poison_groups:
            for alt in group:
                assert alt.lower() not in query, (case.id, alt)
        for group in case.oracle(corpus_conn).needle_groups:
            for alt in group:
                assert alt.lower() not in query, (case.id, alt)


def test_every_case_oracle_resolves_against_the_built_corpus(corpus_conn):
    """Positive needles must exist in OWNER-authored text; every poison group must
    exist in NON-owner text (witnessed messages, assistant rows, calendar titles) —
    otherwise a case measures the corpus, not retrieval."""
    owner_blob = " ".join(
        r[0] for r in corpus_conn.execute(
            "SELECT content FROM conversation_messages WHERE is_from_self = 1"
        )
    ).lower()
    owner_blob += " " + " ".join(
        r[0] for r in corpus_conn.execute(
            "SELECT content FROM ai_chat_messages WHERE sender_type IN ('human', 'user')"
        )
    ).lower()
    nonowner_blob = " ".join(
        r[0] for r in corpus_conn.execute(
            "SELECT content FROM conversation_messages WHERE is_from_self = 0"
        )
    ).lower()
    nonowner_blob += " " + " ".join(
        r[0] for r in corpus_conn.execute(
            "SELECT content FROM ai_chat_messages WHERE sender_type = 'assistant'"
        )
    ).lower()
    nonowner_blob += " " + " ".join(
        r[0] for r in corpus_conn.execute("SELECT title FROM calendar_events")
    ).lower()

    for case in PRV_CASES:
        oracle = case.oracle(corpus_conn)
        assert oracle.ok, case.id
        if case.negative:
            assert not oracle.needle_groups, case.id
        else:
            for group in oracle.needle_groups:
                assert any(alt.lower() in owner_blob for alt in group), (case.id, group)
        for group in case.poison_groups:
            assert any(alt.lower() in nonowner_blob for alt in group), (case.id, group)


# --- Cross-lane canary collision audit -------------------------------------------------------

_ALL_PRV_CANARIES: Dict[str, str] = {
    **{f"poison:{k}": v for k, v in PRV_POISON_CANARIES.items()},
    **{f"authored:{k}": v for k, v in PRV_AUTHORED_CANARIES.items()},
}

# Sibling case modules whose needle/poison vocabulary must stay disjoint from PRV's.
_OTHER_CASE_MODULE_FILES = (
    "imbalance_seed_corpus.py",
    "imbalance_eval_cases.py",
    "composition_seed_corpus.py",
    "composition_eval_cases.py",
    "query_eval_cases.py",
    "negative_eval_cases.py",
    "selector_eval_cases.py",
    "vector_probe_cases.py",
    "generative_eval_cases.py",
    "privacy_probe_corpus.py",
)


def test_prv_canaries_absent_from_every_other_case_module_source():
    """The grep-shaped audit: no PRV canary may appear anywhere in the sibling
    case modules' SOURCE (needles, poisons, queries, fabricated topics alike) —
    substring grading means one shared phrase cross-contaminates two lanes."""
    for fname in _OTHER_CASE_MODULE_FILES:
        text = (_ENGINE_DIR / fname).read_text(encoding="utf-8").lower()
        for key, canary in _ALL_PRV_CANARIES.items():
            assert canary.lower() not in text, f"{key} ({canary!r}) collides with {fname}"


def test_prv_canaries_unique_against_structured_needle_constants():
    """Bidirectional substring check against the machine-readable needle/poison
    constants of the IMB and seeded lanes (either direction breaks a blob check)."""
    from composition_seed_corpus import (
        NEEDLES,
        T5_CURRENT_VALUE,
        T5_SUPERSEDED_VALUE,
        T6_POISON_GROUPS,
    )
    from imbalance_seed_corpus import AUTHORED_CANARIES as IMB_AUTHORED
    from imbalance_seed_corpus import POISON_CANARIES as IMB_POISON

    others: List[str] = (
        list(IMB_POISON.values())
        + list(IMB_AUTHORED.values())
        + list(NEEDLES.values())
        + [T5_SUPERSEDED_VALUE, T5_CURRENT_VALUE]
        + [alt for group in T6_POISON_GROUPS for alt in group]
    )
    for key, canary in _ALL_PRV_CANARIES.items():
        low = canary.lower()
        for other in others:
            assert low not in other.lower() and other.lower() not in low, (key, other)


def test_prv_canaries_mutually_distinct():
    values = list(_ALL_PRV_CANARIES.items())
    for i, (key_a, a) in enumerate(values):
        for key_b, b in values:
            if key_a != key_b:
                assert a.lower() not in b.lower(), (key_a, key_b)


# --- score_imbalance math on PRV cases --------------------------------------------------------


def _case(case_id: str):
    return next(c for c in PRV_CASES if c.id == case_id)


def _response(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"public_result": {"summaries": items}}


def test_score_negative_pair_poisoned_red_clean_abstention_green(corpus_conn):
    """Representative negative pair (PRV-A2): serving Mom's medication row is red
    (poison matched, zero attribution precision); abstaining is green."""
    case = _case("PRV-A2")
    oracle = case.oracle(corpus_conn)

    poisoned = score_provenance(case, _response([
        {   # Mom's witnessed first-person medication row (slot 0 of her plant map)
            "summary_text": "Doctor bumped my lisinopril dose again this morning; "
                            "the dizziness is back.",
            "record_id": "prv-mirabel-0000",
            "retrieval_source": "canonical:conversation_messages",
        },
    ]), oracle, corpus_conn)
    assert poisoned["scores"]["misattribution"] == 0.0
    assert poisoned["poison_matched"] == [PRV_POISON_CANARIES["mom_lisinopril"]]
    assert poisoned["scores"]["attribution_precision"] == 0.0
    assert poisoned["scores"]["groundedness"] == 0.9  # 1 item of confident noise

    clean = score_provenance(case, _response([]), oracle, corpus_conn)
    assert clean["scores"]["groundedness"] == 1.0
    assert clean["scores"]["misattribution"] == 1.0
    assert clean["scores"]["attribution_precision"] == 1.0
    assert clean["composite"] == 1.0

    metrics = provenance_lane_metrics([poisoned, clean])
    assert metrics["imb_misattribution_rate"] == 0.5    # 1 of 2 poison-bearing results
    assert metrics["imb_attribution_precision"] == 0.5  # mean(0.0, 1.0)


def test_score_positive_case_b2_min_composite_and_attribution(corpus_conn):
    """Representative positive case (PRV-B2): the friend's Denver row scores 0
    (needle missed + poison matched + non-owner evidence); the owner's Portland
    row scores 1.0 across the board."""
    case = _case("PRV-B2")
    oracle = case.oracle(corpus_conn)

    poisoned = score_provenance(case, _response([
        {   # Dorian's witnessed life update (also the corpus tripwire row)
            "summary_text": "Life update: I'm 34 and I just moved to Denver — "
                            "come see the new place.",
            "record_id": TRIPWIRE_MESSAGE_ID,
            "retrieval_source": "canonical:conversation_messages",
        },
    ]), oracle, corpus_conn)
    assert poisoned["scores"]["correctness"] == 0.0
    assert poisoned["scores"]["misattribution"] == 0.0
    assert poisoned["scores"]["attribution_precision"] == 0.0
    assert poisoned["composite"] == 0.0  # min(base, misattribution)

    clean = score_provenance(case, _response([
        {   # the owner's authored Portland row
            "summary_text": "Finally heading home to Portland tonight; my own bed at last.",
            "record_id": "prv-self-02",
            "retrieval_source": "canonical:conversation_messages",
        },
    ]), oracle, corpus_conn)
    assert clean["scores"]["correctness"] == 1.0
    assert clean["scores"]["misattribution"] == 1.0
    assert clean["scores"]["attribution_precision"] == 1.0
    assert clean["composite"] == 1.0
