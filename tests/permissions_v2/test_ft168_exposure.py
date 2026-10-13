"""FT168 item 3: what a share must never release through a fact.

A fact is a second way to a statement: its source message may be something the share withholds. For each reason a
share withholds a source, the fact that cites it must not be released by search or by the batch door, must not be
counted as shareable, must not reach the answer writer, and must not be in an answer's body. The reasons are tried
two ways: in place before the share's index is built, and arriving after it was built and before the next build
(the release decides again; no build may be needed for the fact to stop).

Every node has a control: a second work fact with a clean source, which must keep releasing wherever the share is
served, so a test that passes because nothing at all was released is told apart.

Invented data only: an owner who works at "Northwind" on "Project Lantern".
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_ft168_switch_loops import (ANSWERS, advance, fact_members, production_loop,
                                                          sweep_and_observe, written_answer)
from tests.permissions_v2.test_refresh_loop import Clock
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_index import index_path
from topos.permissions_v2.selective_facts import SEMANTICS, UNAVAILABLE, WORK_PREDICATES, selection

CONTROL_TEXT = "I work at Northwind."
CONTROL = "Owner works at Northwind."
SOURCE = "imessage:2"
SOURCE_TEXT = "I work on Project Lantern."
STATEMENT = "Owner works on Project Lantern."
SECOND = "imessage:4"
SECOND_TEXT = "At work, I am working on Project Lantern"
#: Matches both facts and the three work messages, and names nothing a test puts Off-limits.
QUERY = "Northwind work project"
DAY = 86_400
FACTS_ONLY = "grant-facts"


def messages(*, labels=None, age: int = 600) -> list:
    return [ft.Message(CONTROL_TEXT, age=300),
            ft.Message(SOURCE_TEXT, age=age, labels=labels or {}),
            ft.Message("Budget review for the harbor team moved to Thursday", age=900),
            ft.Message(SECOND_TEXT, age=1200)]


def serving(root, monkeypatch, *, labels=None, age: int = 600, cites=(SOURCE,), mark=None, build: bool = True,
            protected: bool = False):
    """Three shares over the same four messages, each with selective consent for Work and career: records and
    facts, answers with sources, and facts only. The control fact cites message 1; the fact under test cites
    `cites`. `mark(node)` runs before the first build. `protected`: the node already has someone Off-limits (a person
    no message mentions), so its people tables exist and its boundary is active, as on a node that uses Off-limits."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    node = ft.build(root, messages(labels=labels, age=age), now=now, policy=ft.policy_at(now), protected=protected)
    node.answers_raw = ft.policy_at(now, grant=ANSWERS, answers="with_sources")
    node.facts_raw = ft.policy_at(now, grant=FACTS_ONLY, kinds=("fact",))
    ft.activate(node, node.answers_raw)
    ft.activate(node, node.facts_raw)
    node.control = ft.state_fact(node, "imessage:1", "works_at", "Northwind")
    node.fact = ft.state_fact(node, list(cites), "works_on", "Project Lantern")
    ft.seed_registry(node)
    if mark is not None:
        mark(node)
        ft.sync(node)
    if build:
        states = node.rebuild()
        assert set(states.values()) == {"ready"}, states
    return node


# --- the reasons a share withholds a source --------------------------------------------------------------------

def _identity(node, message_id=SOURCE):
    return node.corpus.resolver._identity("conversation_messages", message_id, "imessage", twins.DATASET)


def owner_only(node, message_id=SOURCE):
    conn = node.canonical
    columns = [row[1] for row in conn.execute("PRAGMA table_info(owner_only_records)")]
    values = {"canonical_table": "conversation_messages", "record_id": message_id, "created_at": "t",
              "updated_at": "t", "reason": "synthetic restriction"}
    keys = [column for column in columns if column in values]
    conn.execute(f"INSERT INTO owner_only_records({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                 [values[key] for key in keys])
    conn.commit()


def off_limits(node, _message_id=SOURCE):
    """The owner puts "Lantern" Off-limits, by name (no person record needed)."""
    conn = node.canonical
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                 "aliases_json, created_at) VALUES ('ft168-b1','','lantern','Lantern','[]','t')")
    conn.commit()


