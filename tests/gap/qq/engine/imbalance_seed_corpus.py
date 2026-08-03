"""IMB imbalance/attribution corpus: a deterministic scratch database where 99% of
the text is OTHER PEOPLE'S speech and ~1% is the owner's, so the IMB-series can
grade whether retrieval knows WHOSE rows it is serving (PLAN_PROVENANCE_SPLIT.md §5).

Sibling of composition_seed_corpus.py and built the same way: no live data, no
enrichment, no LLM, no network — all ambient text comes from fixed template lists
driven by random.Random(20260708), so the corpus is byte-identical across machines
and runs. The vector layer is intentionally NOT seeded (honest vectors need the
embedding model, which breaks machine-independence); signal_embeddings rows are
seeded with search_text ONLY so the FTS5 triggers give the rare-token df gate
honest 99:1 frequency statistics without any model.

Role seeding uses only existing columns — zero migrations:
  conversation_messages.is_from_self/sender_id ('self'), ai_chat_messages.sender_type
  ('user'/'assistant'), contacts.is_self, entities.is_self, activity_events.activity_type
  ('visit' vs 'browser_highlight') + metadata_json (highlight/page_excerpt spans).

qq-seeded-1 lesson (load-bearing): every seeded row's source_id is in its scope's
default_source_ids (scope_registry.json) — demo_messenger_file / demo_browser_file /
demo_contacts_file / chatgpt_ingestion / stats_engine — or resolve_retrieval_source_ids
filters the rows out and the lane measures the corpus, not retrieval.

Bump IMB_CORPUS_VERSION when the corpus or its canaries change.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

IMB_CORPUS_VERSION = "qq-imb-1"

_RNG_SEED = 20260708

_NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Corpus shape (asserted by unit tests) --------------------------------------------

N_AMBIENT_PER_SENDER = 659          # x3 senders = 1977 observed messages
N_AMBIENT_TOTAL = 3 * N_AMBIENT_PER_SENDER
N_POISON_PER_SENDER = 40            # ambient rows carrying that sender's unique canary
N_AUTHORED = 23                     # owner messages (is_from_self=1, sender_id='self')
N_MESSAGES_TOTAL = N_AMBIENT_TOTAL + N_AUTHORED  # 2000 — the thread-volume stat's number
N_ACTIVITY = 300                    # browser rows (299 'visit' + 1 'browser_highlight')
N_AI_CHAT = 2                       # user-typed preference + assistant opinionated reply
N_EMBEDDINGS = N_MESSAGES_TOTAL + N_ACTIVITY + N_AI_CHAT  # search_text-only FTS rows

CONVERSATION_ID = "imb-groupchat-1"
CONVERSATION_NAME = "Harbor Collective"
DATASET_ID = "imb"

OWNER_CONTACT_ID = "imb-owner"
OWNER_ENTITY_ID = "imb-self-1"

# The message row the corpus tripwire runs fact extraction over: ambient
# first-person speech that WOULD extract a lives_in fact if it were the owner's.
TRIPWIRE_MESSAGE_ID = "imb-tomas-0011"

# Deterministic ids for the rows cases resolve directly.
HIGHLIGHT_EVENT_ID = "imb-act-0137"
PAGE_POISON_EVENT_ID = "imb-act-0042"
AI_USER_MESSAGE_ID = "imb-ai-user-1"
AI_ASSISTANT_MESSAGE_ID = "imb-ai-assist-1"


# --- Canaries --------------------------------------------------------------------------
# POISON canaries: other people's speech/output that must NEVER be presented as the
# owner's. Distinctive, never plausibly in real data (like NEEDLES in the S-corpus).
POISON_CANARIES = {
    "bram_glacier": "the glacier hostel co-op scheme is a terrible investment",
    "bram_crypto_stance": "crypto is a scam",
    "saskia_meadowfoam": "the meadowfoam honey varietal outperforms clover",
    "tomas_basalt": "the basalt cold-plunge ritual at dawn",
    "page_author": "cast-iron cookware is strictly superior",
    "assistant_style": "raw-denim aesthetic",
    "stat_thread_volume": "total thread volume: 2000",
    "mention_only_person": "Odile Ferrant",
}

# AUTHORED canaries: the owner's own expressions — the needles IMB cases must recall.
AUTHORED_CANARIES = {
    "belief_crypto": "personally I think cryptocurrency is genuinely promising for remittances",
    "interest_mandolin": "I've started learning fiddle tunes on the mandolin",
    "needle_greenhouse": "the tessellated greenhouse ledger is reconciled through March",
    "highlight_copper": "the copper still method halves fermentation time",
    "ai_style": "linen workwear",
    "stat_sent": "You sent 23 messages in Harbor Collective.",
}


# --- Senders and their persona templates ------------------------------------------------
# Persona stances are woven through templates so the ambient flood is topically loud:
# Bram (anti-crypto + fermentation), Saskia (urban beekeeping + pro-remote-work),
# Tomas (cold plunges + Marseille + vinyl). Template text deliberately avoids every
# authored-canary term (mandolin/remittances/tessellated/greenhouse/linen/copper still).
SENDERS = (
    {
        "key": "bram",
        "name": "Bram Holloway",
        "sender_id": "bram-7",
        "contact_id": "imb-contact-bram",
        "entity_id": "imb-ent-bram",
        "poison": (
            "Ran the numbers twice: "
            + POISON_CANARIES["bram_glacier"]
            + ", do not put money in."
        ),
        "templates": (
            "Third batch of kombucha this month — the scoby is finally behaving.",
            "Bottled the new kombucha with ginger today, the fizz is unreal.",
            "Anyone want a starter culture? My fermentation shelf is overflowing.",
            "Skimmed another whitepaper someone sent me — still convinced crypto is a scam.",
            "Told my cousin again: crypto is a scam, put it in an index fund instead.",
            "The sauerkraut crock hit day twelve, tastes sharp and clean.",
            "Fermentation update: the hot sauce mash is bubbling nicely.",
            "Please stop forwarding me coin pitches, every one smells like a rug pull.",
            "Brine ratio matters more than temperature for the pickles, fight me.",
            "New batch of miso going down for the long haul this weekend.",
        ),
        "odile_lines": (
            "Odile Ferrant wrote a whole thread on wild ferments once, someone find it.",
            "That article Odile Ferrant shared about starter cultures was solid.",
        ),
        "dossier_text": (
            "Bram Holloway is a core participant in the Harbor Collective thread; "
            "ferments everything and distrusts speculative finance."
        ),
    },
    {
        "key": "saskia",
        "name": "Saskia Vreeland",
        "sender_id": "saskia-3",
        "contact_id": "imb-contact-saskia",
        "entity_id": "imb-ent-saskia",
        "poison": (
            "Tasting notes confirmed it again: "
            + POISON_CANARIES["saskia_meadowfoam"]
            + " in every blind jar test."
        ),
        "templates": (
            "Checked the rooftop hives at dawn — the brood pattern looks strong.",
            "Urban beekeeping tip: give them water before the heat wave hits.",
            "Swarm season prep starts this week, the extra boxes are stacked.",
            "Remote work saved my mornings; I do hive checks before standup.",
            "Another meeting that could have been an email — remote-first forever.",
            "The linden trees are blooming and the bees are ecstatic.",
            "Requeened the second hive today, fingers crossed she takes.",
            "Honestly everyone is more productive at home, the data keeps saying so.",
            "Wax rendering day — the whole kitchen smells like honey.",
            "City council finally approved the community apiary expansion.",
        ),
        "odile_lines": (
            "Apparently Odile Ferrant keeps hives on her balcony too.",
            "Odile Ferrant would love the apiary expansion news.",
        ),
        "dossier_text": (
            "Saskia Vreeland is a core participant in the Harbor Collective thread; "
            "tends rooftop hives and champions working from home."
        ),
    },
    {
        "key": "tomas",
        "name": "Tomas Ferro",
        "sender_id": "tomas-9",
        "contact_id": "imb-contact-tomas",
        "entity_id": "imb-ent-tomas",
        "poison": (
            "Nothing resets the nervous system like "
            + POISON_CANARIES["tomas_basalt"]
            + ", I will die on this hill."
        ),
        "templates": (
            "Four minutes in the cold plunge this morning, mind like glass.",
            "Cold plunge before coffee is the only routine that ever stuck.",
            "Found a first pressing at the record fair — the vinyl haul grows.",
            "Cleaned the stylus and the whole record sounds newborn.",
            "Marseille in October is the correct answer, book the ferry.",
            "Still dreaming about the calanques outside Marseille from the last trip.",
            "The new pressing plant reissue sounds warmer than the digital master.",
            "Ice bath crew meets Saturday, the water is at seven degrees.",
            "Sorted the vinyl shelf by label this weekend, deeply satisfying.",
            "Planning the Marseille trip again — same corner cafe, same order.",
        ),
        "odile_lines": (
            "Odile Ferrant supposedly swims in the calanques year-round.",
            "Someone said Odile Ferrant has the entire early pressing catalogue.",
        ),
        "dossier_text": (
            "Tomas Ferro is a core participant in the Harbor Collective thread; "
            "into ice-water swims, Marseille trips and record collecting."
        ),
    },
)

# Ambient first-person speech that a role-blind extractor would turn into an owner
# fact ("I live in Marseille…" → lives_in). Planted on TRIPWIRE_MESSAGE_ID.
_TRIPWIRE_TEXT = (
    "I live in Marseille these days, at least in my head — counting weeks until the ferry."
)

_ODILE_SLOTS = (3, 7)  # ambient indices (per sender) that mention the passive person
_TRIPWIRE_SLOT = 11    # Tomas-only ambient index carrying the tripwire text

# The owner's 23 messages: 20 bland logistics (authored mass stays ~1% and cases
# can't win trivially) + 3 canaries at fixed slots.
_AUTHORED_BLAND = (
    "Running ten minutes late, start without me.",
    "I'll bring the folding chairs on Saturday.",
    "Door code is the same as last time.",
    "Can someone grab extra cups for the meetup?",
    "Confirmed the room booking for Thursday.",
    "I'll post the minutes tonight.",
    "Parking is rough, take the tram if you can.",
    "Rent split lands on the first, as usual.",
    "The projector cable is in the storage bin.",
    "Count me in for the cleanup shift.",
    "I'll be offline over the weekend, ping me Monday.",
    "Dropping the spare key with the neighbor.",
    "The hall rental payment went through.",
    "Let's keep intros to two minutes each.",
    "I updated the shared sheet with the new dates.",
    "Heads up: the elevator is out again.",
    "I can host next month if we rotate.",
    "Sending the agenda around before dinner.",
    "The printer toner finally arrived.",
    "Reminder: dues are collected at the door.",
)

_AUTHORED_CANARY_SLOTS = {
    2: (
        AUTHORED_CANARIES["belief_crypto"][0].upper()
        + AUTHORED_CANARIES["belief_crypto"][1:]
        + ", whatever the group chat says."
    ),
    10: AUTHORED_CANARIES["interest_mandolin"] + " and it is eating my evenings.",
    17: "For the co-op books: " + AUTHORED_CANARIES["needle_greenhouse"] + ".",
}

_ACTIVITY_TITLES = (
    "Cold Plunge Protocols — Ultimate Guide",
    "Urban Beekeeping Forum — hive splits thread",
    "Why the Latest Coin Is Overhyped — skeptic blog",
    "Kombucha Second Fermentation Timing Chart",
    "Vinyl Pressing Plants of Europe — directory",
    "Marseille Calanques — swimming access map",
    "Remote Work Productivity Studies — meta-review",
    "Honey Varietals Tasting Wheel",
    "Ice Bath Temperature Tables",
    "Record Fair Calendar — spring listings",
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

    # 1977 ambient rows: sender_type='contact', is_from_self=0 (spec §1). Each
    # sender's unique poison canary rides exactly N_POISON_PER_SENDER rows.
    for sender in SENDERS:
        planted = 0
        for i in range(N_AMBIENT_PER_SENDER):
            if i % 16 == 0 and planted < N_POISON_PER_SENDER:
                content = sender["poison"]
                planted += 1
            elif i in _ODILE_SLOTS:
                content = sender["odile_lines"][_ODILE_SLOTS.index(i)]
            elif sender["key"] == "tomas" and i == _TRIPWIRE_SLOT:
                content = _TRIPWIRE_TEXT
            else:
                content = sender["templates"][rng.randrange(len(sender["templates"]))]
            conn.execute(
                insert_sql,
                (
                    f"imb-{sender['key']}-{i:04d}",
                    CONVERSATION_ID,
                    DATASET_ID,
                    "contact",
                    sender["sender_id"],
                    content,
                    _iso(0.5 + i * 0.18),
                    0,
                ),
            )

    # 23 authored rows. sender_id='self' + is_from_self=1 is how ingestion marks
    # self-ness (conversations_tables.py:419); sender_type mirrors the demo
    # messenger path ('human') — never an invented 'self' value.
    for j in range(N_AUTHORED):
        content = _AUTHORED_CANARY_SLOTS.get(j, _AUTHORED_BLAND[j % len(_AUTHORED_BLAND)])
        conn.execute(
            insert_sql,
            (
                f"imb-self-{j:02d}",
                CONVERSATION_ID,
                DATASET_ID,
                "human",
                "self",
                content,
                _iso(2 + j * 5),
                1,
            ),
        )


def _seed_activity(conn: sqlite3.Connection) -> None:
    """300 browser rows under activity:read's default demo_browser_file source.
    Titles echo the SENDERS' topics — exposure, not the owner's expression. Exactly
    one row is expression-like (activity_type='browser_highlight' with a highlight
    span in metadata_json); exactly one carries a page-AUTHOR opinion poison."""
    for i in range(N_ACTIVITY):
        event_id = f"imb-act-{i:04d}"
        if event_id == HIGHLIGHT_EVENT_ID:
            activity_type = "browser_highlight"
            title = "Fermentation Methods Deep Dive"
            metadata = {
                "highlight": AUTHORED_CANARIES["highlight_copper"],
                "page_excerpt": "A survey of traditional fermentation and distilling equipment.",
            }
        elif event_id == PAGE_POISON_EVENT_ID:
            activity_type = "visit"
            title = "Kitchen Essentials Review — cookware"
            metadata = {
                "page_excerpt": (
                    "The author is adamant: "
                    + POISON_CANARIES["page_author"]
                    + ", and no serious cook disputes it."
                )
            }
        else:
            activity_type = "visit"
            title = _ACTIVITY_TITLES[i % len(_ACTIVITY_TITLES)]
            metadata = {"page_excerpt": f"Notes on {title.lower()}."}
        conn.execute(
            """INSERT INTO activity_events
               (event_id, activity_type, url, title, occurred_at, source_id, metadata_json)
               VALUES (?, ?, ?, ?, ?, 'demo_browser_file', ?)""",
            (
                event_id,
                activity_type,
                f"https://example.com/reading/{i}",
                title,
                _iso(1 + i * 0.3),
                json.dumps(metadata),
            ),
        )


def _seed_identities(conn: sqlite3.Connection) -> None:
    # Owner: contact (is_self=1) + entity (is_self=1).
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

    # The 3 ambient senders: contacts (is_self=0) + person entities + dossiers.
    for n, sender in enumerate(SENDERS):
        conn.execute(
            """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
               VALUES (?, ?, 'demo_contacts_file', ?, 0)""",
            (sender["contact_id"], DATASET_ID, sender["name"]),
        )
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count,
                first_seen, last_seen, is_self, contact_id)
               VALUES (?, 'person', ?, ?, ?, ?, ?, 0, ?)""",
            (
                sender["entity_id"],
                sender["name"],
                sender["name"].lower(),
                N_AMBIENT_PER_SENDER,
                _iso(120),
                _iso(1),
                sender["contact_id"],
            ),
        )
        conn.execute(
            """INSERT INTO signal_objects
               (object_id, signal_dimension, object_type, object_key, payload_json,
                confidence, valid_from, created_at, updated_at)
               VALUES (?, 'relationships', 'entity_dossier', ?, ?, 0.9, ?, ?, ?)""",
            (
                f"imb-dossier-{sender['key']}",
                f"dossier:{sender['entity_id']}",
                json.dumps(
                    {
                        "canonical_name": sender["name"],
                        "summary_text": sender["dossier_text"],
                        "top_connections": ["Owner"],
                        "stat_lines": [],
                    }
                ),
                _iso(2), _iso(2), _iso(2),
            ),
        )

    # Odile Ferrant: mention-only passive person. Entity + entity_mentions rows,
    # deliberately NO contacts row and NO dossier — she was never a participant.
    conn.execute(
        """INSERT INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, mention_count,
            first_seen, last_seen, is_self)
           VALUES ('imb-ent-odile', 'person', 'Odile Ferrant', 'odile ferrant', ?, ?, ?, 0)""",
        (len(SENDERS) * len(_ODILE_SLOTS), _iso(90), _iso(2)),
    )
    mention_n = 0
    for sender in SENDERS:
        for slot_idx, i in enumerate(_ODILE_SLOTS):
            # P3.1 / B6: Odile mentions are always on *others'* speech
            # (SENDERS' observed rows) — authored_by_owner=0 by construction.
            # Eval tripwire: test_odile_mentions_not_owner_authored.
            conn.execute(
                """INSERT INTO entity_mentions
                   (mention_id, entity_id, record_id, canonical_table, surface_text,
                    event_at, source_id, authored_by_owner)
                   VALUES (?, 'imb-ent-odile', ?, 'conversation_messages', ?, ?,
                           'demo_messenger_file', 0)""",
                (
                    f"imb-mention-odile-{mention_n}",
                    f"imb-{sender['key']}-{i:04d}",
                    sender["odile_lines"][slot_idx],
                    _iso(0.5 + i * 0.18),
                ),
            )
            mention_n += 1


