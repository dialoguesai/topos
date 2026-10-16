"""Fix A (`ANSWER_PROTECTED_ITEM_FILTER`, off): a released record whose own shown text carries an Off-limits term is
withheld from the writer's prompt and from the with-sources body.

protects: the 1.5.10 candidate refused a whole real-app answer as `answer_protected` that 1.5.9 answered in five
sentences. `close_answer` refuses the whole answer when the kept sentences, joined, carry an Off-limits term
(`mentions_protected`); the share's release vetoes each record on its stored row. Step 0 of the lane read both: they
are the same matcher, but the prompt shows a record's text re-flowed. `_clip` and the supporting line join every
line into one, so a form of a protected name that the matcher reads only as a proper noun, which the row holds at the
start of a line (no proper noun there), stands mid-sentence in the prompt; a writer that repeats it word for word
trips the body check, and the whole answer is refused. A fact's and a relationship's sentence is written around its
value after the veto, with the same effect on the value's first word. These tests pin:
  - the gap itself, on the real boundary and on a real node: the release walk releases the record, the prompt shows
    the form mid-sentence, and today the writer repeating it refuses the whole answer;
  - with the switch on: that record leaves the prompt and the body and the answer is written from the rest; nothing
    is touched when nothing is protected; all records protected is the zero-records path (`nothing_matched`, no new
    reason); the re-checks still walk every released record; the receipt counts the withheld ones;
  - the filter reads exactly the texts the prompt shows (body, kind, date, supporting line), from the one place
    `build_prompt` writes them, and `build_prompt` is byte for byte the prompt of before;
  - off: the service is byte for byte the service of before (its signed results, bodies and ledger rows);
  - what Fix A cannot do: a protected term the writer writes that no item shows is still refused by the body check.

Invented people and texts only ("Harlan Ostrow" is the lane's synthetic Off-limits entry). No model: the writer is
a stand-in that repeats each item it is shown, and the support judge is the folder's stand-in ("yes").
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_bl156_receipt_reasons import _Deferred
from tests.permissions_v2.test_entity_boundary_v8 import gate, withheld
from topos.permissions_v2 import answer_generation, answer_release
from topos.permissions_v2.answer_generation import (ShownItem, build_prompt, post_check_answer, shown_item,
                                                    withhold_protected_items)
from topos.permissions_v2.answer_protocol import ASK, FETCH
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.knowledge_contract import (FactResult, GoalResult, InterestResult, JournalEntryResult,
                                                     MessageResult, RelationshipResult)

SWITCH = "TOPOS_PERMISSIONS_V2_ANSWER_PROTECTED_ITEM_FILTER"
#: The commit this lane starts from: the answer service and the prompt of before Fix A.
BASE = "9ddc77b5c63b06215273f7e225c7b3a0e7027a2d"
PROTECTED = "Harlan Ostrow"
#: A form of the protected surname the matcher reads only as a proper noun, at the start of a line: released.
GAP = "Saturday plans\nOstrows are visiting for lunch"
LUNCH = "Booked the lunch table for Saturday with the team"
MENU = "Finished the lunch menu plans for the offsite"
BUDGET = "Reviewed the budget for the roadmap with finance"
SUMMARY = "Drafted the roadmap budget summary for the board"
DAY = 1_790_000_000 // 86_400 * 86_400


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


# --- records, as the release hands them over --------------------------------------------------------------------------

def _rid(digit: str) -> str:
    return "r." + digit * 64


def _citation(digit, text, source="ownerimport"):
    return {"record_id": _rid(digit), "source_id": source, "content": text}


def message(digit, text, event_at=None):
    return MessageResult.parse({"kind": "message", "record_id": _rid(digit), "content": text,
                                "source_ids": ["ownerimport"], "citations": [_citation(digit, text)],
                                "event_at": event_at})


def journal(digit, text, event_at=None):
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": _rid(digit), "content": text,
                                     "source_ids": ["journal"], "citations": [_citation(digit, text, "journal")],
                                     "event_at": event_at})


def goal(digit, text, source_text, event_at=None):
    """A goal: its text, cited by the message it came from (another row: the supporting line shows that one)."""
    return GoalResult.parse({"kind": "goal", "record_id": _rid(digit), "content": text, "status": "stated_intention",
                             "source_ids": ["ownerimport"], "citations": [_citation("e", source_text)],
                             "event_at": event_at})


def fact(digit, value, event_at=None):
    """An owner-stated fact as `knowledge_projections.fact_projection` writes it: its citation is its own sentence."""
    text = f"Owner works at {value}."
    return FactResult.parse({"kind": "fact", "record_id": _rid(digit), "content": text, "assertion": "owner_stated",
                             "source_ids": ["ownerimport"], "citations": [_citation(digit, text)], "event_at": event_at})


def relationship(digit, target, source_text, event_at=None):
    return RelationshipResult.parse({"kind": "relationship", "record_id": _rid(digit),
                                     "content": f"Owner intends to {target}", "subject": "Owner",
                                     "relation": "pursues", "object": target, "source_ids": ["ownerimport"],
                                     "citations": [_citation("f", source_text)], "event_at": event_at})


def interest(digit, label):
    return InterestResult.parse({"kind": "interest", "record_id": _rid(digit), "content": label, "label": label,
                                 "month": "2026-09", "strength": "medium", "source_ids": ["activity"],
                                 "citations": [{"record_id": _rid(digit), "source_id": "activity",
                                                "content": f"{label}, 2026-09"}]})


def every_kind(event_at=None):
    return [message("1", "Booked  the lunch\ntable.", event_at), journal("2", "Long walk\n\nthen soup.", event_at),
            goal("3", "Plan the Saturday lunch", "My goal is to plan\n the Saturday lunch", event_at),
            fact("4", "Delta Bakery", event_at),
            relationship("5", "plan the Saturday lunch", "I intend to plan the Saturday lunch. " + "Long. " * 40,
                         event_at),
            interest("6", "Sourdough baking")]


class Recording:
    """A boundary that protects nothing and records every question it is asked."""

    def __init__(self, flagged=()):
        self.asked = []
        self.flagged = tuple(flagged)

    def mentions_protected(self, *texts):
        self.asked.append(texts)
        return any(mark in text for text in texts for mark in self.flagged)


# --- step 0, on the real boundary: the gap ----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["message", "journal_entry"])
def test_the_release_passes_a_line_start_form_that_the_prompt_shows_mid_sentence(kind):
    boundary = gate(PROTECTED)
    assert not withheld(kind, boundary, GAP)                        # the row's veto releases it
    assert not boundary.mentions_protected(GAP)                     # the stored text, read alone, is clean
    shown = shown_item(message("1", GAP), "none").body
    assert shown == "Saturday plans Ostrows are visiting for lunch"
    assert boundary.mentions_protected(shown)                       # as the prompt shows it, it is not
    # The same form mid-sentence in a row: the veto withholds it (the release never needs Fix A for it).
    assert withheld(kind, boundary, "The Ostrows are visiting for lunch")


def test_a_fact_or_relationship_sentence_moves_its_values_first_word_mid_sentence():
    """The fact row's veto reads the value as its own text, whose first word opens it; the record's sentence is
    written around it after the veto. (A fact also needs a released source stating it, which today keeps most such
    values out; the prompt text is still not the text the veto read.)"""
    boundary = gate(PROTECTED)
    assert not withheld("fact", boundary, "Ostrowa bakery")
    assert boundary.mentions_protected(fact("4", "Ostrowa bakery").content)
    assert not withheld("relationship", boundary, "Ostrowa visit planning")
    assert boundary.mentions_protected(relationship("5", "Ostrowa visit planning", "x").content)


def test_today_the_writer_repeating_such_an_item_refuses_the_whole_answer():
    boundary = gate(PROTECTED)
    records = [message("1", GAP), message("2", LUNCH), message("3", MENU)]
    prompt = build_prompt("What are the lunch plans?", records, precision="none")
    reply = "\n".join(f"The owner wrote that {shown_item(record, 'none').body} [{number}]."
                      for number, record in enumerate(records, 1))
    checked = post_check_answer(reply, records, prompt, mode="with_sources", boundary=boundary)
    assert (checked.body.outcome, checked.reason, checked.generated, checked.kept) == (
        "no_answer", "answer_protected", 3, 0)
    kept, withheld_count = withhold_protected_items(records, boundary=boundary, precision="none")
    assert (kept, withheld_count) == (records[1:], 1)
    prompt = build_prompt("What are the lunch plans?", kept, precision="none")
    reply = "\n".join(f"The owner wrote that {shown_item(record, 'none').body} [{number}]."
                      for number, record in enumerate(kept, 1))
    checked = post_check_answer(reply, kept, prompt, mode="with_sources", boundary=boundary)
    assert (checked.body.outcome, checked.kept, checked.cited) == ("answered", 2, 2)
    assert [record["record_id"] for record in checked.body.model_dump()["records"]] == [_rid("2"), _rid("3")]


def test_the_supporting_line_is_read_where_only_it_carries_the_term():
    """A goal's supporting line is its source message's text (another row), joined into one line as the body is."""
    boundary = gate(PROTECTED)
    item = goal("3", "Host the Saturday lunch", GAP)
    shown = shown_item(item, "none")
    assert not boundary.mentions_protected(shown.body) and boundary.mentions_protected(shown.support)
    assert withhold_protected_items([item, message("2", LUNCH)], boundary=boundary, precision="none") == (
        [message("2", LUNCH)], 1)


def test_the_body_is_read_where_only_it_carries_the_term():
    boundary = gate(PROTECTED)
    item = journal("2", GAP)
    assert shown_item(item, "none").support is None
    assert withhold_protected_items([item], boundary=boundary, precision="none") == ([], 1)


def test_nothing_is_withheld_when_nothing_is_protected():
    boundary = gate(PROTECTED)
    records = every_kind(DAY)
    assert withhold_protected_items(records, boundary=boundary, precision="day") == (records, 0)


# --- what the filter reads: exactly what the prompt shows ------------------------------------------------------------

@pytest.mark.parametrize("precision", ["none", "day", "second"])
def test_the_filter_asks_the_boundary_about_exactly_the_texts_the_prompt_shows(precision):
    records = every_kind(DAY + 3_723)
    prompt = build_prompt("What are the lunch plans?", records, precision=precision)
    boundary = Recording()
    assert withhold_protected_items(records, boundary=boundary, precision=precision) == (records, 0)
    assert boundary.asked == [shown_item(record, precision).texts() for record in records]
    for number, (record, block) in enumerate(zip(records, prompt.item_lines), 1):
        shown = shown_item(record, precision)
        # The prompt's own block for the item, written back from the shown texts: nothing else of it is shown.
        assert block == (f"[{number}] {shown.kind}" + (f" · {shown.date}" if shown.date else "") + f"\n{shown.body}"
                         + (f"\n\nSupporting text: {shown.support}" if shown.support is not None else ""))
        assert shown.kind == record.kind
    by_kind = {record.kind: shown_item(record, precision) for record in records}
    date = {"none": "", "day": "2026-09-21", "second": "2026-09-21T01:02:03Z"}[precision]
    assert by_kind["goal"] == ShownItem("Plan the Saturday lunch", "goal", date,
                                        "My goal is to plan the Saturday lunch")
    assert by_kind["message"] == ShownItem("Booked the lunch table.", "message", date, None)
    assert len(by_kind["relationship"].support) == 160 and by_kind["fact"].support == "Owner works at Delta Bakery."
    assert by_kind["interest"].texts() == ("Sourdough baking", "interest")        # an interest carries no date


@pytest.mark.parametrize("flag", ["Delta", "fact", "2026-09-21", "My goal is to plan the Saturday"])
def test_each_shown_text_alone_withholds_its_record(flag):
    """The body, the kind, the date and the supporting line: a term in any one of them withholds the record."""
    records = every_kind(DAY)
    target = {"Delta": "fact", "fact": "fact", "2026-09-21": None, "My goal is to plan the Saturday": "goal"}[flag]
    kept, count = withhold_protected_items(records, boundary=Recording([flag]), precision="day")
    if target is None:                      # every dated record shows the date; the interest has none
        assert [record.kind for record in kept] == ["interest"] and count == 5
    else:
        assert [record.kind for record in records if record not in kept] == [target] and count == 1


def test_build_prompt_is_byte_for_byte_the_prompt_of_before():
    """`build_prompt` now writes each item from `shown_item`; for every kind and precision it is the commit before."""
    before = ft.module_at(BASE, "answer_generation")
    assert not hasattr(before, "shown_item")
    for precision in ("none", "day", "second"):
        for records in (every_kind(DAY + 3_723), every_kind(None), [message("1", GAP), goal("3", "x y", " \n ")]):
            now = dataclasses.asdict(build_prompt("What are the lunch plans?", records, precision=precision))
            then = dataclasses.asdict(before.build_prompt("What are the lunch plans?", records, precision=precision))
            assert now == then, precision


# --- the service, on a real node: the release walk, the boundary, the ledger ------------------------------------------

def protect(conn) -> None:
    """The fixture's Off-limits entry (as `direct_search_twins.protect` writes one), naming the lane's person."""
    from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
        ensure_conversation_participants_table, ensure_conversations_table)
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    for conversation, source in conn.execute(
            "SELECT DISTINCT conversation_id, source_id FROM conversation_messages").fetchall():
        conn.execute("INSERT OR IGNORE INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)",
                     (conversation, twins.DATASET, source))
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                 "VALUES('protected-entity','person',?,?)", (PROTECTED, PROTECTED.lower()))
    conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES('bh','protected-entity',?,?,'complete')", (PROTECTED, PROTECTED.lower()))


def node_with(root, monkeypatch, texts, *, now):
    monkeypatch.setattr(twins, "protect", protect)
    policy = ft.policy_at(now, answers="with_sources", kinds=("message",), ontologies=None)
    node = ft.build(root, [ft.Message(text, age=600 + 100 * number) for number, text in enumerate(texts)], now=now,
                    policy=policy, protected=True)
    assert node.rebuild() == {ft.GRANT: "ready"}
    return node, policy


def bodies(prompt) -> list[str]:
    return [block.split("\n", 1)[1].split("\n\nSupporting text: ")[0] for block in prompt.item_lines]


class Writer:
    """The stand-in writer: one cited sentence per item it is shown, repeating the item; or a fixed reply."""

    def __init__(self, reply=None):
        self.prompts, self.reply = [], reply

    async def __call__(self, prompt, *, deadline):
        self.prompts.append(prompt)
        if self.reply is not None:
            return self.reply
        return "\n".join(f"The owner wrote that {body} [{number}]." for number, body in enumerate(bodies(prompt), 1))


def service_for(node, writer, service_class=answer_release.AnswerService):
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = service_class(runtime, clock=lambda: node.now[0], generate=writer)
    service._enabled = lambda: True
    service._executor = _Deferred()
    return service


def ask(node, service, question, label=None):
    """One ask through the door, its job run to its end, and one fetch: (signed ask, pending, signed fetch, body)."""
    payload = {"question": question}
    signed = node._envelope(ft.GRANT, ASK, payload, label or node.next_id("answer"))
    with recipient():
        asked = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    service._executor.run()
    return signed.request_id, asked, fetch(node, service, asked[1]["answer_id"])


def fetch(node, service, answer_id, label=None):
    payload = {"answer_id": answer_id}
    signed = node._envelope(ft.GRANT, FETCH, payload, label or node.next_id("fetch"))
    with recipient():
        return service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)


def receipt(node, request_id):
    with node.ledger._transaction() as db:
        row = db.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id=?",
                         (request_id,)).fetchone()
    return json.loads(row[0]), json.loads(row[1])


def released_ids(node, question):
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, ft.GRANT, node.now[0])
    with recipient():
        _current, _policy, output, _decision = node.search.retrieve_for_answer(
            grant_id=ft.GRANT, question=question, admitted_authority=authority)
    return [(record.record_id, record.content) for record in output.records]


@pytest.fixture
def lunch_node(tmp_path, monkeypatch):
    node, _policy = node_with(tmp_path / "node", monkeypatch, [GAP, LUNCH, MENU, BUDGET, SUMMARY], now=int(time.time()))
    yield node
    ft.close(node)


def test_off_the_released_line_start_form_refuses_the_whole_answer(lunch_node):
    """Today's behaviour, reproduced: the walk releases the item, the writer repeats it, the answer is refused."""
    released = released_ids(lunch_node, "What are the lunch plans?")
    assert GAP in [content for _id, content in released] and len(released) == 3
    writer = Writer()
    service = service_for(lunch_node, writer)
    request_id, _asked, (_signed, body) = ask(lunch_node, service, "What are the lunch plans?")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert any("Saturday plans Ostrows are visiting" in text for text in bodies(writer.prompts[0]))
    got, _decision = receipt(lunch_node, request_id)
    assert (got["reason"], got["records_used"], got["sentences"]["generated"]) == ("answer_protected", 3, 3)
    assert "items_withheld_protected" not in got


