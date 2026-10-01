"""Derived facts (IF-6 v1): a fact the node's extractor drew from one journal entry, released as `inferred`.

The extractor writes facts about the owner from journal entries (`signal_objects`, `object_type = 'fact'`, citing
`journal_entries`). They release only when the entry states the value in one of its class's first-person forms,
which on the measured node is never. With ``TOPOS_PERMISSIONS_V2_DERIVED_FACTS`` on (the owner's opt-in, global,
default off) such a fact releases with the entry it cites, under a grant that already signs the "Journal entries"
option, marked by the existing wire value ``assertion: "inferred"`` (IF-6 §5: no new field, because the CP relay
and the recipient app refuse an unknown key). The recipient already reads the entry; the added exposure is the
extractor's reading of it, so the guards here bound what an inference may say.

This module decides only the value-level guards (IF-6 §3). Everything a stated fact must clear still runs first in
``knowledge_projections.fact_projection`` (closed facts, disclosure, class, scalar, the attested subject, tombstones,
owner-only, Off-limits over the stored row, the entry's own qualification, window, NSFW, the grant's decision and
lineage); the inferred path is reached only when the stated floor and OD-38 have both failed (§2 step 7).

``refusal`` returns the first guard that withholds, as a code (never text), or None; the order is fixed:

1. ``inferred_entry_labels``: the entry's qualified labels are not owner-authored original wording with nothing
   protected (a re-check of what qualification already required, at the point of use).
2. ``inferred_entry_sensitivity``: the entry's own sensitivity is neither none nor personal.
3. ``inferred_value_shape``: not one plain scalar label (type, 2-200 characters, 1-12 words, the shared atomic
   label syntax, and characters the guards can read: NFKC-stable, no control or format character, Latin letters
   only, no combining mark).
4. ``inferred_value_protected``: the value, or the item's wire content, carries an Off-limits term, or the value
   carries a bare part of an Off-limits name as a whole word (the journal family's own rule, since the value is
   drawn from an entry); ``inferred_boundary_unavailable`` when the boundary is missing or cannot answer.
5. ``inferred_value_special``: a special category, by Lane H1's lists (``journal_goal_field._special``, with no
   verb slot: "weed", "fast", "scan" and "smoke" count, except a smoke test).
6. ``inferred_value_question_or_quote``: a question mark, a quote mark, a stray apostrophe, or a question word first.
7. ``inferred_value_not_a_value``: a URL, handle, path or domain; template or code characters; a placeholder; the
   class key or the predicate echoed back; or no letter at all.
8. ``inferred_value_names_person``: a name the node holds for someone else or the entry's people column names; a
   pronoun, kinship, role or trade word; an honorific; a possessive; or, for a predicate whose value is not a proper
   noun, a capitalised word after the first.

The lists are Lane H1's (``journal_goal_field``) and OD-38's (``entailment_grounding``), read by name, never copied:
one vocabulary, one place. ``VERSION`` moves with any change to a guard or a list it reads; the index basis carries
it while the flag is on (``search_index._family_rubric_basis``), so every index is rebuilt when it moves.
Accepted residual (IF-6 §3, guard 8): a person the node does not know, named only as the proper-noun value of a
works_at / worked_at / studied_at / member_of / lives_in / works_on / work.project fact, with no Off-limits term,
honorific or people-column mention, is not caught. The blind set (Lane O) reports it.
"""
from __future__ import annotations

import os
import re
import unicodedata

from . import entailment_grounding as eg
from . import journal_goal_field as jgf
from .fact_contract import atomic_label_syntax
from .predicate_classes import CLASSES

FLAG = "TOPOS_PERMISSIONS_V2_DERIVED_FACTS"
VERSION = "inferred-fact-guards/v1"
CODES = ("inferred_entry_labels", "inferred_entry_sensitivity", "inferred_value_shape", "inferred_value_protected",
         "inferred_boundary_unavailable", "inferred_value_special", "inferred_value_question_or_quote",
         "inferred_value_not_a_value", "inferred_value_names_person")
# Predicates whose value is expected to be a proper noun (an employer, a school, a city, a project): a capitalised
# word does not withhold by itself (OD-38's NAMED_PREDICATES requires one; 35 of the 37 releasable-class facts
# measured are work.project).
PROPER_NOUN_PREDICATES = eg.NAMED_PREDICATES | frozenset({"works_on", "work.project"})
HONORIFICS = frozenset("mr mrs ms mx miss dr prof sir dame lord lady rev fr".split())
TEMPLATE_CHARACTERS = frozenset("{}<>[]$`=;")
_DOMAIN = re.compile(r"\w\.\w{2,}")
_PLURAL_POSSESSIVE = re.compile(r"([^\W\d_]+s)'(?=\s|$|[.!])")


