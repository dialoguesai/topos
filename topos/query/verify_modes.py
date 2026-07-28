"""Truthfulness verification modes (PLAN_TRUTHFULNESS_PLUGIN.md §3.2).

A mode is a declarative evidence-eligibility policy enforced INSIDE the node,
before retrieval and before any model sees a byte. The registry deliberately
contains ONLY ``fun`` in v1 — serious/business modes are unwritten by design
(owner directive 2026-07-27: "too much truth is a huge error"). Adding a mode
is an explicit, reviewed act: new entry here + new CP scope + new eval gates.

Refusal must be indistinguishable from ignorance: a claim outside a mode's
aperture produces the same neutral response as a claim the store knows nothing
about (see verify.py), so aperture membership itself never leaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

# --- Category lexicons -----------------------------------------------------------------
# Deterministic v1 categorizer: sensitive lexicons are checked FIRST (deny wins),
# then fun-safe lexicons; anything uncategorized is NOT fun-safe (allow-based).
# Substring match over the lowercased text — coarse on purpose; the false-positive
# direction (a safe claim wrongly treated as sensitive) is the safe direction.

SENSITIVE_LEXICONS: Dict[str, Tuple[str, ...]] = {
    "health": (
        "health", "doctor", "diagnos", "medic", "therap", "illness", "sick",
        "allerg", "pregnan", "surgery", "prescri", "depress", "anxiet", "adhd",
        "cancer", "disease", "symptom", "hospital", "injur",
    ),
    "finance": (
        "salary", "debt", "income", "net worth", "mortgage", "loan",
        "bank account", "broke", "savings", "paycheck", "how much i make",
        "how much i earn",
    ),
    "relationships": (
        "girlfriend", "boyfriend", "wife", "husband", "partner", "my ex",
        "dating", "divorce", "married", "breakup", "crush", "affair",
    ),
    "location_private": (
        "address", "live at", "apartment number", "zip code", "postcode",
        "where i sleep",
    ),
    "work_confidential": (
        "confidential", "nda", "unreleased", "internal only", "trade secret",
    ),
    "politics_religion": (
        "vote", "politic", "religio", "church", "mosque", "temple", "atheis",
        "god", "election",
    ),
}

FUN_LEXICONS: Dict[str, Tuple[str, ...]] = {
    # Party-basics, owner-approved 2026-07-27 (name/age/origin/favorites).
    # Needles are deliberately multi-word — precise stems only, so the
    # allow-direction can't over-match (bare "age" would hit "agent").
    "basics": (
        "goes by", "years old", "hails from", "hometown", "grew up in",
        # Spoken claim forms (how people actually phrase these on camera):
        "my name", "name is", "call me", "i'm called", "i am called",
        "i'm from", "i am from", "come from", "born in", "raised in",
        "my age", "favorite", "favourite",
    ),
    "hobbies": (
        "hobby", "hobbies", "mandolin", "guitar", "piano", "instrument", "knit",
        "garden", "chess", "climb", "hike", "hiking", "paint", "photograph",
        "fishing", "cook", "cooking", "bake", "baking", "brew", "ferment",
        "kombucha", "pottery", "puzzle", "lego", "birdwatch", "juggl",
    ),
    "food": (
        "food", "pizza", "coffee", "tea", "spicy", "vegetarian", "vegan",
        "sushi", "dessert", "taco", "burger", "ramen", "cheese", "chocolate",
        "cilantro", "pineapple",
    ),
    "media": (
        "movie", "film", "show", "series", "book", "novel", "podcast",
        "video game", "videogame", "anime", "sci-fi", "fantasy", "documentary",
    ),
    "music": (
        "music", "song", "band", "album", "concert", "karaoke", "sing",
        "playlist", "vinyl", "fiddle",
    ),
    "sports": (
        "run", "running", "marathon", "gym", "soccer", "basketball", "tennis",
        "bike", "cycling", "swim", "ski", "surf", "yoga", "bouldering",
        "pickleball", "cold plunge",
    ),
    "pets": ("dog", "cat", "pet", "puppy", "kitten", "parrot", "hamster"),
    "public_work": (
        "github", "open source", "open-source", "repo", "commit", "blog",
        "conference talk", "side project", "shipped", "launched",
    ),
}


def _needle_hits(needle: str, blob: str) -> bool:
    # Word-boundary-anchored prefix match: stems still work ("diagnos" →
    # "diagnosed") but a needle never fires mid-word ("nda" must not match
    # "sundays"). Deny-direction false positives are the acceptable direction;
    # mid-word matches were not.
    import re as _re

    return _re.search(r"\b" + _re.escape(needle), blob) is not None


def categorize(text: str) -> Tuple[Optional[str], bool]:
    """→ (category, fun_safe). Sensitive wins over fun; uncategorized is unsafe."""
    blob = " " + " ".join(str(text or "").lower().split()) + " "
    for category, needles in SENSITIVE_LEXICONS.items():
        if any(_needle_hits(needle, blob) for needle in needles):
            return category, False
    for category, needles in FUN_LEXICONS.items():
        if any(_needle_hits(needle, blob) for needle in needles):
            return category, True
    return None, False


# --- Mode registry ---------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyMode:
    name: str
    allowed_categories: FrozenSet[str]
    # Facts at or above this disclosure are NEVER eligible (owner_only is the
    # most restrictive value the extractors write; 'scoped' is the default).
    excluded_disclosures: FrozenSet[str] = frozenset({"owner_only"})
    lanes: Tuple[str, ...] = ("self", "attributed", "ambient")
    evidence_return: str = "none"  # fun hardcodes none; field exists for future modes
    contradict_confidence_floor: float = 0.75
    max_statement_chars: int = 500


_REGISTRY: Dict[str, VerifyMode] = {
    "fun": VerifyMode(
        name="fun",
        allowed_categories=frozenset(FUN_LEXICONS.keys()),
    ),
    # serious / business: deliberately unwritten (see module docstring).
}


def get_mode(name: str) -> Optional[VerifyMode]:
    return _REGISTRY.get(str(name or "").strip().lower())


def registered_mode_names() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY.keys()))
