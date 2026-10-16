"""FT168 item 2: a share that gives records and facts, asked about a statement it holds both ways.

The share form now always signs records with facts. A work fact is made from a message of the owner's that says the
same thing in the first person, so such a share holds the statement twice: the message ("I work on Project Lantern.")
and the fact ("Owner works on Project Lantern."). What the recipient's search returns, what the answer writer is given,
what a written answer's body lists, and how many different statements fit in the items an answer is written from.

Invented data only.

Expected failures here are findings (FT168 report), each with its reason in one line.
"""
from __future__ import annotations

import time

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.test_ft168_exposure import Writer
from tests.permissions_v2.test_ft168_switch_loops import ANSWERS, project_names, written_answer
from topos.permissions_v2.answer_generation import k_answer

FORMS = {
    # The message is exactly as long as the fact made from it: the rank ties, and the pair sits side by side.
    "a_message_as_short_as_its_fact": "I work on {name}.",
    # The message is longer, as most are: every fact outranks every message.
    "a_message_longer_than_its_fact": "At work, I am working on {name}",
}


def paired(root, monkeypatch, *, count: int, form: str):
    """`count` projects, each stated in one message and held as one work fact; a records share and an answers share
    (with sources), both giving messages and facts."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = project_names(count)
    node = ft.build(root, [ft.Message(FORMS[form].format(name=name), age=600 + 60 * number)
                           for number, name in enumerate(names)], now=now, policy=ft.policy_at(now))
    ft.activate(node, ft.policy_at(now, grant=ANSWERS, answers="with_sources"))
    for number, name in enumerate(names, start=1):
        ft.state_fact(node, f"imessage:{number}", "works_on", name)
    ft.seed_registry(node)
    assert set(node.rebuild().values()) == {"ready"}
    node.names = names
    return node


def statements_among(records) -> tuple[int, int]:
    """(items, different statements) of released records: a fact and the message it cites are one statement. A fact's
    citation carries its source's record id, which is the message's own id in the same share."""
    messages = {record["record_id"] for record in records if record["kind"] == "message"}
    statements = set(messages)
    for record in records:
        if record["kind"] == "fact":
            statements.add(record["citations"][0]["record_id"])
    return len(records), len(statements)


@pytest.fixture(params=sorted(FORMS))
def one_statement(request, tmp_path, monkeypatch):
    node = paired(tmp_path / "ft168", monkeypatch, count=3, form=request.param)
    yield node
    ft.close(node)


def test_search_returns_the_statement_as_a_message_and_as_a_fact_joined_by_the_record_id(one_statement):
    """What "shows both kinds sensibly" rests on: the fact's citation names the message's own record id, so whoever
    shows them can tell they are one statement. The fact's words are the statement, the message's are the owner's."""
    node = one_statement
    name = node.names[0]
    facts, others, refused = ft.ask(node, name.split()[1])
    assert refused is None and len(facts) == 1 and len(others) == 1
    [fact], [message] = facts, others
    assert fact["content"] == f"Owner works on {name}." and name in message["content"]
    assert message["kind"] == "message" and fact["kind"] == "fact"
    assert [citation["record_id"] for citation in fact["citations"]] == [message["record_id"]]
    assert fact["citations"][0]["content"] == fact["content"] != message["content"]
    assert fact["record_id"] != message["record_id"]
    assert statements_among(facts + others) == (2, 1)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="FT168-D5: a statement held as a message and as the fact made from it is two "
                   "items for the answer writer (and two sources in the body when both are cited); nothing joins or "
                   "drops the double")
def test_the_answer_writer_is_given_a_statement_once(one_statement):
    node = one_statement
    writer = Writer(node, "The owner is busy with that effort [1].")
    try:
        written_answer(node, writer.service, node.names[0].split()[1])
    finally:
        writer.close()
    if len(writer.given) != 1:
        raise RuntimeError("the writer was not asked exactly once")
    items = [line for line in writer.given[0].splitlines() if line.startswith("[")]
    assert len(items) == 1, items


def test_an_answer_that_cites_both_lists_two_sources_for_the_one_statement(one_statement):
    """As it is today (D5): the body's records are the message and the fact, the receipt counts two items used and two
    cited. The join is there for whoever renders it: the fact's citation is the message's record id."""
    node = one_statement
    word = node.names[0].split()[1]
    writer = Writer(node, f"The owner is busy with the {word} effort [1, 2].")
    try:
        _answer_id, body = written_answer(node, writer.service, word)
    finally:
        writer.close()
    receipt = ft.answer_receipts(node)[-1]
    assert body["outcome"] == "answered", (receipt["reason"], receipt["sentences"])
    assert sorted(record["kind"] for record in body["records"]) == ["fact", "message"]
    assert statements_among(body["records"]) == (2, 1)
    assert (receipt["records_used"], receipt["records_cited"]) == (2, 2)
    # Inside the prompt the fact's own line and its "Supporting text" are the same sentence: the citation of a fact
    # is its statement, so the support line adds nothing the writer has not just read.
    [given] = writer.given
    statement = f"Owner works on {node.names[0]}."
    assert given.count(statement) >= 2 and f"Supporting text: {statement}" in given


@pytest.mark.parametrize("form", sorted(FORMS))
@pytest.mark.parametrize("statements", [8, 5])
def test_how_many_statements_the_items_of_an_answer_hold(tmp_path, monkeypatch, form, statements, capsys):
    """The writer gets `k_answer` items (8) and search returns k = 8 records. Each project is a message and a fact,
    and the question matches all of them. The items are set to 8 here (`ANSWER_ITEMS`, 1.5.9's number; 30 by default
    since 1.5.10), so the writer's eight items and search's eight records are measured side by side as FT168 did.

    Eight projects: the eight items are eight different statements, whichever of the two leads the rank (the facts
    when the message is the longer text, the messages when it is not): the doubles rank behind and displace nothing.
    Five projects: the eight items are the five statements and three of them a second time. A recipient or a
    scorecard that counts "sources" counts eight; joined by the fact's citation there are five."""
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ANSWER_ITEMS", "8")
    node = paired(tmp_path / "ft168", monkeypatch, count=statements, form=form)
    try:
        assert k_answer() == 8
        facts, others, refused = ft.ask(node, "project", k=8)
        assert refused is None
        searched = statements_among(facts + others)
        writer = Writer(node, "The owner is busy with several efforts [1].")
        try:
            written_answer(node, writer.service, "project")
        finally:
            writer.close()
        [given] = writer.given
        items = [line for line in given.splitlines() if line.startswith("[")]
        kinds = sorted(line.split()[1] for line in items)
        receipt = ft.answer_receipts(node)[-1]
        with capsys.disabled():
            print(f"\nFT168 item 2, {statements} projects, {form}: search k=8 returned {searched[0]} records holding "
                  f"{searched[1]} statements ({len(facts)} facts, {len(others)} messages); the answer writer was "
                  f"given {len(items)} items ({kinds.count('fact')} facts, {kinds.count('message')} messages); "
                  f"receipt records_used {receipt['records_used']}")
        assert len(items) == 8 and receipt["records_used"] == 8
        assert searched == (8, statements)
        assert sorted((kinds.count("fact"), kinds.count("message"))) == sorted((statements, 8 - statements))
    finally:
        ft.close(node)