def enabled(env=None) -> bool:
    """The node flag, read as the family flags are (1/true/yes/on). Inert unless the journal family is on too: a
    journal citation does not resolve without it, so the flag would change nothing a recipient can receive."""
    env = os.environ if env is None else env
    if str(env.get(FLAG, "")).strip().lower() not in ("1", "true", "yes", "on"):
        return False
    from .evidence_families import family
    return family("journal_entries").enabled(env)


def refusal(value, predicate, entry, labels, *, boundary, people, env=None) -> str | None:
    """Why the extractor's ``value`` for ``predicate`` may not release as inferred from this journal ``entry``.

    ``entry``: the member row as qualification loaded it (``content``, ``people``, ``metadata_json``, NSFW flag).
    ``labels``: the entry's qualified classification (``classifications[0]``) before the merge with the fact's
    implicit labels; the owner's correction already wins there, as for a message.
    ``boundary``: an object with ``mentions_protected(*texts)`` (``EntityBoundary``); None withholds.
    ``people``: ``journal_goal_field.known_people(conn)``; None withholds (no third party can be ruled out).
    ``env``: unused by the v1 guards (the flag is the caller's, read before the call); kept so every caller passes
    one signature.
    """
    if not _labels_are(labels, "authorship", "owner_authored") or not _labels_are(labels, "speech", "original_message") \
            or not _labels_are(labels, "protected_content", "none"):
        return "inferred_entry_labels"
    if getattr(labels, "sensitivity", None) not in ("none", "personal"):
        return "inferred_entry_sensitivity"
    return value_refusal(value, predicate, entry, boundary=boundary, people=people)


def value_refusal(value, predicate, entry, *, boundary, people) -> str | None:
    """Guards 3-8 of ``refusal``: the value on its own, read against the boundary and the node's people."""
    if _shape_refused(value):
        return "inferred_value_shape"
    if boundary is None:
        return "inferred_boundary_unavailable"
    try:
        if boundary.mentions_protected(value, wire_content(predicate, value)) or _name_part(boundary, value):
            return "inferred_value_protected"
    except Exception:  # noqa: BLE001 -- an Off-limits check that cannot answer withholds
        return "inferred_boundary_unavailable"
    raw = jgf._TOKEN.findall(jgf._fold(value))
    plain = [jgf._plain(word) for word in raw]
    if jgf._special(plain, None):
        return "inferred_value_special"
    if _question_or_quote(value, [word.casefold() for word in raw]):
        return "inferred_value_question_or_quote"
    if _not_a_value(value, predicate, plain):
        return "inferred_value_not_a_value"
    if _names_person(value, predicate, entry, people, raw, plain):
        return "inferred_value_names_person"
    return None


def wire_content(predicate, value) -> str | None:
    """The item's wire content, exactly as ``fact_projection`` writes it; None for a predicate it never writes."""
    from .knowledge_projections import PREDICATE_TEXT
    if predicate not in PREDICATE_TEXT or not isinstance(value, str):
        return None
    return f"Owner {PREDICATE_TEXT[predicate]} {value}."


def snapshot_people(conn, boundary) -> frozenset:
    """``known_people(conn)``, read once per read snapshot.

    The resolver keeps one ``EntityBoundary`` per read transaction (``EvidenceResolver.entity_boundary``; its
    ``rebind`` is only for a transaction proven to hold the same rows), so the set is kept on that object and dies
    with it. A boundary built outside such a transaction is a fresh object, and the set is read again. Raises what
    ``known_people`` raises; the caller withholds.
    """
    cached = getattr(boundary, "_inferred_fact_people", None)
    if isinstance(cached, frozenset):
        return cached
    people = jgf.known_people(conn)
    try:
        boundary._inferred_fact_people = people
    except AttributeError:
        pass   # a boundary that keeps no attributes is simply read again next time
    return people


# --- the guards ----------------------------------------------------------------------------------

