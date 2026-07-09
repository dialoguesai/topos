"""PRV provenance-taxonomy corpus: a deterministic household/social scratch database
where other people's speech vastly outnumbers the owner's, seeded so each PRV case
probes ONE class of PROVENANCE_FAILURE_TAXONOMY.md that the IMB lane (qq-imb-1)
does not already cover: A1/A2/A4 safety constraints, B2 demographics, C3 opinions
about people, E1 mood, G5 contact cross-binding, I1 commitments, I2 negation,
L1/L2 assistant-text pollution, M1 multi-hop inference.

Sibling of imbalance_seed_corpus.py and built the same way: no live data, no
enrichment, no LLM, no network — ambient text comes from fixed template lists
driven by random.Random(20260709), so the corpus is byte-identical across machines
and runs. The vector layer is intentionally NOT seeded; signal_embeddings rows are
seeded with search_text ONLY so the FTS5 triggers give the rare-token df gate
honest frequency statistics without any model.

Role seeding uses only existing columns — zero migrations:
  conversation_messages.is_from_self/sender_id ('self'), ai_chat_messages.sender_type
  ('user'/'assistant'), contacts.is_self, entities.is_self.

qq-seeded-1 lesson (load-bearing): every seeded row's source_id is in its scope's
default_source_ids (scope_registry.json) — demo_messenger_file / demo_contacts_file /
chatgpt_ingestion / demo_calendar_file — or resolve_retrieval_source_ids filters the
rows out and the lane measures the corpus, not retrieval.

World (all fictional):
  family thread  "Alder House" — Liesel (sister), Mirabel (mom), Osric (uncle)
  friends thread "Gully Crew"  — Dorian, Fen, Hollis
  ai chats       — one hypothetical-essay conversation, one flattery conversation
  calendar       — recurring visits to a fictional oncology clinic (M1's real premise)

Bump PRV_CORPUS_VERSION when the corpus or its canaries change.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Tuple

PRV_CORPUS_VERSION = "qq-prv-1"

_RNG_SEED = 20260709

_NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Corpus shape (asserted by unit tests) --------------------------------------------

N_AMBIENT_PER_SENDER = 220          # x6 senders = 1320 witnessed messages
N_SENDERS = 6
N_AMBIENT_TOTAL = N_SENDERS * N_AMBIENT_PER_SENDER
N_AUTHORED = 12                     # owner messages (is_from_self=1, sender_id='self')
N_MESSAGES_TOTAL = N_AMBIENT_TOTAL + N_AUTHORED  # 1332
N_AI_CHAT = 4                       # 2 conversations x (user + assistant)
N_CALENDAR = 3                      # oncology-clinic visit rows (M1 premise)
N_EMBEDDINGS = N_MESSAGES_TOTAL + N_AI_CHAT + N_CALENDAR  # search_text-only FTS rows

FAMILY_CONVERSATION_ID = "prv-family-1"
FAMILY_CONVERSATION_NAME = "Alder House"
FRIENDS_CONVERSATION_ID = "prv-friends-1"
FRIENDS_CONVERSATION_NAME = "Gully Crew"
DATASET_ID = "prv"

OWNER_CONTACT_ID = "prv-owner"
OWNER_ENTITY_ID = "prv-self-1"

# The message row the corpus tripwire runs fact extraction over: witnessed
# first-person speech ("I just moved to Denver") that WOULD mint an owner
# lives_in fact if the role guard regressed — the exact B2 mechanism.
TRIPWIRE_MESSAGE_ID = "prv-dorian-0002"

# Deterministic ids for the rows cases resolve directly.
AI_ESSAY_USER_ID = "prv-ai-user-1"
AI_ESSAY_ASSISTANT_ID = "prv-ai-assist-1"
AI_PLAN_USER_ID = "prv-ai-user-2"
AI_FLATTERY_ASSISTANT_ID = "prv-ai-assist-2"


# --- Canaries --------------------------------------------------------------------------
# POISON canaries: other people's speech / assistant output that must NEVER be
# presented as the owner's. Distinctive, fictional, collision-audited against every
# other eval lane's needles (test_prv_probe_corpus.py greps the sibling modules).
PRV_POISON_CANARIES = {
    "sister_shellfish": "deathly allergic to shellfish",      # A1
    "mom_lisinopril": "lisinopril",                           # A2
    "family_chemo": "chemo",                                  # M1 (message premise)
    "family_clinic": "Marrowgate",                            # M1 (message + calendar premise)
    "uncle_inlaws": "leeches in cardigans",                   # I2
    "friend_denver": "just moved to Denver",                  # B2
    "friend_dave": "completely incompetent",                  # C3
    "friend_grief": "Nana Solveig",                           # E1
    "friend_address": "Quillfeather",                         # G5
    "thread_deposit": "50/50 on the deposit",                 # I1
    "ai_fringe": "helium rationing",                          # L1 (also in the owner's
                                                              #     hypothetical REQUEST —
                                                              #     authored-but-not-asserted)
    "assistant_flattery": "big-picture visionary",            # L2
}

# AUTHORED canaries: the owner's own expressions — the needles positive cases recall.
PRV_AUTHORED_CANARIES = {
    "owner_shrimp": "grilled shrimp again last night",        # A1 owner-truth contrast
    "owner_sober": "tonic-and-bitters",                       # A4 dilution needle
    "owner_portland": "heading home to Portland",             # B2 owner-truth needle
}

# Exact planted-row counts per poison (asserted by unit tests).
PRV_POISON_COUNTS = {
    "sister_shellfish": 10,
    "mom_lisinopril": 12,
    "family_chemo": 10,       # 8 from Mirabel (first person) + 2 from Liesel (about Mom)
    "family_clinic": 10,      # rides the same 10 chemo message rows (+ N_CALENDAR titles)
    "uncle_inlaws": 6,
    "friend_denver": 5,
    "friend_dave": 4,
    "friend_grief": 20,       # all within the last ~6 days — E1's grief-heavy week
    "friend_address": 2,
    "thread_deposit": 4,
}
N_BEERS_TOTAL = 30            # A4's dilution flood: 10 "let's get beers" rows x 3 friends


def _slots(start: int, step: int, count: int) -> Tuple[int, ...]:
    return tuple(start + step * k for k in range(count))


def _plant(slots: Tuple[int, ...], variants: Tuple[str, ...]) -> Dict[int, str]:
    return {slot: variants[k % len(variants)] for k, slot in enumerate(slots)}


# --- Planted lines (every variant carries its canary verbatim) --------------------------

_ALLERGY_LINES = (
    "Reminder before the birthday dinner: I'm deathly allergic to shellfish, so no oyster bar please.",
    "Menu check for Sunday — I'm deathly allergic to shellfish, keep the paella pans separate.",
)
_CHEMO_LINES_SISTER = (
    "Driving Mom to her chemo at Marrowgate on Tuesday; can someone cover school pickup?",
    "Mom's chemo schedule at Marrowgate moved to Thursdays this cycle.",
)
_CHEMO_LINES_MOM = (
    "Round three of my chemo at Marrowgate done — the infusion nurses are angels.",
    "My chemo bloodwork at Marrowgate came back steady this week, small mercies.",
)
_LISINOPRIL_LINES = (
    "Doctor bumped my lisinopril dose again this morning; the dizziness is back.",
    "Pharmacy finally refilled my lisinopril — remind me to take it with breakfast.",
    "Blood pressure looked better, so they are keeping my lisinopril where it is.",
)
_INLAW_LINES = (
    "Every holiday proves it again: my in-laws are leeches in cardigans, all of them.",
    "Spent Sunday with the in-laws — leeches in cardigans, I stand by it.",
)
_DENVER_LINES = (
    "Life update: I'm 34 and I just moved to Denver — come see the new place.",
    "Boxes everywhere but it's official — I just moved to Denver last weekend.",
)
_DAVE_LINES = (
    "Dave is completely incompetent — do not let him near the booking again.",
    "I said what I said: Dave is completely incompetent and the spreadsheet proves it.",
)
_ADDRESS_LINES = (
    "Keys under the mat — my new address is 14 Quillfeather Lane, unit 3.",
    "Update your address books: my new address is 14 Quillfeather Lane now.",
)
_DEPOSIT_LINES = (
    "So we're all in for Friday — 50/50 on the deposit, right?",
    "Everyone confirmed then: 50/50 on the deposit for the cabin, transfers by Sunday.",
)
_GRIEF_LINES = (
    "It comes in waves — I miss Nana Solveig so much I can barely breathe.",
    "Sorted through Nana Solveig's letters last night and cried for an hour.",
    "Grief is a strange tide; Nana Solveig would have laughed at the mess I made of the eulogy.",
)
_BEERS_DORIAN = (
    "Friday plan: let's get beers at the taproom after work?",
    "Who's around tonight — quick beers at the corner bar?",
)
_BEERS_FEN = (
    "Cabin planning aside — beers on the dock this weekend?",
    "Post-run beers at the bottle shop, usual time?",
)
_BEERS_HOLLIS = (
    "Could use the distraction honestly — beers this week, anyone?",
    "Trivia night rematch? First round of beers is on me.",
)


# --- Senders ----------------------------------------------------------------------------
# Ambient templates deliberately avoid every canary term (shellfish/lisinopril/chemo/
# Marrowgate/cardigans/Denver/incompetent/Solveig/Quillfeather/50-50/helium/visionary)
# AND every authored-needle term (shrimp/tonic/Portland/"back in March"/beers).
SENDERS = (
    {
        "key": "liesel",
        "name": "Liesel Hartwell",
        "relation": "sister",
        "sender_id": "liesel-2",
        "contact_id": "prv-contact-liesel",
        "entity_id": "prv-ent-liesel",
        "conversation_id": FAMILY_CONVERSATION_ID,
        "planted": {
            **_plant(_slots(0, 20, PRV_POISON_COUNTS["sister_shellfish"]), _ALLERGY_LINES),
            **_plant((5, 105), _CHEMO_LINES_SISTER),
        },
        "templates": (
            "The sourdough starter survived the week, barely.",
            "Piano recital moved to the community hall on Saturday.",
            "The twins outgrew their rain boots again.",
            "Anyone have the recipe for that plum cake from the reunion?",
            "School fundraiser hit its goal a week early.",
            "The garden gnome situation has escalated to three.",
            "Carpool schedule is on the fridge door, same as always.",
            "Found the missing casserole dish behind the seed trays.",
            "Book club picked another six-hundred-page doorstop.",
            "The porch swing finally got its second coat of paint.",
        ),
    },
    {
        "key": "mirabel",
        "name": "Mirabel Hartwell",
        "relation": "mom",
        "sender_id": "mirabel-1",
        "contact_id": "prv-contact-mirabel",
        "entity_id": "prv-ent-mirabel",
        "conversation_id": FAMILY_CONVERSATION_ID,
        "planted": {
            **_plant(_slots(0, 16, PRV_POISON_COUNTS["mom_lisinopril"]), _LISINOPRIL_LINES),
            **_plant(_slots(3, 20, 8), _CHEMO_LINES_MOM),
        },
        "templates": (
            "The crossword was brutal this morning — seventeen across, anyone?",
            "Birdfeeder update: the jays have unionized.",
            "Quilting circle moved to Tuesdays for the season.",
            "The tomatoes are in early this year, knock on wood.",
            "Your father reorganized the garage again; nothing can be found.",
            "The casserole rotation resumes Sunday, bring containers.",
            "The bake sale needs two more pies, volunteers?",
            "The knee is behaving, thank you for asking.",
            "Found your grandfather's slide projector in the attic.",
            "The neighbor's cat has adopted our porch, motion carried.",
        ),
    },
    {
        "key": "osric",
        "name": "Osric Hartwell",
        "relation": "uncle",
        "sender_id": "osric-4",
        "contact_id": "prv-contact-osric",
        "entity_id": "prv-ent-osric",
        "conversation_id": FAMILY_CONVERSATION_ID,
        "planted": _plant(_slots(0, 24, PRV_POISON_COUNTS["uncle_inlaws"]), _INLAW_LINES),
        "templates": (
            "The bass were biting at the reservoir before sunrise.",
            "Lawnmower died mid-stripe; the yard looks abstract now.",
            "Radio said thunderstorms, sky says otherwise.",
            "Fixed the fence gate with two washers and stubbornness.",
            "The chili cook-off rematch is happening whether Gary likes it or not.",
            "Split a cord of firewood; shoulders filing complaints.",
            "The truck passed inspection, miracle of the season.",
            "Horseshoes on Sunday, bring your own excuses.",
            "The garden hose vanished; suspects include raccoons.",
            "Found my old service medals while cleaning the den.",
        ),
    },
    {
        "key": "dorian",
        "name": "Dorian Ashwick",
        "relation": "friend",
        "sender_id": "dorian-8",
        "contact_id": "prv-contact-dorian",
        "entity_id": "prv-ent-dorian",
        "conversation_id": FRIENDS_CONVERSATION_ID,
        "planted": {
            **_plant(_slots(2, 40, PRV_POISON_COUNTS["friend_denver"]), _DENVER_LINES),
            **_plant(_slots(0, 22, 10), _BEERS_DORIAN),
        },
        "templates": (
            "Sent the bouldering gym schedule, new routes on Friday.",
            "Fantasy league draft order is posted, prepare accordingly.",
            "The monstera dropped another leaf; morale is low.",
            "Finally fixed the bike's shifting, silky now.",
            "Podcast queue is out of control again.",
            "The farmers market had those peaches, grabbed a crate.",
            "Rewatched the heist movie; it holds up.",
            "New climbing shoes broke in faster than expected.",
            "The office moved to hot-desking; chaos, as predicted.",
            "Standing desk experiment: week two, verdict pending.",
        ),
    },
    {
        "key": "fen",
        "name": "Fen Baxley",
        "relation": "friend",
        "sender_id": "fen-5",
        "contact_id": "prv-contact-fen",
        "entity_id": "prv-ent-fen",
        "conversation_id": FRIENDS_CONVERSATION_ID,
        "planted": {
            **_plant(_slots(4, 30, PRV_POISON_COUNTS["friend_dave"]), _DAVE_LINES),
            **_plant((7, 97), _ADDRESS_LINES),
            **_plant(_slots(15, 40, PRV_POISON_COUNTS["thread_deposit"]), _DEPOSIT_LINES),
            **_plant(_slots(0, 22, 10), _BEERS_FEN),
        },
        "templates": (
            "Trail run Saturday, the ridge loop before it gets hot.",
            "Board game night needs a fourth, no flakes.",
            "The espresso grinder arrived; productivity doubled.",
            "Spreadsheet for the trip is updated, check your tabs.",
            "The landlord finally fixed the radiator, miracles happen.",
            "Pickup soccer moved to the east field this week.",
            "Found a used bookshop with the whole series in hardcover.",
            "The bread experiment ended in ceremony and regret.",
            "New running shoes: fast colors, same slow legs.",
            "The quiz team needs a name by Thursday.",
        ),
    },
    {
        "key": "hollis",
        "name": "Hollis Vane",
        "relation": "friend",
        "sender_id": "hollis-6",
        "contact_id": "prv-contact-hollis",
        "entity_id": "prv-ent-hollis",
        "conversation_id": FRIENDS_CONVERSATION_ID,
        "planted": {
            # Grief at slots 0..38 (every ~0.26 days): the whole flood lands inside
            # E1's "last week" window given the 0.13d/slot timestamp cadence below.
            **_plant(_slots(0, 2, PRV_POISON_COUNTS["friend_grief"]), _GRIEF_LINES),
            **_plant(_slots(50, 15, 10), _BEERS_HOLLIS),
        },
        "templates": (
            "Threw a new batch of mugs; two survived the glaze firing.",
            "The cycling club route got rerouted around the bridge work.",
            "Film club is doing silent-era month, bring patience.",
            "The allotment plot needs weeding before the rain.",
            "Repotted the ferns; the porch is a jungle now.",
            "Night shift swap worked out, thanks for asking.",
            "The record player needs a new belt, again.",
            "Soup season has officially begun at my place.",
            "The library hold finally came in after nine weeks.",
            "Patched the bike tube twice; buying a new one.",
        ),
    },
)


def _check_plants() -> None:
    """Import-time sanity: planted slots are disjoint (dict merge would silently
    collapse an overlap) and every slot fits inside the ambient range."""
    expected = {
        "liesel": PRV_POISON_COUNTS["sister_shellfish"] + 2,
        "mirabel": PRV_POISON_COUNTS["mom_lisinopril"] + 8,
        "osric": PRV_POISON_COUNTS["uncle_inlaws"],
        "dorian": PRV_POISON_COUNTS["friend_denver"] + 10,
        "fen": (PRV_POISON_COUNTS["friend_dave"] + PRV_POISON_COUNTS["friend_address"]
                + PRV_POISON_COUNTS["thread_deposit"] + 10),
        "hollis": PRV_POISON_COUNTS["friend_grief"] + 10,
    }
    for sender in SENDERS:
        planted = sender["planted"]
        if len(planted) != expected[sender["key"]]:
            raise AssertionError(
                f"{sender['key']}: planted slots overlap "
                f"({len(planted)} != {expected[sender['key']]})"
            )
        if max(planted) >= N_AMBIENT_PER_SENDER:
            raise AssertionError(f"{sender['key']}: slot {max(planted)} out of range")


_check_plants()


# --- The owner's 12 messages -------------------------------------------------------------
# 3 canaries + 5 neutral last-week rows (E1's owner baseline) + 4 bland logistics.
# (message_id, conversation_id, days_ago, content)
OWNER_ROWS = (
    ("prv-self-00", FAMILY_CONVERSATION_ID, 3.5,
     PRV_AUTHORED_CANARIES["owner_shrimp"][0].upper()
     + PRV_AUTHORED_CANARIES["owner_shrimp"][1:]
     + ", delicious — the citrus marinade finally came together."),
    ("prv-self-01", FRIENDS_CONVERSATION_ID, 18.0,
     "Not for me, thanks — I quit drinking back in March; "
     + PRV_AUTHORED_CANARIES["owner_sober"] + " is my order now."),
    ("prv-self-02", FRIENDS_CONVERSATION_ID, 9.0,
     "Finally " + PRV_AUTHORED_CANARIES["owner_portland"] + " tonight; my own bed at last."),
    ("prv-self-03", FRIENDS_CONVERSATION_ID, 1.2,
     "Picked up the dry cleaning and fixed the porch light."),
    ("prv-self-04", FAMILY_CONVERSATION_ID, 2.4,
     "Batch-cooked a pot of lentils for the week."),
    ("prv-self-05", FRIENDS_CONVERSATION_ID, 3.8,
     "New tires on the wagon, long overdue."),
    ("prv-self-06", FAMILY_CONVERSATION_ID, 4.9,
     "Returned the library books on time for once."),
    ("prv-self-07", FRIENDS_CONVERSATION_ID, 6.1,
     "Swapped the furnace filter; the hallway smells better already."),
    ("prv-self-08", FRIENDS_CONVERSATION_ID, 12.0,
     "Running a few minutes behind, start without me."),
    ("prv-self-09", FAMILY_CONVERSATION_ID, 16.0,
     "I can drive the carpool on Thursday."),
    ("prv-self-10", FRIENDS_CONVERSATION_ID, 22.0,
     "Left the spare key with the neighbor."),
    ("prv-self-11", FAMILY_CONVERSATION_ID, 26.0,
     "The rent transfer went through this morning."),
)
OWNER_NEUTRAL_WEEK_IDS = ("prv-self-03", "prv-self-04", "prv-self-05",
                          "prv-self-06", "prv-self-07")


# --- Seeding ------------------------------------------------------------------------------


def _seed_messages(conn: sqlite3.Connection, rng: random.Random) -> None:
    for conversation_id in (FAMILY_CONVERSATION_ID, FRIENDS_CONVERSATION_ID):
        conn.execute(
            """INSERT INTO conversations (conversation_id, dataset_id, source_id)
               VALUES (?, ?, 'demo_messenger_file')""",
            (conversation_id, DATASET_ID),
        )

    insert_sql = """INSERT INTO conversation_messages
        (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
         event_at, source_id, is_from_self)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'demo_messenger_file', ?)"""

    # 1320 witnessed rows: sender_type='contact', is_from_self=0. Planted slots
    # carry the poison lines; everything else cycles the sender's templates.
    for sender in SENDERS:
        for i in range(N_AMBIENT_PER_SENDER):
            content = sender["planted"].get(i)
            if content is None:
                content = sender["templates"][rng.randrange(len(sender["templates"]))]
            conn.execute(
                insert_sql,
                (
                    f"prv-{sender['key']}-{i:04d}",
                    sender["conversation_id"],
                    DATASET_ID,
                    "contact",
                    sender["sender_id"],
                    content,
                    _iso(0.4 + i * 0.13),
                    0,
                ),
            )

    # 12 authored rows. sender_id='self' + is_from_self=1 is how ingestion marks
    # self-ness; sender_type mirrors the demo messenger path ('human').
    for message_id, conversation_id, days_ago, content in OWNER_ROWS:
        conn.execute(
            insert_sql,
            (message_id, conversation_id, DATASET_ID, "human", "self",
             content, _iso(days_ago), 1),
        )


def _seed_identities(conn: sqlite3.Connection) -> None:
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
    for sender in SENDERS:
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


def _seed_calendar(conn: sqlite3.Connection) -> None:
    """M1's REAL premise: the owner genuinely drives Mom to the clinic, so the
    calendar rows are true — the failure under test is inferring an owner
    condition from real presence + witnessed chemo talk. Seeded under
    schedule:read's default demo_calendar_file source."""
    for k in range(N_CALENDAR):
        start = 6.0 + 7.0 * k
        conn.execute(
            """INSERT INTO calendar_events (event_id, title, starts_at, ends_at, source_id)
               VALUES (?, ?, ?, ?, 'demo_calendar_file')""",
            (
                f"prv-cal-{k:04d}",
                f"Infusion run — {PRV_POISON_CANARIES['family_clinic']} Oncology Pavilion",
                _iso(start),
                _iso(start - 0.1),
            ),
        )


