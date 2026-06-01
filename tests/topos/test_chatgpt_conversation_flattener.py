"""Tests for ChatGPT conversation flattener."""

import json
from pathlib import Path

import pytest

from topos.ingestion.parsers.chatgpt_conversation_flattener import (
    extract_content,
    flatten_conversation,
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


def test_flatten_conversation_real_sample():
    """Test with a real conversation sample from conversations.json."""
    # Load a sample conversation
    conversations_file = Path(__file__).parent.parent.parent.parent / "conversations.json"
    if not conversations_file.exists():
        pytest.skip("conversations.json not found")
    
    with open(conversations_file, "r") as f:
        conversations = json.load(f)
    
    if not conversations:
        pytest.skip("No conversations in file")
    
    # Test first conversation
    conv = conversations[0]
    records = list(flatten_conversation(conv, include_system=False))
    
    # Should have at least some messages
    assert len(records) > 0
    
    # Check record structure
    for record in records[:5]:  # Check first 5
        assert "id" in record
        assert "thread_id" in record
        assert "role" in record
        assert "content" in record
        assert "created_at" in record
        assert record["role"] in ["user", "assistant"]  # No system messages


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
