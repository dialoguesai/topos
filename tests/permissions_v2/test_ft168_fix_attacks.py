"""FT168: the fixes of D1, D3 and D6 (node commits d2310826 and 9efe7ecc), tried from the outside.

D3's fix: a candidate refused because its own ontology is switched off is walked past without using the candidate
window, and a share with fact consent ranks 500 spare candidates for that (`search_release.candidate_limits`,
`OFF_ONTOLOGY_PASSES`, `_fact_of_ontology_switched_off`).
D6's fix: those spare candidates used to end where a rank lane ends (its best 200). `search_lanes.rank(spare=n)` now
returns the first `limit` as before, then what follows in the same fused order, then what each lane ranks below its
depth.
D1's fix: a `facts_changed` build of an index that is still serving is a refresh, with the refresh's own rate limit
(`refresh_loop.REFRESH_CAUSES`), so it no longer takes the slot a dropped index needs.

What is tried here:
  D3/D6  the spare candidates reach past 200 switched-off facts, with one rank lane and with two (an index with
         vectors: the harness's stand-in embedder, no model);
         a fact refused for another reason while its ontology is on still counts against the window;
         a share without fact consent, and one with consent while every ontology is on, release and record byte for
         byte what 45d6dd02 did, on one fixture; and on a real index the first `limit` candidates are the ones
         45d6dd02 ranked, whatever spare is asked, also for a member one lane ranks below its depth;
         at node size, on request (`FT168_BIG=1`): what comes back and what it costs with 500 and 560 switched-off
         facts ahead of 590 messages, and where past the 500 the loss returns.
  D1     an entry that carries a drop and a switch is restored on the restore's timer; two shares, one dropped and
         one switched, do not delay each other; a switch every three seconds for a minute does not starve a restore.

Invented data only. No model is reachable: the answer writer is a fixed sentence.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import time
from contextlib import contextmanager

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_ft168_switch_loops import (QUERY, advance, fact_members, production_loop, project_names,
                                                          protection_moves, serving_node, sweep_and_observe)
from tests.permissions_v2.test_refresh_loop import Clock
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path, purge

# The node commit before the fixes. The lane's own is 45d6dd02. On the integration branch (IN167) `search_release.py`
# also carries LA164, TR162 and NR166, which that commit's file does not know: the answer job hands
# `retrieve_for_answer` keywords it never had, so its answer is refused and the comparison below fails for a reason
# that has nothing to do with the fixes. The commit before the fixes on this branch is d92f819d: 45d6dd02's facts
# state with every other lane. Same claim, same comparison, the base that exists here.
BASE = "d92f819d"
PLAIN = "grant-plain"


# --- D3 (b): any other refusal still counts ----------------------------------------------------------------------

def test_a_fact_refused_for_another_reason_while_its_ontology_is_on_still_counts(tmp_path, monkeypatch):
    """Fifty-six work facts, each citing its own old message and one newer note. The recipient asks about a time that
    ends before the newer note: every fact is ranked (its own time is inside) and every one is refused (a source is
    outside the asked time). With the ontology on those refusals use up the window of 50, as any refusal did before
    the fix, and nothing is released. Switched off, the very same candidates are refused for the switch instead, are
    walked past, and the messages behind them are released."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = project_names(56)
    node = ft.build(tmp_path / "ft168", [ft.Message("Budget review for the harbor team moved to Thursday", age=300)]
                    + [ft.Message(f"At work, I am working on {name}", age=3000 + number)
                       for number, name in enumerate(names)], now=now, policy=ft.policy_at(now))
    try:
        for number, name in enumerate(names, start=2):
            ft.state_fact(node, [f"imessage:{number}", "imessage:1"], "works_on", name)
        ft.seed_registry(node)
        assert node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == 56
        asked = {"after": now - 6000, "before": now - 1000}

        def ask(window):
            output, refused = node.search_request("project", k=8, window=window)
            assert refused is None
            kinds = [record["kind"] for record in output["records"]]
            return kinds.count("fact"), kinds.count("message")
        assert ask(None) == (8, 0)                         # no time asked: the facts lead and are released
        assert ask(asked) == (0, 0)                        # refused for the time, ontology on: each one counted
        assert ft.switch(node, ft.WORK, False)
        assert ask(asked) == (0, 8)                        # refused for the switch: walked past
        assert ask(None) == (0, 8)
    finally:
        ft.close(node)


