"""chat_ui_raw_payload maps the v1 chat record vocabulary, legacy names as fallback.

The browser extension, the file importer, and the schema registry all speak the
v1 chat shape {id, thread_id, role, content, created_at}. The node's UI-stream
ingest path was written for an older store_message client that sent
{message_id, conversation_id, sender_type} and read ONLY those names. Against a
v1 payload that mislabelled every message role=user (sender_type absent), merged
every chat under the dataset id (conversation_id absent), and minted a fresh id
per record (message_id absent) — defeating message-id idempotency. These pin the
fix at the pure mapping, so the guard is fast and deterministic.
"""
from __future__ import annotations

from topos.ingestion.ingest_helpers import chat_ui_raw_payload


def test_v1_fields_win():
    # Exactly what the browser extension sends: an assistant reply, real thread.
    out = chat_ui_raw_payload(
        {
            "id": "gpt-msg-1",
            "thread_id": "conv-real-1",
            "role": "assistant",
            "content": "You're already very close.",
            "created_at": 1788480000,
        },
        job_id="job-x",
        dataset_id="owner:default:ds",
    )
    assert out["role"] == "assistant"  # not flattened to user
    assert out["thread_id"] == "conv-real-1"  # not the dataset id
    assert out["id"] == "gpt-msg-1"  # real id survives -> idempotent resend
    assert out["content"] == "You're already very close."


def test_legacy_store_message_names_still_work():
    out = chat_ui_raw_payload(
        {
            "message_id": "legacy-1",
            "conversation_id": "conv-legacy-1",
            "sender_type": "assistant",
            "content": "legacy reply",
            "created_at": 1788480000,
        },
        job_id="job-x",
        dataset_id="owner:default:ds",
    )
    assert out["role"] == "assistant"
    assert out["thread_id"] == "conv-legacy-1"
    assert out["id"] == "legacy-1"


def test_missing_thread_and_role_fall_back_safely():
    # No thread id anywhere -> dataset id (a single bucket, but never a crash);
    # no role/sender_type -> the historical "human"->user default.
    out = chat_ui_raw_payload(
        {"content": "hi", "created_at": 1},
        job_id="job-x",
        dataset_id="owner:default:ds",
    )
    assert out["thread_id"] == "owner:default:ds"
    assert out["role"] == "user"
    assert out["id"] == "job-x"  # falls back to the job id, never empty


def test_user_role_preserved_not_rewritten():
    out = chat_ui_raw_payload(
        {"id": "u1", "thread_id": "t1", "role": "user", "content": "q", "created_at": 1},
        job_id="j",
        dataset_id="d",
    )
    assert out["role"] == "user"
    assert out["thread_id"] == "t1"
