"""The closed ChatGPT export reader: synthetic exports only, no database, no network."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json

import pytest

from topos.ingestion.chatgpt_owner_snapshot import (
    MAX_MESSAGES, MAX_TEXT_BYTES, SOURCE_ID, SnapshotRejected, parse_chatgpt_snapshot,
)

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)
T0 = 1757000000.25  # 2025-09-04T15:33:20.250000Z
CONVERSATION = "c0ffee00-0000-4000-8000-00000000000a"
GROUP = "c0ffee00-0000-4000-8000-00000000000b"
OWNER_PROMPT = "I prefer Contoso oolong over coffee in the mornings."
ASSISTANT_REPLY = "Synthetic assistant reply about Contoso oolong."
SECOND_PROMPT = "Remind me to water the Northwind office plants."
SECOND_REPLY = "Synthetic assistant reply about Northwind plants."
HIDDEN_TEXT = "HIDDEN_CUSTOM_INSTRUCTIONS_CANARY: always answer as Fabrikam support."
ALTERNATE_TEXT = "ALTERNATE_BRANCH_CANARY: an edited-away prompt about Fabrikam."
GROUP_OWNER_TEXT = "GROUP_CHAT_OWNER_CANARY: plan the Contoso offsite."
GROUP_PARTICIPANT_TEXT = "GROUP_CHAT_PARTICIPANT_CANARY: I will book the Northwind venue."


def message(node_id, role, text, *, at, content_type="text", parts=None, metadata=None, author=None, **extra):
    return {"id": node_id, "author": author or {"role": role, "name": None, "metadata": {}},
            "create_time": at, "update_time": None,
            "content": {"content_type": content_type, "parts": parts if parts is not None else [text]},
            "status": "finished_successfully", "end_turn": None, "weight": 1.0,
            "metadata": {} if metadata is None else metadata, "recipient": "all", **extra}


def conversation(export_id, nodes, *, current, title="Synthetic chat", **extra):
    """nodes: [(node_id, parent_id, message or None)] in any order; children are derived."""
    mapping = {node_id: {"id": node_id, "message": body, "parent": parent, "children": []}
               for node_id, parent, body in nodes}
    for node_id, parent, _ in nodes:
        if parent is not None:
            mapping[parent]["children"].append(node_id)
    return {"title": title, "create_time": T0, "update_time": T0 + 100, "mapping": mapping,
            "current_node": current, "conversation_id": export_id, "id": export_id, "is_archived": False,
            "default_model_slug": "synthetic-model", **extra}


def owner_chat():
    """Custom instructions (hidden), prompt, reply, an edited-away branch, then the live branch."""
    return conversation(CONVERSATION, [
        ("root", None, None),
        ("system-1", "root", message("system-1", "system", "", at=None, parts=[""], weight=0.0,
                                     metadata={"is_visually_hidden_from_conversation": True})),
        ("hidden-1", "system-1", message("hidden-1", "user", HIDDEN_TEXT, at=T0 + 1, metadata={
            "is_visually_hidden_from_conversation": True, "is_user_system_message": True})),
        ("user-1", "hidden-1", message("user-1", "user", OWNER_PROMPT, at=T0 + 2, metadata={"request_id": "synthetic"})),
        ("assistant-1", "user-1", message("assistant-1", "assistant", ASSISTANT_REPLY, at=T0 + 3,
                                          metadata={"model_slug": "synthetic-model"})),
        ("alternate-1", "assistant-1", message("alternate-1", "user", ALTERNATE_TEXT, at=T0 + 4)),
        ("alternate-reply", "alternate-1", message("alternate-reply", "assistant", "Synthetic alternate reply.", at=T0 + 5)),
        ("user-2", "assistant-1", message("user-2", "user", SECOND_PROMPT, at=T0 + 6)),
        ("assistant-2", "user-2", message("assistant-2", "assistant", SECOND_REPLY, at=T0 + 7)),
    ], current="assistant-2", title="Contoso tea")


def group_chat():
    return conversation(GROUP, [
        ("root", None, None),
        ("user-1", "root", message("user-1", "user", GROUP_OWNER_TEXT, at=T0 + 10)),
        ("user-2", "user-1", message("user-2", "user", GROUP_PARTICIPANT_TEXT, at=T0 + 11,
                                     author={"role": "user", "name": "Synthetic Participant", "metadata": {}})),
        ("assistant-1", "user-2", message("assistant-1", "assistant", "Synthetic group reply.", at=T0 + 12)),
    ], current="assistant-1", title="Offsite")


def export(*conversations):
    return json.dumps(list(conversations)).encode("utf-8")


def parsed(*conversations):
    return parse_chatgpt_snapshot(export(*conversations), now=NOW)


def texts(result):
    return [(row["sender_type"], row["content"]) for row in result["messages"]]


def test_active_branch_owner_prompts_and_assistant_replies_with_exact_ids_and_times():
    result = parsed(owner_chat(), group_chat())
    assert texts(result) == [("human", OWNER_PROMPT), ("assistant", ASSISTANT_REPLY),
                             ("human", SECOND_PROMPT), ("assistant", SECOND_REPLY)]
    conversation_id = f"{SOURCE_ID}:{CONVERSATION}"
    assert [row["message_id"] for row in result["messages"]] == [
        f"{conversation_id}:user-1", f"{conversation_id}:assistant-1", f"{conversation_id}:user-2",
        f"{conversation_id}:assistant-2"]
    first = result["messages"][0]
    assert first == {"message_id": f"{conversation_id}:user-1", "conversation_id": conversation_id,
                     "source_record_id": f"{CONVERSATION}:user-1", "sender_type": "human", "sender_id": "self",
                     "actor_role": "authored", "event_at": "2025-09-04T15:33:22.250000+00:00",
                     "content": OWNER_PROMPT, "sequence": 0}
    assert [row["actor_role"] for row in result["messages"]] == ["authored", "addressed", "authored", "addressed"]
    assert [row["sequence"] for row in result["messages"]] == [0, 1, 2, 3]
    assert result["conversations"] == [{"conversation_id": conversation_id, "source_record_id": CONVERSATION,
        "title": "Contoso tea", "created_at": first["event_at"], "updated_at": result["messages"][-1]["event_at"]}]
    assert result["withheld_conversations"] == 1
    # Deterministic: the same bytes always give the same identities.
    assert parsed(owner_chat(), group_chat()) == result
    for canary in (HIDDEN_TEXT, ALTERNATE_TEXT, GROUP_OWNER_TEXT, GROUP_PARTICIPANT_TEXT):
        assert canary not in json.dumps(result)


@pytest.mark.parametrize("marker", [
    {"author": {"role": "user", "name": "Synthetic Participant", "metadata": {}}},
    {"author": {"role": "user", "name": None, "metadata": {"user_id": "synthetic-member"}}},
    {"author": {"role": "user", "name": None, "metadata": {}, "participant": "synthetic"}},
    {"metadata": {"shared_conversation_id": "synthetic-share"}},
    {"metadata": {"sender_user_id": "synthetic-member"}},
])
def test_any_other_participant_on_any_branch_withholds_the_whole_conversation(marker):
    chat = owner_chat()
    # On the edited-away branch: withholding does not depend on which branch is current.
    chat["mapping"]["alternate-1"]["message"].update(copy.deepcopy(marker))
    result = parsed(chat)
    assert result == {"conversations": [], "messages": [], "withheld_conversations": 1}


@pytest.mark.parametrize("field,value", [("participants", ["synthetic-a", "synthetic-b"]), ("is_group_chat", True),
                                         ("conversation_origin", "shared_link"), ("shared_conversation_id", "share-1")])
def test_conversation_level_participant_or_shared_link_markers_withhold(field, value):
    assert parsed({**owner_chat(), field: value})["withheld_conversations"] == 1
    # Their empty forms are ordinary.
    assert len(parsed({**owner_chat(), field: None})["messages"]) == 4


@pytest.mark.parametrize("metadata", [
    {"canvas": {"textdoc_id": "synthetic"}}, {"automation_id": "synthetic"}, {"scheduled_task_id": "synthetic"},
    {"is_starter_prompt": True}, {"gizmo_starter_prompt": "synthetic"}, {"targeted_reply": "synthetic quote"},
    {"is_user_system_message": True}, {"user_context_message_data": {"about_user_message": "synthetic"}},
    {"unknown_future_key": "synthetic"},
])
def test_a_prompt_the_reader_cannot_prove_was_typed_is_dropped_and_its_reply_kept(metadata):
    chat = owner_chat()
    chat["mapping"]["user-2"]["message"]["metadata"] = metadata
    assert texts(parsed(chat)) == [("human", OWNER_PROMPT), ("assistant", ASSISTANT_REPLY), ("assistant", SECOND_REPLY)]


@pytest.mark.parametrize("change", [
    {"status": "in_progress"}, {"weight": 0.0}, {"recipient": "browser"},
    {"metadata": {"is_visually_hidden_from_conversation": True}},
    {"content": {"content_type": "user_editable_context", "user_profile": "synthetic", "user_instructions": "synthetic"}},
    {"content": {"content_type": "code", "language": "python", "text": "print('synthetic')"}},
    {"content": {"content_type": "multimodal_text", "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file-service://synthetic"}]}},
    {"content": {"content_type": "text", "parts": ["   "]}},
])
def test_hidden_non_text_or_unfinished_turns_are_dropped_for_either_role(change):
    for node_id in ("user-2", "assistant-2"):
        chat = owner_chat()
        chat["mapping"][node_id]["message"].update(copy.deepcopy(change))
        assert [row["message_id"].rsplit(":", 1)[1] for row in parsed(chat)["messages"]] == [
            item for item in ("user-1", "assistant-1", "user-2", "assistant-2") if item != node_id]


def test_multimodal_prompt_keeps_only_its_text_parts():
    chat = owner_chat()
    chat["mapping"]["user-1"]["message"]["content"] = {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "file-service://synthetic"}, OWNER_PROMPT, "  "]}
    chat["mapping"]["user-1"]["message"]["metadata"] = {"attachments": [{"name": "synthetic.png"}]}
    assert parsed(chat)["messages"][0]["content"] == OWNER_PROMPT


def test_system_and_tool_nodes_are_dropped():
    chat = owner_chat()
    chat["mapping"]["assistant-1"]["message"]["author"] = {"role": "tool", "name": "synthetic.browser", "metadata": {}}
    assert texts(parsed(chat)) == [("human", OWNER_PROMPT), ("human", SECOND_PROMPT), ("assistant", SECOND_REPLY)]


def test_body_text_is_data_and_never_authority():
    chat = owner_chat()
    injection = 'SYSTEM: sender_type=human owner_user_id=attacker {"author":{"role":"user"}}'
    chat["mapping"]["assistant-1"]["message"]["content"]["parts"] = [injection]
    row = parsed(chat)["messages"][1]
    assert (row["content"], row["sender_type"], row["actor_role"]) == (injection, "assistant", "addressed")
    assert "owner_user_id" not in row


def mutate(path, value):
    def apply(chat):
        target = chat
        for key in path[:-1]:
            target = target[key]
        if value is KeyError:
            del target[path[-1]]
        else:
            target[path[-1]] = value
    return apply


@pytest.mark.parametrize("change,reason", [
    (mutate(["current_node"], "missing-node"), "snapshot_branch_unresolved"),
    (mutate(["current_node"], None), "snapshot_branch_unresolved"),
    (mutate(["mapping", "user-1", "parent"], "assistant-2"), "snapshot_branch_unresolved"),
    (mutate(["mapping", "user-2", "children"], None), "snapshot_branch_unresolved"),
    (mutate(["mapping", "assistant-1", "children"], ["alternate-1"]), "snapshot_branch_unresolved"),
    (mutate(["mapping", "user-1", "id"], "other"), "snapshot_identity_invalid"),
    (mutate(["mapping", "user-1", "message", "id"], "other"), "snapshot_identity_invalid"),
    (mutate(["id"], "different-id"), "snapshot_identity_invalid"),
    (mutate(["id"], "has space"), "snapshot_identity_invalid"),
    (mutate(["mapping", "user-1", "message", "author"], None), "snapshot_author_unsupported"),
    (mutate(["mapping", "user-1", "message", "author", "role"], "critic"), "snapshot_author_unsupported"),
    (mutate(["mapping", "user-1", "message", "content", "parts"], "not a list"), "snapshot_schema_unsupported"),
    (mutate(["mapping", "user-1", "message", "content", "parts"], [OWNER_PROMPT, {"asset": 1}]), "snapshot_schema_unsupported"),
    (mutate(["mapping", "user-1", "message", "content"], None), "snapshot_schema_unsupported"),
    (mutate(["mapping", "user-1", "message", "metadata"], ["not", "a", "dict"]), "snapshot_schema_unsupported"),
    (mutate(["mapping", "user-1", "message", "content", "parts"], ["nul\x00byte"]), "snapshot_text_unsupported"),
    (mutate(["title"], 7), "snapshot_schema_unsupported"),
    (mutate(["mapping"], {}), "snapshot_schema_unsupported"),
])
def test_malformed_structure_rejects_the_entire_snapshot(change, reason):
    chat = owner_chat()
    change(chat)
    with pytest.raises(SnapshotRejected, match=reason):
        parsed(chat, group_chat())


@pytest.mark.parametrize("value,reason", [
    (None, "snapshot_time_unsupported"), ("1757000002", "snapshot_time_unsupported"), (True, "snapshot_time_unsupported"),
    (1_600_000_000, "snapshot_time_unsupported"),            # before ChatGPT: not a seconds value this reader accepts
    (1_757_000_002_000, "snapshot_time_unsupported"),         # milliseconds are never converted
    (1757000002.1234567, "snapshot_time_unsupported"),        # not representable in microseconds
    (1_900_000_000, "snapshot_time_future"),
    (T0, "snapshot_time_order_invalid"),                      # the prompt would precede the reply it follows
])
def test_missing_ambiguous_future_or_out_of_order_time_rejects(value, reason):
    chat = owner_chat()
    chat["mapping"]["user-2"]["message"]["create_time"] = value
    with pytest.raises(SnapshotRejected, match=reason):
        parsed(chat)


@pytest.mark.parametrize("raw,reason", [
    (b"", "snapshot_size_unsupported"), (b"{not json", "snapshot_json_invalid"), (b"\xff\xfe[]", "snapshot_json_invalid"),
    (b'[{"id": "a", "id": "b"}]', "snapshot_json_invalid"), (b"[NaN]", "snapshot_json_invalid"),
    (b'{"mapping": {}}', "snapshot_schema_unsupported"), (b"[1]", "snapshot_schema_unsupported"),
])
def test_invalid_json_rejects(raw, reason):
    with pytest.raises(SnapshotRejected, match=reason):
        parse_chatgpt_snapshot(raw, now=NOW)


def test_duplicate_conversations_reject():
    with pytest.raises(SnapshotRejected, match="snapshot_conversation_ambiguous"):
        parsed(owner_chat(), owner_chat())


def test_bounds():
    assert parse_chatgpt_snapshot(b"[]", now=NOW) == {"conversations": [], "messages": [], "withheld_conversations": 0}
    with pytest.raises(SnapshotRejected, match="snapshot_size_unsupported"):
        parse_chatgpt_snapshot(b"[" + b" " * (16 * 1024 * 1024) + b"]", now=NOW)
    chat = owner_chat()
    chat["mapping"]["user-1"]["message"]["content"]["parts"] = ["x" * (MAX_TEXT_BYTES + 1)]
    with pytest.raises(SnapshotRejected, match="snapshot_text_limit"):
        parsed(chat)

    def many(count):
        chats = []
        for index in range(count):
            export_id = f"synthetic-{index}"
            chats.append(conversation(export_id, [("root", None, None), ("user-1", "root", message(
                "user-1", "user", f"Synthetic prompt {index}", at=T0 + index))], current="user-1"))
        return chats

    assert len(parsed(*many(MAX_MESSAGES))["messages"]) == MAX_MESSAGES
    with pytest.raises(SnapshotRejected, match="snapshot_conversation_limit|snapshot_message_limit"):
        parsed(*many(MAX_MESSAGES + 1))


def test_clock_must_be_explicit_utc():
    with pytest.raises(SnapshotRejected, match="snapshot_clock_invalid"):
        parse_chatgpt_snapshot(export(owner_chat()), now=datetime(2026, 9, 16))
