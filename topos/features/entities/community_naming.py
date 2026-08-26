"""Stable community naming — derivation engine (PLAN_COMMUNITY_NAMING S2).

A NEW community core (no history match) gets its name from a local model,
prompted with its most central members and the terms that DISTINGUISH it from
every other community (the contrastive discipline from the cluster-label eval
lane). Every generated name passes the same deterministic guards that ended
the person-name / goal-sentence / phone-number label era — a failed validation
falls back to the deterministic dominant-type label, so this layer is never
worse than what it replaces. Fail-open everywhere: no model, no problem, the
deterministic label stands.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("topos.features.entities.community_naming")

ENGINE_CONFIG_KEY_NAMING_MODEL = "community_naming_model"


def naming_enabled() -> bool:
    import os
    return os.environ.get("TOPOS_COMMUNITY_NAMING", "on").strip().lower() not in (
        "0", "false", "off", "no")
MAX_NEW_NAMES_PER_REBUILD = 60

_STOP = {"the", "and", "for", "with", "from", "this", "that", "our", "your",
         "into", "over", "about", "a", "an", "of", "to", "in", "on", "at"}


def valid_label(name: str) -> bool:
    nm = (name or "").strip().strip('"').strip("'")
    if not nm or len(nm) > 40 or len(nm.split()) > 4 or "\n" in nm:
        return False
    if not any(c.isalpha() for c in nm) or nm.startswith("+"):
        return False
    return True


def distinctive_terms(
    member_names: Sequence[str],
    all_other_names: Sequence[str],
    top: int = 6,
) -> List[str]:
    """Tokens frequent here, rare elsewhere — c-TF-IDF-lite, no dependencies."""
    def toks(names):
        out = []
        for n in names:
            out += [t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9'-]+", str(n).lower())
                    if len(t) > 2 and t not in _STOP]
        return out
    here = Counter(toks(member_names))
    there = Counter(toks(all_other_names))
    scored = {t: c / (1 + there.get(t, 0)) for t, c in here.items()}
    return [t for t, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:top]]


def resolve_naming_model(conn: sqlite3.Connection) -> str:
    """Owner-configured under Node functions; defaults to the local extraction
    model — naming stays on-device unless the owner points it elsewhere."""
    try:
        from ...core.state import get_engine_config_value
        v = get_engine_config_value(conn, ENGINE_CONFIG_KEY_NAMING_MODEL)
        if v and str(v).strip():
            return str(v).strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        from ...config.settings import settings
        from ...features.facts.llm_extract import _resolved_extraction_model
        return _resolved_extraction_model(settings, conn)
    except Exception:  # noqa: BLE001
        return "qwen3.5:9b-mlx"


_PROMPT = """Name this group from a personal knowledge graph. The name is a short,
recognizable label a person would use for the cluster — like naming a photo album.

Most central members: {members}
Terms that distinguish this group from the others: {terms}

Rules: 2-4 words. A NAME, not a sentence or a description. No punctuation except
spaces. Prefer the group's own vocabulary over generic words.

Respond with ONLY the name."""


def derive_community_name(
    member_names: Sequence[str],
    terms: Sequence[str],
    llm,
) -> Optional[str]:
    """llm: Callable[[str], str]. Returns a validated name or None (caller
    falls back to the deterministic label)."""
    prompt = _PROMPT.format(
        members=", ".join(list(member_names)[:10]) or "(unnamed items)",
        terms=", ".join(terms) or "(none)",
    )
    try:
        raw = llm(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.debug("community naming llm unavailable: %s", exc)
        return None
    name = str(raw or "").strip().splitlines()[0].strip().strip('"').strip("'") if raw else ""
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name if valid_label(name) else None
