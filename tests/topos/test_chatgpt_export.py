"""Extractor v3 for ChatGPT exports (PLAN_CHATGPT_IMPORT.md Sprint 1).

Each test names a defect the shipped flattener has on the real export, so a
regression reads as the specific thing that broke rather than "counts changed".
"""

from __future__ import annotations

import pytest

from topos.ingestion.parsers.chatgpt_export import (
    DROP_ALTERNATE_BRANCH,
    DROP_DO_NOT_REMEMBER,
    DROP_EMPTY,
    DROP_HIDDEN,
    DROP_OUT_OF_WINDOW,
    DROP_SCAFFOLD,
    DROP_TOOL_OUTPUT,
    DropLedger,
    ExportOptions,
    active_path_ids,
    conversation_activity,
    conversation_in_window,
    extract_content,
    is_conversation,
    iter_export,
    iter_turns,
)

JULY_2025 = 1753277822.0  # 2025-07-23, inside the sample export
MARCH_2025 = 1743399442.0


def node(node_id, *, parent=None, children=(), role=None, content=None, create_time=None, metadata=None, name=None):
    message = None
    if role is not None:
        message = {
            "id": f"msg-{node_id}",
            "author": {"role": role, "name": name},
            "create_time": create_time,
            "content": content if content is not None else {"content_type": "text", "parts": ["hi"]},
            "metadata": metadata or {},
        }
    return {"id": node_id, "message": message, "parent": parent, "children": list(children)}


def conversation(nodes, *, current_node, **envelope):
    payload = {
        "id": "conv-1",
        "conversation_id": "conv-1",
        "title": "Sample chat",
        "create_time": JULY_2025,
        "update_time": JULY_2025 + 600,
        "mapping": {n["id"]: n for n in nodes},
        "current_node": current_node,
    }
    payload.update(envelope)
    return payload


def linear_conversation(**envelope):
    """root → user → assistant, the shape almost every export node has."""
    return conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1"], role="user", create_time=JULY_2025),
            node("a1", parent="u1", role="assistant", create_time=JULY_2025 + 10),
        ],
        current_node="a1",
        **envelope,
    )


# --------------------------------------------------------------------------
# extract_content — the three fields the old reader asked for do not exist
# --------------------------------------------------------------------------


def test_code_reads_text_not_code():
    """Old reader read ``code``; the export writes ``text``, so 147 real code
    blocks became blank rows."""
    text, assets = extract_content(
        {"content_type": "code", "language": "python", "text": "print(1)", "response_format_name": None}
    )
    assert "print(1)" in text
    assert text.startswith("```python")
    assert assets == []


def test_code_with_unknown_language_has_a_bare_fence():
    text, _ = extract_content({"content_type": "code", "language": "unknown", "text": "x=1"})
    assert text.startswith("```\n")


def test_execution_output_reads_text_not_output():
    text, _ = extract_content({"content_type": "execution_output", "text": "42"})
    assert text == "42"


def test_reasoning_recap_reads_content_not_reasoning_recap():
    text, _ = extract_content({"content_type": "reasoning_recap", "content": "Thought for 9 seconds"})
    assert text == "Thought for 9 seconds"


def test_thoughts_prefers_body_over_summary_and_does_not_repeat_it():
    """The old reader emitted the summary twice for a two-thought message."""
    text, _ = extract_content(
        {
            "content_type": "thoughts",
            "thoughts": [
                {"summary": "Pulling a commit", "content": "Checking the ref"},
                {"summary": "Second", "content": ""},
            ],
        }
    )
    assert text == "Checking the ref\n\nSecond"


def test_multimodal_keeps_text_and_lifts_asset_pointers():
    text, assets = extract_content(
        {
            "content_type": "multimodal_text",
            "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-abc", "width": 100, "height": 50},
                "what is in this screenshot?",
            ],
        }
    )
    assert text == "what is in this screenshot?"
    assert assets == [
        {"kind": "image_asset_pointer", "pointer": "file-abc", "width": 100, "height": 50}
    ]


def test_unknown_content_type_falls_back_to_a_generic_field():
    text, _ = extract_content({"content_type": "some_future_type", "text": "still readable"})
    assert text == "still readable"


def test_non_dict_content_is_empty_not_an_exception():
    assert extract_content(None) == ("", [])
    assert extract_content("nope") == ("", [])


# --------------------------------------------------------------------------
# Tree traversal — a turn is not a node
# --------------------------------------------------------------------------


def test_active_path_is_the_current_node_ancestry():
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1", "a2"], role="user"),
            node("a1", parent="u1", role="assistant"),  # regenerated away
            node("a2", parent="u1", role="assistant"),
        ],
        current_node="a2",
    )
    assert active_path_ids(conv) == ["root", "u1", "a2"]


