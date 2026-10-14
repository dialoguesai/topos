"""FT168 item 1: an ontology switch never takes a share dark, with the node's own sweep and refresh loop running.

The builder's tests drive the loop's scheduling against a stub index and rebuild real indexes by hand. Here nothing is
driven: the daemon sweep thread and the refresh loop thread run as on a node (`ft168_support.Running`), the owner's
switch is the route's own `set_pack_enabled`, and a recipient keeps asking on a third thread from before the first
switch until after the last build. Only the timers are shorter than a node's; `PRODUCTION` is what a node waits, read
from the code, and the two clock-driven tests at the end walk the same sequence on those.

Invented data only: an owner who works on "Project Lantern" at "Northwind", and a handful of work notes.

protects (topos#111 at 45d6dd02):
  - off: the next request releases no fact, a cached answer is not served, the same request still releases the
    share's messages; the share is never refused; no `message search index stale` is logged;
  - on: the share keeps serving; a build with cause `facts_changed` is queued and published by the loop itself; the
    fact returns with no owner action;
  - two switches in quick succession, a switch while a build is in flight, and a switch of an ontology outside the
    release (nothing queued).

Expected failures here are findings (FT168 report): each names what it shows in one line.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_refresh_loop import Clock
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.refresh_loop import RefreshLoop, RefreshSettings, protection_sync
from topos.permissions_v2.search_index import index_path, unseal

ANSWERS = "grant-answers"
QUERY = "Lantern"

#: What the same sequence waits for on a node, from the code's own defaults (pinned by the first test below).
PRODUCTION = {"sweep_every": 10.0, "debounce": 30.0, "tick": 5.0, "min_interval": 300.0, "min_interval_floor": 60.0}


def work_messages() -> list:
    return [ft.Message("I work on Project Lantern.", age=600),
            ft.Message("I work at Northwind.", age=900),
            ft.Message("Lantern rollout notes for the harbor team", age=1200),
            ft.Message("Lantern budget review moved to Thursday", age=1500),
            ft.Message("Vendor call about the Lantern launch checklist", age=1800)]


def serving_node(root, monkeypatch, *, wall: bool, answers: bool = True, **policy):
    """A serving node: a records share (and an answers share), each with selective consent for Work and career; two
    owner-stated work facts; the registry seeded (Work and career on); every index built by the owner."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    node = ft.build(root, work_messages(), now=now, policy=ft.policy_at(now, **policy))
    if wall:
        node.now = ft.WallClock()
    if answers:
        ft.activate(node, ft.policy_at(now, grant=ANSWERS, answers="with_sources"))
    node.facts = [ft.state_fact(node, "imessage:1", "works_on", "Project Lantern"),
                  ft.state_fact(node, "imessage:2", "works_at", "Northwind")]
    ft.seed_registry(node)
    assert ft.WORK in ft.switched_on(node), "the bundled default has Work and career on"
    states = node.rebuild()
    assert states[ft.GRANT] == "ready" and (not answers or states[ANSWERS] == "ready"), states
    return node


@pytest.fixture
def served(tmp_path, monkeypatch):
    """On the machine's clock, for the tests that run the node's threads."""
    node = serving_node(tmp_path / "ft168", monkeypatch, wall=True)
    yield node
    ft.close(node)


@pytest.fixture
def clocked(tmp_path, monkeypatch):
    """On a clock the test moves, for the tests that walk a node's own timers without waiting for them."""
    node = serving_node(tmp_path / "ft168", monkeypatch, wall=False, answers=False)
    yield node
    ft.close(node)


def fact_members(node, grant: str = ft.GRANT) -> int:
    """How many fact members the share's index file holds now."""
    key = node.index.keys.get(grant, create=False)
    index = node.index._open(index_path(node.index.root, grant))
    return sum(1 for opaque, sealed in index["sealed"]
               if (unseal(key, opaque, sealed).get("projection") or {}).get("table") == "signal_objects")


def answers_service(node, text: str, *, during=None):
    """The answer door with the model replaced by `text` (no model is reachable); `during` runs while it "writes"."""
    from tests.permissions_v2.test_answer_release import _service
    service = _service(node, text)

    async def generate(_prompt, *, deadline):
        if during is not None:
            during()
        return text
    service.generate = generate
    return service


def answer_call(node, service, kind, payload, *, mode="with_sources"):
    from topos.permissions_v2.answer_protocol import ASK, verify_node_answer
    signed = node._envelope(ANSWERS, kind, payload, node.next_id("ft168-answer"))
    with recipient():
        result, output = (service.submit if kind == ASK else service.fetch)(
            envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode=mode, now=node.now[0])
    return output