def test_on_the_item_leaves_the_prompt_and_the_body_and_the_answer_is_written_from_the_rest(lunch_node, on):
    released = released_ids(lunch_node, "What are the lunch plans?")
    gap_id = next(record_id for record_id, content in released if content == GAP)
    writer = Writer()
    service = service_for(lunch_node, writer)
    request_id, _asked, (_signed, body) = ask(lunch_node, service, "What are the lunch plans?")
    shown = bodies(writer.prompts[0])
    assert sorted(shown) == sorted([LUNCH, MENU]) and not any("Ostrow" in text for text in shown)
    assert body["outcome"] == "answered" and "Ostrow" not in body["answer"]
    assert gap_id not in [record["record_id"] for record in body["records"]] and len(body["records"]) == 2
    assert all("Ostrow" not in json.dumps(record) for record in body["records"])
    got, _decision = receipt(lunch_node, request_id)
    assert (got["reason"], got["items_withheld_protected"], got["records_cited"]) == ("answered", 1, 2)
    # The re-checks still walk every released record, the withheld one too: the counts and digests of before.
    assert got["records_used"] == 3
    assert got["records_digest"] == digest(sorted(record_id for record_id, _content in released))


def test_on_every_re_check_walks_the_withheld_record_too(lunch_node, on, monkeypatch):
    released = [record_id for record_id, _content in released_ids(lunch_node, "What are the lunch plans?")]
    service = service_for(lunch_node, Writer())
    walked = []
    original = lunch_node.search.retrieve_for_answer

    def spy(**kwargs):
        walked.append(kwargs.get("record_ids"))
        return original(**kwargs)

    monkeypatch.setattr(lunch_node.search, "retrieve_for_answer", spy)
    _request_id, asked, (_signed, body) = ask(lunch_node, service, "What are the lunch plans?")
    assert body["outcome"] == "answered"
    _again, body_again = fetch(lunch_node, service, asked[1]["answer_id"])
    assert body_again == body
    # Retrieval, the re-check before the body is kept, and one re-check per fetch: each walks all three.
    assert walked == [None, tuple(released), tuple(released), tuple(released)]