def _seed_stats(conn: sqlite3.Connection) -> None:
    """The paired stats-honesty rows (S5 pattern): sent-count vs thread volume.
    23 vs 2000 — non-overlapping numerals so needle/poison checks can't
    substring-collide ('20' would match '2000')."""
    rows = (
        (
            f"stat:messages.volume.sent.by_thread:{CONVERSATION_ID}",
            "messages.volume.sent.by_thread",
            AUTHORED_CANARIES["stat_sent"],
            {"n": N_AUTHORED},
        ),
        (
            f"stat:messages.volume.by_thread:{CONVERSATION_ID}",
            "messages.volume.by_thread",
            f"{CONVERSATION_NAME} {POISON_CANARIES['stat_thread_volume']} messages.",
            {"n": N_MESSAGES_TOTAL},
        ),
    )
    for fact_id, record_id, tag, stat_summary in rows:
        conn.execute(
            """INSERT INTO signal_facts
               (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at)
               VALUES (?, 'relationships', 'stats_engine', ?, 'stats_engine_v1', 'topos', ?, ?)""",
            (
                fact_id,
                record_id,
                json.dumps(
                    {
                        "fact_id": fact_id,
                        "dimension": "relationships",
                        "source_id": "stats_engine",
                        "record_id": record_id,
                        "object_type": "stat_insight",
                        "tag": tag,
                        "summary_text": tag,
                        "stat_summary": stat_summary,
                        "group_key": CONVERSATION_ID,
                        "confidence": 1.0,
                        "disclosure": "owner_only",
                        "provider": "topos",
                        "model": "stats_engine_v1",
                    }
                ),
                _iso(1),
            ),
        )


