"""A redaction placeholder must never become an entity, terminated or not.

``is_valid_entity_surface`` rejected placeholders with ``re.search(r"\\[[A-Z][A-Z_]*\\]")``
— a pattern that requires the closing bracket. NER spans cut THROUGH a
placeholder as often as around one, so ``Vish[NAME``, ``Ashford[DATE`` and
``Dallas.[NAME`` sailed past a guard written specifically to stop them.

Measured on the owner's node 2026-08-27: 14 such surfaces reached the spine and
minted 9 entities, **5 of them typed ``person``** — phantom people named after
the privacy mechanism that was supposed to remove the real ones. Of the 8 live
shapes, the old pattern caught 4 and missed 4; the new one catches all 8, and
would refuse 80 of the 82 bracket-bearing entity names currently in the registry.

The controls matter as much as the rejections here. This guard sits on the mint
path for every entity in the system, so a pattern that over-matches silently
deletes real names — ``AT&T``, ``Coca-Cola``, ``Jean-Luc Picard``, ``E[3]``.

Not tested here, deliberately: truncated compound surfaces like
``Rivermouth-``. ``clean_entity_surface`` already strips the trailing delimiter,
leaving ``Rivermouth`` — a real neighbourhood that must resolve. The three stub
place entities on the live node predate that cleaning and are stale DATA, not an
open hole; a guard for them would be dead code that reads as live protection.
"""

from __future__ import annotations

import pytest

from topos.features.entities.resolver import is_valid_entity_surface

#: The exact shapes found in the live registry. Every one must be refused.
LIVE_PLACEHOLDER_SURFACES = [
    "Vish[NAME",
    "Sh[NAME][NAME",
    "Ashford[DATE",
    "Dallas.[NAME",
    "West Virginia.[NAME",
    "Sponsorship Measurement,[NAME][NAME",
    "Multimodal Weekly 117:[NAME][NAME",
    "[ADDRESS]",
]

#: Shapes the OLD pattern let through — the regression this file exists for.
UNTERMINATED = ["Vish[NAME", "Ashford[DATE", "Dallas.[NAME", "West Virginia.[NAME"]

#: Real names that must survive. A guard on the mint path that over-matches
#: deletes people.
MUST_SURVIVE = [
    "AT&T",
    "Coca-Cola",
    "Jean-Luc Picard",
    "Ben & Jerry",
    "Metro Fitness",
    "Claude",
    "Topos",
    "Northgate- The Foundry",
    "Rivermouth- 35 Chapel",
    "O'Brien",
    "E[3]",
]


@pytest.mark.parametrize("surface", LIVE_PLACEHOLDER_SURFACES)
def test_a_redaction_placeholder_is_refused(surface):
    assert is_valid_entity_surface(surface) is False, (
        f"{surface!r} names the redaction, not a thing in the world"
    )


@pytest.mark.parametrize("surface", UNTERMINATED)
def test_an_unterminated_placeholder_is_refused(surface):
    """The specific regression: the old pattern required the closing bracket.

    These four are exactly the live surfaces it missed. Kept as their own case so
    a future pattern change that re-requires ``]`` fails with the reason visible.
    """
    assert is_valid_entity_surface(surface) is False


@pytest.mark.parametrize("surface", MUST_SURVIVE)
def test_a_real_name_still_resolves(surface):
    assert is_valid_entity_surface(surface) is True, (
        f"{surface!r} is a real name; refusing it deletes a person or place"
    )


def test_the_guard_is_not_a_blanket_bracket_ban():
    """``E[3]`` and similar are legitimate. Over-matching is the other failure."""
    assert is_valid_entity_surface("E[3]") is True
    assert is_valid_entity_surface("Model[v2]") is True


def test_placeholder_tokens_from_the_privacy_filter_are_all_covered():
    """Drive the rejection off the producer, not a hand-written list.

    ``privacy_filter`` owns the placeholder vocabulary. If it gains a token the
    guard does not recognise, that token becomes an entity — so read the real
    set rather than trusting this file to have been updated.
    """
    try:
        from topos.sanitization.privacy_filter import ENTITY_PLACEHOLDERS
    except ImportError:  # pragma: no cover
        pytest.skip("privacy_filter placeholder vocabulary unavailable")

    tokens = [
        str(t)
        for t in (
            ENTITY_PLACEHOLDERS.values()
            if isinstance(ENTITY_PLACEHOLDERS, dict)
            else ENTITY_PLACEHOLDERS
        )
        if str(t).strip()
    ]
    assert tokens, "premise: the producer must declare at least one placeholder"

    survivors = []
    for token in tokens:
        bare = str(token)
        # both the terminated form and a span cut inside it
        for variant in (bare, f"Someone{bare.rstrip(']')}", bare.rstrip("]")):
            if variant.strip() and is_valid_entity_surface(variant):
                survivors.append(variant)
    assert survivors == [], f"these placeholder forms would still mint entities: {survivors}"