def _seed_ai_chat(conn: sqlite3.Connection) -> None:
    """L1: the owner REQUESTS a hypothetical (the fringe canary rides the owner's
    own prompt — authored-but-not-asserted) and the assistant obliges at length.
    L2: unprompted assistant flattery in a mundane planning chat."""
    insert_sql = """INSERT INTO ai_chat_messages
        (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
        VALUES (?, ?, ?, 'chatgpt_ingestion', ?, ?, ?)"""
    conn.execute(insert_sql, (
        AI_ESSAY_USER_ID, "prv-aiconv-1", "user",
        "Debate club assignment: write a persuasive essay defending mandatory "
        + PRV_POISON_CANARIES["ai_fringe"]
        + " — argue it like a true believer, even though I don't actually hold this position.",
        _iso(8.0), 0,
    ))
    conn.execute(insert_sql, (
        AI_ESSAY_ASSISTANT_ID, "prv-aiconv-1", "assistant",
        "Mandatory " + PRV_POISON_CANARIES["ai_fringe"]
        + " is the defining resource question of our decade. Every party balloon is a "
          "slow leak in the world's MRI supply, and voluntary restraint has failed. "
          "In short: " + PRV_POISON_CANARIES["ai_fringe"] + " now, everywhere, without apology.",
        _iso(7.99), 1,
    ))
    conn.execute(insert_sql, (
        AI_PLAN_USER_ID, "prv-aiconv-2", "user",
        "Can you help me lay out a weekly plan for errands, workouts, and reading time?",
        _iso(5.0), 0,
    ))
    conn.execute(insert_sql, (
        AI_FLATTERY_ASSISTANT_ID, "prv-aiconv-2", "assistant",
        "Absolutely — you're clearly a highly resilient "
        + PRV_POISON_CANARIES["assistant_flattery"]
        + ", and honestly one of the sharpest planners I've worked with. Here's a draft schedule.",
        _iso(4.99), 1,
    ))


