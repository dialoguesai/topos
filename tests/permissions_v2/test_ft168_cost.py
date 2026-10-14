"""FT168 item 6: what the ontology switch costs a request.

Two questions. How many times does one request read the owner's registry (`selective_facts.enabled_ontologies`: one
`SELECT` over `pack_registry`) with N fact candidates? And what does a request cost in milliseconds with and without
facts, on a fixture of 100 messages and 20 work facts (120 index members with the ontology on)?

The counts are assertions. The milliseconds are printed with the machine's load average, never asserted: this Mac
runs the owner's node. Invented data only.
"""
from __future__ import annotations

import os
import statistics
import time

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.test_ft168_fix_attacks import Gate, many_names
from tests.permissions_v2.test_ft168_switch_loops import fact_members
from topos.permissions_v2 import selective_facts

PLAIN = "grant-plain"
MESSAGES, FACTS = 100, 20


@pytest.fixture(scope="module")
def hundred(tmp_path_factory):
    """100 work messages, 20 of them each the source of one work fact; a share with fact consent and a share of the
    same messages without. Built once for the file."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(MESSAGES)
    started = time.monotonic()
    node = ft.build(tmp_path_factory.mktemp("ft168"), [ft.Message(f"At work, I am working on {name}", age=600 + number)
                                                         for number, name in enumerate(names)], now=now,
                    policy=ft.policy_at(now, max_k=20))
    ft.activate(node, ft.policy_at(now, grant=PLAIN, ontologies=None, kinds=("message",), max_k=20))
    for number in range(1, FACTS + 1):
        ft.state_fact(node, f"imessage:{number}", "works_on", names[number - 1])
    ft.seed_registry(node)
    node.fixture_s = time.monotonic() - started
    yield node
    ft.close(node)
    monkeypatch.undo()


class Reads:
    """Counts the reads of the owner's registry, wherever this tree asks for it."""

    def __init__(self, monkeypatch):
        self.count = 0
        real = selective_facts.enabled_ontologies

        def counted(conn):
            self.count += 1
            return real(conn)
        monkeypatch.setattr(selective_facts, "enabled_ontologies", counted)

    def during(self, work):
        before = self.count
        result = work()
        return self.count - before, result


def switched(node, on: bool, *, rebuild: bool) -> None:
    assert ft.switch(node, ft.WORK, on) or ft.WORK in ft.switched_on(node) == on
    if rebuild:
        assert set(node.rebuild().values()) == {"ready"}


def test_registry_reads_per_request(hundred, monkeypatch, capsys):
    """One read per fact candidate the walk decides, and one more for the whole walk when it meets a refusal on a
    share with fact consent (the D3 fix asks which ontologies are on, once). A share without fact consent reads the
    registry never. The build reads it once per fact it considers; the owner's count once per fact, and the preview
    once more per fact."""
    node = hundred
    reads = Reads(monkeypatch)
    table = {}
    switched(node, True, rebuild=True)
    assert fact_members(node) == FACTS
    # Ontology on: the 20 facts lead the rank, k of them are decided and released, the walk ends.
    for k in (1, 5, 20):
        count, (facts, _others, refused) = reads.during(lambda: ft.ask(node, "project", k=k))
        assert refused is None and len(facts) == min(k, FACTS)
        table[f"on, k={k}"] = count
        assert count == k
    # Ontology off, the index not yet rebuilt: every one of the 20 is decided (one read each) and refused; the walk
    # asks once which ontologies are on; then messages are released without a read.
    switched(node, False, rebuild=False)
    for k in (1, 5, 20):
        count, (facts, others, refused) = reads.during(lambda: ft.ask(node, "project", k=k))
        assert refused is None and facts == [] and len(others) == k
        table[f"off before the build, k={k}"] = count
        assert count == FACTS + 1
    # A share of the same messages without fact consent.
    count, (_facts, others, refused) = reads.during(lambda: ft.ask(node, "project", k=20, grant=PLAIN))
    assert refused is None and len(others) == 20 and count == 0
    table["no fact consent, k=20"] = count
    # Ontology off and rebuilt: no fact in the index, no read.
    builds, _ = reads.during(lambda: switched(node, False, rebuild=True))
    table["one build of both shares, off"] = builds
    count, (facts, others, refused) = reads.during(lambda: ft.ask(node, "project", k=20))
    assert refused is None and facts == [] and len(others) == 20 and count == 0
    table["off after the build, k=20"] = count
    # The owner's count and preview.
    counted, result = reads.during(lambda: ft.owner_count(node))
    assert result["kinds"]["facts"]["can_share"] == 0
    table["owner's count and preview, off"] = counted
    assert ft.switch(node, ft.WORK, True)
    builds_on, _ = reads.during(lambda: node.rebuild())
    table["one build of both shares, on"] = builds_on
    assert builds == builds_on == FACTS
    counted_on, result = reads.during(lambda: ft.owner_count(node))
    assert result["kinds"]["facts"]["can_share"] == FACTS and counted == counted_on == FACTS
    table["owner's count and preview, on"] = counted_on
    with capsys.disabled():
        print(f"\nFT168 item 6, registry reads ({MESSAGES} messages, {FACTS} facts): {table}")


