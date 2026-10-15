"""A knowledge share releases an AI chat's title beside its messages only when the owner opted in.

`release_conversation_title` is declared or absent on the knowledge declaration, like `writer_disclosure`: absent
is off and keeps the bytes (and so the hash) of every policy signed before the field, an explicit null or false is
refused so off has one encoding, and on is the literal true. Under it a message from `ai_chat_messages` carries its
chat's title, scrubbed of addresses and numbers the same way an answer is and capped; a message from any other table,
or from a chat with no title, carries no key at all. The title is a column of the parent row whose revision the
owner's review already pins, so a renamed chat is a changed record, not a silently relabelled one.
"""
from __future__ import annotations

import pytest

from tests.ingestion.test_ai_chat_writer_class import captured_jobs  # noqa: F401 (fixture)
from tests.permissions_v2.test_fresh_node_message_tables import (  # noqa: F401 (fixture)
    LINE, NOW, OWNER, _bound, _own_pass, _share_policy, ai_chat_only)
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.knowledge_contract import KnowledgeDeclaration, MessageResult
from topos.permissions_v2.registry import parse_policy
from tests.permissions_v2.test_ingest_provenance import owner

from types import SimpleNamespace

TITLE = "Tide pool talk"


def test_absent_is_off_and_keeps_every_signed_policys_hash():
    raw = knowledge_policy()
    assert "release_conversation_title" not in parse_policy(raw).model_dump()["search"]
    assert digest(parse_policy(raw).model_dump()) == digest(parse_policy(knowledge_policy()).model_dump())
    raw["search"]["release_conversation_title"] = True
    assert parse_policy(raw).model_dump()["search"]["release_conversation_title"] is True


@pytest.mark.parametrize("value", [None, False, "yes", 1])
def test_off_has_one_encoding(value):
    raw = knowledge_policy()["search"]
    with pytest.raises(ValueError):
        KnowledgeDeclaration.model_validate({**raw, "release_conversation_title": value})


def test_a_message_without_a_title_dumps_no_key():
    base = dict(kind="message", record_id="r." + "a" * 64, content="x", source_ids=["s"],
                citations=[dict(record_id="r." + "a" * 64, source_id="s", content="x")], event_at=None)
    assert "conversation_title" not in MessageResult.model_validate(base).model_dump()
    assert MessageResult.model_validate({**base, "conversation_title": TITLE}).model_dump()["conversation_title"] == TITLE
    for bad in ("", "x" * 201):
        with pytest.raises(ValueError):
            MessageResult.model_validate({**base, "conversation_title": bad})


def _released(store, *, title: str | None, opted_in: bool) -> list[dict]:
    if title is not None:
        store.db.execute("UPDATE ai_chat_conversations SET title=?", (title,))
        store.db.commit()
    resolver, reviews = _bound(store)
    _own_pass(resolver, reviews)
    raw = _share_policy()
    if opted_in:
        raw["search"]["release_conversation_title"] = True
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), store.root / "share",
                model=None, search_raw=raw, now=NOW)
    with owner(actor=OWNER):
        node.index.rebuild("grant-search", now=NOW)
    output, refused = node.search_request("tide pools talk", k=10)
    assert refused is None, refused
    return output["records"]


def test_off_releases_the_message_without_its_chats_title(ai_chat_only):
    [record] = _released(ai_chat_only, title=TITLE, opted_in=False)
    assert record["content"] == LINE
    assert "conversation_title" not in record


def test_on_releases_the_chats_title_beside_the_message(ai_chat_only):
    [record] = _released(ai_chat_only, title=TITLE, opted_in=True)
    assert (record["content"], record["conversation_title"]) == (LINE, TITLE)


def test_on_scrubs_the_title_like_an_answer(ai_chat_only):
    [record] = _released(ai_chat_only, title="Notes for sam@example.com", opted_in=True)
    assert record["conversation_title"] == "Notes for [REDACTED_EMAIL]"


def test_on_but_the_chat_has_no_title_carries_no_key(ai_chat_only):
    [record] = _released(ai_chat_only, title=None, opted_in=True)
    assert "conversation_title" not in record


def test_the_ledger_refuses_a_title_the_policy_never_released(ai_chat_only, monkeypatch):
    """Defence in depth: were the release to attach a title without the opt-in, the shape check refuses the read."""
    from topos.permissions_v2 import search_release
    monkeypatch.setattr(search_release, "_conversation_title", lambda conn, identity, row: TITLE)
    monkeypatch.setattr(search_release, "_releases_titles", lambda policy: True)
    store = ai_chat_only
    store.db.execute("UPDATE ai_chat_conversations SET title=?", (TITLE,))
    store.db.commit()
    resolver, reviews = _bound(store)
    _own_pass(resolver, reviews)
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), store.root / "share",
                model=None, search_raw=_share_policy(), now=NOW)
    with owner(actor=OWNER):
        node.index.rebuild("grant-search", now=NOW)
    output, refused = node.search_request("tide pools talk", k=10)
    assert output is None or not output.get("records")
    assert refused is not None


def test_the_node_says_it_can_release_titles(monkeypatch):
    from topos.engine import registration
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    assert registration.build_engine_capabilities()["permissions_v2_conversation_titles"] == 1
