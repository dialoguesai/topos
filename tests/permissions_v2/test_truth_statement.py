"""verify-claim effort high through the real signed door: grants, consent, ledger, receipts, signatures, SQLite labels.
The interpreter's reading is scripted (no model runs); `test_truth_interpret.py` covers the interpreter itself."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_calibration_truth import CLAIM, build_node
from topos.features.calibration.store import save
from topos.permissions_v2 import truth_interpret, truth_release, truth_statement
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.registry import parse_disclosure
from topos.permissions_v2.truth_contract import (INTERPRET, INTERPRETATION_TTL_SECONDS, PACK, VERIFY,
                                                 VERIFY_STATEMENT, ConfirmedTruthIntent, TruthIntent, parse_intent)
from topos.principal import THIRD_PARTY, Principal

STATEMENT = {"pack_id": PACK, "statement": "A pot of tea is my everyday drink"}
P = "TOPOS_PERMISSIONS_V2_"


def reading(question_id="drink", value="tea"):
    return truth_interpret.Reading(question_id, value, "model_b", None, {"model_a": 900.0, "model_b": 400.0})


REFUSED = truth_interpret.Reading(None, None, "model_a", "model_none", {"model_a": 900.0})


class Reader:
    """Scripted interpreter: what each statement is read as, and every call."""

    def __init__(self, *results, side_effect=None):
        self.results, self.calls, self.side_effect = list(results), [], side_effect

    def __call__(self, statement):
        self.calls.append(statement)
        if self.side_effect:
            self.side_effect()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch):
    monkeypatch.setattr(truth_interpret, "ALLOWANCE", truth_interpret.Allowance(per_hour=100))
    monkeypatch.setattr(truth_statement, "SHOWN", truth_statement._Shown())


def make_node(tmp_path, monkeypatch, *args, **kwargs):
    node = build_node(tmp_path, monkeypatch, *args, **kwargs)
    monkeypatch.setattr(truth_statement, "time", SimpleNamespace(time=lambda: node.now[0]))
    return node


@pytest.fixture
def node(tmp_path, monkeypatch):
    return make_node(tmp_path, monkeypatch)


def principal(actor="actor-1"):
    return Principal(cls=THIRD_PARTY, channel="cp_relay", acting_user=actor, client_id="client-2")


def call(node, request_type, payload, request_id, reader, actor="actor-1", envelope=None, dispatch_as=None):
    envelope = envelope or node._envelope("grant-search", request_type, payload, request_id)
    result, output, _revision = truth_statement.dispatch(SimpleNamespace(protocol=node.protocol), principal(actor),
        envelope.model_dump(), payload, request_id, request_type=dispatch_as or request_type, read=reader)
    verify_node_result(result, envelope=envelope, output=output, now=node.now[0],
                       trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()})
    return output


def claim(node, payload, request_id, actor="actor-1"):
    envelope = node._envelope("grant-search", VERIFY, payload, request_id)
    result, output, _ = truth_release.dispatch(SimpleNamespace(protocol=node.protocol), principal(actor),
                                               envelope.model_dump(), payload, request_id)
    verify_node_result(result, envelope=envelope, output=output, now=node.now[0],
                       trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()})
    return output


def ledger_rows(node, sql, *args):
    with node.ledger._transaction() as conn:
        return [tuple(row) for row in conn.execute(sql, args).fetchall()]


def receipt(node, request_id):
    rows = ledger_rows(node, "SELECT receipt_json FROM p2a_receipts WHERE request_id=?", request_id)
    return json.loads(rows[0][0]) if rows else None


def status(node, request_id):
    rows = ledger_rows(node, "SELECT status FROM p2a_requests WHERE request_id=?", request_id)
    return rows[0][0] if rows else None


def questions(node):
    return sum(row[0] for row in ledger_rows(node, "SELECT questions FROM p2a_question_days"))


def ledger_text(node):
    with node.ledger._transaction() as conn:
        return "\n".join(conn.iterdump())


@pytest.mark.parametrize("actor", ["actor-1", "owner-1"])
def test_a_statement_is_checked_as_the_claim_it_was_read_as_and_signed_with_it(tmp_path, monkeypatch, actor):
    node = make_node(tmp_path, monkeypatch, actor)
    reader = Reader(reading("drink", "tea"), reading("drink", "coffee"))
    out = call(node, VERIFY_STATEMENT, STATEMENT, "st-1", reader, actor)
    assert out == {"operation": "verify_claim", "view_id": "calibration.truth.v1", "verdict": "supports",
                   "basis": "owner_answer", "interpreted": {"question_id": "drink", "value": "tea", "path": "high",
                                                            "interpreter": truth_interpret.VERSION}}
    other = dict(STATEMENT, statement="Coffee, black, is my everyday drink")
    assert call(node, VERIFY_STATEMENT, other, "st-2", reader, actor)["verdict"] == "contradicts"
    assert reader.calls == [STATEMENT["statement"], other["statement"]]
    assert questions(node) == 2 and status(node, "st-1") == "checkpointed"
    assert receipt(node, "st-1")["interpreted"] == out["interpreted"]
    text = ledger_text(node)
    assert STATEMENT["statement"] not in text and other["statement"] not in text   # never the statement's text
    with sqlite3.connect(node.corpus.path) as c:
        save(c, dict(CLAIM, value=None, expected_revision=1))
    assert call(node, VERIFY_STATEMENT, STATEMENT, "st-3", Reader(reading()), actor)["verdict"] == "unknown"
    with pytest.raises(PolicyError, match="request_replay"):
        call(node, VERIFY_STATEMENT, STATEMENT, "st-3", Reader(reading()), actor)


@pytest.mark.parametrize("request_type", [VERIFY_STATEMENT, INTERPRET])
def test_a_grant_without_pack_consent_never_reaches_the_model(tmp_path, monkeypatch, request_type):
    node = make_node(tmp_path, monkeypatch, consent=False)
    reader = Reader(reading())
    with pytest.raises(PolicyError, match="permission_denied"):
        call(node, request_type, STATEMENT, "nc-1", reader)
    assert reader.calls == [] and status(node, "nc-1") == "refused" and questions(node) == 0


def test_a_statement_not_understood_spends_the_id_and_no_question(node):
    with pytest.raises(truth_statement.NotUnderstood) as refused:
        call(node, VERIFY_STATEMENT, STATEMENT, "nu-1", Reader(REFUSED))
    assert refused.value.reason == "model_none"
    assert status(node, "nu-1") == "refused" and receipt(node, "nu-1") is None and questions(node) == 0
    with pytest.raises(PolicyError, match="request_replay"):
        call(node, VERIFY_STATEMENT, STATEMENT, "nu-1", Reader(reading()))


@pytest.mark.parametrize("reason", sorted(truth_interpret.OPERATIONAL_REASONS))
def test_a_model_that_could_not_answer_is_never_not_understood(node, reason):
    down = truth_interpret.Reading(None, None, "model_a", reason)
    with pytest.raises(truth_statement.Unavailable):
        call(node, VERIFY_STATEMENT, STATEMENT, "un-1", Reader(down))
    assert status(node, "un-1") == "refused" and questions(node) == 0
    for content in ("model_none", "verification_disagreed", "missing_topic_anchor", "negation"):
        assert type(truth_statement.refusal(truth_interpret.Reading(None, None, "x", content))) is truth_statement.NotUnderstood


def test_busy_spends_the_id_and_is_not_a_refusal_of_the_statement(node):
    with pytest.raises(truth_interpret.Busy):
        call(node, VERIFY_STATEMENT, STATEMENT, "busy-1", Reader(truth_interpret.Busy()))
    assert status(node, "busy-1") == "refused" and questions(node) == 0


def test_the_hourly_allowance_counts_only_statements_the_model_reads(node, monkeypatch):
    monkeypatch.setattr(truth_interpret, "ALLOWANCE", truth_interpret.Allowance(per_hour=1))
    guarded = truth_interpret.Reading(None, None, "guard", "negation")
    with pytest.raises(truth_statement.NotUnderstood):
        call(node, VERIFY_STATEMENT, dict(STATEMENT, statement="I never drink coffee"), "al-1", Reader(guarded))
    call(node, VERIFY_STATEMENT, STATEMENT, "al-2", Reader(reading()))
    reader = Reader(reading())
    with pytest.raises(truth_interpret.Busy, match="interpret_allowance"):
        call(node, VERIFY_STATEMENT, STATEMENT, "al-3", reader)
    assert reader.calls == []


def test_each_door_is_bound_to_its_signed_request_type(node):
    envelope = node._envelope("grant-search", VERIFY_STATEMENT, STATEMENT, "bind-1")
    with pytest.raises(PolicyError, match="request_binding"):
        call(node, VERIFY_STATEMENT, STATEMENT, "bind-1", Reader(reading()), envelope=envelope, dispatch_as=INTERPRET)
    with pytest.raises(PolicyError):
        call(node, VERIFY_STATEMENT, dict(STATEMENT, statement="Coffee is my drink"), "bind-2", Reader(reading()),
             envelope=node._envelope("grant-search", VERIFY_STATEMENT, STATEMENT, "bind-2"))
    with pytest.raises(PolicyError):                       # a statement is never a claim
        truth_release.dispatch(SimpleNamespace(protocol=node.protocol), principal(),
            node._envelope("grant-search", VERIFY, STATEMENT, "bind-3").model_dump(), STATEMENT, "bind-3")
    for bad in ({"pack_id": PACK}, {"pack_id": PACK, "statement": ""}, {"pack_id": PACK, "statement": "x" * 281},
                {"pack_id": "another-pack", "statement": "Tea"}, dict(STATEMENT, effort="high")):
        with pytest.raises(PolicyError):
            call(node, VERIFY_STATEMENT, bad, "bind-4", Reader(reading()))


def test_the_signed_outputs_are_closed():
    with pytest.raises(PolicyError):
        parse_disclosure({"operation": "verify_claim", "view_id": "calibration.truth.v1", "verdict": "supports",
                          "basis": "owner_answer", "interpreted": {"question_id": "drink", "value": "lemonade",
                          "path": "high", "interpreter": truth_interpret.VERSION}},
                         capability="permissions-beta/p2c-v3", request_type=VERIFY_STATEMENT)
    with pytest.raises(PolicyError):
        parse_disclosure({"operation": "verify_claim", "view_id": "calibration.truth.v1", "verdict": "supports",
                          "basis": "owner_answer"}, capability="permissions-beta/p2c-v3", request_type=VERIFY_STATEMENT)


def test_a_revocation_while_the_model_reads_releases_nothing(node):
    def revoke():
        with owner():
            node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-mid-read")
    with pytest.raises(PolicyError):
        call(node, VERIFY_STATEMENT, STATEMENT, "rv-1", Reader(reading(), side_effect=revoke))
    assert receipt(node, "rv-1") is None and status(node, "rv-1") in ("refused", "admitted")


def test_interpret_signs_the_reading_and_reads_no_answer(node, monkeypatch):
    monkeypatch.setattr(truth_release, "read", lambda *a, **k: pytest.fail("the confirm step read an answer"))
    out = call(node, INTERPRET, STATEMENT, "in-1", Reader(reading("drink", "tea")))
    assert out["operation"] == "interpret_claim" and out["status"] == "needs_confirmation"
    assert out["interpretation"] == {"pack_id": PACK, "question_id": "drink", "value": "tea",
                                     "proposition": "Your go-to drink is tea."}
    assert out["alternatives"] == [{"value": "coffee", "proposition": "Your go-to drink is coffee."},
                                   {"value": "neither", "proposition": "Your go-to drink is neither coffee nor tea."}]
    assert out["expires_at"] == node.now[0] + INTERPRETATION_TTL_SECONDS and len(out["interpretation_id"]) == 22
    assert out["interpreter"] == truth_interpret.VERSION
    shown = receipt(node, "in-1")
    assert shown["version"] == "topos-local-receipt/interpret-v1" and shown["interpreted"]["value"] == "tea"
    assert out["interpretation_id"] not in ledger_text(node) and STATEMENT["statement"] not in ledger_text(node)
    assert status(node, "in-1") == "interpreted" and questions(node) == 0
    with pytest.raises(truth_statement.NotUnderstood):
        call(node, INTERPRET, STATEMENT, "in-2", Reader(REFUSED))


def test_the_structured_check_says_whether_it_confirmed_what_was_shown(node):
    shown = call(node, INTERPRET, STATEMENT, "cf-1", Reader(reading("drink", "tea")))["interpretation_id"]
    confirmed = dict(CLAIM, interpretation_id=shown)
    assert claim(node, confirmed, "cf-2")["verdict"] == "supports"
    assert receipt(node, "cf-2")["interpretation"] == "confirmed"
    assert claim(node, confirmed, "cf-3")["verdict"] == "supports"          # one shown reading links one check
    assert receipt(node, "cf-3")["interpretation"] == "unlinked"
    shown = call(node, INTERPRET, STATEMENT, "cf-4", Reader(reading("drink", "tea")))["interpretation_id"]
    assert claim(node, dict(CLAIM, value="coffee", interpretation_id=shown), "cf-5")["verdict"] == "contradicts"
    assert receipt(node, "cf-5")["interpretation"] == "corrected"
    shown = call(node, INTERPRET, STATEMENT, "cf-6", Reader(reading("drink", "tea")))["interpretation_id"]
    node.now[0] += INTERPRETATION_TTL_SECONDS
    assert claim(node, dict(CLAIM, interpretation_id=shown), "cf-7")["verdict"] == "supports"
    assert receipt(node, "cf-7")["interpretation"] == "unlinked"
    assert claim(node, dict(CLAIM, interpretation_id="A" * 22), "cf-8")["verdict"] == "supports"
    assert receipt(node, "cf-8")["interpretation"] == "unlinked"
    claim(node, CLAIM, "cf-9")                                               # a three-key claim: as before
    assert "interpretation" not in receipt(node, "cf-9")
    with pytest.raises(PolicyError):
        claim(node, dict(CLAIM, interpretation_id="short"), "cf-10")


def test_a_confirm_link_is_this_grants_and_this_questions():
    shown = truth_statement._Shown()
    intent = TruthIntent.parse(CLAIM)
    for grant, question in (("grant-other", "drink"), ("grant-search", "time")):
        shown.add("i" * 22, grant_id=grant, question_id=question, value="tea" if question == "drink" else "morning",
                  expires_at=200)
        assert shown.take("i" * 22, grant_id="grant-search", intent=intent, now=100) == "unlinked"
    for n in range(truth_statement.SHOWN_MAX + 1):
        shown.add(f"{n:022d}", grant_id="g", question_id="drink", value="tea", expires_at=200)
    assert shown.take(f"{0:022d}", grant_id="g", intent=intent, now=100) == "unlinked"        # the oldest went
    assert shown.take(f"{truth_statement.SHOWN_MAX:022d}", grant_id="g", intent=intent, now=100) == "confirmed"


def test_a_three_key_claim_parses_and_hashes_as_it_always_did():
    assert type(parse_intent(CLAIM)) is TruthIntent and TruthIntent.parse(CLAIM).model_dump() == CLAIM
    linked = parse_intent(dict(CLAIM, interpretation_id="A" * 22))
    assert isinstance(linked, ConfirmedTruthIntent) and linked.model_dump() == dict(CLAIM, interpretation_id="A" * 22)


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


def frame(node, monkeypatch, kind, payload, request_id, reader, *, switch=True):
    runtime = SimpleNamespace(protocol=node.protocol)
    monkeypatch.setattr(truth_statement, "get_runtime", lambda: runtime)
    monkeypatch.setattr(truth_statement, "verify_relay_stamp", lambda message: principal())
    monkeypatch.setattr(truth_interpret, "read", reader)
    monkeypatch.setenv(P + "MESSAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv(P + "TRUTH_INTERPRET_ENABLED", "true" if switch else "false")
    request_type = truth_statement.REQUEST_TYPES[kind]
    envelope = node._envelope("grant-search", request_type, payload, request_id)
    socket = Socket()
    asyncio.run(truth_statement.dispatch_statement(socket, {"id": request_id, "type": kind,
        "payload": {"envelope": envelope.model_dump(), "intent": payload}}))
    assert len(socket.sent) == 1
    return socket.sent[0]


def test_the_socket_answers_a_verdict_a_reading_not_understood_busy_or_the_uniform_refusal(node, monkeypatch):
    ok = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-1", Reader(reading()))
    assert ok["status"] == "ok" and ok["payload"]["output"]["interpreted"]["value"] == "tea"
    shown = frame(node, monkeypatch, truth_statement.INTERPRET_MESSAGE_TYPE, STATEMENT, "ws-2", Reader(reading()))
    assert shown["status"] == "ok" and shown["payload"]["output"]["status"] == "needs_confirmation"
    refused = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-3", Reader(REFUSED))
    assert (refused["status"], refused["code"], refused["error"], refused["refusal_class"]) == (
        "error", 400, "statement_not_understood", "statement_high_model_none")
    assert "payload" not in refused
    busy = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-4",
                 Reader(truth_interpret.Busy()))
    assert (busy["code"], busy["error"]) == (429, "interpret_busy")
    down = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-7",
                 Reader(truth_interpret.Reading(None, None, "model_a", "deadline")))
    assert (down["code"], down["error"], down["refusal_class"]) == (503, "interpret_unavailable", "interpret_deadline")
    reader = Reader(reading())
    off = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-5", reader, switch=False)
    assert (off["code"], off["error"]) == (403, "permission_denied") and reader.calls == []


def test_the_socket_rechecks_the_label_before_sending(node, monkeypatch):
    original = truth_statement.dispatch

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(node.corpus.path) as c:
            save(c, dict(CLAIM, value="coffee", expected_revision=1))
        return result
    monkeypatch.setattr(truth_statement, "dispatch", changed)
    stale = frame(node, monkeypatch, truth_statement.STATEMENT_MESSAGE_TYPE, STATEMENT, "ws-6", Reader(reading()))
    assert (stale["status"], stale["error"]) == ("error", "permission_denied")


@pytest.fixture
def preview(tmp_path, monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers.signal_features import handle_calibration
    from topos.principal import OWNER_APP, reset_principal, set_principal
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(tmp_path / "labels.db")
    apply_all_migrations(conn)
    save(conn, dict(CLAIM, expected_revision=0))
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    monkeypatch.setenv(P + "MESSAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv(P + "TRUTH_INTERPRET_ENABLED", "true")

    def ask(payload, reader, cls=OWNER_APP):
        monkeypatch.setattr(truth_interpret, "read", reader)
        token = set_principal(Principal(cls=cls, channel="cp_relay", acting_user="owner-1", client_id="topos_home_chat"))
        try:
            return asyncio.run(handle_calibration({"id": "p", "type": "calibration_preview",
                                                   "payload": {**payload, "topos_id": "t"}}))
        finally:
            reset_principal(token)
    yield ask
    conn.close()


def test_the_owner_preview_shows_the_reading_or_where_it_was_refused(preview):
    asked = {"statement": STATEMENT["statement"], "effort": "high"}
    reply = preview(asked, Reader(reading()))["payload"]
    assert (reply["verdict"], reply["interpreted"]["value"], reply["diagnostics"]["stage"]) == ("supports", "tea", "model_b")
    assert reply["interpretation"]["proposition"] == "Your go-to drink is tea."
    reply = preview({**asked, "confirm": "required"}, Reader(reading()))["payload"]
    assert reply["status"] == "needs_confirmation" and "verdict" not in reply
    reply = preview(asked, Reader(REFUSED))["payload"]
    assert reply == {"operation": "interpret_claim", "status": "not_understood",
                     "diagnostics": {"stage": "model_a", "reason": "model_none", "proposed": None,
                                     "ms": {"model_a": 900.0}, "interpreter": truth_interpret.VERSION}}
    assert preview(asked, Reader(truth_interpret.Reading(None, None, "model_a", "deadline")))["payload"]["status"] == "unavailable"
    assert preview(asked, Reader(truth_interpret.Busy()))["code"] == 429
    assert preview({"statement": STATEMENT["statement"]}, Reader(reading()))["code"] == 400       # effort named
    assert preview({**asked, "question_id": "drink"}, Reader(reading()))["code"] == 400
    assert preview(asked, Reader(reading()), cls=THIRD_PARTY)["code"] == 403
    structured = preview({"pack_id": PACK, "question_id": "drink", "value": "tea"}, Reader())["payload"]
    assert structured["verdict"] == "supports" and "interpreted" not in structured   # the structured preview as before


def test_the_heartbeat_says_1_only_when_the_doors_are_on_and_the_interpreter_can_run(monkeypatch):
    from topos.engine import registration
    monkeypatch.setenv(P + "MESSAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv(P + "TRUTH_INTERPRET_ENABLED", "true")
    monkeypatch.setattr(truth_interpret, "available", lambda: True)
    assert registration._truth_interpret_version() == truth_interpret.CAPABILITY_VERSION == 1
    monkeypatch.setattr(truth_interpret, "available", lambda: False)
    assert registration._truth_interpret_version() == 0
    monkeypatch.setattr(truth_interpret, "available", lambda: True)
    monkeypatch.setenv(P + "TRUTH_INTERPRET_ENABLED", "false")
    assert registration._truth_interpret_version() == 0
    monkeypatch.setenv(P + "TRUTH_INTERPRET_ENABLED", "true")
    monkeypatch.setenv(P + "MESSAGE_SEARCH_ENABLED", "false")
    assert registration._truth_interpret_version() == 0
    assert truth_interpret.CAPABILITY_FIELD == "permissions_v2_truth_interpret_version"