def one_read_ms(node) -> float:
    import sqlite3
    conn = sqlite3.connect(str(node.corpus.resolver.path))
    try:
        selective_facts.enabled_ontologies(conn)
        started = time.perf_counter()
        for _ in range(2000):
            selective_facts.enabled_ontologies(conn)
        return (time.perf_counter() - started) / 2000 * 1000
    finally:
        conn.close()


def asks(node, gate, *, grant, k, repeats=15) -> dict:
    totals, holds, shape = [], [], None
    for _ in range(repeats):
        gate.holds.clear()
        started = time.perf_counter()
        facts, others, refused = ft.ask(node, "project", k=k, grant=grant)
        totals.append((time.perf_counter() - started) * 1000)
        holds.append(max(gate.holds) * 1000)
        assert refused is None
        shape = (len(facts), len(others))
    return {"ms": round(statistics.median(totals), 1), "fastest": round(min(totals), 1),
            "gate_ms": round(statistics.median(holds), 1), "facts": shape[0], "messages": shape[1]}


def test_milliseconds_per_request_with_and_without_facts(hundred, monkeypatch, capsys):
    """A measurement. k = 10, the same question, 15 requests each; the median, the fastest and the median hold of the
    write gate by the door."""
    node = hundred
    gate = Gate(monkeypatch)
    rows = {}
    started = time.monotonic()
    switched(node, True, rebuild=True)
    build_on = time.monotonic() - started
    assert fact_members(node) == FACTS
    rows["facts on (10 facts released)"] = asks(node, gate, grant=ft.GRANT, k=10)
    rows["same node, share without fact consent"] = asks(node, gate, grant=PLAIN, k=10)
    switched(node, False, rebuild=False)
    rows["switched off, index not rebuilt (20 facts walked past)"] = asks(node, gate, grant=ft.GRANT, k=10)
    started = time.monotonic()
    switched(node, False, rebuild=True)
    build_off = time.monotonic() - started
    rows["switched off, index rebuilt (no fact in it)"] = asks(node, gate, grant=ft.GRANT, k=10)
    started = time.monotonic()
    count_off = ft.owner_count(node)
    count_off_s = time.monotonic() - started
    assert ft.switch(node, ft.WORK, True)
    started = time.monotonic()
    count_on = ft.owner_count(node)
    count_on_s = time.monotonic() - started
    assert count_off["kinds"]["facts"]["can_share"] == 0 and count_on["kinds"]["facts"]["can_share"] == FACTS
    with capsys.disabled():
        print(f"\nFT168 item 6, milliseconds ({MESSAGES} messages, {FACTS} facts, k=10, 15 requests each; fixture "
              f"made in {node.fixture_s:.1f} s):")
        for label, row in rows.items():
            print(f"  {label}: median {row['ms']} ms, fastest {row['fastest']} ms, gate hold {row['gate_ms']} ms, "
                  f"released {row['facts']} facts and {row['messages']} messages")
        print(f"  both shares built: {build_on:.2f} s with the ontology on (120 + 100 members), {build_off:.2f} s "
              f"off (100 + 100); owner's count and preview: {count_on_s * 1000:.0f} ms on, {count_off_s * 1000:.0f} "
              f"ms off; one registry read: {one_read_ms(node) * 1000:.1f} microseconds; load {ft.load()}")


# --- what the owner's recipient feels: one written answer -------------------------------------------------------------

ANSWERS_PLAIN = "grant-plain-answers"


