"""A place name that contains another names a different place, not the same one.

``token_set_similarity`` returns 1.0 whenever one token set contains the other.
For a person that is right — "Robin" abbreviates "Robin Ellery" — and the
resolver's docstring cites exactly that case as intended. For a PLACE it
inverts: place names compose by containment. "Ashford" is not short for "Ashford
Public Library", it is the city the library stands in.

So every short place entity became a magnet that swallowed any longer name
containing its tokens. Measured on the owner's node 2026-08-27, the magnets were
"US" (123 mentions), "Ashford" (88), "NYC" (49), "SF" (16) and "6th" (5) — and
the swallowed place never got a node at all. Four place entities each held two
genuinely different places:

    Ashford Public Library  ≡  Kelvin Park- Ashford      (both under "Ashford")
    Founders's Club       ≡  Greenmart HQ           (both under "6th")
    Cedar Springs         ≡  Cedar Springs Saloon
    Mill Pond              ≡  Mill Pond Trail

Two things had to be true at once, and each alone is useless:

**Removing the ``ta <= tb`` shortcut does nothing.** When ``ta`` is contained in
``tb`` the intersection equals ``sa`` exactly, so ``ratio(inter, sa)`` returns
1.0 by construction. The function is subset-means-identity by design, not by
that one line.

**Scoring the whole name is too blunt on its own.** It rejects all four
collisions but also splits "Ashford TX" from "Ashford" and "the Riverside Park" from
"Riverside Park" — 40 of 115 live surfaces, most of them correct merges. What
separates the two groups is what the extra tokens ARE: a venue or landform noun
makes a new place inside the old one, a region qualifier or article does not.

The second clause of ``place_similarity`` — a name that is nothing but feature
words is a fragment — is not a refinement but a repair to the first. Thinning
the candidate field lets the ambiguity guard (``at_threshold == 1``) stop
firing, and "the Old Lighthouse park" newly merged into the junk entity
"Park”". With the clause in place that stops, and four surfaces that previously
could not merge at all now resolve correctly, "the Riverside Park" among them.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.resolver import (
    AUTO_MERGE_SCORE,
    CONTAINMENT_TYPES,
    place_similarity,
    token_set_similarity,
)


def _merges(score: float) -> bool:
    return score >= AUTO_MERGE_SCORE


# --------------------------------------------------- the containment semantics


@pytest.mark.parametrize(
    "container,specific",
    [
        ("Ashford", "Ashford Public Library"),
        ("Ashford", "Kelvin Park- Ashford"),
        ("Ashford", "Ashford Regional Airport"),
        ("Ashford", "East Ashford"),
        ("Mill Pond", "Mill Pond Trail"),
        ("Cedar Springs", "Cedar Springs Saloon"),
        ("6th", "Greenmart HQ- 6th Street"),
        ("6th", "Founders Club- 6th @ Vine"),
        ("Northgate", "northgate bridge park"),
        ("Hudson", "Marlow Bay"),
        ("Rivermouth", "the Rivermouth Ferry"),
    ],
)
def test_a_container_does_not_swallow_a_place_inside_it(container, specific):
    assert not _merges(place_similarity(container, specific)), (
        f"{container!r} still swallows {specific!r}"
    )


@pytest.mark.parametrize(
    "a,b",
    [
        ("Ashford", "Ashford TX"),
        ("Caldwell", "caldwell texas"),
        ("Riverside Park", "the Riverside Park"),
        ("Kestrel Park", "Metro- Kestrel Park"),
        ("New Bristol", "New Bristol—I"),
        ("Cedar Springs", "Cedar Spring"),
        ("Metro Fitness", "Metro Fitness"),
    ],
)
def test_a_qualifier_or_article_still_names_the_same_place(a, b):
    """The blunt fix broke these. A region tag is not a new place."""
    assert _merges(place_similarity(a, b)), f"{a!r} and {b!r} should still be one place"


def test_a_bare_feature_word_is_a_fragment_not_a_place():
    """An entity called "park" would otherwise swallow every park there is.

    This clause exists because the first fix made this case WORSE: thinning the
    candidate field stopped the ambiguity guard firing, and this pair newly
    merged where it previously had not.
    """
    assert not _merges(place_similarity("Park", "the Old Lighthouse park"))
    assert not _merges(place_similarity("Park", "mcallen park"))


def test_a_real_place_ending_in_a_feature_word_is_not_a_fragment():
    """Control for the clause above — "Riverside Park" is a place, "Park" is not."""
    assert _merges(place_similarity("Riverside Park", "the Riverside Park"))


# ------------------------------------------------------ the type scoping


def test_people_keep_the_abbreviation_reading():
    """The resolver was designed around this and the docstring cites it.

    Type scoping is what protects it — people never reach ``place_similarity``.
    Note the demotion is narrower than "any containment": "Duncombe" is not a
    feature word, so even the place rule would leave this pair alone. What would
    have broken people is the BLUNT whole-name score, where "Robin" against
    "Robin Ellery" is 0.57.
    """
    from difflib import SequenceMatcher

    assert _merges(token_set_similarity("Robin", "Robin Ellery"))
    assert round(SequenceMatcher(None, "claire", "claire duncombe").ratio(), 2) == 0.57


def test_orgs_keep_the_abbreviation_reading():
    assert _merges(token_set_similarity("Anthropic", "Anthropic Inc"))


def test_only_places_are_treated_as_containment_types():
    assert CONTAINMENT_TYPES == {"place"}


def test_removing_the_subset_shortcut_alone_would_not_have_worked():
    """Pins the reasoning, because the obvious fix is a no-op.

    When one token set contains the other, the intersection IS the smaller
    sorted string, so ``ratio(inter, sa)`` is 1.0 regardless of the shortcut. A
    future reader who deletes the ``ta <= tb`` line expecting a behaviour change
    will get none, and this test says so.
    """
    from difflib import SequenceMatcher

    from topos.features.entities.resolver import normalize_name

    ta = set(normalize_name("Ashford").split())
    tb = set(normalize_name("Ashford Public Library").split())
    inter = " ".join(sorted(ta & tb))
    sa = " ".join(sorted(ta))

    assert SequenceMatcher(None, inter, sa).ratio() == 1.0


# --------------------------------------------------- the resolver uses it


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "res.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_place(conn, entity_id, name, aliases=()):
    from topos.features.entities.resolver import normalize_name

    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self, aliases_json) VALUES (?,?,?,?,1,0,?)",
        (entity_id, "place", name, normalize_name(name), json.dumps(list(aliases))),
    )
    conn.commit()


def test_the_resolver_mints_a_new_place_instead_of_swallowing(conn):
    from topos.features.entities.resolver import EntityResolver

    _seed_place(conn, "ent-austin", "Ashford")

    eid, tier = EntityResolver(conn).resolve(
        "Ashford Public Library", entity_type="place", queue_review=False
    )

    assert eid != "ent-austin"
    assert tier == "created"


def test_the_resolver_still_merges_a_qualifier(conn):
    from topos.features.entities.resolver import EntityResolver

    _seed_place(conn, "ent-austin", "Ashford")

    eid, tier = EntityResolver(conn).resolve(
        "Ashford TX", entity_type="place", queue_review=False
    )

    assert eid == "ent-austin"
    assert tier == "fuzzy"


def test_a_person_of_the_same_shape_still_merges(conn):
    """The rule must key on TYPE, not on the strings."""
    from topos.features.entities.resolver import EntityResolver, normalize_name

    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,1,0)",
        ("ent-robin", "person", "Robin Ellery", normalize_name("Robin Ellery")),
    )
    conn.commit()

    eid, _tier = EntityResolver(conn).resolve(
        "Robin", entity_type="person", queue_review=False
    )

    assert eid == "ent-robin"


# ------------------------------------------------------------ the repair


def test_the_repair_finds_a_swallowed_alias(conn):
    from topos.features.entities.consolidation import container_swallowed_aliases

    _seed_place(conn, "ent-austin", "Ashford", ["Ashford Public Library"])

    found = container_swallowed_aliases(conn)

    assert [f["alias"] for f in found] == ["Ashford Public Library"]


def test_the_repair_leaves_a_qualifier_alias_alone(conn):
    from topos.features.entities.consolidation import container_swallowed_aliases

    _seed_place(conn, "ent-austin", "Ashford", ["Ashford TX"])

    assert container_swallowed_aliases(conn) == []


def test_the_repair_only_runs_in_the_container_direction(conn):
    """The safeguard that stops it destroying correct merges.

    "Battery" on "Kestrel Park" and "U.S" on "US" are the same place written
    shorter. The unscoped sweep proposed 21 splits on the live node of which 4
    were wrong, and all 4 were this shape.
    """
    from topos.features.entities.consolidation import container_swallowed_aliases

    _seed_place(conn, "ent-battery", "Kestrel Park", ["Battery"])

    assert container_swallowed_aliases(conn) == []


def test_the_repair_ignores_the_entitys_own_name(conn):
    from topos.features.entities.consolidation import container_swallowed_aliases

    _seed_place(conn, "ent-tl", "Mill Pond", ["Mill Pond", "mill pond"])

    assert container_swallowed_aliases(conn) == []


def test_the_repair_dry_runs_by_default(conn):
    from topos.features.entities.consolidation import split_container_swallowed_aliases

    _seed_place(conn, "ent-austin", "Ashford", ["Ashford Public Library"])

    stats = split_container_swallowed_aliases(conn)

    assert stats["candidates"] == 1 and stats["split"] == 0
    aliases = json.loads(
        conn.execute(
            "SELECT aliases_json FROM entities WHERE entity_id='ent-austin'"
        ).fetchone()[0]
    )
    assert "Ashford Public Library" in aliases


def test_the_repair_splits_and_guards(conn):
    """``split_surface`` also writes a no_bind row, so the split cannot undo
    itself the next time the surface is seen."""
    from topos.features.entities.consolidation import split_container_swallowed_aliases

    _seed_place(conn, "ent-austin", "Ashford", ["Ashford Public Library"])

    stats = split_container_swallowed_aliases(conn, dry_run=False)
    conn.commit()

    assert stats["split"] == 1
    aliases = json.loads(
        conn.execute(
            "SELECT aliases_json FROM entities WHERE entity_id='ent-austin'"
        ).fetchone()[0]
    )
    assert "Ashford Public Library" not in aliases


def test_the_repair_is_idempotent(conn):
    from topos.features.entities.consolidation import split_container_swallowed_aliases

    _seed_place(conn, "ent-austin", "Ashford", ["Ashford Public Library"])

    split_container_swallowed_aliases(conn, dry_run=False)
    conn.commit()

    assert split_container_swallowed_aliases(conn)["candidates"] == 0