def _name_part(boundary, value) -> bool:
    """The journal family's own Off-limits rule on the value: a bare part of an Off-limits name, as a whole word.

    `mentions_protected` matches whole terms only, but the value is drawn from a journal entry, and an entry
    withholds on a part of an Off-limits name (`entity_boundary.NAME_PART_TABLES`). A name-only Off-limits term
    belongs to no person or contact the node holds, so neither the fact row's own veto nor guard 8 sees it. The
    value alone: the wire content's other words are the node's own template, which a whole term spanning into the
    value is already checked against. Read through the boundary's own name-part scan; a boundary without it cannot
    answer, and the caller withholds."""
    return bool(boundary.name_part_match_only("journal_entries", {"value": value}))


def _labels_are(labels, field: str, expected: str) -> bool:
    return getattr(labels, field, None) == expected


def _shape_refused(value) -> bool:
    if type(value) is not str or not 2 <= len(value.strip()) <= 200 or not 1 <= len(eg.tokens(value)) <= 12:
        return True
    try:
        atomic_label_syntax(value)
    except ValueError:
        return True
    if unicodedata.normalize("NFKC", value) != value:
        return True                  # compatibility forms and decomposed marks: never one plain label
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in ("C", "M"):
            return True              # controls, zero-width and other format characters, unassigned; combining marks
        if category[0] == "L" and not unicodedata.name(ch, "").startswith("LATIN "):
            return True              # the guards read English: a word they cannot read withholds
    return False


def _question_or_quote(value: str, low: list) -> bool:
    if "?" in value or any(mark in value for mark in jgf._QUOTES) or any(mark in value for mark in eg._QUOTES):
        return True
    last = len(value) - 1
    for index, ch in enumerate(value):
        if ch in "'\u2019":
            before = value[index - 1] if index else ""
            after = value[index + 1] if index < last else ""
            if not (before.isalnum() and (after.isalnum() or before in "sS")):
                return True          # an apostrophe that is not inside a word or a plural possessive
    opening = jgf._starts(low, 0, jgf.LEADING_TIME)
    return opening < len(low) and low[opening] in jgf.QUESTION_START


def _not_a_value(value: str, predicate, plain: list) -> bool:
    folded = value.casefold()
    if "://" in value or "www." in folded or any(ch in value for ch in "/@#") or _DOMAIN.search(value):
        return True                  # a URL, a handle or a path, or a domain
    if any(ch in TEMPLATE_CHARACTERS for ch in value):
        return True                  # template or code
    if jgf.PLACEHOLDERS & set(plain) or any(jgf._has(plain, phrase) for phrase in jgf.PLACEHOLDER_PHRASES):
        return True
    klass = CLASSES.get(predicate)
    echoed = {predicate.casefold()} if isinstance(predicate, str) else set()
    if klass is not None and klass.key:
        echoed.add(klass.key.casefold())
    if value.strip().casefold() in echoed:
        return True                  # the class's own field name, or the predicate, as the value
    return not any(ch.isalpha() for ch in value)   # nothing to say: digits and punctuation only


def _names_person(value: str, predicate, entry, people, raw: list, plain: list) -> bool:
    if people is None:
        return True                  # the node's people could not be read: no third party can be ruled out
    words = set(plain)
    try:
        listed = entry["people"]     # the row as loaded: a dict, or a sqlite3.Row
    except (KeyError, IndexError, TypeError):
        listed = None                # a row with no people column names no one there
    if (frozenset(people) | jgf._names_in(listed)) & words:
        return True                  # someone the node knows, or the entry says was there
    if jgf._PEOPLE & words or any(len(word) > 3 and eg.stem(word) in jgf._PEOPLE_STEMS for word in plain):
        return True                  # a pronoun, a relation or a role
    if any(jgf._has(plain, phrase) for phrase in jgf.THIRD_PARTY_PHRASES):
        return True
    if any(word.endswith(jgf.PERSON_SUFFIXES) and word not in jgf.PERSON_SUFFIX_EXEMPT and len(word) > 4
           for word in plain):
        return True                  # a person by trade or field
    if HONORIFICS & words:
        return True
    if any(word.endswith("'s") and word[:-2] not in jgf.TIME_WORDS for word in plain):
        return True                  # someone's: a possessive other than a time's
    if any(jgf._plain(match.group(1)) not in jgf.TIME_WORDS for match in _PLURAL_POSSESSIVE.finditer(jgf._fold(value))):
        return True                  # a plural possessive; "two weeks'" is a time's
    if predicate not in PROPER_NOUN_PREDICATES and any(jgf._name_like(word) for word in raw[1:]):
        return True                  # a capitalised word after the first, where the value is not a proper noun
    return False