def written_answer(node, service, question: str) -> tuple[str, dict]:
    from topos.permissions_v2.answer_protocol import ASK, FETCH
    answer_id = answer_call(node, service, ASK, {"question": question})["answer_id"]
    for _ in range(400):
        body = answer_call(node, service, FETCH, {"answer_id": answer_id})
        if "outcome" in body:
            return answer_id, body
        time.sleep(0.02)
    raise AssertionError("the answer worker did not finish")


def fetch_again(node, service, answer_id: str) -> dict | None:
    """A later fetch of a written answer: its body, or None when the door refuses."""
    from topos.permissions_v2.answer_protocol import FETCH
    try:
        return answer_call(node, service, FETCH, {"answer_id": answer_id})
    except PolicyError:
        return None


def facts_changed_restores(node) -> list[dict]:
    return [receipt for receipt in ft.restores(node) if "facts_changed" in receipt["cause_classes"]]


def stale_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "index stale" in record.getMessage()]


def only_publish_races(asker, reasons, running) -> int:
    """FT168-D2 (pinned on its own below): a request in flight when a build publishes is refused once, because the
    door re-reads the index file's identity before it decides and does not start again. With threads that can
    happen at any publish, so these tests allow that refusal and no other: its reason inside the door is
    `search_index_stale` with no `index stale` line logged (nothing was found stale, nothing was removed), and there
    is at most one per publish for one recipient. Returns how many there were."""
    refused = asker.refusals()
    assert all(code == "search_index_stale" for _at, code, _thread in reasons), reasons
    assert len(refused) == len([r for r in reasons if r[2] == "ft168-asker"]), (refused, reasons)
    assert len(refused) <= len(running.builds), (refused, running.builds)
    return len(refused)


# --- the timers ------------------------------------------------------------------------------------------------

def test_the_timers_this_file_shortens_are_the_nodes_own(monkeypatch):
    """Nothing in `PRODUCTION` is a guess, and the restore's minimum interval cannot be set under 60 s."""
    import inspect
    from topos.permissions_v2.runtime import Runtime
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    monkeypatch.setenv(ft.RESTORE_SWITCH, "true")
    monkeypatch.delenv(ft.MIN_INTERVAL, raising=False)
    settings = RefreshSettings.from_env()
    assert (settings.debounce, settings.tick, settings.min_interval) == (
        PRODUCTION["debounce"], PRODUCTION["tick"], PRODUCTION["min_interval"])
    assert inspect.signature(Runtime._start_sweeper).parameters["interval"].default == PRODUCTION["sweep_every"]
    assert settings.facts, "with the facts switch and the restore on, the loop watches the fact digest"
    monkeypatch.setenv(ft.MIN_INTERVAL, "0")
    assert RefreshSettings.from_env().min_interval == PRODUCTION["min_interval_floor"]
    monkeypatch.setenv(ft.RESTORE_SWITCH, "false")
    assert not RefreshSettings.from_env().facts, "without the restore nothing watches the switch (doc: owner's build)"


# --- the switch, with the node's threads running -----------------------------------------------------------------

