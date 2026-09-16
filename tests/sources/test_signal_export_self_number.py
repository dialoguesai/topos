"""A Signal export row is the owner's only when its number IS the owner's number.

The self-number check used substring containment in both directions, and its
precedence made the second containment run even without an owner number, so a
correspondent whose number was a piece of the owner's became the owner.
"""

from __future__ import annotations

import json

import pytest

from topos.ingestion.sources.signal_export_parser import parse_signal_export_json

OWNER = "+1 555-555-0100"


def _from_self(source: str, msg_type: str = "incoming", *, owner: str | None = OWNER) -> bool:
    export = json.dumps([{
        "conversationId": "conv-1",
        "body": "I work at Ferrograph Instruments",
        "sent_at": 1_780_000_000_000,
        "type": msg_type,
        "source": source,
    }])
    (record,) = parse_signal_export_json(export, my_phone_number=owner)
    assert record["sender_type"] == ("self" if record["from_self"] else "contact")
    return record["from_self"]


@pytest.mark.parametrize("source", [
    "5555550",        # strict substring of the owner's number
    "555-0100",       # suffix of the owner's number
    "5555550100",     # the same number without its country code
    "+155555501001",  # contains the owner's number
])
def test_a_number_that_only_overlaps_the_owners_is_not_self(source):
    assert _from_self(source) is False


def test_a_number_without_an_owner_number_is_not_self():
    assert _from_self("5555550", owner=None) is False


@pytest.mark.parametrize("source", ["+15555550100", "+1 (555) 555-0100", "15555550100"])
def test_the_owners_exact_number_is_self(source):
    assert _from_self(source) is True


def test_a_non_phone_sender_never_matches():
    assert _from_self("self") is False


def test_outgoing_is_self_and_incoming_from_another_number_is_not():
    assert _from_self("+15555550199", "outgoing") is True
    assert _from_self("+15555550199", "incoming") is False