def off_limits_by_mention(node, message_id=SOURCE):
    """The node's own graph links the source to the person who is already Off-limits (`twins.protect`). Neither the
    message's words nor the fact's name that person."""
    conn = node.canonical
    conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table) "
                 "VALUES ('ft168-m1','protected-entity',?,'imessage','conversation_messages')", (message_id,))
    conn.commit()


def assessment_pass(node) -> int:
    """What the node's catch-up does after an Off-limits change: every message it can still read is assessed again
    (the same fixed answer; no model). An Off-limits entry changes the context a neighbouring message was assessed
    under (two messages either side), so until this runs those neighbours are "not checked yet". Returns how many."""
    done = 0
    for number in range(1, 5):
        try:
            with owner():
                ft.assess(node.corpus.resolver, node.corpus.reviews, f"imessage:{number}")
            done += 1
        except PolicyError:
            pass                      # an item the boundary withholds is not assessed
    return done


def off_limits_then_the_assessment_pass(node, message_id=SOURCE):
    off_limits(node, message_id)
    assessment_pass(node)


def excluded_item(node, message_id=SOURCE):
    """The owner's exclusion of the record, as the exclusion store writes its tombstone. The store's own purge of
    what was derived from the record is left out on purpose: the fact stays, so the release itself is what is tried."""
    from topos.features.lifecycle.exclusions import ExclusionStore
    ExclusionStore(node.canonical)._tombstone("record", message_id, None)
    node.canonical.commit()


def opted_out(node, message_id=SOURCE):
    from topos.permissions_v2.message_evidence import message_key
    with owner():
        node.corpus.reviews.opt_out(message_key(_identity(node, message_id)), now=node.now[0])


def fact_opted_out(node, _message_id=SOURCE):
    with owner():
        node.corpus.reviews.opt_out(node.fact, now=node.now[0])


def owner_correction(node, message_id=SOURCE):
    """The owner's own review of the message says it is highly sensitive, over the machine's label."""
    from topos.permissions_v2.automatic_message_review import parse_assessment, prepare
    from topos.permissions_v2.message_evidence import record_message_review
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        prepared = prepare(resolver, reviews, _identity(node, message_id))
        labels = parse_assessment({**ft.PLAIN, "sensitivity": "special"}, prepared["snapshot"].message)
        record_message_review(resolver, reviews, review_id="ft168-owner-correction",
                              expected_snapshot=prepared["snapshot"].model_dump(), classification=labels.model_dump(),
                              expected_current_review_revision=None, reviewed_at=node.now[0])


def assessed_again(**labels):
    def mark(node, message_id=SOURCE):
        with owner():
            ft.assess(node.corpus.resolver, node.corpus.reviews, message_id, **labels)
    return mark


#: reason -> how the node is made with the source already withheld.
BEFORE_THE_BUILD = {
    "topic": dict(labels={"domains": ["health"]}),
    "topic_mixed_with_work": dict(labels={"domains": ["health", "work"]}),
    "sensitivity": dict(labels={"sensitivity": "special"}),
    "not_the_owners_words": dict(labels={"speech": "third_party_quote"}),
    "outside_the_time_window": dict(age=91 * DAY),
    "off_limits": dict(mark=off_limits_then_the_assessment_pass, protected=True),
    "off_limits_by_mention": dict(mark=off_limits_by_mention, protected=True),
    "owner_only": dict(mark=owner_only),
    "excluded_item": dict(mark=excluded_item),
    "opted_out": dict(mark=opted_out),
    "owner_correction": dict(mark=owner_correction),
}


class Writer:
    """The answer door with the model replaced: keeps everything the writer was given, answers with `text`."""

    def __init__(self, node, text: str):
        from tests.permissions_v2.test_answer_release import _service
        self.given: list[str] = []
        self.service = _service(node, text)

        async def generate(prompt, *, deadline):
            self.given.append(prompt.user + "\n" + "\n".join(prompt.raw_texts))
            return text
        self.service.generate = generate

    def close(self):
        self.service.close()