def test_a_switch_never_takes_the_share_dark_with_the_real_sweep_and_loop(served, monkeypatch, caplog):
    """`FT168_NODE_INTERVAL=1` keeps the restore's minimum interval at the node's floor (60 s) instead of 0: the
    same assertions, and the fact then comes back a minute after the off build (measured, FT168 report)."""
    node = served
    floor = os.environ.get("FT168_NODE_INTERVAL") == "1"
    assert fact_members(node) == 2 and fact_members(node, ANSWERS) == 2
    service = answers_service(node, "The owner is busy with the Lantern effort [1, 2].")
    reasons = ft.record_refusal_reasons(node)
    running = ft.Running(node, monkeypatch, min_interval=None if floor else 0.0)
    timings = {}
    try:
        with caplog.at_level("WARNING"), ft.Asker(node, QUERY) as asker:
            # Before: the fact and the messages, and an answer written from them.
            facts, others, refused = ft.ask(node, QUERY)
            assert refused is None and [fact["content"] for fact in facts] == ["Owner works on Project Lantern."]
            assert others, "the share's messages are released beside the fact"
            answer_id, body = written_answer(node, service, QUERY)
            assert body["outcome"] == "answered", body
            assert {record["kind"] for record in body["records"]} == {"message", "fact"}, "written from both"
            assert fetch_again(node, service, answer_id)["outcome"] == "answered"      # kept, while nothing moved
            assert running.wait(lambda: running.observations >= 2) is not None         # the loop has its baseline
            assert running.quiet() and ft.restores(node) == []

            # --- off -----------------------------------------------------------------------------------------
            before = running.counts()
            switched_off = time.monotonic()
            assert ft.switch(node, ft.WORK, False)
            facts, others, refused = ft.ask(node, QUERY)                 # the very next request
            assert refused is None and facts == [] and others, (facts, others, refused)
            timings["off_first_request_ms"] = (time.monotonic() - switched_off) * 1000
            cached = fetch_again(node, service, answer_id)
            assert cached is None or cached.get("outcome") != "answered", "a cached answer was served after the switch"
            timings["off_build_s"] = running.wait(
                lambda: len(facts_changed_restores(node)) >= 1 and fact_members(node) == 0, 60)
            assert timings["off_build_s"] is not None, "no facts_changed build was published after switching off"
            assert running.wait(running.quiet, 30) is not None
            after_off = running.counts()
            [receipt] = facts_changed_restores(node)
            assert receipt["cause_classes"] == ["facts_changed"] and receipt["actor"] == "node_system"
            assert {(grant["grant_id"], grant["state"]) for grant in receipt["grants"]} == {
                (ft.GRANT, "ready"), (ANSWERS, "ready")}
            assert fact_members(node) == 0 and fact_members(node, ANSWERS) == 0
            facts, others, refused = ft.ask(node, QUERY)
            assert refused is None and facts == [] and others
            cached = fetch_again(node, service, answer_id)
            assert cached is None or cached.get("outcome") != "answered", "served again after the build"

            # --- on ------------------------------------------------------------------------------------------
            switched_on = time.monotonic()
            assert ft.switch(node, ft.WORK, True)
            facts, others, refused = ft.ask(node, QUERY)                 # at once: served, the fact not yet back
            assert refused is None and others, (facts, others, refused)
            timings["on_fact_back_s"] = running.wait(
                lambda: bool(ft.ask(node, QUERY, request_id=f"ft168-back-{time.monotonic_ns()}")[0]), 120)
            assert timings["on_fact_back_s"] is not None, "the fact never came back after switching on"
            assert running.wait(lambda: running.quiet() and len(facts_changed_restores(node)) == 2, 30) is not None
            after_on = running.counts()
            assert fact_members(node) == 2 and fact_members(node, ANSWERS) == 2
            assert time.monotonic() - switched_on < (90 if floor else 20)
        # The recipient who kept asking: every ask that was answered released the share's messages, nothing was
        # found stale, and nothing refused it but a publish it happened to straddle (FT168-D2).
        assert stale_lines(caplog) == []
        raced = only_publish_races(asker, reasons, running)
        assert len(asker.seen) > 20
        assert all(others for _at, _facts, others, refused in asker.seen if refused is None)
        assert all(grant["state"] == "ready" for receipt in ft.restores(node) for grant in receipt["grants"])
    finally:
        running.stop()
        service.close()
    off = {key: after_off[key] - before[key] for key in before}
    on = {key: after_on[key] - after_off[key] for key in before}
    print(f"\nFT168 item 1, the node's threads (5 messages, 2 facts, 2 shares; sweep 0.25 s, debounce 1 s, tick 0.2 s, "
          f"minimum interval {'60 s, the floor' if floor else '0'}): off: first request "
          f"{timings['off_first_request_ms']:.0f} ms, no fact; build published {timings['off_build_s']:.2f} s after "
          f"the switch, {off}. on: fact back {timings['on_fact_back_s']:.2f} s after the switch, {on}. standing "
          f"recipient: {len(asker.seen)} asks, {raced} refused at a publish (FT168-D2). builds (share, state, members, seconds): "
          f"{[(g, s, m, round(t, 3)) for g, s, m, t in running.builds]}. load {ft.load()}")


@pytest.mark.parametrize("sequence", [(False, True), (False, True, False), (False, True, False, True)],
                         ids=["off-on", "off-on-off", "off-on-off-on"])