def test_on_nothing_is_touched_when_no_item_is_protected(lunch_node, monkeypatch):
    seen = {}
    for setting in ("false", "true"):
        monkeypatch.setenv(SWITCH, setting)
        writer = Writer()
        request_id, _asked, (_signed, body) = ask(lunch_node, service_for(lunch_node, writer),
                                                  "What about the roadmap budget?")
        seen[setting] = (writer.prompts[0], body, receipt(lunch_node, request_id))
    (prompt_off, body_off, (receipt_off, decision_off)) = seen["false"]
    (prompt_on, body_on, (receipt_on, decision_on)) = seen["true"]
    assert prompt_on == prompt_off and sorted(bodies(prompt_on)) == sorted([BUDGET, SUMMARY])
    assert body_on == body_off and body_on["outcome"] == "answered"
    per_ask = {"request_id", "answer_id", "question_hash", "accepted_at", "started_at", "finished_at"}
    assert {key: value for key, value in receipt_on.items() if key not in per_ask} == {
        **{key: value for key, value in receipt_off.items() if key not in per_ask}, "items_withheld_protected": 0}
    assert decision_on == decision_off


def test_on_all_items_protected_is_the_zero_records_path(lunch_node, on):
    """Only the protected item matches: the writer is never called and the job ends as a job with no records does."""
    assert [content for _id, content in released_ids(lunch_node, "Who is visiting?")] == [GAP]
    writer = Writer()
    request_id, _asked, (_signed, body) = ask(lunch_node, service_for(lunch_node, writer), "Who is visiting?")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"} and writer.prompts == []
    got, decision = receipt(lunch_node, request_id)
    assert (got["reason"], got["records_used"], got["records_digest"], got["items_withheld_protected"]) == (
        "nothing_matched", 0, digest([]), 1)
    assert got["reason"] in answer_release.RECEIPT_REASONS
    assert decision == {"version": "answer-decision/v1", "outcome": "no_answer", "reason": "nothing_matched"}