def test_regenerated_branches_are_dropped_and_counted():
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1", "a2"], role="user"),
            node("a1", parent="u1", role="assistant", content={"content_type": "text", "parts": ["first try"]}),
            node("a2", parent="u1", role="assistant", content={"content_type": "text", "parts": ["second try"]}),
        ],
        current_node="a2",
    )
    ledger = DropLedger()
    turns = list(iter_turns(conv, ExportOptions(), ledger))
    assert [t["content"] for t in turns] == ["hi", "second try"]
    assert ledger.dropped[DROP_ALTERNATE_BRANCH] == 1


def test_alternate_branches_can_be_opted_back_in_and_are_labelled():
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1", "a2"], role="user"),
            node("a1", parent="u1", role="assistant", content={"content_type": "text", "parts": ["first try"]}),
            node("a2", parent="u1", role="assistant", content={"content_type": "text", "parts": ["second try"]}),
        ],
        current_node="a2",
    )
    turns = list(iter_turns(conv, ExportOptions(include_alternate_branches=True)))
    branches = {t["content"]: t["_metadata"]["branch"] for t in turns}
    assert branches["first try"] == "alternate"
    assert branches["second try"] == "active"


def test_missing_current_node_keeps_everything_rather_than_dropping_the_thread():
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", role="user"),
        ],
        current_node="does-not-exist",
    )
    turns = list(iter_turns(conv, ExportOptions()))
    assert [t["_metadata"]["branch"] for t in turns] == ["unknown"]


# --------------------------------------------------------------------------
# Inclusion policy
# --------------------------------------------------------------------------


def test_blank_content_never_becomes_a_turn():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node("a1", parent="root", role="assistant", content={"content_type": "text", "parts": ["", "   "]}),
        ],
        current_node="a1",
    )
    ledger = DropLedger()
    assert list(iter_turns(conv, ExportOptions(), ledger)) == []
    assert ledger.dropped[DROP_EMPTY] == 1


def test_model_scaffolding_is_never_a_turn_even_with_tool_output_on():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node(
                "a1",
                parent="root",
                role="assistant",
                content={"content_type": "reasoning_recap", "content": "Thought for 9 seconds"},
            ),
        ],
        current_node="a1",
    )
    ledger = DropLedger()
    assert list(iter_turns(conv, ExportOptions(include_tool_output=True), ledger)) == []
    assert ledger.dropped[DROP_SCAFFOLD] == 1


def test_tool_output_is_excluded_by_default_and_promotable():
    conv = conversation(
        [
            node("root", children=["t1"]),
            node(
                "t1",
                parent="root",
                role="tool",
                name="web.run",
                content={"content_type": "text", "parts": ["search results"]},
            ),
        ],
        current_node="t1",
    )
    ledger = DropLedger()
    assert list(iter_turns(conv, ExportOptions(), ledger)) == []
    assert ledger.dropped[DROP_TOOL_OUTPUT] == 1
    promoted = list(iter_turns(conv, ExportOptions(include_tool_output=True)))
    assert promoted[0]["role"] == "assistant"
    assert promoted[0]["_metadata"]["original_role"] == "tool"


def test_hidden_messages_are_dropped():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node("a1", parent="root", role="assistant", metadata={"is_visually_hidden_from_conversation": True}),
        ],
        current_node="a1",
    )
    ledger = DropLedger()
    assert list(iter_turns(conv, ExportOptions(), ledger)) == []
    assert ledger.dropped[DROP_HIDDEN] == 1


def test_owner_and_assistant_roles_map_for_the_provenance_gate():
    """``roles.py`` reads ``sender_type``; ``ChatGPTParser`` maps role 'user' →
    'human'. Emitting anything else here silently demotes the owner's words."""
    turns = list(iter_turns(linear_conversation(), ExportOptions()))
    assert [t["role"] for t in turns] == ["user", "assistant"]


# --------------------------------------------------------------------------
# Declared facets
# --------------------------------------------------------------------------


def test_conversation_facets_ride_on_every_turn():
    conv = linear_conversation(default_model_slug="o4-mini-high", gizmo_id="g-1", memory_scope="global_enabled")
    turns = list(iter_turns(conv, ExportOptions()))
    for turn in turns:
        meta = turn["_metadata"]
        assert meta["conversation_title"] == "Sample chat"
        assert meta["model_slug"] == "o4-mini-high"
        assert meta["gizmo_id"] == "g-1"
        assert meta["memory_scope"] == "global_enabled"
    assert [t["_metadata"]["turn_index"] for t in turns] == [0, 1]