def released_everywhere(node) -> dict:
    """Everything the three shares hand out for a question that matches both facts and every message: the records
    share by search and by batch, the facts-only share, and the answers share (what its writer was given, and the
    body). Refusals are kept as they are."""
    out = {}
    facts, others, refused = ft.ask(node, QUERY)
    out["search"] = dict(facts=[fact["content"] for fact in facts], text=json.dumps(facts + others), refused=refused)
    batch, refused = node.search_batch_request([QUERY, "project"], k=10)
    out["batch"] = dict(text=json.dumps(batch), refused=refused,
                        facts=[r["content"] for item in batch or [] for r in item["records"] if r["kind"] == "fact"])
    facts, others, refused = ft.ask(node, QUERY, grant=FACTS_ONLY)
    out["facts_only"] = dict(facts=[fact["content"] for fact in facts], text=json.dumps(facts + others),
                             refused=refused, records=facts + others)
    writer = Writer(node, "The owner is employed at Northwind and busy at work [1, 2].")
    try:
        try:
            _answer_id, body = written_answer(node, writer.service, QUERY)
        except PolicyError as exc:
            body = {"refused": exc.code}
        out["answer"] = dict(given="\n".join(writer.given), body=json.dumps(body), outcome=body.get("outcome"))
    finally:
        writer.close()
    return out


def assert_the_fact_is_withheld(node, *, source_released: bool = False) -> dict:
    """The fact under test is nowhere; the control fact is everywhere the share is served; the withheld source's
    words are nowhere either."""
    out = released_everywhere(node)
    for door in ("search", "batch", "facts_only"):
        assert out[door]["refused"] is None, (door, out[door]["refused"])
        assert STATEMENT not in out[door]["text"], door
        assert CONTROL in out[door]["facts"], (door, out[door]["facts"])
        if not source_released:
            assert SOURCE_TEXT not in out[door]["text"], door
    assert out["facts_only"]["facts"] == [CONTROL]
    assert STATEMENT not in out["answer"]["given"] and STATEMENT not in out["answer"]["body"]
    assert CONTROL in out["answer"]["given"], "the answer writer was given the control fact"
    if not source_released:
        assert SOURCE_TEXT not in out["answer"]["given"] and SOURCE_TEXT not in out["answer"]["body"]
    return out


def count_and_preview(node, raw=None) -> tuple[dict, dict]:
    """(the facts row of the owner's count, the preview's items by fact id) for one share."""
    counted = ft.owner_count(node, raw)
    return counted["kinds"]["facts"], {item["fact_id"]: item for item in counted["fact_preview"]["items"]}


def assert_not_counted_as_shareable(node) -> tuple[dict, dict]:
    facts, preview = count_and_preview(node)
    assert facts["can_share"] == 1, facts
    assert preview[node.control]["eligible"] and preview[node.control]["statement"] == CONTROL
    if node.fact in preview:
        item = preview[node.fact]
        assert not item["eligible"] and item["sources"] == [], item
    return facts, preview


@pytest.mark.parametrize("cites", ["only_the_withheld_source", "the_withheld_source_and_a_clean_one"])
@pytest.mark.parametrize("reason", sorted(BEFORE_THE_BUILD))
def test_a_fact_whose_source_the_share_withholds_is_released_nowhere(tmp_path, monkeypatch, reason, cites):
    """`the_withheld_source_and_a_clean_one`: the fact also cites a clean message that states the same claim, which
    is how the build finds it at all. One withheld source is enough to withhold the fact."""
    both = cites == "the_withheld_source_and_a_clean_one"
    node = serving(tmp_path / "ft168", monkeypatch, cites=(SOURCE, SECOND) if both else (SOURCE,),
                   **BEFORE_THE_BUILD[reason])
    try:
        assert fact_members(node) == 1 and fact_members(node, FACTS_ONLY) == 1      # the control only
        # Off-limits by name reaches the second message too (it names the same thing); otherwise it is released.
        assert_the_fact_is_withheld(node)
        facts, preview = assert_not_counted_as_shareable(node)
        if reason == "outside_the_time_window" and not both:
            assert node.fact not in preview, "a fact whose only source is outside the window is not a candidate"
        else:
            assert node.fact in preview
    finally:
        ft.close(node)


