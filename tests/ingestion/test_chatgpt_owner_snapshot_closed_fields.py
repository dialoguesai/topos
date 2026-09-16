"""Where a sender is recorded is part of the closed reader contract too.

A group chat has to record who sent each message somewhere. Author name, author
metadata and message metadata are already closed; a set field on the message or
its mapping node that an ordinary export does not carry is closed the same way,
so another member's prompt cannot pass as the owner's by carrying its sender
there. Synthetic exports only.
"""
from __future__ import annotations

import copy

import pytest

from tests.ingestion.test_chatgpt_owner_snapshot import (
    ASSISTANT_REPLY, OWNER_PROMPT, SECOND_PROMPT, SECOND_REPLY, owner_chat, parsed, texts,
)


def place(chat, where, field, value):
    node = chat["mapping"]["user-2"]
    (node["message"] if where == "message" else node)[field] = copy.deepcopy(value)
    return chat


@pytest.mark.parametrize("where,field,value", [
    ("message", "sender", {"user_id": "synthetic-member"}),
    ("message", "user_id", "synthetic-member"),
    ("message", "is_from_other_member", True),
    ("node", "sender_user", "synthetic-member"),
])
def test_a_prompt_carrying_a_set_field_the_reader_does_not_know_is_dropped_and_its_reply_kept(where, field, value):
    assert texts(parsed(place(owner_chat(), where, field, value))) == [
        ("human", OWNER_PROMPT), ("assistant", ASSISTANT_REPLY), ("assistant", SECOND_REPLY)]


@pytest.mark.parametrize("where,field", [("message", "sender"), ("node", "sender_user"), ("message", "channel")])
@pytest.mark.parametrize("value", [None, False, "", [], {}])
def test_empty_forms_of_any_field_are_ordinary(where, field, value):
    assert ("human", SECOND_PROMPT) in texts(parsed(place(owner_chat(), where, field, value)))


def test_an_assistant_turn_is_not_dropped_for_a_field_it_carries():
    chat = owner_chat()
    chat["mapping"]["assistant-2"]["message"]["sender"] = {"model": "synthetic"}
    assert texts(parsed(chat))[-1] == ("assistant", SECOND_REPLY)
