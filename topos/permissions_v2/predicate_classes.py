"""Disclosure classes for the predicates a p2c-v3 grant may release as owner facts (OD-46).

One table says, for every releasable predicate: the owner-review-vocabulary/v1 domains and the
sensitivity a fact of that predicate carries under implicit review, the wire text, and the complete
first-person sentences that ground it under the fullmatch floor. Adding a predicate here is the
whole of "widening the allow-list": `knowledge_projections.PREDICATE_TEXT`,
`evidence.IMPLICIT_LABELS` and `native_claim_grounding._FORMS` read it.

Rules for an entry (checked by tests, not by review alone):
  * never a special category: sensitivity is "none" or "personal", and no domain is "health";
  * stated, never inferred: a predicate the derivation packs mark `altitude: inferred` has no
    sentence that states it, so nothing could ground it;
  * about the owner, never a third party: predicates whose value names a person (`rel.*`) wait for
    the owner's decision (j) under OD-15;
  * one scalar: a pack predicate with a structured value is released only through the one key field
    named here, and only when that field is one atomic label.

Off-limits is not a class. The entity boundary vetoes a fact, its value or its cited message
whatever its predicate (`knowledge_projections._unrestricted`, `EntityBoundary.check`).

IF-6 (derived facts, `TOPOS_PERMISSIONS_V2_DERIVED_FACTS`) does not widen this table. "Stated, never
inferred" is a predicate's altitude in the packs: `trait.*` stays excluded. What IF-6 changes is how a
fact of a predicate classed here may be grounded: when the cited journal entry does not state the value
in one of `forms`, the fact may still release, marked `assertion: "inferred"`, if its value clears
`inferred_facts.refusal`. A predicate outside `CLASSES` (`practices`, `training_for`) never releases that
way, whatever the grant permits (`knowledge_projections._inferred`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PredicateClass:
    domains: tuple[str, ...]
    sensitivity: str            # "none" | "personal"
    text: str                   # wire text: "Owner <text> <value>."
    forms: tuple[str, ...]      # fullmatch floor, each with one {value}
    key: str | None = None      # the scalar field of a structured pack value
    first_person: str = ''      # the plain claim sentence ("I ... {v}") an entailment check is asked about


# The twelve predicates released before OD-46, unchanged. `practices` and `training_for` are NOT here:
# their implicit class has always been health/special, which no p2c-v3 policy releases.
_ORIGINAL = {
    'works_at': PredicateClass(('work',), 'none', 'works at', (r'I work at {value}', r'I am employed by {value}')),
    'worked_at': PredicateClass(('work',), 'none', 'worked at', (r'I worked at {value}',)),
    'works_on': PredicateClass(('work',), 'none', 'works on',
                               (r'I work on {value}', r'I am working on {value} at work',
                                r"I['’]m working on {value} at work", r'My work project is {value}')),
    'role_is': PredicateClass(('work',), 'none', 'has the role', (r'My role is {value}',)),
    'certified_in': PredicateClass(('work',), 'none', 'is certified in',
                                   (r'I am certified in {value}', r"I['’]m certified in {value}")),
    'studied_at': PredicateClass(('work',), 'none', 'studied at', (r'I studied at {value}',)),
    'skilled_in': PredicateClass(('work',), 'none', 'is skilled in',
                                 (r'I am skilled in {value}', r"I['’]m skilled in {value}")),
    'prefers': PredicateClass(('hobbies',), 'personal', 'prefers', (r'I prefer {value}',)),
    'member_of': PredicateClass(('relationships',), 'personal', 'is a member of',
                                (r'I am a member of {value}', r"I['’]m a member of {value}")),
    'lives_in': PredicateClass(('home',), 'personal', 'lives in',
                               (r'I live in {value}', r'I currently live in {value}', r'My home is in {value}')),
}

# Health-classed originals keep their text so a what-if can name them; they are never releasable.
SPECIAL_ORIGINAL = {
    'practices': ('practices', (r'I practice {value}', r'I practise {value}')),
    'training_for': ('is training for', (r'I am training for {value}', r"I['’]m training for {value}")),
}

# OD-46: the measured set. Of the pack predicates the owner's current facts use (census copy 30 Sep:
# 168 of 192 facts), these are the ones that are stated, about the owner and not a special category:
# work.project (30 facts) and commit.made (7). The rest are excluded by family below (rel.* 59,
# mind.* 49, values.* 15, health.* 1, trait.* 1) or carry no scalar a sentence can state
# (work.career_event 2, work.employment_shape 1). A predicate joins only when a measurement shows it.
WIDENED = {
    'work.project': PredicateClass(('work',), 'none', 'works on the project',
                                   (r'I am working on {value}', r"I['’]m working on {value}",
                                    r'I am working on {value} at work', r"I['’]m working on {value} at work",
                                    r'I work on {value}', r'My project is {value}'), key='project',
                                   first_person='I am working on {v}'),
    'commit.made': PredicateClass(('plans',), 'personal', 'has committed to',
                                  (r'I will {value}', r"I['’]ll {value}", r'I promise to {value}',
                                   r'I promised to {value}'), key='description',
                                  first_person='I will {v}'),
}

CLASSES: dict[str, PredicateClass] = {**_ORIGINAL, **WIDENED}

# Pack predicates deliberately NOT released, with the rule that excludes each family. Listed so a
# test can prove every pack predicate the lane may meet is either classed or excluded on purpose.
EXCLUDED_FAMILIES = {
    'health.': 'special_category', 'mind.': 'special_category', 'beliefs.': 'special_category',
    'rel.': 'third_party_subject', 'trait.': 'inferred', 'values.': 'no_review_domain',
}


def excluded_reason(predicate: str) -> str | None:
    for prefix, reason in EXCLUDED_FAMILIES.items():
        if predicate.startswith(prefix):
            return reason
    return None


def scalar(predicate, payload) -> str | None:
    """The one label a stored fact of `predicate` releases, or None."""
    klass = CLASSES.get(predicate)
    if klass is None or not isinstance(payload, dict):
        return None
    if klass.key is None:
        value = payload.get('object_value')
    else:
        struct = payload.get('value_struct')
        value = struct.get(klass.key) if isinstance(struct, dict) else None
        if value is None and isinstance(payload.get('object_value'), str) and not payload['object_value'].startswith('{'):
            value = payload['object_value']
    return value if isinstance(value, str) and value.strip() else None
