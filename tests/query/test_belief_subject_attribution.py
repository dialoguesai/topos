"""Subject attribution for belief/interest asks (2026-07-09 live finding).

A message can be owner-AUTHORED yet ABOUT a third party. record_role tracks who
WROTE it (P4), not who it's ABOUT (P2); a first-person "what are my interests"
must not answer from the owner's description of someone else. The live top hit
for that query was an owner-authored imessage: "Her interests are in creating a
secure database ... She likes Topos ...".
"""

from __future__ import annotations

from topos.query.retrieval import _belief_about_other


# --- about a THIRD PARTY (owner authored a description of someone else) -> exclude
def test_third_person_pronoun_subject_is_about_other():
    assert _belief_about_other(
        "Her interests are in creating a secure database for people to store their "
        "medical histories and records. She likes Topos because it fits."
    ) is True


def test_my_relation_subject_is_about_other():
    assert _belief_about_other("my friend is vegan and really into cycling") is True
    assert _belief_about_other("he's obsessed with cold plunges these days") is True
    assert _belief_about_other("they love hiking every weekend") is True


# --- about the OWNER (self-referential) -> keep
def test_first_person_self_is_kept():
    assert _belief_about_other("I'm really into rock climbing lately") is False
    assert _belief_about_other("my interests are linguistics and semiotics") is False
    assert _belief_about_other("these days it's cold brew for me") is False


def test_self_reference_wins_when_both_present():
    # mentions a third party but is still about the owner -> keep (conservative)
    assert _belief_about_other("my sister and I both love hiking") is False
    assert _belief_about_other("talked to her about my climbing obsession") is False


def test_empty_or_neutral_is_not_flagged():
    assert _belief_about_other("") is False
    assert _belief_about_other("Williamsburg! Brooklyn") is False
    assert _belief_about_other("bouldering") is False