def _seed_ai_chat(conn: sqlite3.Connection) -> None:
    """IMB10: the owner's typed preference vs the assistant's opinionated reply.
    sender_type is the role bit for ai_chat rows ('user' = owner-typed)."""
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES (?, 'imb-aiconv-1', 'user', 'chatgpt_ingestion', ?, ?, 0)""",
        (
            AI_USER_MESSAGE_ID,
            "I keep coming back to "
            + AUTHORED_CANARIES["ai_style"]
            + " as my style preference — that is what I actually wear.",
            _iso(4),
        ),
    )
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES (?, 'imb-aiconv-1', 'assistant', 'chatgpt_ingestion', ?, ?, 1)""",
        (
            AI_ASSISTANT_MESSAGE_ID,
            "Based on our chats you clearly have a "
            + POISON_CANARIES["assistant_style"]
            + " and should build your wardrobe around it.",
            _iso(3.99),
        ),
    )


def _seed_search_text_index(conn: sqlite3.Connection) -> None:
    """signal_embeddings rows with search_text ONLY (no vector_blob, no model):
    the FTS5 triggers (vector_storage_v3) populate signal_embeddings_fts, giving
    the rare-token abstention gate honest 99:1 df statistics. Without these rows
    an empty FTS index reads as 'nothing is rare' and abstention runs degraded."""
    rows = conn.execute(
        "SELECT message_id, content, source_id FROM conversation_messages"
    ).fetchall()
    rows += conn.execute(
        "SELECT event_id, title, source_id FROM activity_events"
    ).fetchall()
    rows += conn.execute(
        "SELECT message_id, content, source_id FROM ai_chat_messages"
    ).fetchall()
    for record_id, text, source_id in rows:
        conn.execute(
            """INSERT INTO signal_embeddings
               (embedding_id, record_id, source_id, signal_dimension, text_preview, search_text)
               VALUES (?, ?, ?, 'memory', ?, ?)""",
            (f"imb-emb-{record_id}", record_id, source_id, text, text),
        )


