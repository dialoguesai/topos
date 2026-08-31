"""ChatGPT export → canonical rows, with no enrichment in the way.

Sprint-2 gates from PLAN_CHATGPT_IMPORT.md: a blank turn never reaches
``ai_chat_messages``, the declared conversation title is stored rather than
dropped at the mapper, and the owner's turns land as ``human`` so the provenance
gate in ``features/provenance/roles.py`` can attribute them.

The parse → validate → canonicalize path is driven directly; the manager's
enrichment lane is a separate concern and is what makes the full e2e slow.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Dict, List

import pytest

from topos.ingestion.parser import parse_file
from topos.ingestion.parsers.chatgpt_parser import ChatGPTParser
from topos.ingestion.sources.base import RawRecord
from topos.storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

DATASET_ID = "test-user:chatgpt"
SOURCE_ID = "chatgpt_file_ingestion"

JULY = 1753228800.0  # 2025-07-23
MARCH = 1743379200.0  # 2025-03-31


def _message(node_id: str, role: str, content: Dict[str, Any], at: float, metadata: Dict[str, Any] | None = None):
    return {
        "id": f"msg-{node_id}",
        "author": {"role": role, "name": None},
        "create_time": at,
        "content": content,
        "metadata": metadata or {},
    }


def export_fixture() -> List[Dict[str, Any]]:
    """One conversation carrying every shape the real export mixes together."""
    return [
        {
            "id": "conv-july",
            "conversation_id": "conv-july",
            "title": "GCP VM IAM Role",
            "create_time": JULY,
            "update_time": JULY + 900,
            "default_model_slug": "o4-mini-high",
            "memory_scope": "global_enabled",
            "current_node": "a2",
            "mapping": {
                "root": {"id": "root", "message": None, "parent": None, "children": ["sys"]},
                "sys": {
                    "id": "sys",
                    "parent": "root",
                    "children": ["u1"],
                    "message": _message("sys", "system", {"content_type": "text", "parts": [""]}, JULY,
                                        {"is_visually_hidden_from_conversation": True}),
                },
                "u1": {
                    "id": "u1",
                    "parent": "sys",
                    "children": ["think", "a1", "a2"],
                    "message": _message("u1", "user", {"content_type": "text", "parts": ["which IAM role starts a VM?"]}, JULY),
                },
                # Model scaffolding: the old reader stored both of these as speech.
                "think": {
                    "id": "think",
                    "parent": "u1",
                    "children": [],
                    "message": _message("think", "assistant", {"content_type": "thoughts", "thoughts": [{"summary": "s", "content": "c"}]}, JULY),
                },
                # Superseded regeneration, off the current_node path.
                "a1": {
                    "id": "a1",
                    "parent": "u1",
                    "children": [],
                    "message": _message("a1", "assistant", {"content_type": "text", "parts": ["first draft"]}, JULY),
                },
                "a2": {
                    "id": "a2",
                    "parent": "u1",
                    "children": [],
                    "message": _message(
                        "a2",
                        "assistant",
                        {"content_type": "text", "parts": ["roles/compute.instanceAdmin.v1"]},
                        JULY + 60,
                        {"citations": [{"metadata": {"url": "https://cloud.google.com/iam"}}]},
                    ),
                },
            },
        },
        {
            "id": "conv-march",
            "conversation_id": "conv-march",
            "title": "Old thread",
            "create_time": MARCH,
            "update_time": MARCH,
            "current_node": "m1",
            "mapping": {
                "root2": {"id": "root2", "message": None, "parent": None, "children": ["m1"]},
                "m1": {
                    "id": "m1",
                    "parent": "root2",
                    "children": [],
                    "message": _message("m1", "user", {"content_type": "text", "parts": ["ancient question"]}, MARCH),
                },
            },
        },
    ]


async def _records(payload: List[Dict[str, Any]], options: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    raw = json.dumps(payload).encode("utf-8")

    async def stream():
        yield raw

    return [record async for record in parse_file(stream(), "json", options)]


def _canonicalize(conn: sqlite3.Connection, payload: List[Dict[str, Any]], options: Dict[str, Any] | None = None):
    records = asyncio.run(_records(payload, options))
    parser = ChatGPTParser(dataset_id=DATASET_ID, _schema_id="chatgpt.conversation.v2")
    staging: List[Dict[str, Any]] = []
    for record in records:
        raw = RawRecord(record_id=str(record.get("id")), payload=record)
        result = parser.validate(raw)
        assert result.is_valid, f"record rejected by schema: {result.errors}"
        normalized = parser.parse(raw).payload
        normalized["source_id"] = SOURCE_ID
        staging.append(normalized)

    canonicalizer = Canonicalizer(CanonicalTablesManager(conn))
    return canonicalizer.canonicalize_staging_batch(
        staging, source="chatgpt", sync_batch_id="test-batch", mapping_source_id=SOURCE_ID
    )


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def test_no_blank_turn_reaches_canonical(conn):
    _canonicalize(conn, export_fixture())
    blank = conn.execute(
        "SELECT COUNT(*) FROM ai_chat_messages WHERE TRIM(COALESCE(content,'')) = ''"
    ).fetchone()[0]
    assert blank == 0


def test_declared_title_is_stored_not_dropped(conn):
    """``canonicalizer.py`` passed ``title=None`` since the table was written."""
    _canonicalize(conn, export_fixture())
    titles = {row["conversation_id"]: row["title"] for row in conn.execute(
        "SELECT conversation_id, title FROM ai_chat_conversations"
    )}
    assert titles == {"chatgpt:conv-july": "GCP VM IAM Role", "chatgpt:conv-march": "Old thread"}


def test_owner_turns_land_as_human_for_the_provenance_gate(conn):
    _canonicalize(conn, export_fixture())
    senders = dict(conn.execute("SELECT sender_type, COUNT(*) FROM ai_chat_messages GROUP BY 1").fetchall())
    # 2 owner questions, 1 surviving assistant answer.
    assert senders == {"human": 2, "assistant": 1}


def test_scaffolding_and_superseded_branches_never_land(conn):
    _canonicalize(conn, export_fixture())
    contents = [row["content"] for row in conn.execute("SELECT content FROM ai_chat_messages")]
    assert "roles/compute.instanceAdmin.v1" in contents
    assert "first draft" not in contents  # regenerated away
    assert not any("c" == text for text in contents)  # the thoughts body


def test_declared_facets_survive_into_metadata_json(conn):
    _canonicalize(conn, export_fixture())
    row = conn.execute(
        "SELECT metadata_json FROM ai_chat_messages WHERE content LIKE 'roles/compute%'"
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata["conversation_title"] == "GCP VM IAM Role"
    assert metadata["model_slug"] == "o4-mini-high"
    assert metadata["memory_scope"] == "global_enabled"
    assert metadata["citation_urls"] == ["https://cloud.google.com/iam"]


def test_date_window_is_applied_at_parse_time(conn):
    """The window the UI picked reaches the reader through the job payload."""
    result = _canonicalize(conn, export_fixture(), {"date_from": JULY - 86400})
    assert result["messages_created"] == 2
    ids = [row["conversation_id"] for row in conn.execute("SELECT conversation_id FROM ai_chat_conversations")]
    assert ids == ["chatgpt:conv-july"]


def test_window_default_is_open_when_no_options_are_passed(conn):
    _canonicalize(conn, export_fixture(), None)
    assert conn.execute("SELECT COUNT(*) FROM ai_chat_conversations").fetchone()[0] == 2
