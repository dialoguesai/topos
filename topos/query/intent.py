"""Intent normalization and hashing (PRD §8.3)."""

from __future__ import annotations

import hashlib
import re


def normalize_query(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def compute_intent_hash(*, scope_id: str, access_mode: str, query_text: str) -> str:
    normalized = normalize_query(query_text)
    payload = f"{scope_id}|{access_mode}|{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