def _run_tripwire(conn: sqlite3.Connection) -> None:
    """Corpus tripwire: fact extraction over one ambient row MUST return [] —
    the _is_owner_authored guard (topos/features/facts/extract.py) is the one
    role-aware gate that already exists, and this corpus is its regression net.
    Also assert the same content WITH owner attribution extracts, so the tripwire
    can never rot into vacuity via pattern drift."""
    from topos.features.facts.extract import extract_message_facts

    row = conn.execute(
        """SELECT message_id, sender_type, sender_id, is_from_self, content, event_at
           FROM conversation_messages WHERE message_id = ?""",
        (TRIPWIRE_MESSAGE_ID,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"tripwire row {TRIPWIRE_MESSAGE_ID} missing from corpus")
    ambient = {
        "message_id": row[0],
        "sender_type": row[1],
        "sender_id": row[2],
        "is_from_self": row[3],
        "content": row[4],
        "event_at": row[5],
    }
    facts = extract_message_facts(ambient)
    if facts:
        raise AssertionError(
            "corpus tripwire: fact extraction returned facts for an AMBIENT row — "
            f"the owner-authorship guard regressed: {facts}"
        )
    owner_variant = dict(ambient, is_from_self=1, sender_type="human", sender_id="self")
    if not extract_message_facts(owner_variant):
        raise AssertionError(
            "corpus tripwire is vacuous: the same content no longer extracts even "
            "when owner-authored — the lives_in pattern drifted"
        )


def build_imbalance_corpus(db_path: Path) -> Path:
    """Create the IMB scratch DB at db_path: schema via real migrations + all rows."""
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
        _seed_activity(conn)
        _seed_stats(conn)
        _seed_ai_chat(conn)
        _seed_search_text_index(conn)

        # The mandolin interest is ALSO FactStore-asserted (T-series pattern), so
        # IMB3 can grade the fact layer's contribution alongside message recall.
        from topos.features.facts.store import FactStore

        FactStore(conn).assert_fact(
            subject_entity_id=OWNER_ENTITY_ID,
            predicate="hobby",
            object_value="fiddle tunes on the mandolin",
            dimension="interests",
            valid_from=_iso(20),
        )

        conn.commit()
        _run_tripwire(conn)
    finally:
        conn.close()
    return db_path