@pytest.mark.parametrize("gap", ["no_gap", "one_sweep_apart"])
def test_switches_in_quick_succession_end_in_the_last_state_and_never_refuse(served, monkeypatch, caplog, sequence, gap):
    """Whatever the loop saw of the switches in between, the share ends holding what the last one says, by the
    loop's own builds, and is served throughout."""
    node = served
    reasons = ft.record_refusal_reasons(node)
    running = ft.Running(node, monkeypatch)
    try:
        with caplog.at_level("WARNING"), ft.Asker(node, QUERY) as asker:
            assert running.wait(lambda: running.observations >= 2) is not None
            for on in sequence:
                seen = running.observations
                assert ft.switch(node, ft.WORK, on)
                if gap == "one_sweep_apart":
                    assert running.wait(lambda: running.observations > seen) is not None
                if not on:
                    # Off is decided by the release, whatever the index holds and whatever the loop has seen.
                    assert ft.ask(node, QUERY, request_id=f"ft168-off-{time.monotonic_ns()}")[0] == []
            expected = 2 if sequence[-1] else 0
            settled = running.wait(lambda: running.quiet() and fact_members(node) == expected
                                   and fact_members(node, ANSWERS) == expected, 30)
            assert settled is not None, (fact_members(node), running.builds, ft.restores(node))
            seen = running.observations
            assert running.wait(lambda: running.observations >= seen + 3) is not None     # and it stays there
            assert running.quiet() and fact_members(node) == expected
            facts, others, refused = ft.ask(node, QUERY)
            assert refused is None and others and len(facts) == (1 if sequence[-1] else 0)
        assert stale_lines(caplog) == []
        raced = only_publish_races(asker, reasons, running)
        assert all(others for _at, _facts, others, refused in asker.seen if refused is None)
        if gap == "no_gap" and sequence[-1]:
            assert running.builds == [], "back where it started before any sweep looked: nothing to build"
    finally:
        running.stop()
    print(f"\nFT168 item 1, {'-'.join('on' if on else 'off' for on in sequence)} ({gap}): settled {settled:.2f} s "
          f"after the last switch, builds {len(running.builds)}, asks {len(asker.seen)}, refused at a publish {raced}, "
          f"load {ft.load()}")


@pytest.mark.parametrize("starts", ["on", "off"])
def test_a_switch_while_a_build_is_in_flight_is_built_again_by_the_loop(served, monkeypatch, caplog, starts):
    """The owner switches; the loop's build has read its snapshot; the owner switches back before it publishes.
    The build publishes what its snapshot said, and the loop must build once more for the state that stands. While
    the ontology is off no fact is released, whatever the file on disk holds."""
    node = served
    on_at_start = starts == "on"
    if not on_at_start:
        assert ft.switch(node, ft.WORK, False)
        assert node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == 0
    reasons = ft.record_refusal_reasons(node)
    running = ft.Running(node, monkeypatch)
    flipped: list[float] = []
    build_members = node.index._members

    def switch_back_mid_build(*args, **kwargs):
        # `_members` runs at the end of a build's read snapshot, after its facts were decided and before it publishes.
        if not flipped and threading.current_thread().name == "p2c-search-refresh":
            flipped.append(time.monotonic())
            assert ft.switch(node, ft.WORK, on_at_start)
        return build_members(*args, **kwargs)
    node.index._members = switch_back_mid_build
    released_while_off = []
    try:
        with caplog.at_level("WARNING"), ft.Asker(node, QUERY) as asker:
            assert running.wait(lambda: running.observations >= 2) is not None
            assert ft.switch(node, ft.WORK, not on_at_start)             # queues the build the owner then interrupts
            assert running.wait(lambda: bool(flipped), 30) is not None
            if not on_at_start:
                # Back off, mid-build: the build that is about to publish holds the facts. None may be released.
                for number in range(40):
                    facts, _others, _refused = ft.ask(node, QUERY, request_id=f"ft168-mid-{number}-{time.monotonic_ns()}")
                    released_while_off += facts
                    time.sleep(0.02)
            expected = 2 if on_at_start else 0
            settled = running.wait(lambda: running.quiet() and len(running.builds) >= 4
                                   and fact_members(node) == expected and fact_members(node, ANSWERS) == expected, 30)
            assert settled is not None, (fact_members(node), running.builds)
            facts, others, refused = ft.ask(node, QUERY)
            assert refused is None and others and len(facts) == (1 if on_at_start else 0)
        assert released_while_off == []
        assert stale_lines(caplog) == []
        raced = only_publish_races(asker, reasons, running)
        assert all(others for _at, _facts, others, refused in asker.seen if refused is None)
    finally:
        running.stop()
    print(f"\nFT168 item 1, a switch back while the loop's build was in flight (started {starts}): settled "
          f"{settled:.2f} s after the first switch's build began, builds {len(running.builds)}, refused at a publish "
          f"{raced}, load {ft.load()}")


