"""FT168: the fixes of D1 and D3 (node commit d2310826), tried from the outside.

D3's fix: a candidate refused because its own ontology is switched off is walked past without using the candidate
window, and a share with fact consent ranks 500 spare candidates for that (`search_release.candidate_limits`,
`OFF_ONTOLOGY_PASSES`, `_fact_of_ontology_switched_off`).
D1's fix: a `facts_changed` build of an index that is still serving is a refresh, with the refresh's own rate limit
(`refresh_loop.REFRESH_CAUSES`), so it no longer takes the slot a dropped index needs.

What is tried here:
  D3 (a) more switched-off facts ahead than the spare candidates (the bound itself is pinned at small size in
         test_ft168_switch_loops.py; the node-size run is in the measurement below, on request);
     (b) a fact refused for another reason while its ontology is on still counts against the window;
     (c) a share without fact consent releases and records byte for byte what 45d6dd02 did, on one fixture;
     (d) and (e) what one query costs with 0, 50 and 500 switched-off facts ahead: a measurement, on request
         (`FT168_BIG=1`), because its fixture has more than a thousand index members.
  D1 an entry that carries a drop and a switch is restored on the restore's timer; two shares, one dropped and one
     switched, do not delay each other; a switch every three seconds for a minute does not starve a restore.

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
from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_ft168_switch_loops import (QUERY, advance, fact_members, production_loop, project_names,
                                                          protection_moves, serving_node, sweep_and_observe)
from tests.permissions_v2.test_refresh_loop import Clock
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path, purge

BASE = "45d6dd02"                      # the node commit before the fixes
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


def crowd(root, monkeypatch, count: int):
    """`count` projects, each stated in one message of the owner's; a share with fact consent, k up to 20."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(count)
    node = ft.build(root, [ft.Message(f"At work, I am working on {name}", age=600 + number)
                           for number, name in enumerate(names)], now=now, policy=ft.policy_at(now, max_k=20))
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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="FT168-D6: the D3 fix ranks 500 spare candidates, but a "
                   "rank lane keeps only its best 200 members (search_lanes.LANE_DEPTH), so the walk never sees past "
                   "200 switched-off facts: the first message is lost at 193 of them (k=8) and nothing is released "
                   "from 200 on, where the fix's own bound says 543 and 550")
def test_the_spare_candidates_reach_past_two_hundred_switched_off_facts(tmp_path, monkeypatch, capsys):
    """D3 came back at node size. 210 messages match the question; with 192 switched-off facts ranked ahead of them
    all eight are released, with 193 seven, with 200 none: the lexical lane hands the walk 200 candidates and every
    one is a fact it walks past. (An index with vectors has a second lane of 200.) The spare candidates are 500."""
    from topos.permissions_v2 import search_lanes, search_release
    assert (search_lanes.LANE_DEPTH, search_release.OFF_ONTOLOGY_PASSES, search_release.CANDIDATE_FLOOR) == (200, 500, 50)
    node = crowd(tmp_path / "ft168", monkeypatch, 210)
    released = {}
    try:
        for facts in (192, 193, 200):
            switched_off_with(node, facts)
            found, others, refused = ft.ask(node, "project", k=8)
            if refused is not None or found:
                raise RuntimeError((refused, len(found)))
            released[facts] = len(others)
    finally:
        ft.close(node)
    with capsys.disabled():
        print(f"\nFT168-D6, k=8, switched-off facts ahead -> messages released: {released}; load {ft.load()}")
    if released[192] != 8:
        raise RuntimeError("a loss below the rank lane's depth: not what this test pins")
    assert released[193] == 8 and released[200] == 8, released


# --- D3 (d), (e): what one query costs, at node size, on request -------------------------------------------------------

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


@pytest.mark.skipif(os.environ.get("FT168_BIG") != "1", reason="a measurement on a fixture of 590 messages and up to "
                    "560 facts (four index builds of up to 1,150 members): FT168_BIG=1")
def test_reach_and_cost_with_up_to_560_switched_off_facts_ahead(tmp_path, monkeypatch, capsys):
    """(a) past the 500 spare candidates the loss is back at node size; (d) milliseconds and gate hold for one query
    with 0, 50 and 500 switched-off facts ahead; (e) the same numbers are what a recipient's stopwatch would read."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(590)
    built = time.monotonic()
    node = ft.build(tmp_path / "ft168", [ft.Message(f"At work, I am working on {name}", age=600 + number)
                                         for number, name in enumerate(names)], now=now,
                    policy=ft.policy_at(now, max_k=20))
    ft.seed_registry(node)
    gate = Gate(monkeypatch)
    rows, stated = [], 0
    try:
        for facts in (0, 50, 500, 560):
            for number in range(stated + 1, facts + 1):
                ft.state_fact(node, f"imessage:{number}", "works_on", names[number - 1])
            stated = facts
            assert ft.switch(node, ft.WORK, True)
            started = time.monotonic()
            assert node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == facts
            build_s = time.monotonic() - started
            on = timed_asks(node, gate, k=8) if facts else None
            assert ft.switch(node, ft.WORK, False)
            off = {k: timed_asks(node, gate, k=k) for k in (8, 16, 20)}
            rows.append((facts, build_s, on, off))
            with capsys.disabled():
                print(f"\nFT168 D3 cost, {facts} facts in the index ({590 + facts} members, built in {build_s:.1f} s): "
                      f"ontology on, k=8: {on}; ontology off: k=8 {off[8]}, k=16 {off[16]}, k=20 {off[20]}; "
                      f"load {ft.load()}", flush=True)
    finally:
        ft.close(node)
    by_facts = {facts: off for facts, _build, _on, off in rows}
    assert all(by_facts[facts][k]["messages"] == k for facts in (0, 50) for k in (8, 16, 20))
    assert all(by_facts[facts][k]["facts"] == 0 for facts in by_facts for k in (8, 16, 20))
    # By the fix's own arithmetic 500 switched-off facts ahead cost nothing (the ranked list is max(4k, 50) + 500
    # long) and 560 leave 0, 4 and 20 messages. Measured on 13 Oct: nothing at all from 500, at every k, because a
    # rank lane keeps only its best 200 (FT168-D6, pinned in the test below).
    assert [by_facts[500][k]["messages"] for k in (8, 16, 20)] == [0, 0, 0]
    assert [by_facts[560][k]["messages"] for k in (8, 16, 20)] == [0, 0, 0]
    with capsys.disabled():
        print(f"\nFT168 D3 cost: whole measurement {time.monotonic() - built:.0f} s; load {ft.load()}")


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