# --- the same reasons, arriving after the index was built ----------------------------------------------------------

def window_rolls_past_the_source(node, _message_id=SOURCE):
    """Two hours pass: the source was an hour inside the share's 90 days, and is now an hour outside."""
    node.now[0] += 2 * 3600


AFTER_THE_BUILD = {
    "off_limits": off_limits,
    "off_limits_by_mention": off_limits_by_mention,
    "owner_only": owner_only,
    "excluded_item": excluded_item,
    "opted_out": opted_out,
    "the_fact_opted_out": fact_opted_out,
    "owner_correction": owner_correction,
    "assessed_again_as_sensitive": assessed_again(sensitivity="special"),
    "assessed_again_off_topic": assessed_again(domains=["health"]),
    "window_rolled_past_it": window_rolls_past_the_source,
}


#: What the owner is told about the fact once its source is withheld: (the preview's reason for it, the class the
#: count files it under). None: the count files it under "never", a class its reply does not carry, so the fact is in
#: no number at all while the preview still lists it. As measured on 45d6dd02; the FT168 report discusses the three
#: None rows and the excluded item's reason.
OWNER_IS_TOLD = {
    "assessed_again_as_sensitive": ("evidence_not_permitted", "highly_sensitive"),
    "assessed_again_off_topic": ("evidence_not_permitted", "highly_sensitive"),
    "owner_correction": ("evidence_not_permitted", "highly_sensitive"),
    "opted_out": ("owner_opted_out", "you_held_back"),
    "the_fact_opted_out": ("owner_opted_out", "you_held_back"),
    "excluded_item": ("could_not_check", None),
    "off_limits": ("entity_protected", None),
    "off_limits_by_mention": ("entity_protected", None),
    "owner_only": ("owner_only", None),
    "window_rolled_past_it": ("not listed", None),
}