def test_a_switch_of_an_ontology_outside_the_release_queues_nothing(served, monkeypatch, caplog):
    node = served
    reasons = ft.record_refusal_reasons(node)
    running = ft.Running(node, monkeypatch)
    try:
        with caplog.at_level("WARNING"), ft.Asker(node, QUERY) as asker:
            assert running.wait(lambda: running.observations >= 2) is not None
            for ontology_id, on in (("health.mental", False), ("health.mental", True), ("craft.tooling", True),
                                    ("craft.tooling", False), ("beliefs.civic", True)):
                assert ft.switch(node, ontology_id, on), ontology_id
                seen = running.observations
                assert running.wait(lambda: running.observations >= seen + 2) is not None
                assert running.quiet(), ontology_id
                facts, others, refused = ft.ask(node, QUERY)
                assert refused is None and len(facts) == 1 and others
            # Long enough for a queued build to have run: debounce 1 s, tick 0.2 s.
            time.sleep(2.0)
        assert running.builds == [] and ft.restores(node) == []
        assert asker.refusals() == [] and reasons == [] and stale_lines(caplog) == [], (asker.refusals(), reasons)
        assert all(facts == 1 for _at, facts, _others, _refused in asker.seen)
    finally:
        running.stop()


def test_an_answer_being_written_when_the_ontology_goes_off_is_not_handed_out(served, monkeypatch):
    """The switch lands while the model is writing from a fact: the body is never released."""
    node = served
    service = answers_service(node, "The owner is busy with the Lantern effort [1].",
                              during=lambda: ft.switch(node, ft.WORK, False))
    try:
        _answer_id, body = written_answer(node, service, QUERY)
        assert body.get("outcome") != "answered" and "answer" not in body and not body.get("sources"), body
    finally:
        service.close()


# --- the same sequence on a node's own timers, on a clock the test moves --------------------------------------

def production_loop(node, clock):
    """The loop a default node runs (`RefreshSettings.from_env`: debounce 30 s, minimum interval 300 s), with its
    clock replaced. The index, the ledger and the builds are the real ones."""
    settings = RefreshSettings.from_env({ft.RESTORE_SWITCH: "true", ft.CATCHUP_SWITCH: "false",
                                         ft.FACTS_SWITCH: "true"})
    assert (settings.restore, settings.catchup, settings.facts) == (True, False, True)
    assert (settings.debounce, settings.min_interval) == (PRODUCTION["debounce"], PRODUCTION["min_interval"])
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=settings, clock=clock, sync_protection=protection_sync(node.protocol))


def sweep_and_observe(node, loop) -> int:
    removed = node.index.sweep(now=node.now[0])
    loop.after_sweep(node.index)
    return removed


def advance(node, clock, seconds: int) -> None:
    clock.now += seconds
    node.now[0] += seconds


def test_on_a_nodes_own_timers_off_is_at_once_and_on_waits_for_the_restore_interval(clocked, monkeypatch):
    """What F4 and F7 of the UI gate will see on a default node. Off: the fact stops at the next request; its build
    follows the 30 s debounce. On again soon after: the fact waits for the restore's minimum interval, 300 s from
    the build before it, not for the debounce. The share serves its messages at every step."""
    node = clocked
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)                                  # the loop's baseline
    timeline = []

    def served_now(label):
        facts, others, refused = ft.ask(node, QUERY)
        assert refused is None and others, (label, refused)
        timeline.append((clock.now - start, label, len(facts)))
        return len(facts)
    start = clock.now
    assert served_now("before") == 1
    assert ft.switch(node, ft.WORK, False)
    assert served_now("off, next request") == 0
    assert sweep_and_observe(node, loop) == 0                      # nothing dropped: the index serves on
    advance(node, clock, 29)
    assert loop.run_pending() is None                              # the debounce
    advance(node, clock, 1)
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["facts_changed"] and receipt.grants[0].state == "ready"
    off_built = clock.now - start
    assert off_built == 30 and fact_members(node) == 0

    advance(node, clock, 10)                                       # the owner switches it back on 40 s after off
    assert ft.switch(node, ft.WORK, True)
    assert served_now("on, next request") == 0
    assert sweep_and_observe(node, loop) == 0
    advance(node, clock, 30)                                       # its own debounce has passed
    assert loop.run_pending() is None and served_now("on + 30 s") == 0
    advance(node, clock, 259)                                      # 329 s after off: one second short
    assert loop.run_pending() is None and served_now("on + 289 s") == 0
    advance(node, clock, 1)
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["facts_changed"] and receipt.grants[0].state == "ready"
    assert served_now("on + 290 s") == 1
    print(f"\nFT168 item 1, a default node's timers (clock moved by the test): off build {off_built} s after the "
          f"switch (+ up to {PRODUCTION['sweep_every']:.0f} s sweep, {PRODUCTION['tick']:.0f} s tick); switched on "
          f"40 s after off, the fact returned 290 s after that switch, i.e. 300 s after the off build. timeline "
          f"(seconds, step, facts): {timeline}")


