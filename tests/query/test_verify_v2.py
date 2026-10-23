"""verify_claim v2: stage rules, orchestration, deadline, door and payload validation.

Synthetic only: a fake model, a fake retrieval, no database, no network.
"""
import asyncio
import time
from datetime import datetime, timezone

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
    r = {"type": "did_or_made", "not_checkable_because": "", "subject": "owner", "reported_speech": False,
         "claim": "The owner restyled the pinwheel sidebar.", "names": ["pinwheel"], "date_from": "2026-10-06", "date_to": "2026-10-06"}
    return {**r, **over}


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
    assert [e["ref"] for e in shown] == ["journal_entries:r2"] and dropped == {"as_of": 1, "quoting": 1}


def test_supported_with_role_and_both_calls_agreeing():
    out = run([rec(1, "ambient"), rec(2, "authored")], FakeChat(read_reply(), {1: "shows", 2: "shows"}))
    assert out["verdict"] == "supported" and out["cited_roles"] == ["ambient", "authored"]
    assert out["claim"]["kind"] == "did_or_made" and out["usage"]["model_calls"] == 3
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


def test_effort_low_never_calls_a_model():
    out = run([rec(1, "authored")], FakeChat(fail=True), effort="low")
    assert out["verdict"] == "unable" and out["reason"] == "effort_low"


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
def test_a_split_record_is_decided_by_one_call_on_it_alone(alone, verdict, reason):
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
                                                                                  "facts_expanded": 0, "derived": 0})
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


def test_the_list_judge_counts_only_the_numbers_both_calls_name():
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
