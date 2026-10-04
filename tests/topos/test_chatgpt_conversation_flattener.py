"""Tests for ChatGPT conversation flattener.

Every conversation here is invented, the export-shaped one included (BL-37: that test used to load the owner's own
``conversations.json`` from the workspace root). Nothing outside the repository is read.
"""

import json
from datetime import datetime, timezone

import pytest

from topos.ingestion.parsers.chatgpt_conversation_flattener import (
    extract_content,
    flatten_conversation,
    flatten_conversation_array,
    is_conversation_format,
)


def test_is_conversation_format():
    """Test conversation format detection."""
    # Valid conversation
    conv = {
        "id": "test-conv",
        "conversation_id": "test-conv",
        "mapping": {"node1": {"id": "node1", "message": None}},
    }
    assert is_conversation_format(conv) is True
    
    # Invalid (no mapping)
    assert is_conversation_format({"id": "test"}) is False
    
    # Invalid (mapping is not dict)
    assert is_conversation_format({"mapping": []}) is False


def test_extract_content_text():
    """Test content extraction for text type."""
    content_obj = {
        "content_type": "text",
        "parts": ["Hello", "world"],
    }
    assert extract_content(content_obj, "text") == "Hello world"
    
    # Empty parts
    assert extract_content({"content_type": "text", "parts": []}, "text") == ""
    
    # Single part
    assert extract_content({"content_type": "text", "parts": ["Single"]}, "text") == "Single"


def test_extract_content_thoughts():
    """Test content extraction for thoughts type."""
    content_obj = {
        "content_type": "thoughts",
        "thoughts": [
            {"summary": "Summary 1", "content": "Content 1"},
            {"summary": "Summary 2"},
        ],
    }
    result = extract_content(content_obj, "thoughts")
    assert "Summary 1" in result
    assert "Summary 2" in result


def test_flatten_conversation_simple():
    """Test flattening a simple conversation."""
    conversation = {
        "id": "test-conv",
        "conversation_id": "test-conv",
        "title": "Test Conversation",
        "create_time": 1640995200.0,
        "mapping": {
            "root": {
                "id": "root",
                "message": None,
                "parent": None,
                "children": ["msg1"],
            },
            "msg1": {
                "id": "msg1",
                "message": {
                    "id": "msg-1",
                    "author": {"role": "user"},
                    "create_time": 1640995201.0,
                    "content": {"content_type": "text", "parts": ["Hello"]},
                },
                "parent": "root",
                "children": ["msg2"],
            },
            "msg2": {
                "id": "msg2",
                "message": {
                    "id": "msg-2",
                    "author": {"role": "assistant"},
                    "create_time": 1640995202.0,
                    "content": {"content_type": "text", "parts": ["Hi there"]},
                },
                "parent": "msg1",
                "children": [],
            },
        },
    }
    
    records = list(flatten_conversation(conversation, include_system=False))
    assert len(records) == 2
    
    # Check first record (user)
    assert records[0]["id"] == "msg-1"
    assert records[0]["thread_id"] == "test-conv"
    assert records[0]["role"] == "user"
    assert records[0]["content"] == "Hello"
    assert records[0]["created_at"] == 1640995201.0
    
    # Check second record (assistant)
    assert records[1]["id"] == "msg-2"
    assert records[1]["role"] == "assistant"
    assert records[1]["content"] == "Hi there"


def test_flatten_conversation_skips_system():
    """Test that system messages are skipped by default."""
    conversation = {
        "id": "test-conv",
        "conversation_id": "test-conv",
        "mapping": {
            "msg1": {
                "id": "msg1",
                "message": {
                    "id": "msg-1",
                    "author": {"role": "system"},
                    "content": {"content_type": "text", "parts": ["System message"]},
                },
                "parent": None,
                "children": [],
            },
        },
    }
    
    records = list(flatten_conversation(conversation, include_system=False))
    assert len(records) == 0
    
    # With include_system=True, should include it
    records = list(flatten_conversation(conversation, include_system=True))
    assert len(records) == 1
    assert records[0]["role"] == "system"


#: The invented export's first stamp, computed so no long digit run sits in the source.
STARTED = datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc).timestamp()


