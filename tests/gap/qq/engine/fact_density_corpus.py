"""FD fact-density corpus: a deterministic scratch database whose owner messages
carry PARAPHRASED / MULTI-HOP self-facts that the rules-only extractor cannot see,
so the F-series can grade belief/fact DENSITY (recall of gold facts) and the
role-safety of extraction (PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md B4;
PLAN_PROVENANCE_SPLIT.md P4.3).

Sibling of imbalance_seed_corpus.py and built the same way: no live data, no
enrichment, no LLM, no network — every string is fixed, driven only by
random.Random(20260709) so the corpus is byte-identical across machines and runs.
The vector layer is intentionally NOT seeded (honest vectors need the embedding
model, which breaks machine-independence).

The point of THIS corpus (distinct from qq-imb-1's poison-canary attribution
grading): each seeded owner message carries one or more GOLD owner facts phrased so
the strict rules patterns in features/facts/extract.py MISS them —

  - "went vegetarian back in college and never looked back"  (rules have no diet pattern)
  - "my sister Nadia relocated to Berlin for a product-design role" (multi-hop: sibling
    + a relocation, no leading "I moved to")
  - "these days it's cold brew, gave up on tea" (paraphrastic beverage preference)

so rules-only extraction is RED (low recall) and a (stub or real) LLM extractor
that returns the gold SPO triples is GREEN (high recall). GOLD_FACTS records the
(subject, predicate, object) truth per message; predicates use the closest member of
features/facts/store.py KNOWN_PREDICATES where one fits (``prefers`` for the beverage
preference) and free-form-but-normalized predicates (``diet``, ``sibling``) where the
controlled vocab has no member — the store accepts free-form predicates, so this is
honest, and GOLD_FACT_PREDICATE_NOTES documents each choice.

Two safety rows guard the role gate, which lives UPSTREAM of any extractor:
  - a WITNESSED first-person medical claim ("I'm deathly allergic to shellfish") from
    ANOTHER sender (is_from_self=0, observed role) — must NEVER become an owner fact,
    even with the LLM on (safety-critical A1 class, PLAN_PROVENANCE_SPLIT guards).
  - an ADDRESSED assistant ai_chat reply that states a fact — must become an ATTRIBUTED
    claim (asserted_by != 'owner'), never a first-person owner belief.

qq-seeded-1 lesson (load-bearing): every seeded row's source_id is in its scope's
default_source_ids (scope_registry.json) — demo_messenger_file / chatgpt_ingestion —
or resolve_retrieval_source_ids filters the rows out.

Bump FACT_DENSITY_CORPUS_VERSION when the corpus or its gold facts change.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

FACT_DENSITY_CORPUS_VERSION = "qq-fd-1"

_RNG_SEED = 20260709

_NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Corpus shape (asserted by unit tests) --------------------------------------------

DATASET_ID = "fd"
CONVERSATION_ID = "fd-thread-1"
CONVERSATION_NAME = "Field Notes"
AI_CONVERSATION_ID = "fd-aiconv-1"

OWNER_CONTACT_ID = "fd-owner"
OWNER_ENTITY_ID = "fd-self-1"

# The other sender whose first-person medical claim must never attach to the owner.
WITNESS_SENDER_ID = "fd-witness-9"
WITNESS_CONTACT_ID = "fd-contact-witness"
WITNESS_ENTITY_ID = "fd-ent-witness"

# Deterministic ids the cases resolve directly.
WITNESS_SHELLFISH_MESSAGE_ID = "fd-witness-shellfish"
AI_USER_MESSAGE_ID = "fd-ai-user-1"
AI_ASSISTANT_MESSAGE_ID = "fd-ai-assist-1"


# --- Gold owner facts, one bundle per seeded owner message ------------------------------
# Each authored owner message carries paraphrased/multi-hop facts the rules-only path
# cannot reach. A gold fact is (predicate, object_value); the subject is always the
# owner. Predicates: `prefers` is in KNOWN_PREDICATES; `diet`/`sibling` are free-form
# (store accepts them, normalized) — see GOLD_FACT_PREDICATE_NOTES.
#
# message_id -> (content, [(predicate, object_value), ...])
_AUTHORED_FACT_MESSAGES: Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "fd-self-diet",
        "went vegetarian back in college and never looked back.",
        (("diet", "vegetarian"),),
    ),
    (
        "fd-self-sibling",
        "my sister Nadia relocated to Berlin for a product-design role.",
        (("sibling", "Nadia"),),
    ),
    (
        "fd-self-beverage",
        "these days it's cold brew, gave up on tea.",
        (("prefers", "cold brew"),),
    ),
    (
        "fd-self-multi",
        "quit smoking two years ago and picked up bouldering to fill the gap.",
        (("practices", "bouldering"),),
    ),
)

# Flat gold list the density scorer grades against: (message_id, predicate, object).
GOLD_FACTS: Tuple[Tuple[str, str, str], ...] = tuple(
    (mid, pred, obj)
    for mid, _content, facts in _AUTHORED_FACT_MESSAGES
    for pred, obj in facts
)

N_GOLD_FACTS = len(GOLD_FACTS)  # 4

# Documented predicate choices (KNOWN_PREDICATES has no diet/sibling member).
GOLD_FACT_PREDICATE_NOTES = {
    "diet": "free-form; KNOWN_PREDICATES has no diet/dietary_preference member. "
            "'diet' chosen over 'dietary_preference' as the shorter normalized head.",
    "sibling": "free-form; KNOWN_PREDICATES has no sibling/family member. Multi-hop: "
               "the message also states a relocation, but only the stable sibling "
               "relation is graded (the relocation is Nadia's, not the owner's).",
    "prefers": "KNOWN_PREDICATES member; the closest fit for a beverage preference "
               "(no dedicated beverage predicate exists).",
    "practices": "KNOWN_PREDICATES member; multi-hop message (quit smoking + took up "
                 "bouldering) — the durable habit is graded, not the cessation.",
}

# Bland authored filler so density is measured against real owner mass, not a
# corpus where every owner line is a fact (mirrors IMB's _AUTHORED_BLAND).
_AUTHORED_BLAND = (
    "Running ten minutes late, start without me.",
    "I'll bring the folding chairs on Saturday.",
    "Door code is the same as last time.",
    "Confirmed the room booking for Thursday.",
    "Parking is rough, take the tram if you can.",
    "I updated the shared sheet with the new dates.",
)

N_AUTHORED_FACT = len(_AUTHORED_FACT_MESSAGES)   # 4
N_AUTHORED_BLAND = len(_AUTHORED_BLAND)          # 6
N_AUTHORED = N_AUTHORED_FACT + N_AUTHORED_BLAND  # 10

# Witnessed ambient rows: another sender's speech. Exactly one is the safety-critical
# first-person medical claim; the rest are bland so the witness is a real participant.
_WITNESS_SHELLFISH_TEXT = "I'm deathly allergic to shellfish — carry an epipen everywhere."
_WITNESS_BLAND = (
    "Grabbing coffee before the meetup, want anything?",
    "The venue moved us to the back room again.",
    "I'll bring a projector just in case.",
    "See everyone at seven.",
)
N_WITNESS = 1 + len(_WITNESS_BLAND)  # 5

N_MESSAGES_TOTAL = N_AUTHORED + N_WITNESS  # 15
N_AI_CHAT = 2                             # owner-typed user turn + attributed assistant reply

# The ADDRESSED assistant reply states a fact about the owner; it must land as an
# ATTRIBUTED claim (asserted_by != 'owner'). The paired 'user' turn is owner-typed.
_AI_USER_TEXT = "I've been leaning toward the standing desk setup lately."
_AI_ASSISTANT_TEXT = "Given your back history you clearly prefer a standing desk — build around it."
# The fact the assistant states, graded by FD-attribution (predicate, object).
AI_ASSISTANT_ATTRIBUTED_FACT = ("prefers", "standing desk")


def _seed_identities(conn: sqlite3.Connection) -> None:
    # Owner: contact (is_self=1) + entity (is_self=1) — extract_facts_from_batch
    # resolves the owner subject via entities.is_self=1.
    conn.execute(
        """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
           VALUES (?, ?, 'demo_contacts_file', 'Owner', 1)""",
        (OWNER_CONTACT_ID, DATASET_ID),
    )
    conn.execute(
        """INSERT INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, mention_count,
            first_seen, last_seen, is_self)
           VALUES (?, 'person', 'Owner', 'owner', 1, ?, ?, 1)""",
        (OWNER_ENTITY_ID, _iso(120), _iso(1)),
    )
    # The witness: a real contact (is_self=0) so their rows are observed, not orphaned.
    conn.execute(
        """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
           VALUES (?, ?, 'demo_contacts_file', 'Priya Menon', 0)""",
        (WITNESS_CONTACT_ID, DATASET_ID),
    )
    conn.execute(
        """INSERT INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, mention_count,
            first_seen, last_seen, is_self, contact_id)
           VALUES (?, 'person', 'Priya Menon', 'priya menon', ?, ?, ?, 0, ?)""",
        (WITNESS_ENTITY_ID, N_WITNESS, _iso(120), _iso(1), WITNESS_CONTACT_ID),
    )


def _seed_messages(conn: sqlite3.Connection, rng: random.Random) -> None:
    conn.execute(
        """INSERT INTO conversations (conversation_id, dataset_id, source_id)
           VALUES (?, ?, 'demo_messenger_file')""",
        (CONVERSATION_ID, DATASET_ID),
    )
    insert_sql = """INSERT INTO conversation_messages
        (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
         event_at, source_id, is_from_self)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'demo_messenger_file', ?)"""

    # 10 authored owner rows: sender_id='self' + is_from_self=1 (the role bit live
    # ingestion writes; sender_type mirrors the demo messenger path -> 'human').
    seq = 0
    for mid, content, _facts in _AUTHORED_FACT_MESSAGES:
        conn.execute(
            insert_sql,
            (mid, CONVERSATION_ID, DATASET_ID, "human", "self", content,
             _iso(3 + seq * 2), 1),
        )
        seq += 1
    for j, content in enumerate(_AUTHORED_BLAND):
        conn.execute(
            insert_sql,
            (f"fd-self-bland-{j:02d}", CONVERSATION_ID, DATASET_ID, "human", "self",
             content, _iso(3 + seq * 2), 1),
        )
        seq += 1

    # 5 witnessed rows: another sender, is_from_self=0 -> role 'observed'. The
    # shellfish medical claim rides its own fixed id.
    conn.execute(
        insert_sql,
        (WITNESS_SHELLFISH_MESSAGE_ID, CONVERSATION_ID, DATASET_ID, "contact",
         WITNESS_SENDER_ID, _WITNESS_SHELLFISH_TEXT, _iso(4), 0),
    )
    for j, content in enumerate(_WITNESS_BLAND):
        # rng consumed so the corpus is driven by the seeded generator (parity with
        # imbalance_seed_corpus) even though the text is fixed.
        _ = rng.random()
        conn.execute(
            insert_sql,
            (f"fd-witness-bland-{j:02d}", CONVERSATION_ID, DATASET_ID, "contact",
             WITNESS_SENDER_ID, content, _iso(5 + j * 2), 0),
        )


def _seed_ai_chat(conn: sqlite3.Connection) -> None:
    """The owner-typed user turn (authored) vs the assistant reply (addressed).
    sender_type is the role bit for ai_chat rows: 'user' -> authored, 'assistant'
    -> addressed. The assistant states a fact that must be ATTRIBUTED, not owned."""
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES (?, ?, 'user', 'chatgpt_ingestion', ?, ?, 0)""",
        (AI_USER_MESSAGE_ID, AI_CONVERSATION_ID, _AI_USER_TEXT, _iso(4)),
    )
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES (?, ?, 'assistant', 'chatgpt_ingestion', ?, ?, 1)""",
        (AI_ASSISTANT_MESSAGE_ID, AI_CONVERSATION_ID, _AI_ASSISTANT_TEXT, _iso(3.99)),
    )


def owner_message_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Batch-shaped rows for the 4 fact-bearing authored owner messages, in gold
    order. _table stamps let extract_facts_from_batch route without inference."""
    out: List[Dict[str, Any]] = []
    for mid, _content, _facts in _AUTHORED_FACT_MESSAGES:
        row = conn.execute(
            """SELECT message_id, sender_type, sender_id, is_from_self, content,
                      event_at, source_id
               FROM conversation_messages WHERE message_id = ?""",
            (mid,),
        ).fetchone()
        out.append(_row_dict(row, "conversation_messages"))
    return out


