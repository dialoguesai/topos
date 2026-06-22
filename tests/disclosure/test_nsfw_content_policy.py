"""NSFW tagging and grantee exclusion tests."""

from topos.disclosure.content_policy import apply_grantee_content_policy, is_record_nsfw
from topos.disclosure.tier import apply_disclosure_tier_to_rows


def test_is_record_nsfw():
    assert is_record_nsfw({"content_nsfw": 1}) is True
    assert is_record_nsfw({"content_nsfw": 0}) is False
    assert is_record_nsfw({}) is False


def test_grantee_excludes_nsfw_rows():
    rows = [
        {"message_id": "m1", "content": "hello", "content_nsfw": 0},
        {"message_id": "m2", "content": "bad", "content_nsfw": 1},
    ]
    out = apply_grantee_content_policy(rows, table="conversation_messages", tier="default_disclosure")
    assert len(out) == 1
    assert out[0]["message_id"] == "m1"


def test_owner_keeps_nsfw_rows():
    rows = [{"message_id": "m2", "content": "bad", "content_nsfw": 1}]
    out = apply_disclosure_tier_to_rows(rows, table="conversation_messages", tier="owner_raw")
    assert len(out) == 1
    assert out[0]["content"] == "bad"