def test_off_the_same_question_is_refused_whole_as_before(lunch_node):
    writer = Writer()
    request_id, _asked, (_signed, body) = ask(lunch_node, service_for(lunch_node, writer), "Who is visiting?")
    assert body["outcome"] == "no_answer" and len(writer.prompts) == 1
    assert receipt(lunch_node, request_id)[0]["reason"] == "answer_protected"


def test_the_body_check_still_refuses_a_protected_term_the_writer_writes(lunch_node, on):
    """Case (d): no item shows the term, so Fix A withholds nothing; the body check refuses the whole answer."""
    writer = Writer("Harlan reviewed the budget for the roadmap with finance [1].")
    request_id, _asked, (_signed, body) = ask(lunch_node, service_for(lunch_node, writer),
                                              "What about the roadmap budget?")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert not any("Harlan" in text or "Ostrow" in text for text in bodies(writer.prompts[0]))
    got, _decision = receipt(lunch_node, request_id)
    assert (got["reason"], got["items_withheld_protected"]) == ("answer_protected", 0)


def test_a_boundary_that_cannot_read_a_text_refuses_with_its_own_reason(lunch_node, on, monkeypatch):
    original = lunch_node.search.resolver.entity_boundary

    class Unreadable:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, *texts):
            if any("Booked the lunch table" in text for text in texts):
                raise PolicyError("entity_protection_lineage_unavailable")
            return self.inner.mentions_protected(*texts)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(lunch_node.search.resolver, "entity_boundary", lambda conn: Unreadable(original(conn)))
    writer = Writer()
    request_id, _asked, (_signed, body) = ask(lunch_node, service_for(lunch_node, writer), "What are the lunch plans?")
    assert body["outcome"] == "no_answer" and writer.prompts == []
    assert receipt(lunch_node, request_id)[0]["reason"] == "entity_protection_lineage_unavailable"