# --- D3 (c): a share without fact consent is answered byte for byte as before ----------------------------------------

def release_class_at(commit: str):
    """`MessageSearchRelease` as `commit` has it, over the current tree's other modules; and proof it is the old one."""
    from topos.permissions_v2 import search_release
    module = ft.module_at(commit, "search_release")
    assert hasattr(search_release, "candidate_limits") and not hasattr(module, "candidate_limits")
    assert "OFF_ONTOLOGY_PASSES" not in module.source_text and module.MessageSearchRelease is not \
        search_release.MessageSearchRelease
    return module.MessageSearchRelease


def exercise(node, monkeypatch, *, answers: str) -> str:
    """A fixed run of requests against one node, and everything it left: each release, each signed result, each
    refusal, one written answer, and every row the ledger holds about requests. Returned as one canonical text."""
    from tests.permissions_v2.test_answer_release import _service
    from topos.permissions_v2.answer_protocol import ASK, FETCH
    seen = []
    queries = twins.queries(30, 7, 9) + ["roadmap", "deploy review budget", "item3 item7"]
    counter = iter(range(10_000))
    monkeypatch.setattr("secrets.token_hex", lambda n=16: format(next(counter), "x").rjust(2 * n, "0"))

    def search(label, payload, grant=ft.GRANT):
        envelope = node._envelope(grant, "permissions.v2.search", payload, label)
        try:
            with recipient():
                result, output = node.search.dispatch(envelope=envelope.model_dump(), payload=payload,
                                                      request_id=label)
            seen.append([label, "released", result, output])
        except PolicyError as exc:
            seen.append([label, "refused", exc.code])
    for number, query in enumerate(queries):
        search(f"ft168-c-search-{number}", {"query": query, "k": (1, 5, 10)[number % 3]})
    now = node.now[0]
    search("ft168-c-window", {"query": queries[0], "k": 10, "window": {"after": now - 4000, "before": now - 500}})
    search("ft168-c-above-k", {"query": queries[0], "k": 11})
    payloads = [{"query": query, "k": 5} for query in queries[:4]]
    envelopes = [node._envelope(ft.GRANT, "permissions.v2.search", payload, f"ft168-c-batch:{number}")
                 for number, payload in enumerate(payloads)]
    with recipient():
        batch = node.search.dispatch_batch(items=[
            {"envelope": envelope.model_dump(), "payload": payload, "request_id": envelope.request_id}
            for envelope, payload in zip(envelopes, payloads)])
    seen.append(["ft168-c-batch", "released", None, {"batch": [[result, output] for result, output in batch]}])
    service = _service(node, "The owner is busy with the roadmap effort [1].")
    try:
        def call(kind, payload, label):
            signed = node._envelope(answers, kind, payload, label)
            with recipient():
                return (service.submit if kind == ASK else service.fetch)(
                    envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
        result, pending = call(ASK, {"question": "roadmap"}, "ft168-c-ask")
        seen.append(["ft168-c-ask", "released", result, pending])
        # One fetch, once the writer's thread has ended: how many fetches a wait takes is the thread's speed, not
        # the release, and each fetch is a request of its own in the ledger.
        job = service._jobs[pending["answer_id"]]
        for _ in range(1000):
            if job.state == "ended":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the answer worker did not finish")
        result, body = call(FETCH, {"answer_id": pending["answer_id"]}, "ft168-c-fetch")
        seen.append(["ft168-c-fetch", "released", result, body])
    finally:
        service.close()
    with sqlite3.connect(node.ledger.path) as conn:
        ledger = {table: [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                  for table in ("p2a_requests", "p2a_receipts", "p2a_question_days")}
    return json.dumps({"seen": seen, "ledger": ledger}, sort_keys=True, default=str)


@pytest.mark.parametrize("share", ["without_fact_consent", "with_fact_consent_and_every_ontology_on"])
def test_releases_and_receipts_are_byte_for_byte_those_of_the_commit_before_the_fix(tmp_path, monkeypatch, share):
    """One fixture on disk, restored in place before each run: the current release twice (the method's own control:
    it must call two runs of the same code identical), then the release class of 45d6dd02 over the same stores. The
    releases, their signatures, the refusals, the written answer and the ledger's request rows are compared as bytes.

    `without_fact_consent` is the builder's claim (such a share ranks and walks as before). The second case goes one
    further: a share WITH consent ranks 500 more candidates now; while no ontology is off it must still release
    exactly what it did."""
    consent = share != "without_fact_consent"
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    root, backup = tmp_path / "ft168", tmp_path / "kept"
    texts = twins.texts(30, 7)
    messages = [ft.Message(text, age=600 + 97 * number, labels={"sensitivity": "special"} if number % 11 == 5 else
                           {"domains": ["health"]} if number % 13 == 7 else {})
                for number, text in enumerate(texts)]
    messages += [ft.Message(f"I work on {name}.", age=5000 + number) for number, name in enumerate(project_names(4))]
    ontologies = (ft.WORK,) if consent else None
    kinds = ("message", "fact", "goal", "relationship")
    raw = ft.policy_at(now, ontologies=ontologies, kinds=kinds)
    node = ft.build(root, messages, now=now, policy=raw)
    ft.activate(node, ft.policy_at(now, grant="grant-c-answers", answers="with_sources", ontologies=ontologies,
                                   kinds=kinds))
    for number, name in enumerate(project_names(4), start=31):
        ft.state_fact(node, f"imessage:{number}", "works_on", name)
    ft.seed_registry(node)
    assert set(node.rebuild().values()) == {"ready"}
    assert fact_members(node) == (4 if consent else 0)
    node.canonical.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ft.close(node)
    shutil.copytree(root, backup)

    def run(release_class) -> str:
        shutil.rmtree(root)
        shutil.copytree(backup, root)
        again = ft.reopen(root, now=now, policy=raw, release_class=release_class)
        try:
            return exercise(again, monkeypatch, answers="grant-c-answers")
        finally:
            ft.close(again)
    current, control = run(None), run(None)
    assert current == control, "two runs of the same code differ: the comparison would prove nothing"
    before = run(release_class_at(BASE))
    seen = json.loads(current)["seen"]
    released = [entry for entry in seen if entry[1] == "released"]
    assert len(released) >= 13 and any(entry[1] == "refused" for entry in seen)
    assert sum(len(entry[3]["records"]) for entry in released if isinstance(entry[3], dict) and "records" in entry[3]) > 20
    assert seen[-1][0] == "ft168-c-fetch" and seen[-1][3]["outcome"] == "answered", seen[-1][3]
    assert len(json.loads(current)["ledger"]["p2a_receipts"]) >= 18
    assert before == current


# --- D3 (a): how far the spare candidates really reach -------------------------------------------------------------------

def many_names(count: int) -> list[str]:
    starts = ("Al", "Be", "Ca", "Do", "El", "Fa", "Gi", "Ho")
    middles = ("ra", "ne", "li", "to", "su", "ka", "mo", "vi", "de", "po")
    ends = ("nar", "rin", "lot", "mes", "vik", "dan", "sor", "tul")
    names = [f"Project {start}{middle}{end}" for start in starts for middle in middles for end in ends]
    assert len(set(names)) == len(names) >= count
    return names[:count]


def crowd(root, monkeypatch, count: int, *, vectors: bool = False):
    """`count` projects, each stated in one message of the owner's; a share with fact consent, k up to 20."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(count)
    node = ft.build(root, [ft.Message(f"At work, I am working on {name}", age=600 + number)
                           for number, name in enumerate(names)], now=now, policy=ft.policy_at(now, max_k=20),
                    vectors=vectors)
    ft.seed_registry(node)
    node.names, node.stated = names, 0
    return node


def switched_off_with(node, facts: int) -> None:
    """`facts` work facts in the index (built while the ontology was on), and the ontology then switched off."""
    for number in range(node.stated + 1, facts + 1):
        ft.state_fact(node, f"imessage:{number}", "works_on", node.names[number - 1])
    node.stated = max(node.stated, facts)
    if not (ft.switch(node, ft.WORK, True) or ft.WORK in ft.switched_on(node)):
        raise RuntimeError("the ontology could not be switched on")
    if node.rebuild()[ft.GRANT] != "ready" or fact_members(node) != facts:
        raise RuntimeError("the index does not hold the facts this test needs")
    if not ft.switch(node, ft.WORK, False):
        raise RuntimeError("the ontology could not be switched off")


def loaded_index(node, grant=ft.GRANT):
    """The share's index as a request loads it, and its policy."""
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2.registry import parse_policy
    ft.sync(node)
    with owner():
        authority = node.ledger.authority_snapshot(grant, now=node.now[0])
    return node.index.load(grant, authority), parse_policy(node.search_raw)


def facts_leading(node, loaded, policy, query: str) -> dict:
    """Per rank lane: how many of its best 200 members are facts (proof that a lane is crowded, and that there are
    as many lanes as the test says). And `messages_before`: how many messages the whole list of 45d6dd02's `rank`
    holds, which is all the walk could ever reach before the fix."""
    from tests.permissions_v2.message_search_harness import fake_embedder
    from topos.permissions_v2 import search_lanes
    from topos.permissions_v2.search_index import unseal
    now = node.now[0]
    lower, upper = (now - policy.search.window.max_age_seconds) * 1_000_000, now * 1_000_000
    inside = search_lanes.within(loaded, lower, upper)
    key = node.index.keys.get(ft.GRANT, create=False)
    sealed = {member.opaque_id: member.sealed for member in inside.members}

    def is_fact(opaque) -> bool:
        return bool(unseal(key, opaque, sealed[opaque]).get("projection"))
    vector = fake_embedder(query, loaded.model) if loaded.model else None
    found = {}
    for name, scored in (("lexical", search_lanes.lexical(inside, query)), ("vector", search_lanes.vector(inside, vector))):
        best = search_lanes._ranked(inside, list(scored), policy.search.release_event_time)
        found[name] = sum(1 for opaque in best if is_fact(opaque))
    whole = ft.module_at(BASE, "search_lanes").rank(loaded, query, vector, limit=10_000, lower_us=lower, upper_us=upper,
                                                    precision=policy.search.release_event_time)
    found["messages_before"] = sum(1 for opaque in whole if not is_fact(opaque))
    return found


@pytest.mark.parametrize("lanes", ["one_lane", "two_lanes"])
def test_the_spare_candidates_reach_past_two_hundred_switched_off_facts(tmp_path, monkeypatch, capsys, lanes):
    """FT168-D6, which was: 210 messages match the question; with 192 switched-off facts ranked ahead of them all
    eight were released, with 193 seven, with 200 none, because a rank lane hands on its best 200 and every one was a
    fact the walk passed. Fixed in 9efe7ecc: k messages at every count, at k = 8 and at k = 20.

    `two_lanes`: the index has vectors (the harness's stand-in embedder for members and queries; no model), so there
    is a vector lane as well. The facts lead it too, though not every place: the stand-in hashes words into 32
    buckets, and a few messages land ahead. So with 200 facts the list before the fix still held a handful of
    messages (fewer than 20): k = 20 is the case that tells the two commits apart there, and k = 8 on one lane."""
    from topos.permissions_v2 import search_lanes, search_release
    assert (search_lanes.LANE_DEPTH, search_release.OFF_ONTOLOGY_PASSES, search_release.CANDIDATE_FLOOR) == (200, 500, 50)
    two = lanes == "two_lanes"
    node = crowd(tmp_path / "ft168", monkeypatch, 210, vectors=two)
    released, leading = {}, {}
    try:
        for facts in (192, 193, 200):
            switched_off_with(node, facts)
            loaded, policy = loaded_index(node)
            leading[facts] = facts_leading(node, loaded, policy, "project")
            row = []
            for k in (8, 20):
                found, others, refused = ft.ask(node, "project", k=k)
                if refused is not None or found:
                    raise RuntimeError((refused, len(found)))
                row.append(len(others))
            released[facts] = row
        if bool(loaded.vectors) != two:
            raise RuntimeError("the index does not have the lanes this case is about")
    finally:
        ft.close(node)
    with capsys.disabled():
        print(f"\nFT168-D6 fixed, {lanes}, switched-off facts ahead -> messages released at k=8 and k=20: "
              f"{released}; per count, facts among each lane's best 200 and the messages the list before the fix "
              f"held: {leading}; load {ft.load()}")
    crowded = leading[200]
    if crowded["lexical"] != 200 or (crowded["vector"] < 150 if two else crowded["vector"] != 0) \
            or crowded["messages_before"] >= 20 or (not two and crowded["messages_before"] != 0):
        raise RuntimeError("the lanes are not crowded as this case needs", crowded)
    assert released == {192: [8, 20], 193: [8, 20], 200: [8, 20]}, released


@pytest.mark.parametrize("lanes", ["one_lane", "two_lanes"])
def test_the_first_candidates_are_the_ones_the_commit_before_ranked_whatever_spare_is_asked(tmp_path, monkeypatch,
                                                                                            capsys, lanes):
    """The builder's claim for a share WITH fact consent while nothing is switched off: asking for spare candidates
    changes nothing a walk reads first. On a real index of 460 members (420 notes of mixed work words, 40 project
    messages and their 40 facts), for eight questions and the three windows k = 8 to 12, 16 and 20 give: the list
    45d6dd02's `rank` returns, the list today's returns without spare, and the first `limit` of today's with 500
    spare are the same list; the whole list 45d6dd02 could return (all a lane's depth allows) is a prefix of the one
    with spare; and a member that one lane ranks below its depth and the other inside it stays exactly where it was
    (read deeper it would gain a second vote and move up)."""
    from tests.permissions_v2.message_search_harness import fake_embedder
    from topos.permissions_v2 import search_lanes
    before = ft.module_at(BASE, "search_lanes")
    assert "spare" not in before.source_text and before.LANE_DEPTH == search_lanes.LANE_DEPTH == 200
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = project_names(40)
    notes = twins.texts(420, 7)
    node = ft.build(tmp_path / "ft168", [ft.Message(text, age=900 + 31 * number) for number, text in enumerate(notes)]
                    + [ft.Message(f"At work, I am working on {name}", age=300 + number)
                       for number, name in enumerate(names)], now=now, policy=ft.policy_at(now, max_k=20),
                    vectors=lanes == "two_lanes")
    try:
        for number, name in enumerate(names, start=421):
            ft.state_fact(node, f"imessage:{number}", "works_on", name)
        ft.seed_registry(node)
        assert node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == 40
        loaded, policy = loaded_index(node)
        assert bool(loaded.vectors) == (lanes == "two_lanes") and len(loaded.members) == 500
        lower, upper = (now - policy.search.window.max_age_seconds) * 1_000_000, now * 1_000_000
        precision = policy.search.release_event_time
        inside = search_lanes.within(loaded, lower, upper)
        deep_lanes = moved_candidates = compared = 0
        # Six questions of one to three work words, one that every project message and fact matches, and one of six
        # work words that most of the notes match (so the lexical lane alone is deeper than 200).
        for query in twins.queries(420, 7, 6) + ["project", " ".join(mc.WORK_WORDS[:6])]:
            vector = fake_embedder(query, loaded.model) if loaded.model else None
            ask = dict(lower_us=lower, upper_us=upper, precision=precision)
            lanes_scored = [search_lanes.lexical(inside, query), search_lanes.vector(inside, vector)]
            orders = [search_lanes._ranked(inside, list(scored), precision, depth=10_000) for scored in lanes_scored]
            deep_lanes += sum(len(order) > search_lanes.LANE_DEPTH for order in orders)
            whole_before = before.rank(loaded, query, vector, limit=10_000, **ask)
            for limit in (50, 64, 80):
                then = before.rank(loaded, query, vector, limit=limit, **ask)
                plain = search_lanes.rank(loaded, query, vector, limit=limit, **ask)
                spared = search_lanes.rank(loaded, query, vector, limit=limit, spare=500, **ask)
                assert then == plain == spared[:limit], (query, limit)
                assert spared[:len(whole_before)] == whole_before[:limit + 500], (query, limit)
                assert len(spared) == len(set(spared)) == min(limit + 500, len(set(orders[0]) | set(orders[1])))
                compared += 1
            # Members below one lane's depth and inside the other's: with one vote today, two if read deeper.
            for own, other in ((orders[0], orders[1]), (orders[1], orders[0])):
                for opaque in set(own[search_lanes.LANE_DEPTH:]) & set(other[:search_lanes.LANE_DEPTH]):
                    assert spared.index(opaque) == whole_before.index(opaque)
                    moved_candidates += 1
    finally:
        ft.close(node)
    with capsys.disabled():
        print(f"\nFT168-D6 fixed, {lanes}: {compared} lists compared with {BASE}'s; lanes deeper than 200: "
              f"{deep_lanes}; members below one lane's depth and inside the other's, each found where it was: "
              f"{moved_candidates}")
    if deep_lanes == 0 or (lanes == "two_lanes" and moved_candidates == 0):
        raise RuntimeError("no lane was deeper than its depth: this fixture does not try the claim")


# --- at node size, on request: what comes back, what it costs, and where the bound is --------------------------------

class Gate:
    """How long the search door itself holds the node's write gate, per request: its admission, and the one read in
    which every candidate is decided again. Nothing is changed but the stopwatch around the door's own `with`."""

    def __init__(self, monkeypatch):
        from topos.permissions_v2 import search_release
        self.holds: list[float] = []
        real = search_release.with_db_write

        @contextmanager
        def timed():
            with real():
                started = time.perf_counter()
                try:
                    yield
                finally:
                    self.holds.append(time.perf_counter() - started)
        monkeypatch.setattr(search_release, "with_db_write", timed)


def timed_asks(node, gate: Gate, *, k: int, repeats: int = 7) -> dict:
    totals, holds, kinds = [], [], None
    for _ in range(repeats):
        gate.holds.clear()
        started = time.perf_counter()
        facts, others, refused = ft.ask(node, "project", k=k)
        totals.append((time.perf_counter() - started) * 1000)
        holds.append(max(gate.holds) * 1000)
        assert refused is None
        kinds = (len(facts), len(others))
    return {"ms": round(statistics.median(totals), 1), "fastest_ms": round(min(totals), 1),
            "gate_ms": round(statistics.median(holds), 1), "facts": kinds[0], "messages": kinds[1]}


def node_size(root, monkeypatch):
    """590 work messages, each the possible source of one work fact; one share with fact consent, k up to 20."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(590)
    node = ft.build(root, [ft.Message(f"At work, I am working on {name}", age=600 + number)
                           for number, name in enumerate(names)], now=now, policy=ft.policy_at(now, max_k=20))
    ft.seed_registry(node)
    node.names, node.stated = names, 0
    return node


def by_the_fix(facts: int, k: int, messages: int = 590) -> int:
    """What the fix's own arithmetic says: the ranked list is max(4k, 50) + 500 long, the switched-off facts lead it."""
    return max(0, min(k, max(4 * k, 50) + 500 - facts, messages))


@pytest.mark.skipif(os.environ.get("FT168_BIG") != "1", reason="a measurement on 590 messages with 500 and 560 facts "
                    "(two index builds of about 1,100 members): FT168_BIG=1")
def test_reach_and_cost_with_500_and_560_switched_off_facts_ahead(tmp_path, monkeypatch, capsys):
    """What comes back, and the milliseconds and gate hold of one request (median of 5), at k = 8, 16 and 20, with
    the ontology on and off. On d2310826 this returned nothing from 500 facts at every k (FT168-D6)."""
    node = node_size(tmp_path / "ft168", monkeypatch)
    gate = Gate(monkeypatch)
    released = {}
    try:
        for facts in (500, 560):
            for number in range(node.stated + 1, facts + 1):
                ft.state_fact(node, f"imessage:{number}", "works_on", node.names[number - 1])
            node.stated = facts
            assert ft.switch(node, ft.WORK, True) or ft.WORK in ft.switched_on(node)
            started = time.monotonic()
            assert node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == facts
            build_s = time.monotonic() - started
            on = {k: timed_asks(node, gate, k=k, repeats=5) for k in (8, 16, 20)}
            assert ft.switch(node, ft.WORK, False)
            off = {k: timed_asks(node, gate, k=k, repeats=5) for k in (8, 16, 20)}
            released[facts] = [off[k]["messages"] for k in (8, 16, 20)]
            with capsys.disabled():
                print(f"\nFT168 D6 fixed, {facts} facts in the index ({590 + facts} members, built in {build_s:.1f} "
                      f"s): ontology on: k=8 {on[8]}, k=16 {on[16]}, k=20 {on[20]}; ontology off: k=8 {off[8]}, "
                      f"k=16 {off[16]}, k=20 {off[20]}; load {ft.load()}", flush=True)
            assert all(off[k]["facts"] == 0 for k in off) and all(on[k]["facts"] == k for k in on)
    finally:
        ft.close(node)
    assert released == {facts: [by_the_fix(facts, k) for k in (8, 16, 20)] for facts in (500, 560)}, released
    assert released == {500: [8, 16, 20], 560: [0, 4, 20]}


@pytest.mark.skipif(os.environ.get("FT168_BIG") != "1", reason="a measurement on 590 messages with 542 to 561 facts "
                    "(five index builds of about 1,140 members): FT168_BIG=1")
def test_past_the_spare_candidates_the_loss_returns_at_node_size(tmp_path, monkeypatch, capsys):
    """FT168-D3, what is left of it after both fixes, at node size: the first message is lost at
    max(4k, 50) + 500 - k + 1 switched-off facts ranked ahead, which is 543 at k=8, 549 at k=16 and 561 at k=20, and
    nothing is released from max(4k, 50) + 500 (550 at k=8)."""
    node = node_size(tmp_path / "ft168", monkeypatch)
    released = {}
    try:
        for facts in (542, 543, 549, 550, 561):
            switched_off_with(node, facts)
            row = []
            for k in (8, 16, 20):
                found, others, refused = ft.ask(node, "project", k=k)
                assert refused is None and found == []
                row.append(len(others))
            released[facts] = row
            with capsys.disabled():
                print(f"\nFT168 past the bound, {facts} switched-off facts ahead: messages released at k=8, 16, 20: "
                      f"{row}; load {ft.load()}", flush=True)
    finally:
        ft.close(node)
    assert released == {facts: [by_the_fix(facts, k) for k in (8, 16, 20)] for facts in released}, released
    assert released == {542: [8, 16, 20], 543: [7, 16, 20], 549: [1, 15, 20], 550: [0, 14, 20], 561: [0, 3, 19]}


# --- D1: the restore's slot is the restore's -----------------------------------------------------------------------

@pytest.fixture
def two_shares(tmp_path, monkeypatch):
    """A share with fact consent (and two work facts), and a share of the same messages with none. On a clock the
    test moves."""
    node = serving_node(tmp_path / "ft168", monkeypatch, wall=False, answers=False)
    ft.activate(node, ft.policy_at(node.now[0], grant=PLAIN, ontologies=None, kinds=("message",)))
    assert set(node.rebuild().values()) == {"ready"}
    yield node
    ft.close(node)


def there(node, grant) -> bool:
    return index_path(node.index.root, grant).exists()


def drop_one(node, loop, grant) -> None:
    """One share's index goes (as a sweep drops one it finds stale) and the loop sees it at its next observation."""
    purge(node.index.root, grant)
    sweep_and_observe(node, loop)
    assert not there(node, grant)


def seconds_until(node, loop, clock, done, *, step: int = 5, limit: int = 700) -> int:
    """Seconds, in the loop's own ticks, until `done()`; the loop's pending work is run at every tick."""
    waited = 0
    while waited <= limit:
        loop.run_pending()
        if done():
            return waited
        advance(node, clock, step)
        waited += step
    raise AssertionError("never")


def test_an_entry_with_a_drop_and_a_switch_is_restored_on_the_restores_timer(two_shares):
    """A refresh has just run, so the refresh's slot is closed for 300 s. The owner switches again (a refresh is
    owed) and then the same share's index is dropped: the entry now carries both causes, and must be restored after
    the debounce on the restore's timer, not wait with the refresh."""
    node = two_shares
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    advance(node, clock, 30)
    assert loop.run_pending().cause_classes == ["facts_changed"]           # the refresh slot is now taken
    advance(node, clock, 10)
    assert ft.switch(node, ft.WORK, True)
    sweep_and_observe(node, loop)
    advance(node, clock, 10)
    assert loop.run_pending() is None                                       # owed, and waiting on the refresh slot
    protection_moves(node)
    assert sweep_and_observe(node, loop) == 2                               # both shares dropped: dark from here
    with loop._lock:
        assert loop._pending[ft.GRANT]["causes"] == {"facts_changed", "protection_changed"}
    dark = seconds_until(node, loop, clock, lambda: there(node, ft.GRANT) and there(node, PLAIN))
    assert dark <= 30, dark
    assert fact_members(node) == 2, "restored from a snapshot with the ontology on: the facts are back with it"
    restore = ft.restores(node)[-1]
    assert set(restore["cause_classes"]) == {"facts_changed", "protection_changed"}
    assert {grant["state"] for grant in restore["grants"]} == {"ready"}
    with loop._lock:
        assert loop._pending == {} and loop._last_restore_at is not None
    print(f"\nFT168 D1, a drop on top of an owed switch: dark {dark} s")


def test_two_shares_one_dropped_and_one_switched_do_not_delay_each_other(two_shares):
    node = two_shares
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)

    # 1. The switch's refresh runs; twenty seconds later the other share is dropped: restored after its debounce.
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    advance(node, clock, 30)
    assert loop.run_pending().cause_classes == ["facts_changed"]
    advance(node, clock, 20)
    drop_one(node, loop, PLAIN)
    dark = seconds_until(node, loop, clock, lambda: there(node, PLAIN))
    assert dark == 30, dark

    # 2. That restore has just run; the owner switches back: the refresh waits for the refresh before it (300 s
    #    after the first), and not a second longer for the restore.
    refreshed_at = ft.restores(node)[0]["started_at"]
    assert ft.switch(node, ft.WORK, True)
    sweep_and_observe(node, loop)
    waited = seconds_until(node, loop, clock, lambda: fact_members(node) == 2)
    assert clock.now - refreshed_at == 300, (clock.now - refreshed_at, waited)

    # 3. And a share dropped while that refresh was owed was not held by it either.
    assert [receipt["cause_classes"] for receipt in ft.restores(node)] == [
        ["facts_changed"], ["context_changed"], ["facts_changed"]]
    print(f"\nFT168 D1, two shares: the dropped one dark {dark} s; the switched one built 300 s after the refresh "
          f"before it")


def test_a_restore_does_not_hold_back_a_switchs_build(two_shares):
    """The other direction: a restore has just run (no refresh has), and the owner switches: built after the debounce."""
    node = two_shares
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    drop_one(node, loop, PLAIN)
    assert seconds_until(node, loop, clock, lambda: there(node, PLAIN)) == 30
    advance(node, clock, 10)
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    assert ft.ask(node, QUERY)[0] == []
    waited = seconds_until(node, loop, clock, lambda: fact_members(node) == 0)
    assert waited == 30, waited


@pytest.mark.parametrize("dropped", [PLAIN, ft.GRANT], ids=["another_share_is_dropped", "the_switched_share_is_dropped"])
def test_a_switch_every_three_seconds_for_a_minute_does_not_starve_a_restore(two_shares, dropped):
    """The owner flips Work and career every three seconds for a minute (a sweep sees each one). Ten seconds in, a
    share's index is dropped. It is restored within the debounce of its drop, and the flipping costs one build a
    minute, not one per flip."""
    node = two_shares
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    start, restored_at, on = clock.now, None, True
    builds = []
    rebuild = node.index.rebuild

    def counted(grant_id, *args, **kwargs):
        builds.append((clock.now - start, grant_id))
        return rebuild(grant_id, *args, **kwargs)
    node.index.rebuild = counted
    for second in range(0, 61):
        if second % 3 == 0 and second < 60:
            on = not on
            assert ft.switch(node, ft.WORK, on)
            sweep_and_observe(node, loop)
        if second == 10:
            drop_one(node, loop, dropped)
        if second % 5 == 0:
            loop.run_pending()
        if restored_at is None and second > 10 and there(node, dropped):
            restored_at = second
        _facts, others, refused = ft.ask(node, QUERY, grant=ft.GRANT if dropped == PLAIN else PLAIN)
        assert refused is None and others, "the share that was not dropped is served at every second"
        advance(node, clock, 1)
    assert restored_at is not None and restored_at - 10 <= 30, restored_at
    assert len([grant for _at, grant in builds if grant == ft.GRANT]) <= 2, builds
    assert len([grant for _at, grant in builds if grant == PLAIN]) <= 1, builds
    print(f"\nFT168 D1, a switch every 3 s for 60 s ({dropped} dropped at 10 s): restored at {restored_at} s; "
          f"builds (second, share): {builds}")