@pytest.mark.parametrize("reason", sorted(AFTER_THE_BUILD))
def test_a_source_withheld_after_the_index_was_built_stops_its_fact_at_the_next_request(tmp_path, monkeypatch, reason,
                                                                                        capsys):
    """WS0's case: the share includes the source when its index is built; the owner then marks it Off-limits or
    excludes it (or anything else here), and the next build has not run. The fact is in the index file. It must not
    leave: either the release refuses that candidate, or the door refuses the request. The owner's count and
    preview, which read no index, must say the same. Then the loop restores what needs restoring, and the share
    serves the control and not the fact."""
    age = 90 * DAY - 3600 if reason == "window_rolled_past_it" else 600
    node = serving(tmp_path / "ft168", monkeypatch, age=age, protected=reason.startswith("off_limits"))
    try:
        assert fact_members(node) == 2
        facts, _others, refused = ft.ask(node, QUERY)
        assert refused is None and sorted(fact["content"] for fact in facts) == sorted([CONTROL, STATEMENT])
        clock = Clock(node.now[0])
        loop = production_loop(node, clock)
        sweep_and_observe(node, loop)
        AFTER_THE_BUILD[reason](node)
        if reason == "window_rolled_past_it":
            clock.now = node.now[0]

        # The next request, on the index that still holds the fact.
        first = {}
        facts, others, refused = ft.ask(node, QUERY)
        first["search"] = "refused" if refused else "served without it"
        assert STATEMENT not in json.dumps(facts) and SOURCE_TEXT not in json.dumps(others)
        if refused is None:
            assert [fact["content"] for fact in facts] == [CONTROL]
        batch, refused = node.search_batch_request([QUERY, "project"], k=10)
        assert STATEMENT not in json.dumps(batch) and SOURCE_TEXT not in json.dumps(batch)
        facts, _others, refused = ft.ask(node, QUERY, grant=FACTS_ONLY)
        first["facts_only"] = "refused" if refused else "served without it"
        assert STATEMENT not in json.dumps(facts)
        still_there = index_path(node.index.root, ft.GRANT).exists()

        # The owner's count and preview, which read the database and no index.
        counted, preview = count_and_preview(node)
        # Off-limits by name also reaches the two messages either side of one that names it: their assessments were
        # made under another context, so they are "not checked yet" until the node's next assessment pass. Here that
        # is the control's source, so for that while the control fact is held back too.
        neighbours_wait = reason == "off_limits"
        assert counted["can_share"] == (0 if neighbours_wait else 1), counted
        assert counted["held_back"]["not_checked_yet"] == (1 if neighbours_wait else 0), counted
        if node.fact in preview:
            assert not preview[node.fact]["eligible"] and preview[node.fact]["sources"] == []
        listed = preview.get(node.fact, {}).get("reason", "not listed")
        held = {name: number for name, number in counted["held_back"].items() if number}
        counted_classes = counted["can_share"] + sum(counted["held_back"].values())
        told, filed = OWNER_IS_TOLD[reason]
        assert listed == told, (listed, told)
        assert {name for name in held if name != "not_checked_yet"} == ({filed} if filed else set()), held

        # The loop: a sweep, an observation, the debounce, a restore if anything was dropped.
        sweep_and_observe(node, loop)
        advance(node, clock, 30)
        receipt = loop.run_pending()
        if not index_path(node.index.root, ft.GRANT).exists():       # dropped by a request before any sweep saw it
            sweep_and_observe(node, loop)
            advance(node, clock, 30)
            receipt = loop.run_pending() or receipt
        if neighbours_wait:
            assessment_pass(node)                                    # the node's catch-up, with its fixed answer
            sweep_and_observe(node, loop)
            advance(node, clock, 300)                                # a second restore waits for the interval (D1)
            receipt = loop.run_pending() or receipt
        ft.sync(node)
        states = {grant: index_path(node.index.root, grant).exists() for grant in (ft.GRANT, ANSWERS, FACTS_ONLY)}
        if not all(states.values()):
            assert node.rebuild() and all(index_path(node.index.root, grant).exists() for grant in states)
        assert_the_fact_is_withheld(node)
        assert_not_counted_as_shareable(node)
        with capsys.disabled():
            print(f"\nFT168 item 3, {reason} after the build: next request {first}; index kept until a build: "
                  f"{still_there}; preview: {len(preview)} listed, this fact: {listed}; count: can share "
                  f"{counted['can_share']}, held back {held}, {counted_classes} counted in all; loop restore: "
                  f"{None if receipt is None else receipt.cause_classes}; all three indexes back by the loop "
                  f"alone: {all(states.values())}")
    finally:
        ft.close(node)


def test_a_fact_is_released_only_when_every_source_is_inside_the_asked_time(tmp_path, monkeypatch):
    """The recipient narrows the time. The control's source was sent 300 s ago; the fact cites two messages, sent
    600 s and 1,200 s ago."""
    node = serving(tmp_path / "ft168", monkeypatch, cites=(SOURCE, SECOND))
    try:
        now = node.now[0]

        def asked(seconds_ago: int):
            output, refused = node.search_request(QUERY, k=10, window={"after": now - seconds_ago, "before": now})
            assert refused is None
            return (sorted(r["content"] for r in output["records"] if r["kind"] == "fact"),
                    [r["content"] for r in output["records"] if r["kind"] == "message"])
        facts, texts = asked(450)
        assert facts == [CONTROL] and texts == [CONTROL_TEXT]
        facts, texts = asked(900)                  # one source inside, the other not: the fact stays out
        assert facts == [CONTROL] and SOURCE_TEXT in texts and SECOND_TEXT not in texts
        facts, texts = asked(1500)
        assert facts == sorted([CONTROL, STATEMENT])
    finally:
        ft.close(node)


# --- the citation is the statement ---------------------------------------------------------------------------------

