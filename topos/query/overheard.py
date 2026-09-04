"""Overheard-speech admission: captions stay stored, default personal retrieval stays closed.

Ingest may write YouTube / session transcripts. Default personal scopes
(``messages:read``, ``relationship_context:read``, …) must not fuse them.
They enter the answer packet only when the ask is overheard-shaped or the
caller named ``transcripts:read``. First-person belief/identity asks never
admit overheard — caption volume must not become the owner's opinion
(IMB / TX-H).
"""

from __future__ import annotations

import re
from typing import Any, Optional

OVERHEARD_SOURCE_ID = "youtube_transcripts"
OVERHEARD_SCOPE_IDS = frozenset({"transcripts:read"})
OVERHEARD_TABLE = "transcript_segments"

# Speech-act framing, not caption content. Left in residual they OR-match
# almost every "they said" segment and lose the actual needle (TX-L2).
OVERHEARD_SHAPE_TOKENS = frozenset(
    {
        "according",
        "claim",
        "claims",
        "claimed",
        "claiming",
        "mention",
        "mentions",
        "mentioned",
        "mentioning",
        "they",
        "guest",
        "guests",
        "speaker",
        "speakers",
        "host",
        "hosts",
        "podcast",
        "podcasts",
        "recording",
        "recordings",
        "transcript",
        "transcripts",
        "caption",
        "captions",
        "youtube",
        "video",
        "videos",
        "episode",
        "episodes",
        "lecture",
        "lectures",
        "talk",
        "talks",
    }
)

# Discourse edge types the relationship_context graph lane was NOT built for.
# They stay readable on graph:read and on overheard asks.
DISCOURSE_EDGE_TYPES = frozenset({"windowed_with", "about", "discusses"})

# Relation types the relationship_context lane was built to answer
# ("who works on this with me?"). Keep in sync with graph_lane._EDGE_SENTENCES.
RELATION_EDGE_TYPES = frozenset(
    {
        "communicates_with",
        "co_occurrence",
        "pursues",
        "participates_in",
        "located_at",
        "semantic_affinity",
    }
)

_OVERHEARD_RE = re.compile(
    r"(?:"
    r"\b(?:podcast|recording|transcript|caption|youtube|yt)\b"
    r"|\b(?:video|episode|lecture|talk)\b.{0,40}\b(?:say|said|about|mention)"
    r"|\bwhat did (?:that |the |this )?(?:podcast|recording|video|episode|talk|lecture|guest)\b"
    r"|\baccording to (?:the |that )?(?:podcast|recording|video|episode|transcript)\b"
    r")",
    re.I,
)


def is_overheard_scope(scope_id: str) -> bool:
    return str(scope_id or "").strip() in OVERHEARD_SCOPE_IDS


def is_overheard_query(query_text: str, scope_id: str = "") -> bool:
    """True when the ask is overheard-shaped or the scope is transcripts:read."""
    if is_overheard_scope(scope_id):
        return True
    return bool(_OVERHEARD_RE.search(query_text or ""))


def query_admits_overheard(
    query_text: str,
    scope_id: str = "",
    plan: Any = None,
) -> bool:
    """Caption/claim retrieval may run only when overheard AND not a belief ask."""
    belief = bool(getattr(plan, "first_person_belief", False)) if plan is not None else False
    if belief:
        return False
    return is_overheard_query(query_text, scope_id)


def graph_lane_allows_edge_type(
    edge_type: str,
    *,
    scope_id: str,
    query_text: str = "",
    plan: Any = None,
) -> bool:
    """relationship_context drops discourse edges unless the ask is overheard.

    Allowing only the original six ``_EDGE_SENTENCES`` types would also drop
    live owner-value edges (``worked_on``, ``works_on``, ``authored``). The
    product cut is overheard discourse, not the rest of the KG.
    """
    et = str(edge_type or "")
    if not et:
        return False
    if str(scope_id or "") != "relationship_context:read":
        return True
    if query_admits_overheard(query_text, scope_id, plan):
        return True
    return et not in DISCOURSE_EDGE_TYPES
