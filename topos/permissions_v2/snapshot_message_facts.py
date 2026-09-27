"""Small additional rules used only by the attested snapshot derivation lane.

No model or broad project inference: accept a complete, explicit first-person
work-project sentence. Evidence/ownership is still the ingestion service's job.
"""
from __future__ import annotations

import re

from topos.features.facts.extract import extract_message_facts, _is_owner_authored
from topos.features.facts.reactions import quotes_another_message
from .fact_contract import atomic_label_syntax

# Each word starts as a proper label; prose, conjunction clauses, pronouns,
# questions and hypothetical prefixes are outside this rule. The work context
# is explicit so a personal activity is not labelled as a work project.
_LABEL = r"([A-Z][A-Za-z0-9]*(?:[ -][A-Z0-9][A-Za-z0-9]*){0,4})"
_PROJECT = tuple(re.compile(pattern) for pattern in (
    r"\AMy (?:current )?work project is " + _LABEL + r"\.?\Z",
    r"\AAt work, I(?: am|'m|’m) working on " + _LABEL + r"\.?\Z",
    r"\AI(?: am|'m|’m) working on " + _LABEL + r" at work\.?\Z",
))


def extract_snapshot_message_facts(row, conn=None, *, table):
    facts = extract_message_facts(row, conn, table=table)
    if not _is_owner_authored(row, table) or quotes_another_message(row):
        return facts
    content = row.get('content')
    if type(content) is not str:
        return facts
    for pattern in _PROJECT:
        match = pattern.fullmatch(content)
        if match is None:
            continue
        value = match.group(1)
        if len(value) > 40:
            continue
        try:
            atomic_label_syntax(value)
        except ValueError:
            continue
        facts.append({'predicate': 'works_on', 'object_value': value, 'confidence': 0.6,
            'dimension': 'work', 'disclosure': 'scoped', 'valid_from': row.get('event_at')})
        break
    return facts
