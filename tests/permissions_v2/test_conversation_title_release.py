"""A knowledge share releases an AI chat's title beside its messages only when the owner opted in.

`release_conversation_title` is declared or absent on the knowledge declaration, like `writer_disclosure`: absent
is off and keeps the bytes (and so the hash) of every policy signed before the field, an explicit null or false is
refused so off has one encoding, and on is the literal true. Under it a message from `ai_chat_messages` carries its
chat's title, scrubbed of addresses and numbers the same way an answer is and capped; a message from any other table,
or from a chat with no title, carries no key at all.

A title is preferred, never required (owner's ruling of 15 Oct 2026). `title` is the first record's and every review
pins it; a later title goes to `latest_title` (migration 83), which no review and no context revision pins. So a chat
named or renamed after its messages were reviewed keeps releasing them from the index already built, with the new
name, and the Off-limits veto, which reads every column, still withholds a message whose chat title names an entry.
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


def _served_after_review(store, *, first: str | None, later: str | None, opted_in: bool = True, off_limits=None,
                         caplog=None):
    """Review and build with the chat titled `first` (and `off_limits` already an entry), then let a later batch write
    `later` and ask the SAME index."""
    store.db.execute("UPDATE ai_chat_conversations SET title=?, latest_title=?", (first, first))
    if off_limits:
        store.db.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,"
                         "rebuild_state) VALUES('bh-title','',?,?,'complete')", (off_limits, off_limits.lower()))
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
    [before] = node.search_request("tide pools talk", k=10)[0]["records"]
    store.db.execute("UPDATE ai_chat_conversations SET latest_title=?", (later,))
    store.db.commit()
    if caplog is not None:
        caplog.clear()
    output, refused = node.search_request("tide pools talk", k=10)
    _served_after_review.node = node
    return before, (output or {}).get("records", []), refused


def test_a_title_that_arrives_after_the_review_withholds_nothing(ai_chat_only, caplog):
    """Before migration 83 the later title moved the parent revision the review pinned: 1 record, then 0, then the
    whole share refused once the next review pass staled the index (measured on this fixture, 15 Oct 2026)."""
    before, after, refused = _served_after_review(ai_chat_only, first=None, later=TITLE, caplog=caplog)
    assert "conversation_title" not in before
    assert refused is None and [(r["content"], r["conversation_title"]) for r in after] == [(LINE, TITLE)]
    assert not [r for r in caplog.records if "stale" in r.getMessage()]


def test_a_rename_after_the_review_releases_the_new_name(ai_chat_only):
    before, after, refused = _served_after_review(ai_chat_only, first=TITLE, later="Tide pool field notes")
    assert before["conversation_title"] == TITLE
    assert refused is None and [r["conversation_title"] for r in after] == ["Tide pool field notes"]


def test_a_later_title_is_released_only_to_a_share_that_opted_in(ai_chat_only):
    _, after, refused = _served_after_review(ai_chat_only, first=None, later=TITLE, opted_in=False)
    assert refused is None and [r["content"] for r in after] == [LINE] and "conversation_title" not in after[0]


def test_an_off_limits_entry_the_title_does_not_name_leaves_the_message_and_its_title(ai_chat_only):
    """The control for the next test: the same entry, a later title that does not name it."""
    _, after, refused = _served_after_review(ai_chat_only, first=None, later=TITLE, off_limits="Odrin Vale")
    assert refused is None and [(r["content"], r["conversation_title"]) for r in after] == [(LINE, TITLE)]


def test_a_later_title_naming_an_off_limits_entry_withholds_the_message(ai_chat_only):
    """The veto reads every column of the chat row, latest_title included: neither the name nor the message leaves,
    as for any chat whose row names an entry. Only the title differs from the control above."""
    _, after, refused = _served_after_review(ai_chat_only, first=None, later="Notes with Odrin Vale",
                                             off_limits="Odrin Vale")
    # Today the index that holds the message is refused whole (`refused`) until the refresh rebuilds it: fail closed.
    assert after == []
    node = _served_after_review.node
    with owner(actor=OWNER):
        node.index.rebuild("grant-search", now=NOW)
    output, refused = node.search_request("tide pools talk", k=10)
    assert refused is None and output["records"] == []                  # rebuilt: served, the message left out