def test_message_facets_capture_citations_queries_and_attachments():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node(
                "a1",
                parent="root",
                role="assistant",
                metadata={
                    "citations": [{"metadata": {"url": "https://example.com/a"}}],
                    "search_result_groups": [{"entries": [{"url": "https://example.com/b"}]}],
                    "search_queries": [{"q": "gcp iam roles"}],
                    "attachments": [{"name": "diagram.png", "mime_type": "image/png", "size": 10}],
                    "dictation": {"duration": 3},
                    "canvas": {"textdoc_id": "td-1", "title": "Draft", "version": 2},
                },
            ),
        ],
        current_node="a1",
    )
    meta = list(iter_turns(conv, ExportOptions()))[0]["_metadata"]
    assert meta["citation_urls"] == ["https://example.com/a", "https://example.com/b"]
    assert meta["search_queries"] == ["gcp iam roles"]
    assert meta["attachments"][0]["name"] == "diagram.png"
    assert meta["is_dictated"] is True
    assert meta["canvas"]["textdoc_id"] == "td-1"


def test_duplicate_citation_urls_are_collapsed_in_order():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node(
                "a1",
                parent="root",
                role="assistant",
                metadata={
                    "citations": [
                        {"metadata": {"url": "https://example.com/a"}},
                        {"metadata": {"url": "https://example.com/a"}},
                        {"metadata": {"url": "https://example.com/c"}},
                    ]
                },
            ),
        ],
        current_node="a1",
    )
    meta = list(iter_turns(conv, ExportOptions()))[0]["_metadata"]
    assert meta["citation_urls"] == ["https://example.com/a", "https://example.com/c"]


def test_message_timestamp_falls_back_to_the_conversation_stamp():
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", role="user", create_time=None),
        ],
        current_node="u1",
    )
    assert list(iter_turns(conv, ExportOptions()))[0]["created_at"] == JULY_2025


# --------------------------------------------------------------------------
# Declared facets carried forward from nodes that are not turns
# --------------------------------------------------------------------------


def test_a_dropped_tool_call_hands_its_declared_facets_to_the_answer():
    """A web search is three nodes: ask, run, answer. The query and half the
    citations are declared on the middle node, which is not a turn — dropping it
    dropped the declaration, and search queries measured zero on a corpus with 91."""
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["t1"], role="user"),
            node(
                "t1",
                parent="u1",
                children=["a1"],
                role="tool",
                name="web.run",
                content={"content_type": "text", "parts": ["results"]},
                metadata={
                    "search_queries": [{"q": "gcp iam roles"}],
                    "citations": [{"metadata": {"url": "https://cloud.google.com/iam"}}],
                },
            ),
            node("a1", parent="t1", role="assistant",
                 content={"content_type": "text", "parts": ["Use instanceAdmin."]},
                 metadata={"citations": [{"metadata": {"url": "https://stackoverflow.com/q/1"}}]}),
        ],
        current_node="a1",
    )
    turns = list(iter_turns(conv, ExportOptions()))
    answer = turns[-1]["_metadata"]
    assert answer["search_queries"] == ["gcp iam roles"]
    # The tool's citation and the answer's own citation both survive, in order.
    assert answer["citation_urls"] == ["https://cloud.google.com/iam", "https://stackoverflow.com/q/1"]


def test_carried_facets_reach_only_the_next_turn():
    conv = conversation(
        [
            node("root", children=["t1"]),
            node("t1", parent="root", children=["a1"], role="tool",
                 content={"content_type": "text", "parts": ["r"]},
                 metadata={"search_queries": [{"q": "first"}]}),
            node("a1", parent="t1", children=["a2"], role="assistant",
                 content={"content_type": "text", "parts": ["one"]}),
            node("a2", parent="a1", role="assistant",
                 content={"content_type": "text", "parts": ["two"]}),
        ],
        current_node="a2",
    )
    turns = list(iter_turns(conv, ExportOptions()))
    assert turns[0]["_metadata"]["search_queries"] == ["first"]
    assert "search_queries" not in turns[1]["_metadata"]


def test_an_empty_message_still_hands_on_what_it_declared():
    conv = conversation(
        [
            node("root", children=["a1"]),
            node("a1", parent="root", children=["a2"], role="assistant",
                 content={"content_type": "text", "parts": [""]},
                 metadata={"citations": [{"metadata": {"url": "https://a.example/x"}}]}),
            node("a2", parent="a1", role="assistant", content={"content_type": "text", "parts": ["answer"]}),
        ],
        current_node="a2",
    )
    turns = list(iter_turns(conv, ExportOptions()))
    assert turns[0]["_metadata"]["citation_urls"] == ["https://a.example/x"]


