"""Synthetic eval persona: Ada Voss.

Ada is an ML engineer at Heliograph Labs (previously at Lumon Industries),
an amateur ceramicist, trains for a half marathon, chats with an AI about a
side project called "Kilnwatch" (a kiln-temperature monitor), and messages
two people regularly: Maya Chen (climbing partner, potter) and Dr. Patel
(physical therapist).

Every record carries a stable record_id so eval cases can assert exact
retrieval hits. Dates cluster in May–June 2026.
"""

from __future__ import annotations

from typing import Any, Dict, List

PERSONA_SOURCE_ID = "eval_persona_file"


def canonical_rows() -> Dict[str, List[Dict[str, Any]]]:
    """Rows per canonical table, shaped like post-canonicalization output."""
    return {
        "journal_entries": [
            {
                "record_id": "jrn-001",
                "entry_at": "2026-05-04T06:40:00Z",
                "content": "Morning run, 8k along the river. Left knee felt fine until the last hill. Icing it now.",
                "mood_tag": "energized",
                "category": "exercise",
                "duration_minutes": 52,
            },
            {
                "record_id": "jrn-002",
                "entry_at": "2026-05-06T21:15:00Z",
                "content": "Pottery studio night. Threw four bowls; two warped. Maya says my clay is too wet. Trying a stiffer stoneware next week.",
                "mood_tag": "content",
                "category": "hobby",
                "people": "Maya Chen",
                "place_name": "Mudlark Studio",
                "duration_minutes": 110,
            },
            {
                "record_id": "jrn-003",
                "entry_at": "2026-05-11T07:05:00Z",
                "content": "10k run this morning, longest yet for the half marathon block. Knee held up. Averaged 5:40/km.",
                "mood_tag": "proud",
                "category": "exercise",
                "duration_minutes": 57,
            },
            {
                "record_id": "jrn-004",
                "entry_at": "2026-05-19T22:30:00Z",
                "content": "Couldn't sleep. Kilnwatch firmware kept dropping readings above 1200 degrees, wrote the debounce fix in my head at 1am.",
                "mood_tag": "restless",
                "category": "sleep",
            },
            {
                "record_id": "jrn-005",
                "entry_at": "2026-06-02T20:00:00Z",
                "content": "PT session with Dr. Patel. She wants me to cut mileage 30 percent for two weeks and add hip strengthening.",
                "mood_tag": "worried",
                "category": "health",
                "people": "Dr. Patel",
                "duration_minutes": 45,
            },
            {
                "record_id": "jrn-006",
                "entry_at": "2026-06-09T21:40:00Z",
                "content": "Glaze firing came out beautifully — the celadon bowls for the market stall are done. Maya is taking half to sell at Saturday market.",
                "mood_tag": "delighted",
                "category": "hobby",
                "people": "Maya Chen",
                "place_name": "Mudlark Studio",
                "duration_minutes": 95,
            },
        ],
        "ai_chat_messages": [
            {
                "record_id": "chat-001",
                "ts": "2026-05-07T13:20:00Z",
                "conversation_id": "conv-kilnwatch",
                "content": "How do I debounce a thermocouple reading on an ESP32 when values spike above 1200C? Kilnwatch drops samples during fast ramps.",
                "sender_type": "user",
            },
            {
                "record_id": "chat-002",
                "ts": "2026-05-07T13:25:00Z",
                "conversation_id": "conv-kilnwatch",
                "content": "For Kilnwatch's ESP32 thermocouple noise you can apply a median-of-5 filter before the moving average, and reject deltas over 50C per sample.",
                "sender_type": "assistant",
            },
            {
                "record_id": "chat-003",
                "ts": "2026-05-21T09:10:00Z",
                "conversation_id": "conv-taxes",
                "content": "I need to file quarterly estimated taxes for my market stall pottery income. It's roughly 900 dollars this quarter.",
                "sender_type": "user",
            },
            {
                "record_id": "chat-004",
                "ts": "2026-06-04T18:45:00Z",
                "conversation_id": "conv-spanish",
                "content": "Give me five Spanish phrases for talking to a physical therapist about knee pain.",
                "sender_type": "user",
            },
            {
                "record_id": "chat-005",
                "ts": "2026-06-11T10:30:00Z",
                "conversation_id": "conv-kilnwatch",
                "content": "Kilnwatch update: the debounce fix worked. Now I want an alert when the kiln cools below 200C so I know when to unload.",
                "sender_type": "user",
            },
        ],
        "calendar_events": [
            {
                "record_id": "cal-001",
                "title": "Half marathon — Riverfront race",
                "starts_at": "2026-06-28T08:00:00Z",
                "ends_at": "2026-06-28T11:00:00Z",
                "is_busy": True,
            },
            {
                "record_id": "cal-002",
                "title": "PT appointment with Dr. Patel",
                "starts_at": "2026-06-02T17:00:00Z",
                "ends_at": "2026-06-02T18:00:00Z",
                "is_busy": True,
            },
            {
                "record_id": "cal-003",
                "title": "Pottery studio night",
                "starts_at": "2026-05-06T19:00:00Z",
                "ends_at": "2026-05-06T21:30:00Z",
                "is_busy": True,
            },
            {
                "record_id": "cal-004",
                "title": "Saturday market stall with Maya",
                "starts_at": "2026-06-13T07:30:00Z",
                "ends_at": "2026-06-13T14:00:00Z",
                "is_busy": True,
            },
            {
                "record_id": "cal-005",
                "title": "Heliograph quarterly planning",
                "starts_at": "2026-06-16T09:00:00Z",
                "ends_at": "2026-06-16T12:00:00Z",
                "is_busy": True,
            },
        ],
        "conversation_messages": [
            {
                "record_id": "msg-001",
                "ts": "2026-05-30T16:02:00Z",
                "conversation_id": "conv-maya",
                "sender_id": "maya.chen",
                "content": "Bring the celadon test tiles Saturday — if the glaze holds I'll order a full bag of that feldspar.",
            },
            {
                "record_id": "msg-002",
                "ts": "2026-05-30T16:05:00Z",
                "conversation_id": "conv-maya",
                "sender_id": "ada.voss",
                "content": "Will do. Also climbing Tuesday? My knee is supposedly fine for bouldering per Dr. Patel.",
            },
            {
                "record_id": "msg-003",
                "ts": "2026-06-05T11:00:00Z",
                "conversation_id": "conv-patel",
                "sender_id": "dr.patel",
                "content": "Reminder: hip strengthening twice daily, and keep runs under 6k until our June 16 check-in.",
            },
        ],
        "profile_records": [
            {
                "record_id": "prof-001",
                "record_type": "experience",
                "title": "Senior ML Engineer",
                "organization": "Heliograph Labs",
                "description": "Time-series anomaly detection for industrial sensors. 2024–present.",
            },
            {
                "record_id": "prof-002",
                "record_type": "experience",
                "title": "Data Engineer",
                "organization": "Lumon Industries",
                "description": "Built the macrodata refinement ETL platform. 2021–2024.",
            },
            {
                "record_id": "prof-003",
                "record_type": "certification",
                "title": "Wilderness First Responder",
                "organization": "NOLS",
                "description": "Certified 2025, expires 2028.",
            },
        ],
        "activity_events": [
            {
                "record_id": "web-001",
                "occurred_at": "2026-05-07T12:50:00Z",
                "url": "https://docs.espressif.com/projects/esp-idf/thermocouple",
                "title": "ESP32 thermocouple interface guide",
            },
            {
                "record_id": "web-002",
                "occurred_at": "2026-05-13T20:10:00Z",
                "url": "https://bigceramicstore.com/celadon-glaze-recipes",
                "title": "Celadon glaze recipes for cone 10 stoneware",
            },
            {
                "record_id": "web-003",
                "occurred_at": "2026-06-01T08:15:00Z",
                "url": "https://www.runnersworld.com/half-marathon-taper-plans",
                "title": "Half marathon taper plans for injured runners",
            },
        ],
    }


