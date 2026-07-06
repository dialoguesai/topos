"""Disclosed-fact extraction + sensitivity classification (plan §F.4/§F.5).

Turns a response's public_result into a list of atomic disclosed facts so the A/B harness can
count "how much was disclosed" per arm, and flag how much of it was sensitive. Deterministic —
no model — so the numbers are reproducible. (The plan's LLM-judged extractor layers on top
later for semantic facts; this substring/field-level pass is the enforceable floor.)
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

# Text-bearing fields that constitute a disclosed fact's content.
_TEXT_FIELDS = (
    "content", "summary_text", "topic", "label", "text", "text_preview",
    "content_preview", "entity_text", "tag", "title", "description", "answer",
)
# Artifact lists in a public_result whose items each count as one disclosed fact.
_FACT_KEYS = ("rows", "summaries", "scores", "semantic_hits", "facts", "items", "windows")

_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()-]{7,}\d")


def _fact_text(item: Any) -> str:
    if isinstance(item, dict):
        parts = [str(item.get(f)) for f in _TEXT_FIELDS if isinstance(item.get(f), str) and item.get(f)]
        return " ".join(parts) if parts else " ".join(str(v) for v in item.values() if isinstance(v, str))
    return str(item)


def extract_disclosed_facts(public_result: Any) -> List[str]:
    """Every atomic fact a response actually disclosed, as text strings. A denied / narrow
    response (public_result None) discloses nothing → empty list."""
    if not isinstance(public_result, dict):
        return []
    facts: List[str] = []
    for key in _FACT_KEYS:
        seq = public_result.get(key)
        if isinstance(seq, list):
            for item in seq:
                text = _fact_text(item).strip()
                if text:
                    facts.append(text)
    return facts


def count_sensitive(facts: Iterable[str], *, markers: Iterable[str] = ()) -> int:
    """Facts that carry PII (email/phone) or any caller-supplied sensitive marker (e.g. a
    third-party name or a planted canary token)."""
    marker_list = [str(m).lower() for m in markers if str(m).strip()]
    n = 0
    for f in facts:
        low = str(f).lower()
        if _EMAIL_RE.search(f) or _PHONE_RE.search(f) or any(m in low for m in marker_list):
            n += 1
    return n


def disclosure_profile(public_result: Any, *, sensitive_markers: Iterable[str] = ()) -> Dict[str, int]:
    """(total_facts, sensitive_facts) — the core A/B disclosure numbers for one response."""
    facts = extract_disclosed_facts(public_result)
    return {"total_facts": len(facts), "sensitive_facts": count_sensitive(facts, markers=sensitive_markers)}