# --- off: byte for byte the service of before ---------------------------------------------------------------------------

def _exercise(node, monkeypatch, service_class) -> str:
    """A fixed run of asks and fetches, and every ledger row about requests, as one canonical text."""
    counter = iter(range(10_000))
    monkeypatch.setattr("secrets.token_hex", lambda n=16: format(next(counter), "x").rjust(2 * n, "0"))
    seen = []
    service = service_for(node, Writer(), service_class)
    for number, question in enumerate(("What are the lunch plans?", "What about the roadmap budget?",
                                       "Who is visiting?")):
        request_id, asked, fetched = ask(node, service, question, label=f"pif-ask-{number}")
        seen.append([request_id, asked, fetched, fetch(node, service, asked[1]["answer_id"], f"pif-again-{number}")])
    with sqlite3.connect(node.ledger.path) as conn:
        ledger = {table: [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                  for table in ("p2a_requests", "p2a_receipts", "p2a_question_days")}
    return json.dumps({"seen": seen, "ledger": ledger}, sort_keys=True, default=str)


def test_off_the_service_is_byte_for_byte_the_service_of_before(tmp_path, monkeypatch):
    """One fixture on disk, restored before each run: the current service twice (the method's own control), the
    service of `BASE` (its answer_release with its own build_prompt), and the current one with the switch on, which
    must differ: the comparison can see the change."""
    now = int(time.time())
    root, kept = tmp_path / "pif", tmp_path / "kept"
    node, policy = node_with(root, monkeypatch, [GAP, LUNCH, MENU, BUDGET, SUMMARY], now=now)
    node.canonical.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ft.close(node)
    shutil.copytree(root, kept)
    before_release = ft.module_at(BASE, "answer_release")
    before_release.build_prompt = ft.module_at(BASE, "answer_generation").build_prompt
    assert not hasattr(before_release, "withhold_protected_items")

    def run(service_class) -> str:
        shutil.rmtree(root)
        shutil.copytree(kept, root)
        again = ft.reopen(root, now=now, policy=policy)
        try:
            return _exercise(again, monkeypatch, service_class)
        finally:
            ft.close(again)
    current, control = run(answer_release.AnswerService), run(answer_release.AnswerService)
    assert current == control, "two runs of the same code differ: the comparison would prove nothing"
    reasons = [json.loads(row[1])["reason"] for row in json.loads(current)["ledger"]["p2a_receipts"]]
    assert reasons == ["answer_protected", "answered", "answer_protected"]
    assert run(before_release.AnswerService) == current
    monkeypatch.setenv(SWITCH, "true")
    switched = run(answer_release.AnswerService)
    reasons = [json.loads(row[1])["reason"] for row in json.loads(switched)["ledger"]["p2a_receipts"]]
    assert reasons == ["answered", "answered", "nothing_matched"]