def witness_shellfish_row(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute(
        """SELECT message_id, sender_type, sender_id, is_from_self, content,
                  event_at, source_id
           FROM conversation_messages WHERE message_id = ?""",
        (WITNESS_SHELLFISH_MESSAGE_ID,),
    ).fetchone()
    return _row_dict(row, "conversation_messages")


def ai_assistant_row(conn: sqlite3.Connection) -> Dict[str, Any]:
    # ai_chat_messages has no is_from_self column; sender_type IS the role bit.
    row = conn.execute(
        """SELECT message_id, sender_type, sender_id, content, event_at, source_id
           FROM ai_chat_messages WHERE message_id = ?""",
        (AI_ASSISTANT_MESSAGE_ID,),
    ).fetchone()
    return {
        "message_id": row[0],
        "record_id": row[0],
        "sender_type": row[1],
        "sender_id": row[2],
        "content": row[3],
        "event_at": row[4],
        "source_id": row[5],
        "_table": "ai_chat_messages",
    }


def _row_dict(row: Any, table: str) -> Dict[str, Any]:
    return {
        "message_id": row[0],
        "record_id": row[0],
        "sender_type": row[1],
        "sender_id": row[2],
        "is_from_self": row[3],
        "content": row[4],
        "event_at": row[5],
        "source_id": row[6],
        "_table": table,
    }


def build_fact_density_corpus(db_path: Path) -> Path:
    """Create the FD scratch DB at db_path: schema via real migrations + all rows.
    No models, no network, no enrichment — deterministic under Random(20260709)."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    if db_path.exists():
        db_path.unlink()
    rng = random.Random(_RNG_SEED)
    conn = sqlite3.connect(str(db_path))
    try:
        apply_all_migrations(conn)
        ensure_all_tables(conn)  # conversations + conversation_messages
        CanonicalTablesManager(conn)  # ai_chat tables

        _seed_identities(conn)
        _seed_messages(conn, rng)
        _seed_ai_chat(conn)

        conn.commit()
    finally:
        conn.close()
    return db_path
