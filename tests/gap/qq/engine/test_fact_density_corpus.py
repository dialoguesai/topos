"""Guards for the FD fact-density lane (PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md B4).

Pure and deterministic: builds the qq-fd-1 scratch corpus through the real
migrations in a tmpdir (no models, no network) and pins its shape — the owner /
witness / ai-chat row counts, the gold owner facts, the source_id scope gate, the
role bits — then grades the density lane with a STUB LLM extractor so the whole
lane is machine-independent. NO real ollama is ever called (extract_owner_facts_llm
raises if handed extractor=None).

The load-bearing assertions:
  - rules-only recall is LOW (the red): the strict patterns miss every paraphrased /
    multi-hop gold fact.
  - stub-LLM recall is HIGH (the green target) with clean precision.
  - the witnessed shellfish row yields ZERO owner facts even with an extractor that
    WOULD emit an owner triple — the role gate is upstream.
  - the addressed assistant reply becomes an ATTRIBUTED claim, never an owner belief.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fact_density_cases import (
    FACT_DENSITY_CASES,
    Triple,
    extract_facts_rules_only,
    extract_owner_facts_llm,
    fact_density_lane_metrics,
    score_attribution,
    score_fact_density,
    score_role_safety,
)
from fact_density_corpus import (
    AI_ASSISTANT_ATTRIBUTED_FACT,
    AI_ASSISTANT_MESSAGE_ID,
    AI_USER_MESSAGE_ID,
    FACT_DENSITY_CORPUS_VERSION,
    GOLD_FACTS,
    N_AI_CHAT,
    N_AUTHORED,
    N_AUTHORED_FACT,
    N_GOLD_FACTS,
    N_MESSAGES_TOTAL,
    N_WITNESS,
    WITNESS_SHELLFISH_MESSAGE_ID,
    build_fact_density_corpus,
)

_SCOPE_REGISTRY = (
    Path(__file__).resolve().parents[4] / "topos" / "query" / "scope_registry.json"
)


# --- The STUB LLM extractor (deterministic, NO network) --------------------------------
# Maps each seeded message's content to the SPO triples a competent LLM extractor
# WOULD return. The gold-bearing owner rows get their gold triples. The shellfish and
# assistant rows get the triples a naive extractor would emit for their first-person
# content — the ROLE GATE (not the stub) is what must stop / re-attribute them, so the
# stub deliberately hands the gate a hot potato.

_STUB_TRIPLES: Dict[str, List[Triple]] = {
    "went vegetarian back in college": [("owner", "diet", "vegetarian")],
    "my sister Nadia relocated to Berlin": [("owner", "sibling", "Nadia")],
    "these days it's cold brew": [("owner", "prefers", "cold brew")],
    "picked up bouldering": [("owner", "practices", "bouldering")],
    # Hot potatoes for the gate: first-person content the extractor reads literally.
    "deathly allergic to shellfish": [("owner", "allergic_to", "shellfish")],
    "standing desk": [("owner", "prefers", "standing desk")],
}


def _stub_extractor(row: Dict[str, Any]) -> List[Triple]:
    content = str(row.get("content") or "")
    for needle, triples in _STUB_TRIPLES.items():
        if needle.lower() in content.lower():
            return list(triples)
    return []


def _stub_llm_path(conn: sqlite3.Connection, rows: List[Dict[str, Any]]):
    return extract_owner_facts_llm(conn, rows, extractor=_stub_extractor)


@pytest.fixture(scope="module")
def corpus_conn() -> sqlite3.Connection:
    import tempfile

    db_path = build_fact_density_corpus(Path(tempfile.mkdtemp()) / "fd.db")
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _one(conn: sqlite3.Connection, sql: str, *params) -> Any:
    return conn.execute(sql, params).fetchone()[0]


# --- Corpus shape ----------------------------------------------------------------------


def test_version_is_pinned():
    assert FACT_DENSITY_CORPUS_VERSION == "qq-fd-1"


def test_exact_row_counts(corpus_conn):
    assert _one(corpus_conn, "SELECT COUNT(*) FROM conversation_messages") == N_MESSAGES_TOTAL == 15
    assert _one(
        corpus_conn, "SELECT COUNT(*) FROM conversation_messages WHERE is_from_self = 1"
    ) == N_AUTHORED == 10
    assert _one(
        corpus_conn, "SELECT COUNT(*) FROM conversation_messages WHERE is_from_self = 0"
    ) == N_WITNESS == 5
    assert _one(corpus_conn, "SELECT COUNT(*) FROM ai_chat_messages") == N_AI_CHAT == 2


def test_authored_rows_carry_the_role_bit(corpus_conn):
    # Owner rows: sender_id='self' + is_from_self=1 — and NOTHING else is.
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE sender_id='self' AND is_from_self!=1",
    ) == 0
    assert _one(
        corpus_conn,
        "SELECT COUNT(*) FROM conversation_messages WHERE is_from_self=1 AND sender_id!='self'",
    ) == 0
    types = {
        r[0] for r in corpus_conn.execute("SELECT DISTINCT sender_type FROM conversation_messages")
    }
    assert types == {"contact", "human"}  # never an invented 'self' value
    # ai_chat role bit lives in sender_type.
    assert _one(
        corpus_conn, "SELECT sender_type FROM ai_chat_messages WHERE message_id=?", AI_USER_MESSAGE_ID
    ) == "user"
    assert _one(
        corpus_conn, "SELECT sender_type FROM ai_chat_messages WHERE message_id=?", AI_ASSISTANT_MESSAGE_ID
    ) == "assistant"


def test_gold_facts_are_planted_and_owner_authored(corpus_conn):
    assert len(GOLD_FACTS) == N_GOLD_FACTS == 4
    # Every gold-bearing message is a distinct owner-authored row.
    gold_mids = {mid for mid, _p, _o in GOLD_FACTS}
    assert len(gold_mids) == N_AUTHORED_FACT == 4
    for mid in gold_mids:
        assert _one(
            corpus_conn,
            "SELECT is_from_self FROM conversation_messages WHERE message_id=?",
            mid,
        ) == 1, mid
    # The shellfish medical claim is a WITNESSED row (is_from_self=0), never the owner's.
    assert _one(
        corpus_conn,
        "SELECT is_from_self FROM conversation_messages WHERE message_id=?",
        WITNESS_SHELLFISH_MESSAGE_ID,
    ) == 0
    assert "allergic to shellfish" in _one(
        corpus_conn,
        "SELECT content FROM conversation_messages WHERE message_id=?",
        WITNESS_SHELLFISH_MESSAGE_ID,
    )


def test_every_seeded_source_id_is_in_scope_registry_defaults(corpus_conn):
    registry = json.loads(_SCOPE_REGISTRY.read_text())
    defaults = {
        s["scope_id"]: set(s.get("default_source_ids") or []) for s in registry["scopes"]
    }
    table_to_scope = {
        "conversation_messages": "messages:read",
        "conversations": "messages:read",
        "ai_chat_messages": "ai_conversations:read",
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


def test_corpus_is_deterministic(tmp_path):
    """Byte-identical message content across two independent builds (Random(seed))."""
    a = build_fact_density_corpus(tmp_path / "a.db")
    b = build_fact_density_corpus(tmp_path / "b.db")
    ca, cb = sqlite3.connect(str(a)), sqlite3.connect(str(b))
    try:
        rows_a = ca.execute(
            "SELECT message_id, content FROM conversation_messages ORDER BY message_id"
        ).fetchall()
        rows_b = cb.execute(
            "SELECT message_id, content FROM conversation_messages ORDER BY message_id"
        ).fetchall()
        assert rows_a == rows_b
    finally:
        ca.close()
        cb.close()


# --- FD-density: rules-only is RED, stub-LLM is GREEN -----------------------------------


def test_rules_only_recall_is_low_the_red(corpus_conn):
    """The RED baseline: strict patterns miss every paraphrased/multi-hop gold fact."""
    result = score_fact_density(extract_facts_rules_only, corpus_conn)
    assert result["recall"] == 0.0, result
    assert result["missed"] and not result["hit"]
    # And it fabricates nothing — a low-recall floor, not a noisy one.
    assert result["precision"] == 1.0
    assert result["fabricated"] == []


def test_stub_llm_recall_is_high_the_green_target(corpus_conn):
    """The GREEN target: the stub LLM extractor recovers all gold facts, cleanly."""
    result = score_fact_density(_stub_llm_path, corpus_conn)
    assert result["recall"] == 1.0, result
    assert result["precision"] == 1.0
    assert result["fabricated"] == []
    assert set(result["hit"]) == {f"{p}={o}" for _m, p, o in GOLD_FACTS}
    assert result["missed"] == []


def test_lane_metrics_report_the_lift(corpus_conn):
    rules = score_fact_density(extract_facts_rules_only, corpus_conn)
    llm = score_fact_density(_stub_llm_path, corpus_conn)
    metrics = fact_density_lane_metrics(rules, llm)
    assert metrics["fd_recall_rules"] == 0.0
    assert metrics["fd_recall_llm"] == 1.0
    assert metrics["fd_recall_lift"] == 1.0   # the B4 win
    assert metrics["fd_precision_llm"] == 1.0


# --- FD-role-safety: witnessed row => 0 owner facts, regardless of extractor -----------


def test_witnessed_shellfish_yields_zero_owner_facts_even_with_llm(corpus_conn):
    """Safety-critical A1: the stub WOULD emit ('owner','allergic_to','shellfish') for
    the first-person medical claim, but the row is 'observed' (is_from_self=0) so the
    upstream gate never calls the extractor — zero owner facts."""
    # Sanity: the stub really does try to emit an owner triple for this content.
    from fact_density_corpus import witness_shellfish_row

    assert _stub_extractor(witness_shellfish_row(corpus_conn)) == [
        ("owner", "allergic_to", "shellfish")
    ]
    result = score_role_safety(_stub_extractor, corpus_conn)
    assert result["owner_facts"] == 0
    assert result["passed"] is True

    # A hallucinating extractor that emits an owner triple for EVERYTHING still can't
    # mint an owner fact from the observed row (the gate is upstream of the callable).
    def _hallucinator(row: Dict[str, Any]):
        return [("owner", "diet", "carnivore")]

    result2 = score_role_safety(_hallucinator, corpus_conn)
    assert result2["owner_facts"] == 0


# --- FD-attribution: addressed row => attributed claim, never owner belief -------------


def test_addressed_assistant_reply_becomes_attributed_claim(corpus_conn):
    result = score_attribution(_stub_extractor, corpus_conn)
    assert result["owner_facts"] == 0        # never an owner belief
    assert result["attributed"] >= 1         # it IS captured, attributed
    assert result["asserted_by"] == ["assistant"]
    assert result["passed"] is True


def test_addressed_fact_carries_the_stated_predicate(corpus_conn):
    """The attributed claim carries the fact the assistant actually stated."""
    from fact_density_corpus import ai_assistant_row

    facts = extract_owner_facts_llm(
        corpus_conn,
        [ai_assistant_row(corpus_conn)],
        extractor=_stub_extractor,
    )
    pred, obj = AI_ASSISTANT_ATTRIBUTED_FACT
    assert any(
        f["predicate"] == pred and obj in f["object_value"] and f["asserted_by"] == "assistant"
        for f in facts
    ), facts


def test_attributed_fact_asserts_with_non_owner_asserted_by(corpus_conn, tmp_path):
    """End-to-end through FactStore: asserting the addressed row writes a fact whose
    payload asserted_by != 'owner' (P4.4 attribution), on a throwaway DB copy so the
    module corpus stays read-only."""
    from fact_density_corpus import ai_assistant_row

    db = build_fact_density_corpus(tmp_path / "attr.db")
    conn = sqlite3.connect(str(db))
    try:
        extract_owner_facts_llm(
            conn, [ai_assistant_row(conn)], extractor=_stub_extractor, assert_facts=True
        )
        rows = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload_json FROM signal_objects WHERE object_type='fact'"
            )
        ]
        assert rows, "no fact asserted"
        assert all(r.get("asserted_by") == "assistant" for r in rows), rows
        assert all(r.get("asserted_by") != "owner" for r in rows)
    finally:
        conn.close()


# --- Case catalog ----------------------------------------------------------------------


def test_case_catalog_shape():
    assert [c.id for c in FACT_DENSITY_CASES] == [
        "FD-density", "FD-role-safety", "FD-attribution"
    ]
    negatives = {c.id for c in FACT_DENSITY_CASES if c.negative}
    assert negatives == {"FD-role-safety"}


def test_extract_owner_facts_llm_refuses_none_extractor(corpus_conn):
    """The lane must never fall through to real ollama: extractor=None raises."""
    from fact_density_corpus import owner_message_rows

    with pytest.raises(RuntimeError):
        extract_owner_facts_llm(corpus_conn, owner_message_rows(corpus_conn), extractor=None)