def test_a_fact_carries_its_statement_and_never_its_sources_words(tmp_path, monkeypatch):
    """A facts-only share releases nothing but statements: no record, no citation, no answer body and nothing the
    answer writer is given holds a source message's words. A fact's citation is its own statement."""
    node = serving(tmp_path / "ft168", monkeypatch, cites=(SOURCE, SECOND))
    try:
        out = released_everywhere(node)
        records = out["facts_only"]["records"]
        assert sorted(record["content"] for record in records) == sorted([CONTROL, STATEMENT])
        for record in records:
            assert record["kind"] == "fact" and record["assertion"] == "owner_stated"
            assert record["ontology_id"] == ft.WORK and record["predicate"] in WORK_PREDICATES
            assert record["citations"] and all(c["content"] == record["content"] for c in record["citations"])
            assert set(record) == {"kind", "record_id", "content", "source_ids", "citations", "event_at", "assertion",
                                   "ontology_id", "predicate"}
        for words in (CONTROL_TEXT, SOURCE_TEXT, SECOND_TEXT):
            assert words not in out["facts_only"]["text"]
        assert node.fact not in out["facts_only"]["text"] and node.control not in out["facts_only"]["text"]
        # The same share, asked for a written answer.
        ft.activate(node, ft.policy_at(node.now[0], grant="grant-facts-answers", kinds=("fact",),
                                       answers="with_sources"))
        assert node.rebuild()["grant-facts-answers"] == "ready"
        from tests.permissions_v2.test_answer_release import _service
        from topos.permissions_v2.answer_protocol import ASK, FETCH, verify_node_answer
        from tests.permissions_v2.message_search_harness import recipient
        given = []
        wording = "The owner is busy with the Lantern effort [1]."
        service = _service(node, wording)

        async def generate(prompt, *, deadline):
            given.append(prompt.user + "\n" + "\n".join(prompt.raw_texts))
            return wording
        service.generate = generate

        def call(kind, payload):
            signed = node._envelope("grant-facts-answers", kind, payload, node.next_id("ft168-facts-answer"))
            with recipient():
                result, output = (service.submit if kind == ASK else service.fetch)(
                    envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
            verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                               envelope=signed, output=output, mode="with_sources", now=node.now[0])
            return output
        try:
            answer_id = call(ASK, {"question": "Lantern"})["answer_id"]
            for _ in range(400):
                body = call(FETCH, {"answer_id": answer_id})
                if "outcome" in body:
                    break
                time.sleep(0.02)
        finally:
            service.close()
        receipt = ft.answer_receipts(node)[-1]
        assert body["outcome"] == "answered", (receipt["reason"], receipt["sentences"], receipt["records_used"])
        assert {record["kind"] for record in body["records"]} == {"fact"}
        assert [record["content"] for record in body["records"]] == [STATEMENT]
        assert all(c["content"] == STATEMENT for record in body["records"] for c in record["citations"])
        text = "\n".join(given) + json.dumps(body)
        assert STATEMENT in "\n".join(given)
        for words in (CONTROL_TEXT, SOURCE_TEXT, SECOND_TEXT):
            assert words not in text
    finally:
        ft.close(node)


# --- ontologies outside the release ------------------------------------------------------------------------------------

#: One per reason class of the catalog.
UNADMITTED = {"special_category": "health.mental", "unsupported_grounding": "obligations.commitments",
              "inferred_or_owner_only": "personality.traits", "third_party_subject": "relationships.social"}


def test_the_four_classes_are_the_catalogs_own():
    assert {UNAVAILABLE[ontology_id][1] for ontology_id in UNADMITTED.values()} == set(UNADMITTED)
    assert {reason for _title, reason in UNAVAILABLE.values()} == set(UNADMITTED), "a reason class this file misses"
    assert set(SEMANTICS) == {ft.WORK}


@pytest.mark.parametrize("ontology_id", sorted(UNAVAILABLE))
def test_a_share_cannot_sign_an_ontology_outside_the_release(ontology_id):
    """Whatever the owner's app sends: the policy does not parse, so no such share is ever installed."""
    raw = ft.knowledge_policy()
    for forged in ({"ontology_id": ontology_id, "revision": SEMANTICS[ft.WORK]["revision"],
                    "predicates": list(WORK_PREDICATES)},
                   {"ontology_id": ontology_id, "revision": "none", "predicates": ["any.predicate"]}):
        raw["search"]["selective_facts"] = {**selection([ft.WORK]), "ontologies": [forged]}
        with pytest.raises((ValueError, PolicyError)):
            parse_policy(raw)
        raw["search"]["selective_facts"] = {**selection([ft.WORK]),
                                            "ontologies": selection([ft.WORK])["ontologies"] + [forged]}
        with pytest.raises((ValueError, PolicyError)):
            parse_policy(raw)