def protection_moves(node) -> None:
    """An owner-only mark on a record no share releases: the protection clock every index basis binds moves."""
    conn = node.canonical
    columns = [row[1] for row in conn.execute("PRAGMA table_info(owner_only_records)")]
    values = {"canonical_table": "conversation_messages", "record_id": "imessage:unrelated", "created_at": "t",
              "updated_at": "t", "reason": "synthetic restriction"}
    keys = [column for column in columns if column in values]
    conn.execute(f"INSERT INTO owner_only_records({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                 [values[key] for key in keys])
    conn.commit()


def dark_seconds_after_a_protection_move(node, loop, clock, *, step: int = 5, limit: int = 400) -> int:
    """Seconds from a protection move that drops the share's index until the loop has restored it."""
    protection_moves(node)
    assert sweep_and_observe(node, loop) == 1                      # dropped: the share is dark from here
    dark = 0
    while dark < limit:
        if loop.run_pending() is not None and index_path(node.index.root, ft.GRANT).exists():
            return dark
        advance(node, clock, step)
        dark += step
    raise AssertionError("never restored")


def test_without_a_switch_a_dropped_index_is_restored_after_the_debounce(clocked):
    """The control for the finding below: the same drop with no switch before it."""
    node = clocked
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    assert dark_seconds_after_a_protection_move(node, loop, clock) == 30


def test_a_switch_does_not_lengthen_the_dark_time_of_a_drop_that_follows_it(clocked):
    """`review_added` refreshes got their own rate limit for this reason (R-N153 R1); `facts_changed` builds of an
    index that is still there did not. The switch itself darkens nothing; it delays the cure of the next drop."""
    node = clocked
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    advance(node, clock, 30)
    assert loop.run_pending().cause_classes == ["facts_changed"]
    advance(node, clock, 20)                                       # 20 s later something else drops the index
    dark = dark_seconds_after_a_protection_move(node, loop, clock)
    print(f"\nFT168-D1: dark {dark} s after a drop that followed a switch's build by 20 s (30 s without the switch)")
    assert dark <= 30


@pytest.mark.xfail(strict=True, reason="FT168-D2: a request in flight when the loop publishes the switch's build is "
                   "refused once (inside the door: search_index_stale, nothing logged); the door re-reads the index "
                   "file's identity and does not start again. Seen with the node's threads in 3 of 9 runs")
def test_a_request_in_flight_when_the_switchs_build_publishes_is_served(clocked, monkeypatch):
    """The recipient has loaded and ranked the index; the loop's build for the switch publishes; the request's own
    gated recheck then finds another file. The next request is served: this one is refused."""
    from topos.permissions_v2 import search_release
    node = clocked
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    reasons = ft.record_refusal_reasons(node)
    sweep_and_observe(node, loop)
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    advance(node, clock, 30)
    rank, published = search_release.rank, []

    def rank_then_the_build_publishes(*args, **kwargs):
        order = rank(*args, **kwargs)
        if not published:
            published.append(loop.run_pending())
        return order
    monkeypatch.setattr(search_release, "rank", rank_then_the_build_publishes)
    in_flight = ft.ask(node, QUERY)
    assert published[0].cause_classes == ["facts_changed"] and published[0].grants[0].state == "ready"
    facts, others, refused = ft.ask(node, QUERY)
    assert refused is None and facts == [] and others, "the request after it is served from the new index"
    assert [code for _at, code, _thread in reasons] in ([], ["search_index_stale"])
    assert in_flight[2] is None, (in_flight[2], reasons)


def project_names(count: int) -> list[str]:
    starts, ends = ("Al", "Be", "Ca", "Do", "El", "Fa", "Gi", "Ho"), ("nar", "rin", "lot", "mes", "vik", "dan", "sor",
                                                                      "tul", "pax", "gim")
    return [f"Project {start}{end}" for start in starts for end in ends][:count]


@pytest.fixture
def many_work_facts(tmp_path, monkeypatch):
    """Sixty projects, each stated in one message of the owner's and held as one work fact. A fact's statement is
    shorter than the message it restates, so the lexical rank puts the facts first."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = project_names(60)
    node = ft.build(tmp_path / "ft168", [ft.Message(f"At work, I am working on {name}", age=600 + number)
                                         for number, name in enumerate(names)], now=now, policy=ft.policy_at(now))
    for number, name in enumerate(names, start=1):
        ft.state_fact(node, f"imessage:{number}", "works_on", name)
    ft.seed_registry(node)
    assert node.rebuild()[ft.GRANT] == "ready"
    yield node
    ft.close(node)


def test_after_switching_off_a_share_with_many_facts_still_releases_its_messages_before_the_build(many_work_facts):
    node = many_work_facts
    clock = Clock(node.now[0])
    loop = production_loop(node, clock)
    sweep_and_observe(node, loop)
    facts, others, refused = ft.ask(node, "project")
    assert refused is None and len(facts) + len(others) == 10
    assert ft.switch(node, ft.WORK, False)
    sweep_and_observe(node, loop)
    facts, others, refused = ft.ask(node, "project")
    before_build = len(others)
    advance(node, clock, 30)
    assert loop.run_pending().cause_classes == ["facts_changed"]
    facts_after, others_after, refused_after = ft.ask(node, "project")
    assert refused_after is None and facts_after == [] and len(others_after) == 10     # the build cures it
    print(f"\nFT168-D3: 60 messages and 60 facts match; with the ontology off, before the build: {before_build} "
          f"records released (refused: {refused}); after the build: {len(others_after)}")
    assert refused is None and facts == [] and before_build > 0


@pytest.mark.xfail(strict=True, reason="FT168-D4: facts count against the share's record cap, so switching an ontology "
                   "on for a share at its cap makes the loop publish an over-cap index and the whole share refuses "
                   "until the owner acts (production cap 5,000 records)")
def test_switching_on_never_puts_a_share_at_its_record_cap_out_of_service(tmp_path, monkeypatch):
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    node = ft.build(tmp_path / "ft168", work_messages(), now=now, policy=ft.policy_at(now, max_permitted=5))
    try:
        ft.state_fact(node, "imessage:1", "works_on", "Project Lantern")
        ft.seed_registry(node)
        assert ft.switch(node, ft.WORK, False)
        assert node.rebuild()[ft.GRANT] == "ready"            # five messages: at the cap, serving
        clock = Clock(node.now[0])
        loop = production_loop(node, clock)
        sweep_and_observe(node, loop)
        assert ft.ask(node, QUERY)[2] is None
        assert ft.switch(node, ft.WORK, True)
        sweep_and_observe(node, loop)
        advance(node, clock, 30)
        receipt = loop.run_pending()
        _facts, _others, refused = ft.ask(node, QUERY)
        print(f"\nFT168-D4: the build after switching on ended {receipt.grants[0].state}; the next request: "
              f"{'served' if refused is None else 'refused'}")
        assert receipt.grants[0].state == "ready" and refused is None
    finally:
        ft.close(node)


# --- FT168-D3, before and after its fix ----------------------------------------------------------------------
# The walk counts at most max(4k, 50) ranked candidates. On 45d6dd02 every refused candidate counted, so with F
# switched-off facts ranked ahead of the messages the first message was lost at F = max(4k, 50) - k + 1 (measured
# with FT168_D3_SCAN=1: 43 at k=8, 49 at k=16, 61 at k=20) and nothing was released from 50, 64 and 80.
# d2310826 walks past a candidate refused because its own ontology is off, without counting it, and ranks
# OFF_ONTOLOGY_PASSES (500) spare candidates for a share with fact consent. The tests below pin both halves: nothing
# is lost where it was, and the spare candidates are a bound, past which the same loss is back.

D3_OLD_ONSET = {8: 43, 16: 49, 20: 61}


class Precondition(RuntimeError):
    """What a D3 test needs to be true before its assertions; never read as the finding itself."""


def must(condition, *detail) -> None:
    if not condition:
        raise Precondition(detail)


def crowded(root, monkeypatch, count: int):
    """`count` projects, each stated in one message of the owner's; the share answers up to k = 20. No fact yet."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = project_names(count)
    node = ft.build(root, [ft.Message(f"At work, I am working on {name}", age=600 + number)
                           for number, name in enumerate(names)], now=now, policy=ft.policy_at(now, max_k=20))
    ft.seed_registry(node)
    node.names, node.stated = names, 0
    return node


def messages_released_while_off(node, facts: int, ks) -> dict:
    """With `facts` work facts in the index (built while the ontology was on) and the ontology then switched off:
    how many messages a question that matches every message and every fact releases, per k."""
    for number in range(node.stated + 1, facts + 1):
        ft.state_fact(node, f"imessage:{number}", "works_on", node.names[number - 1])
    node.stated = max(node.stated, facts)
    must(ft.switch(node, ft.WORK, True) and node.rebuild()[ft.GRANT] == "ready")
    must(fact_members(node) == facts, fact_members(node), facts)
    must(ft.switch(node, ft.WORK, False))
    released = {}
    for k in ks:
        found, others, refused = ft.ask(node, "project", k=k)
        must(refused is None and found == [], refused, len(found))
        released[k] = len(others)
    return released


def messages_released_after_the_build(node, ks) -> dict:
    must(node.rebuild()[ft.GRANT] == "ready" and fact_members(node) == 0)
    return {k: len(ft.ask(node, "project", k=k)[1]) for k in ks}


@pytest.mark.parametrize("k", sorted(D3_OLD_ONSET))
def test_where_the_first_message_was_lost_nothing_is_lost_now(tmp_path, monkeypatch, k):
    """Fails on 45d6dd02 with k - 1 messages at the onset count."""
    onset = D3_OLD_ONSET[k]
    node = crowded(tmp_path / "ft168", monkeypatch, onset + 3)
    try:
        assert messages_released_while_off(node, onset - 1, [k])[k] == k
        assert messages_released_while_off(node, onset, [k])[k] == k
        assert messages_released_while_off(node, onset + 3, [k])[k] == k
        assert messages_released_after_the_build(node, [k])[k] == k
    finally:
        ft.close(node)


def test_the_spare_candidates_are_a_bound_and_past_it_the_loss_is_back(tmp_path, monkeypatch, capsys):
    """FT168-D3, what is left of it. With the spare candidates set to 12 for this test, the first message is lost at
    max(4k, 50) + 12 - k + 1 switched-off facts and nothing is released from max(4k, 50) + 12. By the same arithmetic
    the node's 500 spare candidates put that at 543 and 550 for k=8. On d2310826 they did not reach that far (a rank
    lane kept only its best 200: FT168-D6); since 9efe7ecc they do, measured at node size: 542 facts ahead 8
    messages, 543 seven, 550 none (`test_ft168_fix_attacks.py`, on request)."""
    from topos.permissions_v2 import search_release
    assert search_release.OFF_ONTOLOGY_PASSES == 500 and search_release.CANDIDATE_FLOOR == 50
    monkeypatch.setattr(search_release, "OFF_ONTOLOGY_PASSES", 12)
    node = crowded(tmp_path / "ft168", monkeypatch, 64)
    try:
        measured = {facts: messages_released_while_off(node, facts, [8])[8] for facts in (54, 55, 58, 61, 62, 64)}
        after = messages_released_after_the_build(node, [8])[8]
    finally:
        ft.close(node)
    with capsys.disabled():
        print(f"\nFT168-D3 after the fix, spare candidates set to 12 (k=8): switched-off facts ahead -> messages "
              f"released: {measured}; after the build: {after}")
    assert measured == {54: 8, 55: 7, 58: 4, 61: 1, 62: 0, 64: 0} and after == 8


@pytest.mark.skipif(os.environ.get("FT168_D3_SCAN") != "1", reason="a measurement, 29 index builds: FT168_D3_SCAN=1")
def test_scan_from_36_to_64_switched_off_facts(tmp_path, monkeypatch):
    """One fact at a time. On 45d6dd02 this printed the loss curve min(k, max(4k, 50) - facts); on d2310826 every
    point releases k messages."""
    ks = sorted(D3_OLD_ONSET)
    node = crowded(tmp_path / "ft168", monkeypatch, 64)
    try:
        table = {facts: messages_released_while_off(node, facts, ks) for facts in range(36, 65)}
        after = messages_released_after_the_build(node, ks)
    finally:
        ft.close(node)
    print("\nFT168-D3 scan (facts off: messages released at k=8, 16, 20): "
          + "; ".join(f"{facts}: {[released[k] for k in ks]}" for facts, released in table.items())
          + f". after the build: {[after[k] for k in ks]}. load {ft.load()}")
    assert all(released == after for released in table.values())