# Text used for embedding per table, mirroring EmbeddingsJob content selection.
CONTENT_FIELDS = {
    "journal_entries": "content",
    "ai_chat_messages": "content",
    "conversation_messages": "content",
    "calendar_events": "title",
    "profile_records": "description",
    "activity_events": "title",
}

DIMENSION_BY_TABLE = {
    "journal_entries": "wellbeing",
    "ai_chat_messages": "memory",
    "conversation_messages": "relationships",
    "calendar_events": "time",
    "profile_records": "profile",
    "activity_events": "interests",
}


def eval_cases() -> List[Dict[str, Any]]:
    """query -> expected record ids (any hit in top-k counts), by class."""
    return [
        # -- entity lookups --
        {
            "id": "entity-kilnwatch",
            "class": "entity",
            "query": "What is Kilnwatch?",
            "expected_any": ["chat-001", "chat-002", "chat-005", "jrn-004"],
        },
        {
            "id": "entity-maya",
            "class": "entity",
            "query": "Who is Maya Chen to me?",
            "expected_any": ["msg-001", "msg-002", "jrn-002", "jrn-006", "cal-004"],
        },
        {
            "id": "entity-patel",
            "class": "entity",
            "query": "What has Dr. Patel told me to do?",
            "expected_any": ["msg-003", "jrn-005", "cal-002"],
        },
        {
            "id": "entity-prior-employer",
            "class": "entity",
            "query": "Where did I work before Heliograph Labs?",
            "expected_any": ["prof-002"],
        },
        # -- temporal --
        {
            "id": "temporal-june-2",
            "class": "temporal",
            "query": "What happened at my appointment on June 2?",
            "expected_any": ["jrn-005", "cal-002"],
        },
        {
            "id": "temporal-race",
            "class": "temporal",
            "query": "When is my half marathon race?",
            "expected_any": ["cal-001"],
        },
        # -- semantic / paraphrase (no keyword overlap on purpose) --
        {
            "id": "semantic-knee",
            "class": "semantic",
            "query": "Have I had any leg injuries from training?",
            "expected_any": ["jrn-001", "jrn-005", "msg-003", "web-003"],
        },
        {
            "id": "semantic-ceramics-selling",
            "class": "semantic",
            "query": "Am I selling any of my ceramics anywhere?",
            "expected_any": ["jrn-006", "chat-003", "cal-004", "msg-001"],
        },
        {
            "id": "semantic-firmware",
            "class": "semantic",
            "query": "sensor noise problems in my electronics hobby project",
            "expected_any": ["chat-001", "chat-002", "jrn-004", "web-001"],
        },
        # -- cross-source --
        {
            "id": "cross-glaze",
            "class": "cross_source",
            "query": "celadon glaze",
            "expected_any": ["jrn-006", "msg-001", "web-002"],
            "min_distinct_tables": 2,
        },
        # -- aggregate-shaped (stats layer target; embeddings may still hit records) --
        {
            "id": "aggregate-running",
            "class": "aggregate",
            "query": "How often do I go running and how long are my runs?",
            "expected_any": ["jrn-001", "jrn-003"],
        },
        # -- negative control: nothing in corpus should match confidently --
        {
            "id": "negative-sailing",
            "class": "negative",
            "query": "my sailing certification and boat maintenance schedule",
            "expected_any": [],
            "max_top1_similarity": 0.55,
        },
    ]