def test_a_turns_own_scalar_facet_beats_a_carried_one():
    conv = conversation(
        [
            node("root", children=["t1"]),
            node("t1", parent="root", children=["a1"], role="tool",
                 content={"content_type": "text", "parts": ["r"]},
                 metadata={"model_slug": "tool-model"}),
            node("a1", parent="t1", role="assistant", content={"content_type": "text", "parts": ["hi"]},
                 metadata={"model_slug": "answer-model"}),
        ],
        current_node="a1",
    )
    assert list(iter_turns(conv, ExportOptions()))[0]["_metadata"]["model_slug"] == "answer-model"


# --------------------------------------------------------------------------
# Date window
# --------------------------------------------------------------------------


def test_window_is_conversation_level_on_last_activity():
    """A thread started in March but answered in July belongs to July, whole."""
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1"], role="user", create_time=MARCH_2025),
            node("a1", parent="u1", role="assistant", create_time=JULY_2025),
        ],
        current_node="a1",
        create_time=MARCH_2025,
        update_time=JULY_2025,
    )
    created, last_active = conversation_activity(conv)
    assert created == MARCH_2025 and last_active == JULY_2025
    options = ExportOptions(date_from=JULY_2025 - 86400)
    assert conversation_in_window(conv, options) is True
    assert len(list(iter_turns(conv, options))) == 2  # not torn in half


def test_conversations_outside_the_window_are_dropped_and_counted():
    ledger = DropLedger()
    old = linear_conversation(create_time=MARCH_2025, update_time=MARCH_2025)
    turns = list(iter_export([old], ExportOptions(date_from=JULY_2025), ledger))
    assert turns == []
    assert ledger.dropped[DROP_OUT_OF_WINDOW] == 1
    assert ledger.conversations_seen == 1 and ledger.conversations_kept == 0
    # A skipped conversation is not 77 dropped "nodes" — the units are separate.
    stats = ledger.as_dict()
    assert stats["dropped_conversations_total"] == 1
    assert stats["dropped_nodes_total"] == 0


def test_undated_conversations_are_kept_rather_than_silently_lost():
    conv = conversation(
        [node("root", children=["u1"]), node("u1", parent="root", role="user")],
        current_node="u1",
        create_time=None,
        update_time=None,
    )
    conv["mapping"]["u1"]["message"]["create_time"] = None
    assert conversation_in_window(conv, ExportOptions(date_from=JULY_2025)) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        (JULY_2025, JULY_2025),
        (JULY_2025 * 1000, JULY_2025),
        ("2025-07-23", 1753228800.0),
        ("2025-07-23T00:00:00Z", 1753228800.0),
        (None, None),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_date_bounds_accept_seconds_millis_and_iso(raw, expected):
    options = ExportOptions.from_payload({"date_from": raw})
    assert options.date_from == expected


# --------------------------------------------------------------------------
# Export level
# --------------------------------------------------------------------------


def test_do_not_remember_conversations_are_skipped_by_default():
    ledger = DropLedger()
    conv = linear_conversation(is_do_not_remember=True)
    assert list(iter_export([conv], ExportOptions(), ledger)) == []
    assert ledger.dropped[DROP_DO_NOT_REMEMBER] == 1
    assert len(list(iter_export([conv], ExportOptions(respect_do_not_remember=False)))) == 2


def test_ledger_accounts_for_every_message_node():
    """message_nodes == turns_emitted + dropped_nodes_total, per conversation."""
    conv = conversation(
        [
            node("root", children=["u1"]),
            node("u1", parent="root", children=["a1", "a2"], role="user"),
            node("a1", parent="u1", role="assistant", content={"content_type": "thoughts", "thoughts": []}),
            node("a2", parent="u1", role="assistant"),
            node("s1", parent="root", role="system"),
        ],
        current_node="a2",
    )
    ledger = DropLedger()
    list(iter_export([conv], ExportOptions(), ledger))
    stats = ledger.as_dict()
    assert stats["message_nodes"] == stats["turns_emitted"] + stats["dropped_nodes_total"]


def test_a_broken_conversation_does_not_end_the_import():
    ledger = DropLedger()
    broken = conversation([node("root", children=["u1"])], current_node="root")
    broken["mapping"]["root"]["children"] = None
    good = linear_conversation()
    turns = list(iter_export([broken, good, {"not": "a conversation"}], ExportOptions(), ledger))
    assert len(turns) == 2
    assert ledger.conversations_seen == 2


def test_is_conversation_needs_a_mapping_and_an_id():
    assert is_conversation(linear_conversation()) is True
    assert is_conversation({"mapping": {}}) is False
    assert is_conversation({"id": "x"}) is False
    assert is_conversation([]) is False