@pytest.mark.parametrize("reason_class, shape", [(reason_class, "an_admitted_predicate") for reason_class in sorted(UNADMITTED)]
                         + [("special_category", "its_own_predicate"), ("special_category", "no_pack_in_the_payload")])
def test_a_fact_of_an_ontology_outside_the_release_is_never_released(tmp_path, monkeypatch, reason_class, shape):
    """The ontology is switched on at the node, the fact is scoped, owner-asserted, stated, and its source is a clean
    message that states the very words. The share signs Work and career. Nothing of it leaves, by any door, and it
    is not counted. The hardest shape names an admitted predicate under the other ontology."""
    ontology_id = UNADMITTED[reason_class]
    node = serving(tmp_path / "ft168", monkeypatch, build=False)
    try:
        # Take the fact under test away, and store it again as the other ontology's.
        node.canonical.execute("DELETE FROM signal_objects WHERE object_id=?", (node.fact,))
        node.canonical.commit()
        predicate = "works_on" if shape != "its_own_predicate" else ontology_id.split(".")[0] + ".note"
        node.fact = ft.pack_fact(node, SOURCE, ontology_id=ontology_id, version="0.2.3", predicate=predicate,
                                 value="Project Lantern")
        if shape == "no_pack_in_the_payload":
            payload = json.loads(node.canonical.execute("SELECT payload_json FROM signal_objects WHERE object_id=?",
                                                        (node.fact,)).fetchone()[0])
            payload.pop("pack")
            payload.pop("extractor")
            node.canonical.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?",
                                   (json.dumps(payload), node.fact))
            node.canonical.commit()
        assert ft.switch(node, ontology_id, True, consent=True)
        assert {ontology_id, ft.WORK} <= ft.switched_on(node)
        assert set(node.rebuild().values()) == {"ready"}
        assert fact_members(node) == 1
        assert_the_fact_is_withheld(node, source_released=True)
        facts, preview = assert_not_counted_as_shareable(node)
        assert preview[node.fact]["reason"] == "fact_ontology_unsupported"
    finally:
        ft.close(node)


@pytest.mark.parametrize("how", ["altitude_inferred", "altitude_predicted", "asserted_by_the_assistant",
                                 "received_not_authored", "about_someone_else", "has_lineage", "owner_only",
                                 "a_later_pack_version", "a_predicate_outside_the_set", "not_what_the_source_says"])
def test_a_work_fact_that_is_not_the_owners_own_plain_statement_is_never_released(tmp_path, monkeypatch, how):
    """Inside the admitted ontology, everything short of an owner-stated, scoped, terminal fact with a source that
    says it in so many words."""
    node = serving(tmp_path / "ft168", monkeypatch, build=False)
    try:
        conn = node.canonical
        conn.execute("DELETE FROM signal_objects WHERE object_id=?", (node.fact,))
        conn.commit()
        if how == "about_someone_else":
            from tests.permissions_v2.test_owner_identity_binding import add_entity
            add_entity(conn, "colleague-entity", is_self=0)
            conn.commit()
            node.fact = ft.state_fact(node, SOURCE, "works_on", "Project Lantern", subject="colleague-entity")
        elif how == "asserted_by_the_assistant":
            node.fact = ft.state_fact(node, SOURCE, "works_on", "Project Lantern", asserted_by="assistant")
        elif how == "owner_only":
            node.fact = ft.state_fact(node, SOURCE, "works_on", "Project Lantern", disclosure="owner_only")
        elif how == "not_what_the_source_says":
            node.fact = ft.state_fact(node, "imessage:3", "works_on", "the harbor team")
        elif how in ("received_not_authored", "has_lineage"):
            node.fact = ft.state_fact(node, SOURCE, "works_on", "Project Lantern")
            payload = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?",
                                              (node.fact,)).fetchone()[0])
            payload.update({"actor_role": "received"} if how == "received_not_authored"
                           else {"lineage": [{"object_id": node.control}]})
            conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), node.fact))
            conn.commit()
        else:
            altitude = {"altitude_inferred": "inferred", "altitude_predicted": "predicted"}.get(how, "stated")
            node.fact = ft.pack_fact(node, SOURCE, ontology_id=ft.WORK, predicate="work.role" if how ==
                                     "a_predicate_outside_the_set" else "work.project",
                                     version="0.2.4" if how == "a_later_pack_version" else "0.2.3",
                                     value="Project Lantern", altitude=altitude)
        assert set(node.rebuild().values()) == {"ready"}
        assert fact_members(node) == 1
        out = assert_the_fact_is_withheld(node, source_released=True)
        assert "Project Lantern." not in "".join(out["facts_only"]["facts"])
        assert "harbor" not in out["facts_only"]["text"]
        assert count_and_preview(node)[0]["can_share"] == 1
    finally:
        ft.close(node)


