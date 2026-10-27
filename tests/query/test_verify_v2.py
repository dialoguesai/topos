"""verify_claim v2: stage rules, orchestration, deadline, door and payload validation.

Synthetic only: a fake model, a fake retrieval, no database, no network.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import verify_v2 as v2
from topos.query.verify_v2_models import ModelUnavailable, Usage

SENTENCE = "I changed it so the pinwheel sidebar looks better."


def claim(**over):
    base = dict(kind="did_or_made", subject="owner", reported_speech=False, text="The owner restyled the pinwheel sidebar.",
                names=["pinwheel"], date_from="2026-10-06", date_to="2026-10-06")
    return v2.Claim(**{**base, **over})


def rec(n, role, day="2026-10-06", table="journal_entries", text=None):
    return {"table": table, "record_id": f"r{n}", "event_at": f"{day}T12:00:00", "role": role, "lanes": ["canonical"],
            "lane": "summaries", "text": text or f"record {n}: pinwheel sidebar restyle"}


class FakeChat:
    name = "fake"

    def __init__(self, read=None, labels=None, delay=0.0, fail=False):
        self.read, self.labels, self.delay, self.fail = read, labels or {}, delay, fail

    def __call__(self, system, user, schema, timeout, usage):
        if self.fail:
            raise ModelUnavailable("http_503")
        time.sleep(self.delay)
        usage.add(10, 2)
        if system in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
            return self.read
        return {"labels": [{"n": n, "label": lab} for n, lab in self.labels.items()]}


def read_reply(**over):
    """A reader reply in the category + claims shape. Claim-level overrides go into the main claim; ``claims`` replaces
    the list; ``type="not_checkable"`` empties it."""
    top = {"category": over.pop("category", "did"), "not_checkable_because": over.pop("not_checkable_because", "")}
    claims = over.pop("claims", None)
    c = {"type": "did_or_made", "subject": "owner", "reported_speech": False, "claim": "The owner restyled the pinwheel sidebar.",
         "names": ["pinwheel"], "date_from": "2026-10-06", "date_to": "2026-10-06", **over}
    if c["type"] == "not_checkable":
        return {**top, "claims": []}
    return {**top, "claims": claims if claims is not None else [c]}


def run(records, chat, **req):
    async def gather(fn):
        return records, {"candidates": len(records), "aggregates_skipped": 0, "facts_expanded": 0, "derived": 0}
    request = v2.ClaimRequest(statement=req.pop("statement", SENTENCE), said_at="2026-10-07T19:47:36-05:00", **req)
    return asyncio.run(v2.verify_owner_claim(request, chat=chat, gather_in_worker=gather, to_thread=asyncio.to_thread))


@pytest.mark.parametrize("text, reason", [("", "empty"), ("x" * 1001, "too_long"), ("Did you change it?", "question"),
                                          ("Yeah, totally.", "fragment"), ("The sidebar is cramped today.", None),
                                          ("I restyled the sidebar last week. Have you seen it?", None)])
def test_gate(text, reason):
    assert v2.gate(text) == reason


def test_names_drop_generic_kinds_of_thing():
    assert v2.clean_names(["the project", "my open source repo", "Pinwheel app", "the Pinwheel"]) == ["Pinwheel app", "Pinwheel"]


@pytest.mark.parametrize("role, kind, subject, ok, why", [
    ("authored", "opinion", "owner", True, "accepted"),
    ("ambient", "opinion", "owner", False, "opinion_not_authored"),
    ("derived", "opinion", "owner", False, "opinion_not_authored"),
    ("ambient", "did_or_made", "owner", True, "accepted"),
    ("observed", "did_or_made", "owner", False, "role_not_allowed"),
    ("addressed", "did_or_made", "owner", False, "addressed_without_authored"),
    ("derived", "setup", "owner", False, "derived_without_authored"),
    ("authored", "did_or_made", "owner", True, "accepted"),
    ("observed", "did_or_made", "someone_else", True, "accepted"),
    ("ambient", "did_or_made", "someone_else", False, "role_not_allowed"),
])
def test_role_rules(role, kind, subject, ok, why):
    c = claim(kind=kind, subject=subject, date_from="", date_to="")
    assert v2.accept(role, None, c, None, False) == (ok, why)


def test_addressed_counts_alongside_authored_and_period_is_enforced():
    c = claim()
    w = v2.window_of(c)
    assert v2.accept("addressed", v2._day("2026-10-06"), c, w, True) == (True, "accepted")
    assert v2.accept("authored", v2._day("2026-09-01"), c, w, False) == (False, "outside_period")


def test_select_evidence_as_of_quote_and_dedupe():
    now = datetime(2026, 10, 8, 0, 47, tzinfo=timezone.utc)
    records = [rec(1, "authored", day="2026-10-20"), rec(2, "authored"), rec(2, "authored"),
               rec(3, "authored", text="note: i changed it so the pinwheel sidebar looks better today")]
    shown, dropped = v2.select_evidence(records, SENTENCE, claim(), now)
    assert [e["ref"] for e in shown] == ["journal_entries:r2"]
    assert dropped == {"as_of": 1, "quoting": 1, "own_record": 0, "same_thread": 0, "occurrence": 0}
    shown, dropped = v2.select_evidence(records, SENTENCE, claim(), now, exclude_refs=["journal_entries:r2"])
    assert shown == [] and dropped["occurrence"] == 1  # an excluded ref only removes; nothing is added or re-ranked


def test_supported_with_role_and_both_calls_agreeing(monkeypatch):
    monkeypatch.setattr(v2, "AGREEMENT", True)
    out = run([rec(1, "ambient"), rec(2, "authored")], FakeChat(read_reply(), {1: "shows", 2: "shows"}))
    assert out["verdict"] == "supported" and out["cited_roles"] == ["ambient", "authored"]
    assert out["claim"]["kind"] == "did_or_made" and out["usage"]["model_calls"] == 3
    monkeypatch.setattr(v2, "AGREEMENT", False)
    out = run([rec(1, "ambient"), rec(2, "authored")], FakeChat(read_reply(), {1: "shows", 2: "shows"}))
    assert out["verdict"] == "supported" and out["usage"]["model_calls"] == 2  # one look per claim
    assert set(out["timings_ms"]) >= {"gate", "read", "retrieve", "judge", "total"}


def test_opinion_backed_only_by_others_is_not_supported():
    out = run([rec(1, "observed"), rec(2, "addressed")], FakeChat(read_reply(type="opinion", date_from="", date_to=""), {1: "shows", 2: "shows"}))
    assert out["verdict"] == "no_evidence" and out["reason"] == "shows_failed_rules"


def test_not_checkable_stops_before_retrieval():
    async def gather(fn):
        raise AssertionError("retrieval must not run")
    request = v2.ClaimRequest(statement="I think it is loading the model now.")
    out = asyncio.run(v2.verify_owner_claim(request, chat=FakeChat(read_reply(type="not_checkable", not_checkable_because="about_the_call_itself")),
                                            gather_in_worker=gather, to_thread=asyncio.to_thread))
    assert out["verdict"] == "unable" and out["reason"] == "not_checkable:about_the_call_itself"


def test_gate_refusal_makes_no_model_call():
    out = run([], FakeChat(fail=True), statement="Did you change it?")
    assert out["verdict"] == "unable" and out["reason"] == "not_checkable:question" and out["usage"]["model_calls"] == 0


def test_model_failure_is_unable_not_a_guess():
    out = run([rec(1, "authored")], FakeChat(fail=True))
    assert out["verdict"] == "unable" and out["reason"] == "model_unavailable:http_503"


def test_deadline_is_unable():
    out = run([rec(1, "authored")], FakeChat(read_reply(), {1: "shows"}, delay=0.4), deadline_s=0.5)
    assert out["verdict"] == "unable" and out["reason"] == "deadline"


def test_effort_low_checks_the_main_claim_with_one_judge_call():
    second = {"type": "did_or_made", "subject": "owner", "reported_speech": False, "claim": "The owner also wrote the docs.",
              "names": ["docs"], "date_from": "", "date_to": "", "as_written": ""}
    reply = read_reply(claims=[read_reply()["claims"][0], second])
    out = run([rec(1, "authored")], FakeChat(reply, {1: "shows"}), effort="low")
    assert out["verdict"] == "supported" and out["effort"] == "low" and out["usage"]["model_calls"] == 2  # read + one order
    assert len(out["claims"]) == 1 and out["stages"]["judge"]["orders"] == 1
    out = run([rec(1, "authored")], FakeChat(reply, {1: "shows"}), effort="high")
    assert out["usage"]["model_calls"] == 3 and len(out["claims"]) == 2 and out["stages"]["judge"]["orders"] == 1  # read + one look per claim


def test_agreement_switch_restores_two_orders_and_the_tie_break_at_effort_high(monkeypatch):
    monkeypatch.setattr(v2, "AGREEMENT", True)
    out = run([rec(1, "authored"), rec(2, "authored")], SplitChat("shows"), effort="high")
    assert out["verdict"] == "supported" and out["stages"]["judge"]["orders"] == 2 and out["stages"]["judge"]["tiebroken"] == 1
    assert out["usage"]["model_calls"] == 4
    monkeypatch.setattr(v2, "AGREEMENT", False)
    out = run([rec(1, "authored"), rec(2, "authored")], SplitChat("shows"), effort="high")
    assert out["stages"]["judge"]["orders"] == 1 and out["stages"]["judge"]["tiebroken"] == 0 and out["usage"]["model_calls"] == 2


def test_no_records_is_no_evidence():
    out = run([], FakeChat(read_reply()))
    assert out["verdict"] == "no_evidence" and out["reason"] == "no_records"


@pytest.mark.parametrize("cls, channel, admitted", [(OWNER_APP, "uds", True), (CP_RELAY, "cp_relay", False),
                                                    (THIRD_PARTY, "local_http", False)])
def test_truth_door_admits_owner_only(cls, channel, admitted, monkeypatch):
    from topos.query.truth_door import truth_door_admits

    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "truth-mirror")
    principal = Principal(cls, channel)
    assert truth_door_admits(principal, "verify_claim_owner") is admitted


@pytest.mark.parametrize("payload, ok", [
    ({"statement": SENTENCE}, True),
    ({"statement": SENTENCE, "context": [{"speaker": "guest", "text": "how is it going?"}], "said_at": "2026-10-07T19:47:36-05:00",
      "speaker": "owner", "effort": "high", "deadline_s": 8}, True),
    ({"statement": 3}, False),
    ({"statement": SENTENCE, "effort": "max"}, False),
    ({"statement": SENTENCE, "deadline_s": 60}, False),
    ({"statement": SENTENCE, "deadline_s": True}, False),
    ({"statement": SENTENCE, "about_others": True}, True),
    ({"statement": SENTENCE, "about_others": "yes"}, False),
    ({"statement": SENTENCE, "context": "turns"}, False),
    ({"statement": SENTENCE, "context": [{"speaker": 1, "text": "x"}]}, False),
    ("not a dict", False),
    ({"statement": SENTENCE, "deadline_s": 30, "read_timeout_s": 20}, True),
    ({"statement": SENTENCE, "deadline_s": 30, "read_timeout_s": 30}, True),     # up to the deadline itself
    ({"statement": SENTENCE, "read_timeout_s": 0.5}, True),
    ({"statement": SENTENCE, "read_timeout_s": 20}, False),                      # past the default 10 s deadline
    ({"statement": SENTENCE, "deadline_s": 5, "read_timeout_s": 8}, False),
    ({"statement": SENTENCE, "read_timeout_s": 0.4}, False),
    ({"statement": SENTENCE, "read_timeout_s": True}, False),
    ({"statement": SENTENCE, "read_timeout_s": "8"}, False),
    ({"statement": SENTENCE, "read_timeout_s": None}, False),
    ({"statement": SENTENCE, "source_kind": "message"}, True),
    ({"statement": SENTENCE, "source_kind": "journal"}, True),
    ({"statement": SENTENCE, "source_kind": "email"}, False),
    ({"statement": SENTENCE, "source_kind": ["message"]}, False),
])
def test_payload_validation(payload, ok):
    from topos.core.handlers.query import _owner_claim_request

    assert (_owner_claim_request(payload) is not None) is ok


def test_handler_refuses_a_non_owner_before_reading_the_payload():
    from topos.core.handlers.query import handle_verify_claim_owner

    token = set_principal(Principal(CP_RELAY, "cp_relay"))
    try:
        out = asyncio.run(handle_verify_claim_owner({"id": "r1", "type": "verify_claim_owner", "payload": "not even valid"}))
    finally:
        reset_principal(token)
    assert out["status"] == "error" and out["error"] == "owner_mode_required"


def test_fact_refs_follow_id_or_record_id_and_legacy_aliases():
    import sqlite3

    class Roles:
        ROLES = ("authored", "addressed", "participated", "observed", "ambient")

        @staticmethod
        def record_role(row, table):
            return "authored" if table == "journal_entries" else "observed"

    conn = sqlite3.connect(":memory:")
    conn.executescript("""CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, source_refs_json TEXT);
        CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT);
        CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, event_at TEXT, content TEXT);
        CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, canonical_table TEXT, record_id TEXT);
        INSERT INTO signal_objects VALUES ('f1', '[{"table":"Grow Journal","record_id":"j1"},
            {"table":"iMessage","kind":"m","record_id":"c1","source_id":"imessage"},{"table":"entity_mentions","record_id":"e-entity"}]');
        INSERT INTO journal_entries VALUES ('j1', '2026-10-06T21:00:00', 'restyled the pinwheel sidebar');
        INSERT INTO conversation_messages VALUES ('c1', '2026-10-06T20:00:00', 'looks great');""")
    ev = v2.NodeEvidence.__new__(v2.NodeEvidence)
    ev.conn, ev.roles = conn, Roles
    out = ev._expand_fact({"record_id": "f1", "fusion_sources": ["facts_store"]})
    assert [(r["table"], r["record_id"], r["role"]) for r in out] == [("journal_entries", "j1", "authored"), ("conversation_messages", "c1", "observed")]


def test_retrieval_query_spells_out_the_period():
    assert v2.retrieval_query(claim()) == "The owner restyled the pinwheel sidebar on 2026-10-06."
    assert v2.retrieval_query(claim(date_from="2026-10-01", date_to="2026-10-03")).endswith("between 2026-10-01 and 2026-10-03.")
    assert v2.retrieval_query(claim(date_from="", date_to="")) == "The owner restyled the pinwheel sidebar."


def test_session_title_reaches_the_judge_line():
    seen = []

    class Recording(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            seen.append(user)
            return super().__call__(system, user, schema, timeout, usage)

    r = {**rec(1, "authored", table="ai_chat_messages"), "context": "pinwheel sidebar polish"}
    out = run([r], Recording(read_reply(), {1: "shows"}))
    assert out["verdict"] == "supported" and any("(session: pinwheel sidebar polish)" in u for u in seen)


def test_retrieval_query_prefers_the_owners_own_wording():
    c = claim(as_written="I restyled the pinwheel sidebar")
    assert v2.retrieval_query(c) == "I restyled the pinwheel sidebar on 2026-10-06."


class SplitChat(FakeChat):
    """Record 1 is "shows" only when it is listed first; on its own it gets ``alone``."""

    def __init__(self, alone):
        super().__init__(read_reply())
        self.alone = alone

    def __call__(self, system, user, schema, timeout, usage):
        if system in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
            return super().__call__(system, user, schema, timeout, usage)
        usage.add(10, 2)
        excerpts = [l for l in user.splitlines() if l.startswith("[")]
        if len(excerpts) == 1:
            return {"labels": [{"n": 1, "label": self.alone}]}
        return {"labels": [{"n": 1, "label": "shows" if excerpts[0].startswith("[1]") else "related"}]}


@pytest.mark.parametrize("alone, verdict, reason", [("shows", "supported", None), ("related", "no_evidence", "judges_disagree")])
def test_a_split_record_is_decided_by_one_call_on_it_alone(alone, verdict, reason, monkeypatch):
    monkeypatch.setattr(v2, "AGREEMENT", True)
    out = run([rec(1, "authored"), rec(2, "authored")], SplitChat(alone))
    assert out["verdict"] == verdict and out.get("reason") == reason
    assert out["usage"]["model_calls"] == 4 and out["stages"]["judge"]["tiebroken"] == (1 if alone == "shows" else 0)


def test_a_missing_dossier_is_not_materialised_when_the_path_must_not_write(monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from topos.features.entities import dossier, edges, linking

    built = []
    monkeypatch.setattr(dossier, "ensure_dossier", lambda conn, entity: built.append(entity["entity_id"]))
    monkeypatch.setattr(dossier, "load_dossier_for_entity", lambda conn, eid: None)
    monkeypatch.setattr(edges, "top_edges", lambda *a, **k: [])
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entity_mentions (entity_id TEXT, canonical_table TEXT, record_id TEXT, surface_text TEXT, "
                 "event_at TEXT, source_id TEXT, created_at TEXT)")
    ask = lambda: linking.entity_context_items(conn, [{"entity_id": "e1", "canonical_name": "Pinwheel"}],
                                               manifest=SimpleNamespace(canonical_tables=["journal_entries"]))
    token = linking.MATERIALIZE_MISSING_DOSSIERS.set(False)
    try:
        ask()
    finally:
        linking.MATERIALIZE_MISSING_DOSSIERS.reset(token)
    assert built == []
    ask()  # every other path keeps materialising
    assert built == ["e1"]


def test_gather_turns_dossier_writes_off_only_for_its_own_retrieval():
    from topos.features.entities.linking import MATERIALIZE_MISSING_DOSSIERS

    seen = []

    class Adapter:
        def retrieve(self, req):
            seen.append(MATERIALIZE_MISSING_DOSSIERS.get())
            return SimpleNamespace(context_packet={})

    from types import SimpleNamespace

    ev = v2.NodeEvidence.__new__(v2.NodeEvidence)
    ev.adapter, ev.manifest, ev._Request, ev._Ledger = Adapter(), None, (lambda **kw: kw), (lambda: None)
    assert ev.gather("q", "", datetime(2026, 10, 8, tzinfo=timezone.utc)) == ([], {"candidates": 0, "aggregates_skipped": 0,
                                                                                  "facts_expanded": 0, "facts_grounded": 0, "derived": 0, "derived_grounded": 0})
    assert seen == [False] and MATERIALIZE_MISSING_DOSSIERS.get() is True


def test_reader_sees_unknown_speakers_as_plain_lines():
    seen = []

    class Recording(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            seen.append(user)
            return super().__call__(system, user, schema, timeout, usage)

    run([], Recording(read_reply()), context=[{"speaker": "", "text": "how is the sidebar?"}, {"speaker": "guest", "text": "nice"}])
    assert "- how is the sidebar?\nguest: nice" in seen[0] and "someone" not in seen[0]


@pytest.mark.parametrize("about_others, verdict, reason", [(False, "unable", "not_checkable:not_about_the_owner"),
                                                           (True, "supported", None)])
def test_claims_about_someone_else_are_checked_only_when_asked(about_others, verdict, reason):
    reply = read_reply(subject="someone_else")
    out = run([rec(1, "observed")], FakeChat(reply, {1: "shows"}), about_others=about_others)
    assert out["verdict"] == verdict and out.get("reason") == reason


def test_the_reader_is_told_the_scope_the_request_asked_for():
    seen = []

    class Recording(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            seen.append((system, schema["properties"]["not_checkable_because"]["enum"]))
            return super().__call__(system, user, schema, timeout, usage)

    run([], Recording(read_reply()))
    run([], Recording(read_reply()), about_others=True)
    (own, own_enum), (others, others_enum) = seen  # one reader call each; no records, no judge
    assert own is v2.READ_SYSTEM and "not_about_the_owner" in own_enum
    assert others is v2.READ_SYSTEM_WITH_OTHERS and "not_about_the_owner" not in others_enum


class LabelChat(FakeChat):
    """A judge that labels only; ``fail`` makes it unavailable."""

    def __init__(self, labels=None, fail=False):
        super().__init__(None, labels, fail=fail)


@pytest.mark.parametrize("first, second, verdict, reason", [
    (LabelChat({1: "shows"}), LabelChat({1: "related"}), "supported", None),         # either judge's agreed pair counts
    (LabelChat({1: "related"}), LabelChat({1: "related"}), "no_evidence", "no_record_shows_it"),
    (LabelChat(fail=True), LabelChat({1: "shows"}), "supported", None),             # one judge down: the other decides
    (LabelChat(fail=True), LabelChat(fail=True), "unable", "model_unavailable:http_503"),
])
def test_two_judges_count_a_record_either_one_shows(first, second, verdict, reason):
    async def gather(fn):
        return [rec(1, "authored")], {"candidates": 1, "aggregates_skipped": 0, "facts_expanded": 0, "derived": 0}
    request = v2.ClaimRequest(statement=SENTENCE, said_at="2026-10-07T19:47:36-05:00")
    out = asyncio.run(v2.verify_owner_claim(request, chat=FakeChat(read_reply()), judges=[first, second], gather_in_worker=gather,
                                            to_thread=asyncio.to_thread))
    assert out["verdict"] == verdict and out.get("reason") == reason
    if verdict != "unable":
        assert out["stages"]["judge"]["judges"] == 2


def test_hosted_setting_adds_a_second_judge_only_when_configured(monkeypatch):
    from topos.query import verify_v2_models as m

    monkeypatch.setattr(m.HostedChat, "__init__", lambda self, model=None, **k: setattr(self, "model", model or m.DEFAULT_HOSTED_MODEL)
                        or setattr(self, "_key", "k"))
    monkeypatch.delenv("TOPOS_VERIFY_V2_MODEL", raising=False)
    monkeypatch.delenv("TOPOS_VERIFY_V2_SECOND_JUDGE", raising=False)
    assert [j.model for j in m.judges_for_setting()] == [m.DEFAULT_HOSTED_MODEL]
    monkeypatch.setenv("TOPOS_VERIFY_V2_SECOND_JUDGE", m.SECOND_JUDGE_MODEL)
    assert [j.model for j in m.judges_for_setting()] == [m.DEFAULT_HOSTED_MODEL, m.SECOND_JUDGE_MODEL]
    monkeypatch.setenv("TOPOS_VERIFY_V2_MODEL", "local")
    assert [type(j).__name__ for j in m.judges_for_setting()] == ["LocalChat"]


def test_verify_names_its_hosted_models_directly():
    from topos.query.verify_v2_models import SECOND_JUDGE_MODEL, HostedChat

    assert HostedChat(model=SECOND_JUDGE_MODEL, api_key="k", base_url="https://example.invalid/v1").model == SECOND_JUDGE_MODEL


class ListChat(FakeChat):
    """A judge that answers with the numbers of the showing excerpts (the endpoint's default reply shape)."""

    def __call__(self, system, user, schema, timeout, usage):
        if system in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
            return super().__call__(system, user, schema, timeout, usage)
        assert system == v2.LIST_SYSTEM and schema is v2.LIST_SCHEMA
        usage.add(10, 2)
        return {"shows": [n for n, lab in self.labels.items() if lab == "shows"]}


def test_the_list_judge_counts_only_the_numbers_both_calls_name(monkeypatch):
    monkeypatch.setattr(v2, "JUDGE_OUTPUT", "list")
    monkeypatch.setattr(v2, "AGREEMENT", True)
    out = run([rec(1, "authored"), rec(2, "authored")], ListChat(read_reply(), {1: "shows", 2: "related"}))
    assert out["verdict"] == "supported" and [e["n"] for e in out["evidence"] if e["cited"]] == [1]
    assert out["usage"]["model_calls"] == 3


def test_a_judge_reply_is_read_in_either_shape():
    assert v2.shows_in({"shows": [2, "3", True, 0.5, None, "x"]}) == {2, 3}
    assert v2.shows_in({"labels": [{"n": 1, "label": "shows"}, {"n": 2, "label": "related"}, "junk"]}) == {1}
    assert v2.shows_in({}) == set()


def test_hosted_asks_for_plain_json_with_a_cap_when_the_schema_says_so(monkeypatch):
    from topos.query import verify_v2_models as m

    sent = []

    def fake_post(url, body, headers, timeout):
        sent.append(body)
        return {"choices": [{"message": {"content": '{"shows": [1]}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}

    monkeypatch.setattr(m, "_post_json", fake_post)
    chat = m.HostedChat(model=m.DEFAULT_HOSTED_MODEL, api_key="k", base_url="https://example.invalid/v1")
    chat._wallet_allows = lambda: True
    u = m.Usage()
    assert chat("s", "u", v2.LIST_SCHEMA, 5.0, u) == {"shows": [1]}
    assert sent[-1]["response_format"] == {"type": "json_object"} and sent[-1]["max_tokens"] == 64
    assert '"shows"' in sent[-1]["messages"][0]["content"]  # the schema travels in the prompt instead
    chat("s", "u", v2.LABEL_SCHEMA, 5.0, u)
    assert sent[-1]["response_format"]["type"] == "json_schema" and "max_tokens" not in sent[-1]
    assert u.calls == 2 and u.output_tokens == 14


def test_local_caps_the_reply_the_same_way(monkeypatch):
    from topos.query import verify_v2_models as m

    sent = []

    def fake_post(url, body, headers, timeout):
        sent.append(body)
        return {"message": {"content": '{"shows": []}'}, "prompt_eval_count": 5, "eval_count": 4}

    monkeypatch.setattr(m, "_post_json", fake_post)
    chat = m.LocalChat(model="qwen-test", base_url="http://127.0.0.1:1")
    assert chat("s", "u", v2.LIST_SCHEMA, 5.0, m.Usage()) == {"shows": []}
    assert sent[-1]["options"]["num_predict"] == 64 and sent[-1]["format"] == dict(v2.LIST_SCHEMA)
    chat("s", "u", v2.LABEL_SCHEMA, 5.0, m.Usage())
    assert "num_predict" not in sent[-1]["options"]


def test_sharded_judge_labels_every_record_across_parallel_calls(monkeypatch):
    monkeypatch.setattr(v2, "JUDGE_SHARD", 2)
    monkeypatch.setattr(v2, "AGREEMENT", True)
    seen = []

    class Recording(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            if system == v2.LABEL_SYSTEM:
                seen.append([l.split("]")[0][1:] for l in user.splitlines() if l.startswith("[")])
            return super().__call__(system, user, schema, timeout, usage)

    records = [rec(n, "authored") for n in range(1, 6)]
    out = run(records, Recording(read_reply(), {1: "shows", 5: "shows", 3: "related"}))
    assert out["verdict"] == "supported" and [e["n"] for e in out["evidence"] if e["cited"]] == [1, 5]
    assert sorted(seen) == [["1", "2"], ["2", "1"], ["3", "4"], ["4", "3"], ["5"], ["5"]]  # 3 shards, both orders each
    assert out["usage"]["model_calls"] == 7


def test_a_grounded_fact_counts_alone_but_not_for_an_opinion():
    c = claim(kind="did_or_made", date_from="", date_to="")
    assert v2.accept("authored", None, c, None, False, via="fact") == (True, "accepted")
    assert v2.accept("authored", None, claim(kind="opinion", date_from="", date_to=""), None, False, via="fact") == (False, "opinion_not_authored")


def _fact_db():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript("""CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, source_refs_json TEXT);
        CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT);
        CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, event_at TEXT, content TEXT);
        INSERT INTO signal_objects VALUES ('f1', '[{"table":"conversation_messages","record_id":"c1"},{"table":"journal_entries","record_id":"j1"}]');
        INSERT INTO signal_objects VALUES ('f2', '[{"table":"journal_entries","record_id":"missing"}]');
        INSERT INTO journal_entries VALUES ('j1', '2026-10-06T21:00:00', 'restyled the pinwheel sidebar');
        INSERT INTO conversation_messages VALUES ('c1', '2026-10-06T20:00:00', 'looks great');""")
    return conn


@pytest.mark.parametrize("on", [True, False])
def test_a_fact_whose_refs_resolve_is_shown_ahead_of_its_records(monkeypatch, on):
    monkeypatch.setattr(v2, "GROUNDED_DERIVED", False)
    from types import SimpleNamespace

    monkeypatch.setattr(v2, "GROUNDED_FACTS", on)

    class Roles:
        ROLES = ("authored", "addressed", "participated", "observed", "ambient")

        @staticmethod
        def record_role(row, table):
            return "authored" if table == "journal_entries" else "observed"

    items = [{"record_id": "f1", "fusion_sources": ["facts_store"], "summary_text": "Owner restyled the pinwheel sidebar", "event_at": "2026-10-05"},
             {"record_id": "f2", "fusion_sources": ["facts_store"], "summary_text": "Owner likes pinwheels"}]

    class Adapter:
        def retrieve(self, req):
            return SimpleNamespace(context_packet={"summaries": items})

    ev = v2.NodeEvidence.__new__(v2.NodeEvidence)
    ev.conn, ev.roles, ev.adapter, ev.manifest, ev._table_of = _fact_db(), Roles, Adapter(), None, {}
    ev._Request, ev._Ledger = (lambda **kw: kw), (lambda: None)
    records, stats = ev.gather("q", "", datetime(2026, 10, 8, tzinfo=timezone.utc))
    got = [(r["table"], r["record_id"], r["role"], r.get("via") or "") for r in records]
    expected = ([("facts", "f1", "authored", "fact")] if on else []) + [("conversation_messages", "c1", "observed", ""),
                                                                      ("journal_entries", "j1", "authored", ""), ("derived", "f2", "derived", "")]
    assert got == expected and stats["facts_grounded"] == (1 if on else 0) and stats["facts_expanded"] == 1
    if on:
        assert records[0]["event_at"] == "2026-10-05" and records[0]["context"] == "journal_entries"  # the fact's own date; its strongest source


def test_the_judge_sees_a_grounded_fact_as_the_nodes_own():
    seen = []

    class Recording(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            if system == v2.LABEL_SYSTEM:
                seen.append(user)
            return super().__call__(system, user, schema, timeout, usage)

    fact = {**rec(1, "authored", table="facts", text="Owner restyled the pinwheel sidebar"), "via": "fact", "context": "journal_entries"}
    out = run([fact], Recording(read_reply(), {1: "shows"}))
    assert out["verdict"] == "supported" and out["evidence"][0]["via"] == "fact"
    assert "[1] fact from journal_entries, 2026-10-06, derived from a record written by the owner: Owner restyled" in seen[0]


def test_an_agreed_derived_item_counts_under_the_role_its_refs_resolve_to(monkeypatch):
    grounded = {**rec(1, "derived", table="derived", text="Owner restyled the pinwheel sidebar"), "grounded_role": "authored"}
    out = run([grounded], FakeChat(read_reply(), {1: "shows"}))
    assert out["verdict"] == "supported" and out["evidence"][0]["grounded_role"] == "authored"
    monkeypatch.setattr(v2, "GROUNDED_DERIVED", False)
    out = run([grounded], FakeChat(read_reply(), {1: "shows"}))
    assert out["verdict"] == "no_evidence" and out["reason"] == "shows_failed_rules"
    monkeypatch.setattr(v2, "GROUNDED_DERIVED", True)
    unresolved = {**rec(1, "derived", table="derived"), "grounded_role": ""}
    assert run([unresolved], FakeChat(read_reply(), {1: "shows"}))["reason"] == "shows_failed_rules"
    opinion = FakeChat(read_reply(type="opinion"), {1: "shows"})
    out = run([grounded], opinion)
    assert out["verdict"] == "no_evidence" and out["stages"]["judge"]["rejections"] == ["opinion_not_authored"]


@pytest.mark.parametrize("vector", [False, True])
def test_grounding_resolves_signal_object_refs_and_embedding_rows_only_when_asked(monkeypatch, vector):
    from types import SimpleNamespace

    monkeypatch.setattr(v2, "GROUNDED_VECTOR", vector)

    class Roles:
        ROLES = ("authored", "addressed", "participated", "observed", "ambient")

        @staticmethod
        def record_role(row, table):
            return "authored" if table == "journal_entries" else "observed"

    conn = _fact_db()
    conn.executescript("""CREATE TABLE signal_embeddings (embedding_id TEXT, record_id TEXT, record_type TEXT);
        INSERT INTO signal_embeddings VALUES ('e1', 'c1', 'conversation_message');""")
    items = [{"record_id": "f1", "fusion_sources": ["derived_objects"], "summary_text": "a fact via the derived lane", "object_type": "fact"},
             {"record_id": "c1", "fusion_sources": ["vector"], "summary_text": "a vector hit", "retrieval_source": "vector"},
             {"record_id": "nothing", "fusion_sources": ["vector"], "summary_text": "unresolved", "retrieval_source": "vector"}]

    class Adapter:
        def retrieve(self, req):
            return SimpleNamespace(context_packet={"summaries": items})

    ev = v2.NodeEvidence.__new__(v2.NodeEvidence)
    ev.conn, ev.roles, ev.adapter, ev.manifest, ev._table_of = conn, Roles, Adapter(), None, {}
    ev._Request, ev._Ledger = (lambda **kw: kw), (lambda: None)
    records, stats = ev.gather("q", "", datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert [(r["record_id"], r["role"], r["grounded_role"]) for r in records] == [("f1", "derived", "authored"), ("c1", "derived", "observed" if vector else ""),
                                                                                  ("nothing", "derived", "")]
    assert stats["derived"] == 3 and stats["derived_grounded"] == (2 if vector else 1)


def test_the_turn_is_supported_when_any_claim_is():
    main = read_reply()["claims"][0]
    other = {**main, "claim": "The owner painted the fence.", "names": ["fence"]}

    class ByClaim(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            if system in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
                return super().__call__(system, user, schema, timeout, usage)
            usage.add(10, 2)
            return {"labels": [{"n": 1, "label": "shows" if "painted the fence" in user else "related"}]}  # the claim head

    out = run([rec(1, "authored")], ByClaim(read_reply(claims=[main, other])))
    assert out["verdict"] == "supported" and [c["verdict"] for c in out["claims"]] == ["no_evidence", "supported"]
    assert out["claim"]["text"] == main["claim"] and out["category"] == "did" and out["evidence"][0]["cited"]


@pytest.mark.parametrize("category, reason", [("meta", "about_the_call_itself"), ("world", "not_about_the_owner")])
def test_meta_and_world_turns_are_refused_at_the_reader(category, reason):
    out = run([rec(1, "authored")], FakeChat(read_reply(category=category), {1: "shows"}))
    assert out["verdict"] == "unable" and out["reason"] == f"not_checkable:{reason}" and out["category"] == category
    assert out["usage"]["model_calls"] == 1


def test_the_reader_schema_carries_the_categories_and_up_to_three_claims():
    assert v2.READ_SCHEMA["properties"]["category"]["enum"] == list(v2.CATEGORIES)
    assert v2.READ_SCHEMA["properties"]["claims"]["maxItems"] == 3
    assert "not_checkable" not in v2.READ_SCHEMA["properties"]["claims"]["items"]["properties"]["type"]["enum"]


def test_the_judge_reports_each_orders_marks():
    out = v2.judge(claim(), [{**rec(1, "authored"), "n": 1, "ref": "r1", "source": "journal_entries", "date": "2026-10-06",
                              "snippet": "x", "context": "", "origin": "journal_entries", "via": "", "grounded_role": ""}],
                   LabelChat({1: "shows"}), Usage(), 5.0)
    assert out["first_ns"] == [1] and out["second_ns"] == [1] and out["tiebroken_ns"] == [] and out["cites"] == [1]


@pytest.fixture()
def owner_socket_client(monkeypatch):
    """The owner-socket route mounted alone, with the handler replaced by an echo: the route's own wiring is under test."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from topos.api import local_mcp
    from topos.auth import resolve_request_principal

    async def echo(msg, principal=None):
        return {"status": "ok", "payload": {"verdict": "unable", "reason": "effort_low", "type": msg["type"], "got": msg["payload"]}}

    monkeypatch.setattr(local_mcp, "handle_control_plane_request", echo)
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "truth-mirror")
    app = FastAPI()
    app.include_router(local_mcp.router)

    def _as(principal):
        app.dependency_overrides[resolve_request_principal] = lambda: principal
        return TestClient(app)

    return _as


def test_the_owner_socket_route_reads_the_body_and_answers(owner_socket_client):
    """Regression: the route's Request and Response annotations were unimported, so FastAPI read them as required
    query parameters and answered 422 to the owner (found 23 Oct 2026 by the truth-lane session)."""
    client = owner_socket_client(Principal(OWNER_APP, "uds"))
    body = {"statement": SENTENCE, "effort": "low", "deadline_s": 5}
    r = client.post("/api/local/verify_claim_owner", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "verify_claim_owner" and r.json()["got"] == body
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Request-ID"]
    bad = client.post("/api/local/verify_claim_owner", content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid_truth_request"


def test_the_owner_socket_route_refuses_a_relay_before_reading_the_body(owner_socket_client):
    client = owner_socket_client(Principal(CP_RELAY, "cp_relay"))
    r = client.post("/api/local/verify_claim_owner", json={"statement": SENTENCE})
    assert r.status_code == 403 and SENTENCE not in r.text


# --- the reader's own timeout (per request) -----------------------------------------------------------------------------
class TimedChat(FakeChat):
    """Records the timeout and system prompt each reader call gets."""

    def __init__(self, read):
        super().__init__(read)
        self.reads = []

    def __call__(self, system, user, schema, timeout, usage):
        if "not_checkable_because" in schema.get("properties", {}):
            self.reads.append({"timeout": timeout, "system": system})
            usage.add(10, 2)
            return self.read
        return super().__call__(system, user, schema, timeout, usage)


def test_the_default_read_timeout_and_source_kind_are_unchanged_for_interactive_callers():
    from topos.core.handlers.query import _owner_claim_request

    assert v2.READ_TIMEOUT_S == 8.0
    request = v2.ClaimRequest(statement=SENTENCE)
    assert request.read_timeout_s == v2.READ_TIMEOUT_S and request.source_kind == "spoken"
    door = _owner_claim_request({"statement": SENTENCE, "effort": "low", "deadline_s": 30})
    assert door.read_timeout_s == v2.READ_TIMEOUT_S and door.source_kind == "spoken"
    chat = TimedChat(read_reply(type="not_checkable", not_checkable_because="too_vague"))
    run([], chat, deadline_s=30)
    [read] = chat.reads
    assert 7.5 < read["timeout"] <= 8.0 and read["system"] is v2.READ_SYSTEM


@pytest.mark.parametrize("read_timeout, deadline, low, high", [(20.0, 30.0, 19.5, 20.0), (20.0, 5.0, 4.5, 5.0),
                                                                (0.2, 30.0, 0.45, 0.5)])
def test_a_requests_read_timeout_reaches_the_reader_inside_its_deadline(read_timeout, deadline, low, high):
    chat = TimedChat(read_reply(type="not_checkable", not_checkable_because="too_vague"))
    run([], chat, deadline_s=deadline, read_timeout_s=read_timeout)
    [read] = chat.reads
    assert low < read["timeout"] <= high


def test_an_out_of_range_read_timeout_is_refused_before_any_model_call(monkeypatch):
    from topos.core.handlers import query as handlers
    from topos.query import verify_v2_models

    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "truth-mirror")
    monkeypatch.setattr(verify_v2_models, "chat_for_setting", lambda *a, **k: pytest.fail("no model may be built"))
    token = set_principal(Principal(OWNER_APP, "uds"))
    try:
        for payload in ({"statement": SENTENCE, "deadline_s": 10, "read_timeout_s": 11},
                        {"statement": SENTENCE, "read_timeout_s": 0.1},
                        {"statement": SENTENCE, "source_kind": "email"}):
            out = asyncio.run(handlers.handle_verify_claim_owner({"id": "r1", "type": "verify_claim_owner", "payload": payload}))
            assert out == {"id": "r1", "status": "error", "error": "invalid_truth_request"}
    finally:
        reset_principal(token)


# --- source-aware refusals ----------------------------------------------------------------------------------------------
def test_the_spoken_reader_prompt_is_the_one_fun_camera_had():
    """The source kinds rephrase "meta" for other kinds only: the spoken prompts are byte for byte 1.5.18's."""
    import hashlib

    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()[:16]  # noqa: E731
    assert digest(v2.read_system(False, "spoken")) == "bf8e9feae13079cc"
    assert digest(v2.read_system(True, "spoken")) == "809dc67ec673933c"
    assert v2.read_system() is v2.READ_SYSTEM and v2.read_system(True) is v2.READ_SYSTEM_WITH_OTHERS
    assert v2.read_system(False, "unknown") is v2.READ_SYSTEM  # an unknown kind reads as spoken


@pytest.mark.parametrize("kind, says, never", [
    ("spoken", ["only about the call, the screen or this conversation itself",
                "only a remark about the call itself happening right now"], []),
    ("message", ["a message someone sent in a chat thread", "a reaction, a greeting", "the logistics of the chat",
                 'the message is "meta"'], ["the call", "the screen"]),
    ("transcript", ["a recorded conversation", "only about the recording, the room or this conversation itself"],
     ["the call", "the screen"]),
    ("journal", ["wrote in their journal", "only about the act of writing this entry"],
     ["the call", "the screen", "this conversation"]),
])
def test_the_reader_is_told_what_meta_means_for_the_source_kind(kind, says, never):
    for about_others in (False, True):
        chat = TimedChat(read_reply(category="meta"))
        out = run([], chat, source_kind=kind, about_others=about_others)
        [read] = chat.reads
        assert read["system"] == v2.read_system(about_others, kind)
        meta_line = next(line for line in read["system"].splitlines() if line.startswith('- "meta"'))
        for phrase in says:
            assert phrase in read["system"], phrase
        for phrase in never:
            assert phrase not in meta_line, phrase
        # The enum values do not move: the reply contract and the control plane need nothing.
        assert out["category"] == "meta" and out["reason"] == "not_checkable:about_the_call_itself"


def test_the_reader_schema_is_the_same_for_every_source_kind():
    seen = []

    class Schemas(TimedChat):
        def __call__(self, system, user, schema, timeout, usage):
            seen.append(schema)
            return super().__call__(system, user, schema, timeout, usage)

    for kind in v2.SOURCE_KINDS:
        run([], Schemas(read_reply(type="not_checkable", not_checkable_because="too_vague")), source_kind=kind)
    assert all(schema is v2.READ_SCHEMA for schema in seen) and len(seen) == len(v2.SOURCE_KINDS)
    assert "about_the_call_itself" in v2.WHY_NOT and "meta" in v2.CATEGORIES


def test_the_engine_sha16_is_the_digest_the_weekly_lock_captures():
    """The lock (eval/weekly_lock.py) hashes the same four-file manifest of the installed package; rows carry the same.
    The recipe is pinned here byte for byte (path, NUL, bytes, NUL, in manifest order) because the lock tool copies
    it rather than importing it."""
    import hashlib
    from pathlib import Path

    root = Path(v2.__file__).resolve().parents[1]
    assert v2.ENGINE_MANIFEST == ("query/verify_v2.py", "query/verify_v2_models.py", "query/truth_checks.py", "ingestion/truth_inbound.py")
    digest = hashlib.sha256()
    for relative in v2.ENGINE_MANIFEST:
        digest.update(relative.encode() + b"\0" + root.joinpath(*relative.split("/")).read_bytes() + b"\0")
    assert v2.engine_sha16() == digest.hexdigest()[:16] == v2.manifest_sha16(str(root))
    assert len(v2.engine_sha16()) == 16 and v2.engine_sha16() is v2.engine_sha16()
    with open(v2.__file__, "rb") as source:  # no longer the one-file digest: a companion's change moves the stamp
        assert v2.engine_sha16() != hashlib.sha256(source.read()).hexdigest()[:16]
    assert v2.manifest_sha16("/nonexistent") == ""


# --- VERIFY_V2_READ_POLARITY and the stored reading (lean v3 increment B) ------------------------------------------------
#: Twenty propositions as the reader words them for a statement and for its negation alike ("I use Pinwheel every day"
#: and "I never use Pinwheel" both read as the first), with the names the reader gives; polarity is the only difference.
NEGATION_PAIRS = [
    ("The owner uses Pinwheel every day.", ["Pinwheel"]), ("The owner finished the Pinwheel sidebar.", ["Pinwheel", "sidebar"]),
    ("The owner drinks coffee in the morning.", ["coffee"]), ("The owner runs on Sundays.", ["running"]),
    ("The owner likes the new keyboard.", ["keyboard"]), ("The owner read the Orbit paper.", ["Orbit"]),
    ("The owner pushed the Orbit fix.", ["Orbit"]), ("The owner owns a Fender bass.", ["Fender"]),
    ("The owner uses tabs in Python.", ["Python", "tabs"]), ("The owner speaks French at home.", ["French"]),
    ("The owner called Robin on Monday.", ["Robin"]), ("The owner backs up the Vault nightly.", ["Vault"]),
    ("The owner eats cilantro.", ["cilantro"]), ("The owner pays for Orbit.", ["Orbit"]),
    ("The owner finished the shed roof.", ["shed"]), ("The owner keeps a journal.", ["journal"]),
    ("The owner uses Signal with Robin.", ["Signal", "Robin"]), ("The owner watered the Monstera.", ["Monstera"]),
    ("The owner merged the Pinwheel branch.", ["Pinwheel"]), ("The owner likes vinyl records.", ["vinyl"]),
]


def _read_claim(text, names, polarity):
    return v2.Claim(kind="did_or_made", subject="owner", reported_speech=False, text=text, names=names, date_from="", date_to="",
                    polarity=polarity)


def test_the_claim_key_is_polarity_blind_and_the_pair_groups_apart():
    """Twenty propositions, each read once affirmed and once denied: the pair shares one key and differs in polarity, so
    (key, polarity) makes forty groups and zero shared ones. Different claims about one named thing keep different
    keys; negation words never enter the key; a bare number is not a name."""
    groups, keys = set(), set()
    for text, names in NEGATION_PAIRS:
        key_yes = v2.claim_key("did", _read_claim(text, names, 1))
        key_no = v2.claim_key("did", _read_claim(text, names, -1))
        assert key_yes == key_no, text
        keys.add(key_yes)
        groups.add((key_yes, 1))
        groups.add((key_no, -1))
    assert len(keys) == 20 and len(groups) == 40  # three Orbit claims and three Pinwheel claims stay apart
    assert v2.claim_key("did", _read_claim("The owner uses Pinwheel every day.", ["Pinwheel"], 1)) == "did|did_or_made|none|day every pinwheel"
    # no names: the first three distinctive words, minus negation cues, lowercased; the key never sees the negation
    assert v2.claim_key("did", _read_claim("I never pushed the fix.", [], -1)) == v2.claim_key("did", _read_claim("I pushed the fix.", [], 1))
    assert v2.claim_key("did", _read_claim("I never pushed the fix.", [], -1)) == "did|did_or_made|none|fix pushed"
    # cardinal-blind, sorted, lowercased; the window class is part of the key
    five, eight = _read_claim("My team has 5 people.", ["Team", "5"], 1), _read_claim("My team has 8 people.", ["5", "team"], 1)
    assert v2.claim_key("count", five) == v2.claim_key("count", eight) == "count|did_or_made|none|people team"
    dated = v2.Claim(kind="did_or_made", subject="owner", reported_speech=False, text="x", names=["Orbit"], date_from="2026-10-06", date_to="2026-10-06")
    assert v2.window_class(dated) == "day" and v2.window_class(v2.replace(dated, date_to="2026-10-08")) == "range"
    assert v2.window_class(v2.replace(dated, date_to="")) == "open" and v2.claim_key("did", dated) == "did|did_or_made|day|orbit"
    assert v2.reading_key({"category": "did", "claims": [v2.claim_dict(dated)]}) == ("did|did_or_made|day|orbit", None)
    assert v2.reading_key({"claims": []}) == (None, None) and v2.reading_key(None) == (None, None)


def test_read_polarity_adds_one_field_behind_its_switch(monkeypatch):
    req = v2.ClaimRequest(statement=SENTENCE)
    assert v2.read_schema_for(req) is v2.READ_SCHEMA and "polarity" not in v2.READ_SCHEMA["properties"]["claims"]["items"]["properties"]
    assert v2.read_system(False, "spoken") == v2.READ_SYSTEM and '"polarity"' not in v2.READ_SYSTEM
    monkeypatch.setattr(v2, "READ_POLARITY", True)
    schema = v2.read_schema_for(req)
    assert schema["properties"]["claims"]["items"]["properties"]["polarity"] == {"type": "string", "enum": ["affirms", "denies"]}
    assert "polarity" in schema["properties"]["claims"]["items"]["required"] and schema["properties"]["claims"]["maxItems"] == v2.MAX_CLAIMS
    system = v2.read_system(False, "spoken")
    assert system != v2.READ_SYSTEM and '"polarity": "affirms"' in system and system.replace(v2._POLARITY_LINE, "") == v2.READ_SYSTEM
    monkeypatch.setattr(v2, "LEAN_READ", True)
    lean = v2.read_schema_for(v2.ClaimRequest(statement=SENTENCE, effort="low"))
    assert lean["properties"]["claims"]["maxItems"] == 1 and "polarity" in lean["properties"]["claims"]["items"]["required"]
    assert v2._claim_from({**read_reply()["claims"][0], "polarity": "denies"}, "").polarity == -1
    assert v2._claim_from({**read_reply()["claims"][0], "polarity": "affirms"}, "").polarity == 1
    assert v2._claim_from(read_reply()["claims"][0], "").polarity is None


class PolarityChat(FakeChat):
    """FakeChat that recognises the reader's prompt with the polarity line on."""

    def __call__(self, system, user, schema, timeout, usage):
        if system.startswith("You prepare"):
            usage.add(10, 2)
            return self.read
        return super().__call__(system, user, schema, timeout, usage)


def test_the_answer_carries_the_reading_and_a_stored_reading_makes_no_reader_call(monkeypatch):
    monkeypatch.setattr(v2, "READ_POLARITY", True)
    out = run([rec(1, "authored")], PolarityChat(read_reply(polarity="denies"), {1: "shows"}))
    assert out["verdict"] == "supported" and out["claim"]["polarity"] == -1 and out["usage"]["model_calls"] == 2
    reading = out["reading"]
    assert reading["category"] == "did" and [c["polarity"] for c in reading["claims"]] == ["denies"]
    assert set(reading["claims"][0]) == {"type", "subject", "reported_speech", "claim", "names", "date_from", "date_to", "as_written", "polarity"}

    class NoReader(FakeChat):
        def __call__(self, system, user, schema, timeout, usage):
            assert not system.startswith("You prepare"), "a stored reading must make no reader call"
            return super().__call__(system, user, schema, timeout, usage)

    again = run([rec(1, "authored")], NoReader(None, {1: "shows"}), reading=reading)
    assert again["verdict"] == "supported" and again["usage"]["model_calls"] == 1 and again["stages"]["read"] == {"claims": 1, "stored": True}
    assert again["reading"] == reading and again["claim"]["polarity"] == -1 and again["claim"]["kind"] == "did_or_made"


def test_the_reading_survives_a_deadline_right_after_the_read():
    """The lane stores the reading off any answer, so a deadline that lands after the read still leaves nothing to
    read twice."""
    out = run([rec(1, "authored")], FakeChat(read_reply(), {1: "shows"}, delay=0.35), deadline_s=1.2)
    assert out["verdict"] == "unable" and out["reason"] == "deadline"
    assert out["reading"]["category"] == "did" and len(out["reading"]["claims"]) == 1 and out["claim"]["kind"] == "did_or_made"


# --- VERIFY_V2_READ_INTENTIONS: plans (lean v3 increment C) -------------------------------------------------------------
PLAN_SAID = "2026-10-07T19:47:36-05:00"


def plan_reply(**over):
    base = dict(type="intention", claim="The owner will finish the shed roof by Wednesday.", names=["shed"],
                date_from="2026-10-08", date_to="2026-10-08", polarity="affirms", category="plan")
    return read_reply(**{**base, **over})


def test_read_intentions_is_a_fourth_type_behind_its_switch(monkeypatch):
    assert v2.READ_INTENTIONS is False and v2.INTENTION == "intention" and v2.INTENTION not in v2.KINDS
    assert v2.read_system(False, "spoken") == v2.READ_SYSTEM and "only a future plan or intention" in v2.READ_SYSTEM
    assert "intention" not in v2.read_schema_for(v2.ClaimRequest(statement=SENTENCE))["properties"]["claims"]["items"]["properties"]["type"]["enum"]
    monkeypatch.setattr(v2, "READ_POLARITY", True)
    monkeypatch.setattr(v2, "READ_INTENTIONS", True)
    system = v2.read_system(False, "spoken")
    assert '"intention": something the speaker says they will do' in system and "only a future plan or intention" not in system
    assert "a plan with no time at all" in system and v2._POLARITY_LINE in system
    enum = v2.read_schema_for(v2.ClaimRequest(statement=SENTENCE))["properties"]["claims"]["items"]["properties"]["type"]["enum"]
    assert enum == ["did_or_made", "setup", "opinion", "intention"]
    # a plan with no time can never come due: the reader's reply is refused as plan_without_window
    windowless = plan_reply(date_from="", date_to="")
    chat = PolarityChat(windowless)
    category, claims, why = v2.read_claims(v2.ClaimRequest(statement="I'll finish the shed roof.", said_at=PLAN_SAID), chat, Usage(), 5.0)
    assert (category, claims, why) == ("plan", [], "plan_without_window")
    _, claims, why = v2.read_claims(v2.ClaimRequest(statement="I'll finish the shed roof by Wednesday.", said_at=PLAN_SAID), PolarityChat(plan_reply()), Usage(), 5.0)
    assert why is None and claims[0].kind == "intention" and claims[0].polarity == 1 and claims[0].date_to == "2026-10-08"
    assert v2._claim_from(plan_reply()["claims"][0], "").kind == "intention"  # a stored plan is read back whatever the switch says


def test_a_plans_due_moment_is_the_end_of_its_last_day_in_the_speakers_own_zone():
    claim = v2._claim_from(plan_reply()["claims"][0], "")
    due = v2.due_instant(claim, PLAN_SAID)
    assert due == datetime(2026, 10, 9, 4, 59, 59, tzinfo=timezone.utc)  # 2026-10-08 23:59:59 at -05:00
    assert v2.due_instant(v2.replace(claim, date_to="", date_from="2026-10-10"), PLAN_SAID) == datetime(2026, 10, 11, 4, 59, 59, tzinfo=timezone.utc)
    assert v2.due_instant(v2.replace(claim, date_to="", date_from=""), PLAN_SAID) is None
    assert v2.due_instant(v2.replace(claim, kind="did_or_made"), PLAN_SAID) is None
    assert v2.due_at_for({"category": "plan", "claims": [v2.claim_dict(claim)]}, PLAN_SAID) == "2026-10-09T04:59:59+00:00"
    assert v2.due_at_for({"category": "did", "claims": [v2.claim_dict(v2.replace(claim, kind="did_or_made"))]}, PLAN_SAID) is None
    assert v2.said_zone("2026-10-07T19:47:36Z") == timezone.utc and v2.said_zone(None) is not None


def test_a_plan_waits_until_its_window_and_the_grace_have_passed_then_is_checked_from_the_stored_reading():
    """The R-11 fixture: a plan said day 0 in thread T, the record that shows it done on day +2 in the SAME thread, due
    day +1, grace 7 days. Said: no retrieval, no judge, "waiting_until" the due moment. Checked as of that moment from
    the stored reading: the record is cited (the same-thread window is two-sided around the statement, so a reply two
    days later counts), one model call (the judge), and a record dated before the plan was said never shows it."""
    done = {**rec(1, "authored", day="2026-10-09", table="conversation_messages", text="Shed roof finished and painted."), "conversation_id": "T"}
    before = {**rec(2, "authored", day="2026-10-06", table="journal_entries", text="Shed roof finished last week."), "conversation_id": "J"}
    request = dict(statement="I'll finish the shed roof by Wednesday.", conversation_id="T", source_ref="conversation_messages:own")  # run() says it at PLAN_SAID
    chat = PolarityChat(plan_reply(), {1: "shows", 2: "shows"})

    said = run([done], chat, **request)
    assert said["verdict"] == "unable" and said["reason"] == "waiting_until:2026-10-16T04:59:59+00:00"
    assert said["usage"]["model_calls"] == 1 and said["stages"]["retrieve"] == "waiting"  # the reader only; retrieval never ran
    assert said["reading"]["claims"][0]["type"] == "intention" and said["claim"]["kind"] == "intention"
    reading = said["reading"]

    early = run([done], chat, reading=reading, as_of="2026-10-16T04:59:58+00:00", **request)
    assert early["verdict"] == "unable" and early["reason"].startswith("waiting_until:") and early["usage"]["model_calls"] == 0

    due = run([done, before], chat, reading=reading, as_of="2026-10-16T05:00:00+00:00", **request)
    assert due["verdict"] == "supported" and due["usage"]["model_calls"] == 1  # the judge; no reader call
    assert [e["ref"] for e in due["evidence"] if e["cited"]] == ["conversation_messages:r1"]
    assert due["stages"]["retrieve"]["dropped_same_thread"] == 0 and due["stages"]["retrieve"]["dropped_as_of"] == 0
    assert due["claims"][0]["period"]["from"] == "2026-10-08" and due["claims"][0]["period"]["to"] >= "2026-10-15"  # the rules' window starts a day earlier

    silence = run([before], chat, reading=reading, as_of="2026-10-16T05:00:00+00:00", **request)
    assert silence["verdict"] == "no_evidence" and silence["reason"] == "shows_failed_rules"  # a week-old record is outside the plan's period
    assert silence["stages"]["judge"]["rejections"]


def test_the_same_thread_window_is_two_sided_around_the_statement():
    said = datetime(2026, 10, 7, 19, 47, tzinfo=timezone.utc)
    now = said + timedelta(days=9)
    same = lambda n, when: {**rec(n, "authored", table="conversation_messages", text=f"record {n}: the roof"), "event_at": when.isoformat(), "conversation_id": "T"}  # noqa: E731
    records = [same(1, said + timedelta(days=2)), same(2, said + timedelta(hours=3)), same(3, said - timedelta(hours=3)),
               same(4, said - timedelta(days=2)), {**same(5, said + timedelta(hours=1)), "conversation_id": "other"}]
    shown, dropped = v2.select_evidence(records, "I'll fix the roof by Wednesday.", claim(date_from="", date_to=""), now, conversation_id="T", said_at=said)
    assert [e["ref"] for e in shown] == ["conversation_messages:r1", "conversation_messages:r4", "conversation_messages:r5"]
    assert dropped["same_thread"] == 2 and dropped["as_of"] == 0
    # with no said_at the window is around now, as before: anything after now is out as of, and the thread's last 12 h too
    shown, dropped = v2.select_evidence(records, "I'll fix the roof by Wednesday.", claim(date_from="", date_to=""), said, conversation_id="T")
    assert [e["ref"] for e in shown] == ["conversation_messages:r4"] and dropped == {"as_of": 3, "quoting": 0, "own_record": 0, "same_thread": 1, "occurrence": 0}


def test_one_time_rule_for_every_stored_time():
    """The engine and the store read a stored time by one rule (truth_checks.instant): an offset or Z is honoured, a
    naive time is the node's LOCAL time (the journal writes wall-clock times with no offset), a bare date is not an
    instant. Until 27 Oct 2026 the store read naive as UTC and the engine as local."""
    from topos.query import truth_checks as store

    local_noon = datetime(2026, 10, 22, 12, 0).astimezone(timezone.utc)  # what the node's own zone makes of it
    for value, expected in [("2026-10-22T12:00:00", local_noon), ("2026-10-22T12:00:00Z", datetime(2026, 10, 22, 12, tzinfo=timezone.utc)),
                            ("2026-10-22T12:00:00-05:00", datetime(2026, 10, 22, 17, tzinfo=timezone.utc)),
                            ("2026-10-22T12:00:00+00:00", datetime(2026, 10, 22, 12, tzinfo=timezone.utc)),
                            ("2026-10-22", None), ("", None), (None, None), ("not a time", None)]:
        assert store.instant(value) == expected, value
        assert v2._instant(value) == expected, value
        assert store._aware(value) == expected, value  # the old name reflection.py reads
    assert v2.conversation_time("2026-10-22T12:00:00-05:00") == datetime(2026, 10, 22, 17, tzinfo=timezone.utc)
    assert v2.conversation_time("2026-10-22T12:00:00") == local_noon
    assert abs((v2.conversation_time(None) - datetime.now(timezone.utc)).total_seconds()) < 5
    assert store.with_offset("2026-10-22T12:00:00Z") == datetime(2026, 10, 22, 12, tzinfo=timezone.utc).astimezone().isoformat()


# --- VERIFY_V2_LEAN_READ (tracker round r2) -----------------------------------------------------------------------------
def _reader_schemas(monkeypatch, lean, **req):
    monkeypatch.setattr(v2, "LEAN_READ", lean)
    seen = []

    class Schemas(TimedChat):
        def __call__(self, system, user, schema, timeout, usage):
            if "not_checkable_because" in schema.get("properties", {}):
                seen.append(schema)
            return super().__call__(system, user, schema, timeout, usage)

    run([], Schemas(read_reply(type="not_checkable", not_checkable_because="too_vague")), **req)
    return seen


def test_lean_read_off_asks_every_effort_for_up_to_three_claims(monkeypatch):
    for effort in ("low", "high"):
        assert _reader_schemas(monkeypatch, False, effort=effort) == [v2.READ_SCHEMA]
        assert _reader_schemas(monkeypatch, False, effort=effort)[0] is v2.READ_SCHEMA


def test_lean_read_asks_for_no_more_claims_than_the_effort_checks(monkeypatch):
    [low] = _reader_schemas(monkeypatch, True, effort="low")
    [high] = _reader_schemas(monkeypatch, True, effort="high")
    [others] = _reader_schemas(monkeypatch, True, effort="low", about_others=True)
    assert low["properties"]["claims"]["maxItems"] == v2.EFFORTS["low"]["claims"] == 1
    assert high is v2.READ_SCHEMA and v2.READ_SCHEMA["properties"]["claims"]["maxItems"] == 3  # never mutated
    assert others["properties"]["claims"]["maxItems"] == 1
    assert "not_about_the_owner" not in others["properties"]["not_checkable_because"]["enum"]
    assert "not_about_the_owner" in low["properties"]["not_checkable_because"]["enum"]
    # Only the claim count differs from the full schema.
    assert {**low, "properties": {**low["properties"], "claims": None}} == {**v2.READ_SCHEMA, "properties": {**v2.READ_SCHEMA["properties"], "claims": None}}
    assert low["properties"]["claims"]["items"] == v2.READ_SCHEMA["properties"]["claims"]["items"]


def test_lean_read_keeps_the_answer_for_a_one_claim_reply(monkeypatch):
    outs = []
    for lean in (False, True):
        monkeypatch.setattr(v2, "LEAN_READ", lean)
        outs.append(run([], FakeChat(read_reply()), effort="low"))
    strip = lambda o: {k: v for k, v in o.items() if k not in ("timings_ms",)}
    assert strip(outs[0]) == strip(outs[1])


# --- Calibrate step 1: speaker subject, strict as-of, same thread -----------------------------------------------------
NOW = datetime(2026, 10, 26, 1, 49, tzinfo=timezone.utc)


def at(minutes=0, hours=0, table="conversation_messages", n=1, conversation="c1"):
    when = NOW + timedelta(minutes=minutes, hours=hours)
    return {"table": table, "record_id": f"m{n}", "event_at": when.isoformat(), "role": "authored", "lanes": ["canonical"],
            "lane": "summaries", "text": f"record {n}: pinwheel sidebar restyle", "conversation_id": conversation}


def refs(records, **kw):
    shown, dropped = v2.select_evidence(records, SENTENCE, claim(), NOW, **kw)
    return [e["ref"].split(":")[1] for e in shown], dropped


def test_strict_as_of_drops_a_record_made_after_the_claim_even_the_same_day(monkeypatch):
    later, earlier = at(minutes=3, n=1, conversation="other"), at(minutes=-3, n=2, conversation="other")
    day_only = {**at(n=3, conversation="other"), "event_at": "2026-10-26"}
    next_day = {**at(n=4, conversation="other"), "event_at": "2026-10-27"}
    assert refs([later, earlier, day_only, next_day]) == (["m2", "m3"], {"as_of": 2, "quoting": 0, "own_record": 0, "same_thread": 0, "occurrence": 0})
    monkeypatch.setattr(v2, "STRICT_AS_OF", False)  # 1.5.28: anything up to the next day counted
    assert refs([later, earlier, day_only, next_day])[0] == ["m1", "m2", "m3", "m4"]


def test_the_claims_own_exchange_is_context_not_evidence(monkeypatch):
    records = [at(minutes=-1, n=1), at(hours=-11, n=2), at(hours=-13, n=3), at(minutes=-1, n=4, conversation="c2"),
               at(minutes=-5, n=5)]
    got, dropped = refs(records, source_ref="conversation_messages:m5", conversation_id="c1")
    assert got == ["m3", "m4"] and dropped["same_thread"] == 2 and dropped["own_record"] == 1
    # Without a conversation (spoken, journal) nothing is dropped for its thread.
    assert refs(records)[0] == ["m1", "m2", "m3", "m4", "m5"]
    monkeypatch.setattr(v2, "SAME_THREAD_GUARD", False)
    assert refs(records, source_ref="conversation_messages:m5", conversation_id="c1")[0] == ["m1", "m2", "m3", "m4", "m5"]


def test_a_contacts_first_person_claim_is_someone_else_to_the_evidence_rules(monkeypatch):
    def subjects(**req):
        request = v2.ClaimRequest(statement=SENTENCE, said_at="2026-10-07T19:47:36-05:00", **req)
        _, claims, _ = v2.read_claims(request, FakeChat(read_reply()), Usage(), 5.0)
        return [c.subject for c in claims]

    assert subjects(speaker="a contact", speaker_is_owner=False, about_others=True) == ["someone_else"]
    assert subjects() == ["owner"]  # the owner's own speech is unchanged
    assert subjects(speaker="a contact", speaker_is_owner=False, about_others=False) == []  # and still out of scope
    monkeypatch.setattr(v2, "SPEAKER_SUBJECT", False)
    assert subjects(speaker="a contact", speaker_is_owner=False, about_others=True) == ["owner"]


# --- LABELS_AS_EVIDENCE --------------------------------------------------------------------------------------------------
def _label_row(n, label, text, refs=(), at="2026-10-07T10:00:00+00:00", said="2026-10-06T09:00:00+00:00"):
    import json as _json
    return (f"vc{n}", _json.dumps({"claim": text, "label": label}), _json.dumps(list(refs)), at, said)


LATER = datetime(2026, 10, 8, 0, 47, tzinfo=timezone.utc)


def test_an_earlier_label_sharing_the_claims_words_is_the_owners_own_evidence():
    c = claim(text="The owner restyled the pinwheel sidebar.", names=["pinwheel"])
    rows = [_label_row(1, "true", "I restyled the pinwheel sidebar last month."),
            _label_row(2, "false", "The pinwheel sidebar was never restyled by me."),
            _label_row(3, "true", "I walked the dog."),                          # nothing shared
            _label_row(4, "maybe", "The pinwheel sidebar restyle happened."),     # not a label value
            _label_row(5, "true", "Pinwheel sidebar restyled.", refs=[{"table": "conversation_messages", "record_id": "m9"}])]
    got = v2.labelled_evidence(rows, c, LATER, source_ref="conversation_messages:m9")
    assert [r["record_id"] for r in got] == ["vc1", "vc2"]
    assert got[0]["role"] == "authored" and got[0]["text"].startswith("The owner confirmed this is true: ")
    assert got[1]["text"].startswith("The owner said this is false: ")
    assert all(r["table"] == "verified_claim" and r["event_at"] == "2026-10-06T09:00:00+00:00" for r in got)


def test_a_label_made_after_the_claim_never_counts():
    c = claim()
    assert v2.labelled_evidence([_label_row(1, "true", "I restyled the pinwheel sidebar.", at="2026-10-09T10:00:00+00:00")], c, LATER) == []
    assert len(v2.labelled_evidence([_label_row(1, "true", "I restyled the pinwheel sidebar.")], c, LATER)) == 1


def test_labels_are_read_only_when_the_switch_is_on(monkeypatch):
    seen = []

    class Gather:
        def __init__(self, conn):
            pass

        def gather(self, *args):
            return [], {"candidates": 0}

    monkeypatch.setattr(v2, "NodeEvidence", Gather)
    monkeypatch.setattr(v2, "owner_labels", lambda conn: seen.append(1) or [_label_row(1, "true", "I restyled the pinwheel sidebar.")])

    async def gather_in_worker(fn):
        return fn(None)

    def go():
        request = v2.ClaimRequest(statement=SENTENCE, said_at="2026-10-07T19:47:36-05:00")
        return asyncio.run(v2.verify_owner_claim(request, chat=FakeChat(read_reply(), {1: "shows"}), gather_in_worker=gather_in_worker,
                                                 to_thread=asyncio.to_thread))

    assert go()["verdict"] == "no_evidence" and seen == []
    monkeypatch.setattr(v2, "LABELS_AS_EVIDENCE", True)
    out = go()
    assert seen == [1] and out["verdict"] == "supported" and out["evidence"][0]["source"] == "verified_claim"


# --- evidence techniques: TESTIMONY, JOURNAL_CHANNEL, CONSISTENCY -------------------------------------------------------
def test_testimony_lets_someone_in_the_owners_thread_attest_what_the_owner_did(monkeypatch):
    did, view = claim(), claim(kind="opinion")
    w = v2.window_of(did)
    assert v2.accept("participated", v2._day("2026-10-06"), did, w, False) == (False, "role_not_allowed")
    monkeypatch.setattr(v2, "TESTIMONY", True)
    assert v2.accept("participated", v2._day("2026-10-06"), did, w, False) == (True, "accepted")
    assert v2.accept("participated", v2._day("2026-10-06"), view, w, False) == (False, "opinion_not_authored")  # never a view
    assert v2.accept("observed", v2._day("2026-10-06"), did, w, False) == (False, "role_not_allowed")  # a stranger's message


def test_the_journal_channel_adds_unseen_entries_from_a_journal_only_search(monkeypatch):
    from dataclasses import dataclass as _dc

    @_dc(frozen=True)
    class Manifest:
        canonical_tables: tuple = ("conversation_messages", "journal_entries")
        signal_objects: tuple = ("fact",)

    asked = []

    class Evidence:
        def __init__(self, conn):
            self.manifest = Manifest()

        def gather(self, query, needles, now):
            asked.append(tuple(self.manifest.canonical_tables))
            return [rec(n, "authored") for n in (1, 2, 3, 4, 5)], {}

    monkeypatch.setattr(v2, "NodeEvidence", Evidence)
    found = [rec(1, "authored")]  # retrieval already had r1
    got = v2.journal_records(None, "q", "", datetime(2026, 10, 8, tzinfo=timezone.utc), found)
    assert asked == [("journal_entries",)] and [r["record_id"] for r in got] == ["r2", "r3", "r4"]


def test_consistency_lets_several_own_words_together_back_an_opinion(monkeypatch):
    class Together(FakeChat):
        def __init__(self, together):
            super().__init__(read_reply(type="opinion"), {})
            self.together, self.asked = together, 0

        def __call__(self, system, user, schema, timeout, usage):
            if system == v2.CONSISTENCY_SYSTEM:
                self.asked += 1
                usage.add(10, 2)
                return {"together": self.together}
            return super().__call__(system, user, schema, timeout, usage)

    records = [rec(1, "authored"), rec(2, "authored"), rec(3, "participated")]
    off = Together(True)
    assert run(records, off)["verdict"] == "no_evidence" and off.asked == 0
    monkeypatch.setattr(v2, "CONSISTENCY", True)
    yes = Together(True)
    out = run(records, yes)
    assert out["verdict"] == "supported" and yes.asked == 1
    assert sorted(e["ref"] for e in out["evidence"] if e["cited"]) == ["journal_entries:r1", "journal_entries:r2"]
    assert out["claims"][0]["support"] == "consistent_set"
    no = Together(False)
    assert run(records, no)["verdict"] == "no_evidence" and no.asked == 1
    single = Together(True)
    assert run([rec(1, "authored")], single)["verdict"] == "no_evidence"  # one record is not a set


# --- contradictions, per category -------------------------------------------------------------------------------------
class ContraChat(FakeChat):
    """Labels from {n: label}; asked with the contradiction prompt only when the category is opted in."""

    def __init__(self, labels):
        super().__init__(read_reply(), labels)
        self.systems = []

    def __call__(self, system, user, schema, timeout, usage):
        if system not in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
            self.systems.append(system)
        return super().__call__(system, user, schema, timeout, usage)


def test_contradictions_are_asked_for_only_in_an_opted_in_category(monkeypatch):
    chat = ContraChat({1: "contradicts"})
    out = run([rec(1, "authored")], chat)
    assert out["verdict"] == "no_evidence" and all(s == v2.LABEL_SYSTEM for s in chat.systems)
    monkeypatch.setattr(v2, "CONTRADICTION_CATEGORIES", frozenset({"did"}))
    chat = ContraChat({1: "contradicts"})
    out = run([rec(1, "authored")], chat)
    assert out["verdict"] == "contradicted" and all(s == v2.LABEL_SYSTEM_CONTRA for s in chat.systems)
    assert [e["ref"] for e in out["evidence"] if e.get("contradicts")] == ["journal_entries:r1"]


def test_support_wins_over_contradiction_and_the_rules_still_apply(monkeypatch):
    monkeypatch.setattr(v2, "CONTRADICTION_CATEGORIES", frozenset({"did"}))
    assert run([rec(1, "authored"), rec(2, "authored")], ContraChat({1: "contradicts", 2: "shows"}))["verdict"] == "supported"
    # someone else's message cannot contradict the owner's own claim, as it cannot back it
    assert run([rec(1, "observed")], ContraChat({1: "contradicts"}))["verdict"] == "no_evidence"
    # out of the claim's period: no contradiction
    assert run([rec(1, "authored", day="2026-09-01")], ContraChat({1: "contradicts"}))["verdict"] == "no_evidence"