class Retrievals:
    """Times every retrieval the answer door makes (`retrieve_for_answer`), and the longest hold of the write gate
    inside each. One written answer makes three: the ranked one before the writer, the re-check of the items it was
    written from, and that re-check again when the body is fetched."""

    def __init__(self, node, gate: Gate):
        self.calls: list[tuple] = []
        real = node.search.retrieve_for_answer

        def timed(**kwargs):
            gate.holds.clear()
            started = time.perf_counter()
            try:
                return real(**kwargs)
            finally:
                self.calls.append(("recheck" if kwargs.get("record_ids") is not None else "ranked",
                                   time.perf_counter() - started, max(gate.holds, default=0.0)))
        node.search.retrieve_for_answer = timed


def one_answer(node, service, retrievals: Retrievals, grant: str) -> dict:
    """Ask, wait for the writer's thread to end, fetch once. Seconds from the ask to the body in hand."""
    from tests.permissions_v2.message_search_harness import recipient
    from topos.permissions_v2.answer_protocol import ASK, FETCH

    def call(kind, payload):
        signed = node._envelope(grant, kind, payload, node.next_id("ft168-cost-answer"))
        with recipient():
            return (service.submit if kind == ASK else service.fetch)(
                envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)[1]
    retrievals.calls.clear()
    started = time.perf_counter()
    pending = call(ASK, {"question": "project"})
    job = service._jobs[pending["answer_id"]]
    while job.state != "ended":
        if time.perf_counter() - started > 120:
            raise AssertionError("the answer worker did not finish")
        time.sleep(0.005)
    body = call(FETCH, {"answer_id": pending["answer_id"]})
    total = time.perf_counter() - started
    calls = list(retrievals.calls)
    return {"total_s": total, "outcome": body.get("outcome"), "kinds": sorted(r["kind"] for r in body.get("records", [])),
            "retrieval_s": sum(seconds for _what, seconds, _hold in calls),
            "ranked_s": sum(seconds for what, seconds, _hold in calls if what == "ranked"),
            "rechecks": len([1 for what, _s, _h in calls if what == "recheck"]),
            "gate_s": max((hold for _what, _seconds, hold in calls), default=0.0),
            "gate_total_s": sum(hold for _what, _seconds, hold in calls)}


@pytest.mark.skipif(os.environ.get("FT168_BIG") != "1", reason="a measurement on 500 messages and up to "
                    "200 facts (six index builds of 500 to 700 members): FT168_BIG=1")
def test_one_written_answer_on_500_messages_with_0_20_and_200_facts(tmp_path, monkeypatch, capsys):
    """Answers mode (with sources), the writer a fixed sentence, so what is timed is the node's own part: the three
    retrievals and their holds of the write gate. A share with fact consent and the ontology on, beside a share of
    the same 500 messages without fact consent."""
    from tests.permissions_v2.test_answer_release import _service
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    names = many_names(500)
    node = ft.build(tmp_path / "ft168", [ft.Message(f"At work, I am working on {name}", age=600 + number)
                                         for number, name in enumerate(names)], now=now,
                    policy=ft.policy_at(now, answers="with_sources"))
    ft.activate(node, ft.policy_at(now, grant=ANSWERS_PLAIN, answers="with_sources", ontologies=None,
                                   kinds=("message",)))
    ft.seed_registry(node)
    gate = Gate(monkeypatch)
    retrievals = Retrievals(node, gate)
    service = _service(node, "The owner is busy with the project effort [1].")
    stated = 0
    try:
        for facts in (0, 20, 200):
            for number in range(stated + 1, facts + 1):
                ft.state_fact(node, f"imessage:{number}", "works_on", names[number - 1])
            stated = facts
            started = time.monotonic()
            assert set(node.rebuild().values()) == {"ready"} and fact_members(node) == facts
            build_s = time.monotonic() - started
            rows = {}
            for label, grant in (("with fact consent, ontology on", ft.GRANT), ("without fact consent", ANSWERS_PLAIN)):
                runs = [one_answer(node, service, retrievals, grant) for _ in range(3)]
                middle = sorted(runs, key=lambda run: run["total_s"])[1]
                rows[label] = {key: (round(value, 3) if isinstance(value, float) else value)
                               for key, value in middle.items()}
            with capsys.disabled():
                print(f"\nFT168 item 6, one written answer, 500 messages and {facts} facts (both indexes built in "
                      f"{build_s:.1f} s), the middle of 3 runs: {rows}; load {ft.load()}", flush=True)
    finally:
        service.close()
        ft.close(node)