# --- the registry ----------------------------------------------------------------------------------------------------

def _registry(node, sql, *args):
    conn = sqlite3.connect(str(node.corpus.resolver.path), timeout=30)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


REGISTRY = {
    "table_missing": lambda node: _registry(node, "DROP TABLE pack_registry"),
    "column_unreadable": lambda node: _registry(node, "ALTER TABLE pack_registry RENAME COLUMN enabled TO was_on"),
    "row_missing": lambda node: _registry(node, "DELETE FROM pack_registry WHERE pack_id=?", ft.WORK),
    "row_under_another_spelling": lambda node: _registry(node, "UPDATE pack_registry SET pack_id='Work.Career' "
                                                         "WHERE pack_id=?", ft.WORK),
    "row_with_a_space": lambda node: _registry(node, "UPDATE pack_registry SET pack_id=' work.career' "
                                               "WHERE pack_id=?", ft.WORK),
    "enabled_is_two": lambda node: _registry(node, "UPDATE pack_registry SET enabled=2 WHERE pack_id=?", ft.WORK),
    "enabled_is_a_word": lambda node: _registry(node, "UPDATE pack_registry SET enabled='on' WHERE pack_id=?", ft.WORK),
    "enabled_is_minus_one": lambda node: _registry(node, "UPDATE pack_registry SET enabled=-1 WHERE pack_id=?", ft.WORK),
    "every_other_ontology_on": lambda node: _registry(node, "UPDATE pack_registry SET enabled=(pack_id<>?)", ft.WORK),
}


@pytest.mark.parametrize("state", sorted(REGISTRY))
def test_a_registry_that_does_not_say_on_shares_no_fact(tmp_path, monkeypatch, state, caplog):
    """The index was built while Work and career was on and holds both facts. Whatever then becomes of the registry,
    short of the one row saying 1: no fact by any door, none counted, the preview says the ontology is off, and the
    share goes on serving its messages."""
    node = serving(tmp_path / "ft168", monkeypatch)
    try:
        assert fact_members(node) == 2
        REGISTRY[state](node)
        assert ft.WORK not in ft.switched_on(node)
        with caplog.at_level("WARNING"):
            out = released_everywhere(node)
        for door in ("search", "batch", "facts_only"):
            assert out[door]["refused"] is None and out[door]["facts"] == [], (door, out[door])
            assert CONTROL not in out[door]["text"] and STATEMENT not in out[door]["text"]
        assert SOURCE_TEXT in out["search"]["text"] and CONTROL_TEXT in out["search"]["text"]     # the messages
        assert CONTROL not in out["answer"]["given"] and STATEMENT not in out["answer"]["given"]
        assert CONTROL not in out["answer"]["body"] and STATEMENT not in out["answer"]["body"]
        assert not [r.getMessage() for r in caplog.records if "index stale" in r.getMessage()]
        facts, preview = count_and_preview(node)
        assert facts["can_share"] == 0 and facts["held_back"]["you_held_back"] == 2, facts
        assert {item["reason"] for item in preview.values()} == {"fact_ontology_off"}
        assert all(not item["eligible"] and item["sources"] == [] for item in preview.values())
        assert fact_members(node) == 2, "nothing was dropped or rebuilt for it"
    finally:
        ft.close(node)