def _node(node_id, role=None, content=None, *, parent=None, children=(), at=None, hidden=False):
    """One node of an export's message tree, as ChatGPT writes it: no message on the root."""
    message = None
    if role is not None:
        message = {"id": f"msg-{node_id}", "author": {"role": role, "name": None, "metadata": {}},
                   "create_time": at, "update_time": None, "content": content, "status": "finished_successfully",
                   "metadata": {"is_visually_hidden_from_conversation": True} if hidden else {}}
    return {"id": node_id, "message": message, "parent": parent, "children": list(children)}


def _text(*parts):
    return {"content_type": "text", "parts": list(parts)}


def _conversation(conversation_id, title, nodes, *, current_node, at):
    return {"id": conversation_id, "conversation_id": conversation_id, "title": title, "create_time": at,
            "update_time": at + 900, "mapping": {node["id"]: node for node in nodes}, "current_node": current_node,
            "is_archived": False, "default_model_slug": "invented-model"}


def _invented_export() -> list:
    """An export in the shape of ChatGPT's ``conversations.json``: a list of conversations, each a tree of message
    nodes under ``mapping`` with a root that carries no message. Every title, word, id and time is coined."""
    first = _conversation("conv-rota", "Plot watering rota", [
        _node("root", children=["sys"]),
        _node("sys", "system", _text(""), parent="root", children=["u1"], hidden=True),
        _node("u1", "user", _text("Draft a four-week watering rota for a shared plot."), parent="sys",
              children=["a1"], at=STARTED + 5),
        _node("a1", "assistant", {"content_type": "thoughts",
                                  "thoughts": [{"summary": "Split the weeks between two people",
                                                "content": "Alternate the mornings."}]},
              parent="u1", children=["a2"], at=STARTED + 9),
        _node("a2", "assistant", _text("Week one: Ferrow waters on Monday, Quellin on Thursday."), parent="a1",
              children=["u2"], at=STARTED + 12),
        _node("u2", "user", {"content_type": "multimodal_text",
                             "parts": [{"content_type": "image_asset_pointer",
                                        "asset_pointer": "file-service://invented"},
                                       "Here is the plot plan; does the rota fit it?"]},
              parent="a2", children=["t1"], at=STARTED + 60),
        _node("t1", "tool", {"content_type": "execution_output", "output": "rows: 4, beds: 6"}, parent="u2",
              children=["a3"], at=STARTED + 61),
        _node("a3", "assistant", {"content_type": "code", "language": "python", "code": "beds_per_week = 6 // 4"},
              parent="t1", children=["a4"], at=STARTED + 63),
        _node("a4", "assistant", _text(""), parent="a3", at=STARTED + 64),
    ], current_node="a4", at=STARTED)
    second = _conversation("conv-loaf", "Rye loaf timings", [
        _node("root", children=["u1"]),
        _node("u1", "user", _text("How long should a rye loaf proof?"), parent="root", children=["a1"],
              at=STARTED + 86_400),
        _node("a1", "assistant", _text("Try a longer second proof and check the crumb."), parent="u1",
              at=STARTED + 86_410),
    ], current_node="a1", at=STARTED + 86_400)
    return [first, second]


def test_flatten_conversation_export_sample():
    """An export-shaped sample: the first conversation's turns in tree order, system and empty turns left out."""
    conversations = json.loads(json.dumps(_invented_export()))     # as the export file decodes
    assert all(is_conversation_format(conversation) for conversation in conversations)

    records = list(flatten_conversation(conversations[0], include_system=False))

    assert [(record["id"], record["role"]) for record in records] == [
        ("msg-u1", "user"), ("msg-a1", "assistant"), ("msg-a2", "assistant"), ("msg-u2", "user"),
        ("msg-t1", "assistant"), ("msg-a3", "assistant")]
    for record in records:
        assert set(record) == {"id", "thread_id", "role", "content", "created_at", "_metadata"}
        assert record["thread_id"] == "conv-rota" and record["content"]
        assert record["role"] in ["user", "assistant"]  # No system messages
    assert records[0]["created_at"] == STARTED + 5
    assert records[1]["content"] == "Split the weeks between two people"         # a thought's summary
    assert records[3]["content"] == "Here is the plot plan; does the rota fit it?"  # the image part left out
    assert records[4]["_metadata"]["original_role"] == "tool"
    assert records[5]["content"] == "```\nbeds_per_week = 6 // 4\n```"
    # The whole export: both conversations, each turn once.
    assert len(list(flatten_conversation_array(conversations))) == len(records) + 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
