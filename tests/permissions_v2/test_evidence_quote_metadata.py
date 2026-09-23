"""The quoted-metadata denylist covers the keys the messenger readers actually write.

A Signal reply stores the quoted message and its author under the Signal reader's
own camelCase keys, and an iMessage tapback stores the message it reacts to in
associated_message_guid/associated_message_type. Each of these rows carries another
person's words next to the owner's, so an owner review cannot make it the owner's
self-statement. This only withholds: a fact released under an existing grant whose
evidence row carries one of these keys stops qualifying.
"""
import json

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, decision, edit  # noqa: F401 (corpus is a fixture)


def _with_metadata(corpus, metadata):
    edit(corpus, "ALTER TABLE conversation_messages ADD COLUMN metadata_json TEXT")
    edit(corpus, "UPDATE conversation_messages SET metadata_json=?", (json.dumps(metadata),))
    attest(corpus)
    return decision(corpus)


@pytest.mark.parametrize("metadata", [
    {"quoteText": "Another person's claim"},
    {"quoteBody": "Another person's claim"},
    {"quoteAuthor": "Correspondent"},
    {"quoteAuthorAci": "5a8e1f0c-0000-4000-8000-000000000001"},
    {"quoteAuthorUuid": "5a8e1f0c-0000-4000-8000-000000000002"},
    {"quoteId": 1780000000000},
    {"quotedMessageId": "signal:41:1780000000"},
    {"storyReplyContext": {"messageId": "story-1"}},
    {"associated_message_guid": "p:0/0B1E6E5A-0000-4000-8000-000000000003"},
    {"associated_message_type": 2000},
])
def test_messenger_quote_and_tapback_metadata_is_not_an_owner_self_statement(corpus, metadata):
    assert _with_metadata(corpus, metadata).reason_code == "not_owner_self_statement"


def test_an_ordinary_imessage_row_still_qualifies(corpus):
    # The reader stores associated_message_type on every message; 0 means "none".
    metadata = {"associated_message_type": 0, "message_guid": "0B1E6E5A-0000-4000-8000-000000000004",
                "chat_guid": "iMessage;-;+15555550100"}
    assert _with_metadata(corpus, metadata).verdict == "qualified"