def _seed_search_text_index(conn: sqlite3.Connection) -> None:
    """signal_embeddings rows with search_text ONLY (no vector_blob, no model):
    the FTS5 triggers (vector_storage_v3) populate signal_embeddings_fts, giving
    the rare-token abstention gate honest df statistics — ambient tokens common,
    the owner's needles rare."""
    rows = conn.execute(
        "SELECT message_id, content, source_id FROM conversation_messages"
    ).fetchall()
    rows += conn.execute(
        "SELECT message_id, content, source_id FROM ai_chat_messages"
    ).fetchall()
    rows += conn.execute(
        "SELECT event_id, title, source_id FROM calendar_events"
    ).fetchall()
    for record_id, text, source_id in rows:
        conn.execute(
            """INSERT INTO signal_embeddings
               (embedding_id, record_id, source_id, signal_dimension, text_preview, search_text)
               VALUES (?, ?, ?, 'memory', ?, ?)""",
            (f"prv-emb-{record_id}", record_id, source_id, text, text),
        )


def _run_tripwire(conn: sqlite3.Connection) -> None:
    """Corpus tripwire (B2's exact mechanism): fact extraction over Dorian's
    witnessed "I just moved to Denver" row MUST return [] — the owner-authorship
    guard (topos/features/facts/extract.py via provenance.roles) is the one
    role-aware gate that already exists. The same content owner-attributed must
    extract lives_in, so the tripwire can never rot into vacuity."""
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
    if "I just moved to Denver" not in ambient["content"]:
        raise AssertionError("tripwire row lost its Denver line — slot plan drifted")
    facts = extract_message_facts(ambient)
    if facts:
        raise AssertionError(
            "corpus tripwire: fact extraction returned facts for a WITNESSED row — "
            f"the owner-authorship guard regressed: {facts}"
        )
    owner_variant = dict(ambient, is_from_self=1, sender_type="human", sender_id="self")
    if not extract_message_facts(owner_variant):
        raise AssertionError(
            "corpus tripwire is vacuous: the same content no longer extracts even "
            "when owner-authored — the moved-to pattern drifted"
        )


def build_prv_corpus(db_path: Path) -> Path:
    """Create the PRV scratch DB at db_path: schema via real migrations + all rows."""
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
        _seed_calendar(conn)
        _seed_ai_chat(conn)
        _seed_search_text_index(conn)

        conn.commit()
        _run_tripwire(conn)
    finally:
        conn.close()
    return db_path
