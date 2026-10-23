"""Kept questions (lane OFF1N): the node keeps a kept answer for its window, and defers a kept ask it cannot serve now.

The control plane keeps a question asked while the share owner's Topos is offline and delivers it later as a normal
ask, with a signed side field, `kept_stamp`, that says until when its asker may come back for the answer. Here:

- the stamp: its grammar, its signed bytes, its binding to one ask, and the transport (an ask without it is the ask of
  before; a fetch never carries it; one that is malformed or does not verify is the uniform refusal);
- the keep window: a kept job's ended body stays for its window (at most fourteen days, never less than an unkept
  job's ten minutes), in memory only, with every fetch-time re-check as before;
- deferred: a kept ask whose retrieval meets a share index not served now ends `deferred` (no body, a counts-only
  receipt the owner's week does not count, its question given back, a fetch that answers `answer_deferred`, so the
  control plane asks again later); an unkept one ends `no_answer`;
- the capability the control plane reads before it delivers a kept question.

Invented data only; no model, network or personal data.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_bl159_answer_delivery import (  # noqa: F401 -- fixtures
    ANSWER, QUESTION, answers_node, first_day, journal_database, rebuild, service,
)
from topos.permissions_v2 import answer_release, answer_transport, kept_stamp as ks, owner_account_words as oaw
from topos.permissions_v2.answer_protocol import ASK, FETCH, AskIntent, verify_node_answer
from topos.permissions_v2.answer_release import AnswerBusy, AnswerDeferred, AnswerService, Job
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public

KEY = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"off1n/kept-stamp/cp").digest())
PUBLIC = KEY.public_key().public_bytes_raw()
OTHER = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"off1n/kept-stamp/other").digest())
KEEP = 2_000_000                     # an invented end of a keep window, in epoch seconds
KQ = hashlib.sha256(b"off1n/kept-question/one").hexdigest()[:32]   # an invented kept question's opaque id
LOGGER = "topos.permissions_v2.answer_release"
DAY = 86_400


def _stamp(keep_until=KEEP, msg_id="req-1", signer=KEY, kq=KQ):
    return ks.sign_stamp(keep_until, kq, msg_id=msg_id, msg_type=ks.MESSAGE_TYPE, signer=signer)


def _raw_stamp(*, keep_until=KEEP, kq=KQ, msg_id="req-1", msg_type=ks.MESSAGE_TYPE, signer=KEY):
    """A stamp signed over exactly these values, past the module's own checks: what a wrong signer would send."""
    text = json.dumps({"v": 1, "keep_until": keep_until, "kq": kq, "msg_id": msg_id, "msg_type": msg_type},
                      sort_keys=True, separators=(",", ":"))
    signature = signer.sign(ks.DOMAIN + text.encode("ascii"))
    return {"v": 1, "keep_until": keep_until, "kq": kq,
            "sig": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}


# --- the stamp --------------------------------------------------------------------------------------------------------

def test_the_stamp_verifies_for_its_own_ask_only():
    stamp = _stamp()
    assert set(stamp) == {"v", "keep_until", "kq", "sig"} and (stamp["v"], stamp["keep_until"], stamp["kq"]) == (
        1, KEEP, KQ)
    assert ks.verify_stamp(stamp, msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC) == (KEEP, KQ)
    for kwargs in ({"msg_id": "req-2", "msg_type": ks.MESSAGE_TYPE, "key": PUBLIC},              # another ask
                   {"msg_id": "req-1", "msg_type": "permissions_v2_answer_fetch", "key": PUBLIC},  # a fetch
                   {"msg_id": "req-1", "msg_type": ks.MESSAGE_TYPE, "key": OTHER.public_key().public_bytes_raw()},
                   {"msg_id": "req-1", "msg_type": ks.MESSAGE_TYPE, "key": None},                # no key pinned
                   {"msg_id": "req-1", "msg_type": ks.MESSAGE_TYPE, "key": b"short"},
                   {"msg_id": "", "msg_type": ks.MESSAGE_TYPE, "key": PUBLIC}):
        with pytest.raises(PolicyError) as refused:
            ks.verify_stamp(stamp, **kwargs)
        assert refused.value.code == ks.INVALID


def test_the_signed_bytes_are_pinned():
    assert ks.signing_bytes(KEEP, KQ, msg_id="req-1", msg_type=ks.MESSAGE_TYPE) == (
        b'topos-kept-stamp/v1\n'
        b'{"keep_until":2000000,"kq":"' + KQ.encode("ascii") + b'","msg_id":"req-1",'
        b'"msg_type":"permissions_v2_answer_submit","v":1}')
    assert _stamp()["sig"] == base64.urlsafe_b64encode(KEY.sign(ks.signing_bytes(
        KEEP, KQ, msg_id="req-1", msg_type=ks.MESSAGE_TYPE))).decode().rstrip("=")
    assert (ks.FIELD, ks.VERSION, ks.MESSAGE_TYPE, ks.SKEW_SECONDS, ks.CAPABILITY_VERSION) == (
        "kept_stamp", 1, "permissions_v2_answer_submit", 300, 1)
    assert _raw_stamp() == _stamp()                       # the test's own signer signs the module's bytes


def test_a_stamp_reads_as_no_other_signed_thing():
    """The owner account words' stamp is signed by the same key for the same ask: its signature never verifies here."""
    assert ks.DOMAIN != oaw.DOMAIN and not ks.DOMAIN.startswith(b"{")
    words = oaw.sign_stamp(["ivo"], msg_id="req-1", msg_type=oaw.MESSAGE_TYPE, signer=KEY)
    with pytest.raises(PolicyError):
        ks.verify_stamp({"v": 1, "keep_until": KEEP, "kq": KQ, "sig": words["sig"]}, msg_id="req-1",
                        msg_type=ks.MESSAGE_TYPE, key=PUBLIC)


@pytest.mark.parametrize("value", [0, -1, True, False, "2000000", 2_000_000.0, None, [KEEP], 2**53], ids=repr)
def test_a_window_outside_the_grammar_is_refused(value):
    with pytest.raises(PolicyError) as refused:
        ks.parse_keep_until(value)
    assert refused.value.code == ks.INVALID
    with pytest.raises(PolicyError):
        ks.signing_bytes(value, KQ, msg_id="req-1", msg_type=ks.MESSAGE_TYPE)
    with pytest.raises(PolicyError):
        ks.verify_stamp({**_stamp(), "keep_until": value}, msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC)


def test_the_widest_window_is_accepted():
    assert ks.parse_keep_until(1) == 1 and ks.parse_keep_until(2**53 - 1) == 2**53 - 1
    assert ks.verify_stamp(_stamp(2**53 - 1), msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC) == (
        2**53 - 1, KQ)


@pytest.mark.parametrize("value", [0, -1, True, 2**53], ids=repr)
def test_a_window_outside_the_grammar_is_refused_even_when_the_key_signed_it(value):
    with pytest.raises(PolicyError) as refused:
        ks.verify_stamp(_raw_stamp(keep_until=value), msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC)
    assert refused.value.code == ks.INVALID


def _bad_kq():
    """Kept question ids outside the grammar: exactly 32 lowercase hex characters, nothing else."""
    assert KQ != KQ.upper() and len(KQ) == 32
    return {"upper-case": KQ.upper(), "short": KQ[:-1], "long": KQ + "a", "not-hex": KQ[:-1] + "g",
            "leading-space": " " + KQ[1:], "trailing-newline": KQ[:-1] + "\n", "empty": "", "none": None, "number": 7,
            "list": [KQ], "bytes": KQ.encode("ascii")}


@pytest.mark.parametrize("name", sorted(_bad_kq()))
def test_a_kept_question_id_outside_the_grammar_is_refused(name):
    value = _bad_kq()[name]
    with pytest.raises(PolicyError) as refused:
        ks.parse_kq(value)
    assert refused.value.code == ks.INVALID
    with pytest.raises(PolicyError):
        ks.signing_bytes(KEEP, value, msg_id="req-1", msg_type=ks.MESSAGE_TYPE)
    with pytest.raises(PolicyError):
        ks.verify_stamp({**_stamp(), "kq": value}, msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC)
    if type(value) is str:                                 # the key itself signed it: still refused
        with pytest.raises(PolicyError):
            ks.verify_stamp(_raw_stamp(kq=value), msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC)


def test_a_stamp_that_is_not_exactly_the_grammar_is_refused():
    good = _stamp()
    last = good["sig"][-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    # The same signature bytes written another way: the last character's unused bits set.
    other_spelling = next(good["sig"][:-1] + char for char in alphabet if char != last and base64.urlsafe_b64decode(
        good["sig"][:-1] + char + "==") == base64.urlsafe_b64decode(good["sig"] + "=="))
    another = hashlib.sha256(b"off1n/kept-question/two").hexdigest()[:32]
    for bad in (None, [], "stamp", {}, {**good, "extra": 1}, {k: v for k, v in good.items() if k != "v"},
                {k: v for k, v in good.items() if k != "keep_until"}, {k: v for k, v in good.items() if k != "kq"},
                {**good, "v": 2}, {**good, "v": True},
                {**good, "v": "1"}, {**good, "sig": good["sig"] + "="}, {**good, "sig": good["sig"][:-1]},
                {**good, "sig": 7}, {**good, "sig": "A" * 86}, {**good, "sig": other_spelling},
                {**good, "keep_until": KEEP + 60}, {**good, "kq": another}):
        with pytest.raises(PolicyError) as refused:
            ks.verify_stamp(bad, msg_id="req-1", msg_type=ks.MESSAGE_TYPE, key=PUBLIC)
        assert refused.value.code == ks.INVALID, bad


def test_a_window_is_expired_only_past_the_clocks_skew():
    assert not ks.expired(KEEP, now=KEEP - DAY) and not ks.expired(KEEP, now=KEEP)
    assert not ks.expired(KEEP, now=KEEP + ks.SKEW_SECONDS)
    assert ks.expired(KEEP, now=KEEP + ks.SKEW_SECONDS + 1)
    with pytest.raises(PolicyError):
        ks.expired(True, now=KEEP)


def test_the_skew_is_the_relay_stamps():
    from topos import relay_stamp
    assert ks.SKEW_SECONDS == relay_stamp.SKEW_S


# --- the transport ----------------------------------------------------------------------------------------------------

class Socket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(json.loads(frame))


def _dispatch(monkeypatch, message, *, key=PUBLIC):
    seen = []

    class Service:
        def submit(self, **kwargs):
            seen.append(("submit", kwargs))
            return {"signed": True}, {"state": "pending"}

        def fetch(self, **kwargs):
            seen.append(("fetch", kwargs))
            raise PolicyError("permission_denied")

    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: Service()))
    if key is None:
        monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
        monkeypatch.setattr("topos.relay_stamp._load_public_key_bytes", lambda: None)
    else:
        monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(key).decode())
    socket = Socket()
    asyncio.run(answer_transport.dispatch_answer(socket, message))
    return socket.frames[0], seen


def _submit(**extra):
    return {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "payload": {"envelope": {}, "intent": {}}, **extra}


REFUSED = {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "status": "error", "code": 403,
           "error": "permission_denied"}


def test_an_ask_without_the_stamp_reaches_the_service_exactly_as_before(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit())
    assert frame["status"] == "ok"
    assert set(seen[0][1]) == {"envelope", "payload", "request_id", "owner_account_words", "accepted_version"}


def test_an_ask_with_a_verified_stamp_reaches_the_service_with_its_window(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit(kept_stamp=_stamp()))
    assert frame["status"] == "ok"
    assert (seen[0][1]["keep_until"], seen[0][1]["kept_question"]) == (KEEP, KQ)


def test_the_stamp_rides_beside_the_owner_account_stamp(monkeypatch):
    words = oaw.sign_stamp(["ivo"], msg_id="req-1", msg_type=oaw.MESSAGE_TYPE, signer=KEY)
    frame, seen = _dispatch(monkeypatch, _submit(kept_stamp=_stamp(), owner_account_stamp=words))
    assert frame["status"] == "ok"
    assert (seen[0][1]["keep_until"], seen[0][1]["kept_question"], seen[0][1]["owner_account_words"]) == (
        KEEP, KQ, frozenset({"ivo"}))


@pytest.mark.parametrize("stamp", [
    None, {}, "kept", {"v": 1, "keep_until": KEEP, "kq": KQ, "sig": "A" * 86}, {**_stamp(), "keep_until": KEEP + 60},
    _stamp(msg_id="another-request"), _raw_stamp(msg_type="permissions_v2_answer_fetch"), _stamp(signer=OTHER),
    {**_stamp(), "more": True}, _raw_stamp(keep_until=0), _raw_stamp(keep_until=True),
    {k: v for k, v in _stamp().items() if k != "kq"}, _raw_stamp(kq=KQ.upper()), _raw_stamp(kq=KQ[:-1]),
    {**_stamp(), "kq": hashlib.sha256(b"off1n/kept-question/two").hexdigest()[:32]},
], ids=["null", "empty", "string", "unsigned", "other-window", "other-message", "signed-for-a-fetch", "other-key",
        "extra-key", "zero", "boolean", "no-question-id", "upper-case-id", "short-id", "other-question-id"])
def test_an_ask_whose_stamp_is_malformed_or_does_not_verify_is_refused(monkeypatch, stamp):
    frame, seen = _dispatch(monkeypatch, _submit(kept_stamp=stamp))
    assert frame == REFUSED
    assert seen == []


def test_an_ask_with_the_stamp_is_refused_on_a_node_that_holds_no_stamp_key(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _submit(kept_stamp=_stamp()), key=None)
    assert frame == REFUSED and seen == []


def test_a_fetch_never_carries_the_stamp(monkeypatch):
    message = {"id": "req-1", "type": answer_transport.FETCH_TYPE, "payload": {"envelope": {}, "intent": {}},
               "kept_stamp": _stamp()}
    frame, seen = _dispatch(monkeypatch, message)
    assert frame == {**REFUSED, "type": answer_transport.FETCH_TYPE} and seen == []


def test_the_stamp_is_no_part_of_the_closed_payload(monkeypatch):
    """The relay payload stays exactly {envelope, intent}, and the asker's intent has no room for a window."""
    frame, seen = _dispatch(monkeypatch, {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "payload": {
        "envelope": {}, "intent": {}, "kept_stamp": _stamp()}})
    assert frame == REFUSED and seen == []
    with pytest.raises(PolicyError):
        AskIntent.parse({"question": QUESTION, "keep_until": KEEP})


# --- the keep window ----------------------------------------------------------------------------------------------

def _ask(node, service, *, keep_until=None, kq=None, question=QUESTION):
    """One ask; with `keep_until`, a kept one, for the kept question `kq` (a fresh one unless given)."""
    payload = {"question": question}
    signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
    kept = {} if keep_until is None else {"keep_until": keep_until,
                                          "kept_question": secrets.token_hex(16) if kq is None else kq}
    with recipient():
        result, output = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id,
                                        **kept)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return output["answer_id"]


def _fetch(node, service, answer_id):
    payload = {"answer_id": answer_id}
    signed = node._envelope("grant-search", FETCH, payload, node.next_id("fetch"))
    with recipient():
        result, output = service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return output


def _ended(service, answer_id):
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            return job
        time.sleep(.01)
    raise AssertionError("the answer worker did not finish")


def _receipt(node, job):
    with node.ledger._transaction() as db:
        row = db.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id=?",
                         (job.request_id,)).fetchone()
    return json.loads(row[0]), json.loads(row[1])


def _questions(node):
    with node.ledger._transaction() as db:
        return db.execute("SELECT COALESCE(SUM(questions), 0) FROM p2a_question_days").fetchone()[0]


def test_a_kept_answer_is_fetched_after_ten_minutes_and_gone_at_the_end_of_its_window(answers_node, service):
    node = answers_node
    keep_until = node.now[0] + 3 * DAY
    answer_id = _ask(node, service, keep_until=keep_until)
    job = _ended(service, answer_id)
    assert job.keep_until == keep_until and answer_release.keep_end(job) == keep_until
    first = _fetch(node, service, answer_id)
    assert first["outcome"] == "answered"
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS + 1          # an unkept answer is gone by now
    assert canonical_bytes(_fetch(node, service, answer_id)) == canonical_bytes(first)
    node.now[0] = keep_until - 1
    assert canonical_bytes(_fetch(node, service, answer_id)) == canonical_bytes(first)
    node.now[0] = keep_until
    with pytest.raises(PolicyError):
        _fetch(node, service, answer_id)
    assert answer_id not in service._jobs
    assert _questions(node) == 1                      # the ask spent one question; no fetch spent any


def test_an_unkept_answer_keeps_the_window_of_before(answers_node, service):
    node = answers_node
    answer_id = _ask(node, service)
    job = _ended(service, answer_id)
    assert job.keep_until is None and answer_release.keep_end(job) == job.ended_at + answer_release.UNFETCHED_SECONDS
    assert _fetch(node, service, answer_id)["outcome"] == "answered"
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS
    with pytest.raises(PolicyError):
        _fetch(node, service, answer_id)
    assert answer_id not in service._jobs


def test_a_kept_window_is_at_most_fourteen_days(answers_node, service):
    node = answers_node
    answer_id = _ask(node, service, keep_until=node.now[0] + 60 * DAY)
    job = _ended(service, answer_id)
    assert answer_release.KEPT_MAX_SECONDS == 14 * DAY
    assert answer_release.keep_end(job) == job.ended_at + answer_release.KEPT_MAX_SECONDS
    with service._lock:
        service._clean(job.ended_at + answer_release.KEPT_MAX_SECONDS - 1)
    assert answer_id in service._jobs
    with service._lock:
        service._clean(job.ended_at + answer_release.KEPT_MAX_SECONDS)
    assert answer_id not in service._jobs


def test_a_kept_window_is_never_shorter_than_an_unkept_answers(answers_node, service):
    """A window that ends before an unkept answer's would keeps the answer as long as an unkept one: being kept only
    ever lengthens the stay."""
    node = answers_node
    answer_id = _ask(node, service, keep_until=node.now[0] + 1)
    job = _ended(service, answer_id)
    assert answer_release.keep_end(job) == job.ended_at + answer_release.UNFETCHED_SECONDS
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS - 1
    assert _fetch(node, service, answer_id)["outcome"] == "answered"


def test_a_kept_answer_is_rechecked_at_every_fetch_as_before(answers_node, service):
    """Past an unkept answer's window, the share's authority moving still refuses the fetch and drops the body."""
    node = answers_node
    answer_id = _ask(node, service, keep_until=node.now[0] + DAY)
    job = _ended(service, answer_id)
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS + 1
    node.activate(node.search_raw, generation=2)
    with pytest.raises(PolicyError):
        _fetch(node, service, answer_id)
    assert answer_id not in service._jobs


def test_a_kept_question_and_its_answer_never_reach_a_database(answers_node, service):
    node = answers_node
    question = "What is the owner working on lately?"
    answer_id = _ask(node, service, keep_until=node.now[0] + DAY, question=question)
    body = _fetch(node, service, _ended(service, answer_id).answer_id)
    assert body["outcome"] == "answered"
    for path in (node.ledger.path, node.corpus.path):
        for stored in (path, path.with_name(path.name + "-wal")):
            if stored.exists():
                data = stored.read_bytes()
                assert question.encode() not in data and body["answer"].encode() not in data


def test_an_ask_whose_window_already_ended_is_refused_and_spends_no_question(answers_node, service):
    node = answers_node
    with pytest.raises(PolicyError):
        _ask(node, service, keep_until=node.now[0] - ks.SKEW_SECONDS - 1)
    assert service._jobs == {} and _questions(node) == 0
    # Inside the clocks' skew it is a kept ask: the control plane delivered it inside its window.
    answer_id = _ask(node, service, keep_until=node.now[0] - ks.SKEW_SECONDS)
    assert _ended(service, answer_id).keep_until == node.now[0] - ks.SKEW_SECONDS


@pytest.mark.parametrize("value", [0, -5, True, "2000000", 2_000_000.5], ids=repr)
def test_the_service_checks_the_windows_grammar_again(answers_node, service, value):
    node = answers_node
    with pytest.raises(PolicyError):
        _ask(node, service, keep_until=value)
    assert service._jobs == {} and _questions(node) == 0


@pytest.mark.parametrize("name", ["upper-case", "short", "not-hex", "empty", "number"])
def test_the_service_checks_the_question_ids_grammar_again(answers_node, service, name):
    node = answers_node
    with pytest.raises(PolicyError):
        _ask(node, service, keep_until=node.now[0] + DAY, kq=_bad_kq()[name])
    assert service._jobs == {} and _questions(node) == 0


@pytest.mark.parametrize("kept", [{"keep_until": 2 * DAY}, {"kept_question": "kq"}], ids=["window-alone", "id-alone"])
def test_a_kept_ask_carries_its_window_and_its_question_id_together(answers_node, service, kept):
    node = answers_node
    if "kept_question" in kept:
        kept = {"kept_question": KQ}
    payload = {"question": QUESTION}
    signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
    with recipient(), pytest.raises(PolicyError):
        service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id, **kept)
    assert service._jobs == {} and _questions(node) == 0


def _job(number, *, keep_until, ended_at, deferred=False):
    return Job(f"ans-kept-{number}", f"req-{number}", "grant-a", "assignment-a", "actor-1", "client-2", None, "only",
               None, "hash", 1, state="ended", ended_at=ended_at, keep_until=keep_until, deferred=deferred)


def test_more_than_five_hundred_kept_answers_log_one_counts_only_line(caplog):
    service = AnswerService(SimpleNamespace(), clock=lambda: 100)
    caplog.set_level(logging.WARNING, logger=LOGGER)

    def keep_lines():
        return [record.getMessage() for record in caplog.records
                if record.name == LOGGER and record.getMessage().startswith("permissions answer keep")]
    try:
        assert answer_release.KEPT_ENDED_BOUND == 500
        for number in range(500):
            service._jobs[f"ans-kept-{number}"] = _job(number, keep_until=DAY, ended_at=10)
        for number in range(500, 520):          # unkept answers, still inside their window: not counted
            service._jobs[f"ans-kept-{number}"] = _job(number, keep_until=None, ended_at=99)
        service._clean(100)
        assert keep_lines() == []
        service._jobs["ans-kept-x"] = _job(999, keep_until=DAY, ended_at=10)
        service._clean(100)
        service._clean(100)                      # still over: no second line
        assert keep_lines() == ["permissions answer keep: kept_ended=501 bound=500"]
        del service._jobs["ans-kept-x"], service._jobs["ans-kept-0"]
        service._clean(100)
        service._jobs["ans-kept-0"] = _job(0, keep_until=DAY, ended_at=10)
        service._jobs["ans-kept-x"] = _job(999, keep_until=DAY, ended_at=10)
        service._clean(100)                      # over again: a second line
        assert keep_lines() == ["permissions answer keep: kept_ended=501 bound=500"] * 2
    finally:
        service.close()


# --- the sweep ----------------------------------------------------------------------------------------------------

def _gone(service, answer_id, seconds=5.0):
    """Whether the job leaves the service within `seconds`, with no ask and no fetch in between."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if answer_id not in service._jobs:
            return True
        time.sleep(.01)
    return False


def test_an_expired_kept_answer_leaves_memory_without_another_ask_or_fetch(answers_node, service, monkeypatch):
    """A kept answer can stay for days. Dropped only at the next ask or fetch, it stayed in memory past its window on
    a node nobody asked again. The sweep drops it within SWEEP_SECONDS of its window's end (it was never served after
    that either way: every ask and fetch cleans first)."""
    monkeypatch.setattr(answer_release, "SWEEP_SECONDS", 0.02)
    node = answers_node
    keep_until = node.now[0] + DAY
    answer_id = _ask(node, service, keep_until=keep_until)
    job = _ended(service, answer_id)
    assert _fetch(node, service, answer_id)["outcome"] == "answered"
    node.now[0] = keep_until - 1
    assert not _gone(service, answer_id, seconds=0.3)            # inside its window, many sweeps keep it
    node.now[0] = keep_until
    assert _gone(service, answer_id)                             # past it, with no ask and no fetch: dropped
    assert job.body is None and job.question is None


def test_the_sweep_runs_only_while_a_kept_job_is_in_memory(answers_node, service, monkeypatch):
    monkeypatch.setattr(answer_release, "SWEEP_SECONDS", 0.02)
    node = answers_node
    live = _ask(node, service)
    _ended(service, live)
    assert service._sweeper is None                              # a live ask starts nothing: as before
    kept = _ask(node, service, keep_until=node.now[0] + DAY)
    sweeper = service._sweeper
    assert sweeper is not None and sweeper.is_alive() and sweeper.daemon
    _ended(service, kept)
    node.now[0] += 2 * DAY                                       # both past their windows
    assert _gone(service, kept) and _gone(service, live)
    sweeper.join(timeout=5)
    assert not sweeper.is_alive() and service._sweeper is None   # no kept job left: the sweep ended
    _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    assert service._sweeper is not None and service._sweeper is not sweeper and service._sweeper.is_alive()


def test_a_sweep_that_cannot_start_leaves_the_kept_ask_served(answers_node, service, monkeypatch, caplog):
    """Out of threads, the ask the ledger already admitted is still answered; cleaning waits for the next ask or
    fetch, as before the sweep."""
    import threading as real

    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(answer_release, "threading", SimpleNamespace(Thread=NoThread, Lock=real.Lock, Event=real.Event))
    caplog.set_level(logging.WARNING, logger=LOGGER)
    node = answers_node
    answer_id = _ask(node, service, keep_until=node.now[0] + DAY)
    assert _fetch(node, service, _ended(service, answer_id).answer_id)["outcome"] == "answered"
    assert service._sweeper is None
    assert "permissions answer keep: the sweep did not start" in [record.getMessage() for record in caplog.records]


def test_closing_the_service_ends_its_sweep(answers_node, service):
    node = answers_node
    _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    sweeper = service._sweeper
    assert sweeper.is_alive()
    service.close()
    sweeper.join(timeout=5)
    assert not sweeper.is_alive()


# --- deferred -----------------------------------------------------------------------------------------------------

FETCH_LINE = re.compile(r"permissions answer fetch: outcome=([a-z_]+) reason=([a-z0-9_]+|-) seconds=\d+\.\d")


def _fetch_lines(caplog):
    found = []
    for record in caplog.records:
        if record.name == LOGGER and record.getMessage().startswith("permissions answer fetch"):
            match = FETCH_LINE.fullmatch(record.getMessage())
            assert match, f"not the counts-only shape: {record.getMessage()!r}"
            found.append((match.group(1), match.group(2)))
    return found


def _unserve(node):
    index_path(node.index.root, "grant-search").unlink()        # as a sweep's drop, or a restart's rebuild, leaves it


def test_a_kept_ask_that_meets_an_index_not_served_now_ends_deferred(answers_node, service, caplog):
    node = answers_node
    _unserve(node)
    answer_id = _ask(node, service, keep_until=node.now[0] + DAY)
    job = _ended(service, answer_id)
    assert (job.deferred, job.body, job.question, job.not_served) == (True, None, None, "search_index_stale")
    receipt, decision = _receipt(node, job)
    assert (receipt["outcome"], receipt["reason"], receipt["records_used"]) == ("deferred", "search_index_stale", 0)
    assert decision == {"version": "answer-decision/v1", "outcome": "deferred", "reason": "search_index_stale"}
    caplog.set_level(logging.INFO, logger=LOGGER)
    for _ in range(2):                             # every fetch: deferred, never busy, and the id is kept
        with pytest.raises(AnswerDeferred) as deferred:
            _fetch(node, service, answer_id)
        assert not isinstance(deferred.value, AnswerBusy)
        assert answer_id in service._jobs
    assert _fetch_lines(caplog) == [("deferred", "search_index_stale")] * 2
    assert _questions(node) == 0                   # the deferral gave its ask's question back (WS-OFF, Q1)


def test_deferred_asks_give_their_questions_back_and_an_answered_kept_ask_counts_once(answers_node, service):
    """WS-OFF, 23 Oct (Q1): a kept question counts toward the share's daily limit on the day it is answered, not once
    per delivery. Each deferral gives its ask's question back with its receipt; an answered kept ask counts once; a
    live ask counts as before, answered or not."""
    node = answers_node
    _unserve(node)
    assert _ended(service, _ask(node, service)).body.outcome == "no_answer"     # a live ask counts, as before
    assert _questions(node) == 1
    for _ in range(3):
        assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY)).deferred
        assert _questions(node) == 1                                           # the day did not move
    rebuild(node)
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY)).body.outcome == "answered"
    assert _questions(node) == 2
    assert _ended(service, _ask(node, service)).body.outcome == "answered"
    assert _questions(node) == 3


def test_a_kept_question_delivered_again_the_same_day_counts_once(answers_node, service):
    """R-OFF1 L1 (WS-OFF, 23 Oct): a redelivery (a lost answer, a restart) carries the same kept question id, and the
    node counts the question against its share's day only the first time that day sees the id."""
    node = answers_node
    kq, other = secrets.token_hex(16), secrets.token_hex(16)
    for _ in range(3):
        assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).body.outcome == "answered"
        assert _questions(node) == 1
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=other)).body.outcome == "answered"
    assert _questions(node) == 2                                   # another kept question counts
    assert _ended(service, _ask(node, service)).body.outcome == "answered"
    assert _questions(node) == 3                                   # a live ask counts, as before


def test_a_deferral_gives_back_only_the_question_its_own_ask_counted(answers_node, service):
    """A kept question deferred and delivered again counts once, on the delivery that is answered. A delivery that
    did not count (the id was already counted that day) gives nothing back when it defers."""
    node = answers_node
    kq = secrets.token_hex(16)
    _unserve(node)
    for _ in range(2):
        assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).deferred
        assert _questions(node) == 0
    rebuild(node)
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).body.outcome == "answered"
    assert _questions(node) == 1
    _unserve(node)
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).deferred
    assert _questions(node) == 1                                   # this delivery counted nothing: nothing back


def test_the_same_kept_question_counts_again_on_another_utc_day(answers_node, service):
    from topos.permissions_v2.ledger import utc_day
    node = answers_node
    kq = secrets.token_hex(16)
    first = _ended(service, _ask(node, service, keep_until=node.now[0] + 3 * DAY, kq=kq))
    node.now[0] += DAY
    second = _ended(service, _ask(node, service, keep_until=node.now[0] + 2 * DAY, kq=kq))
    assert utc_day(first.accepted_at) != utc_day(second.accepted_at)
    with node.ledger._transaction() as db:
        days = dict(db.execute("SELECT utc_day, questions FROM p2a_question_days").fetchall())
    assert days == {utc_day(first.accepted_at): 1, utc_day(second.accepted_at): 1}


def test_the_ledger_keeps_only_a_hash_of_the_kept_question_id(answers_node, service):
    node = answers_node
    kq = secrets.token_hex(16)
    job = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq))
    with node.ledger._transaction() as db:
        rows = [tuple(row) for row in db.execute("SELECT * FROM p2a_kept_question_days").fetchall()]
    assert len(rows) == 1
    grant_id, _day, kq_hash, request_id = rows[0]
    assert (grant_id, request_id) == ("grant-search", job.request_id)
    assert re.fullmatch(r"[0-9a-f]{64}", kq_hash) and kq not in kq_hash
    for stored in (node.ledger.path, node.ledger.path.with_name(node.ledger.path.name + "-wal")):
        if stored.exists():
            assert kq.encode("ascii") not in stored.read_bytes()


def test_kept_question_rows_are_pruned_with_their_day(answers_node, service):
    from topos.permissions_v2.ledger import QUESTION_DAY_RETENTION_DAYS, utc_day
    node = answers_node
    old = utc_day(node.now[0] - (QUESTION_DAY_RETENTION_DAYS + 2) * DAY)
    with node.ledger._transaction() as db:
        db.execute("INSERT INTO p2a_question_days VALUES ('grant-search', ?, 1)", (old,))
        db.execute("INSERT INTO p2a_kept_question_days VALUES ('grant-search', ?, ?, 'answer-old')",
                   (old, hashlib.sha256(b"an invented old kept question").hexdigest()))
    _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))          # a counted ask prunes
    with node.ledger._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM p2a_question_days WHERE utc_day=?", (old,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM p2a_kept_question_days WHERE utc_day=?", (old,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM p2a_kept_question_days").fetchone()[0] == 1


def test_a_redelivery_is_not_held_to_the_limit_its_question_was_counted_against(answers_node, service):
    """The share allows one question a day. The kept question was counted, then its answer was lost and the control
    plane delivers it again: the node answers it, and still refuses any other question that day."""
    node = answers_node
    node.activate({**node.search_raw, "read_budget_per_day": 1, "policy_version_id": "policy-grant-search-budget"},
                  generation=2)
    rebuild(node)
    kq = secrets.token_hex(16)
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).body.outcome == "answered"
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY, kq=kq)).body.outcome == "answered"
    assert _questions(node) == 1
    with pytest.raises(PolicyError):
        _ask(node, service, keep_until=node.now[0] + DAY)        # another kept question: over the day's limit
    with pytest.raises(PolicyError):
        _ask(node, service)                                      # and a live one
    assert _questions(node) == 1


def test_a_refund_never_takes_the_day_below_zero(answers_node, service, monkeypatch):
    """A day's row holds at least one question (its CHECK). A refund whose row is already gone changes nothing."""
    node = answers_node
    ledger = node.protocol.ledger
    original = ledger.checkpoint_answer_receipt

    def emptied_first(request_id, receipt, **kwargs):     # the day's row is gone when the receipt is filed
        with ledger._transaction() as db:
            db.execute("DELETE FROM p2a_question_days")
        return original(request_id, receipt, **kwargs)

    monkeypatch.setattr(ledger, "checkpoint_answer_receipt", emptied_first)
    _unserve(node)
    job = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    assert job.deferred and _receipt(node, job)[0]["outcome"] == "deferred"
    with node.ledger._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM p2a_question_days").fetchone()[0] == 0


def test_a_deferred_receipt_gives_its_question_back_once(answers_node, service):
    """The refund is the receipt's own transaction: filing the same receipt again is refused whole and gives back
    nothing more."""
    from topos.permissions_v2.ledger import utc_day
    node = answers_node
    _unserve(node)
    job = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    receipt, _decision = _receipt(node, job)
    with node.ledger._transaction() as db:      # two other questions asked that day
        db.execute("INSERT INTO p2a_question_days VALUES (?, ?, 2) ON CONFLICT(grant_id, utc_day) "
                   "DO UPDATE SET questions=questions+2", ("grant-search", utc_day(job.accepted_at)))
    with pytest.raises(PolicyError) as replayed:
        node.protocol.ledger.checkpoint_answer_receipt(job.request_id, receipt, now=node.now[0])
    assert replayed.value.code == "request_replay" and _questions(node) == 2


def test_an_unkept_ask_that_meets_an_index_not_served_now_ends_no_answer_as_before(answers_node, service):
    node = answers_node
    _unserve(node)
    answer_id = _ask(node, service)
    job = _ended(service, answer_id)
    assert job.deferred is False and job.not_served is None and job.body.outcome == "no_answer"
    receipt, decision = _receipt(node, job)
    assert (receipt["outcome"], receipt["reason"]) == ("no_answer", "search_index_stale")
    assert decision == {"version": "answer-decision/v1", "outcome": "no_answer", "reason": "search_index_stale"}
    assert _fetch(node, service, answer_id) == {"version": "topos-answer/v1", "outcome": "no_answer"}


def test_a_kept_ask_on_a_served_index_is_answered_with_the_receipt_of_a_live_one(answers_node, service):
    node = answers_node
    live = _ended(service, _ask(node, service))
    kept = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    assert live.body.outcome == kept.body.outcome == "answered" and not kept.deferred
    live_receipt, kept_receipt = _receipt(node, live)[0], _receipt(node, kept)[0]
    different = {"request_id", "answer_id", "accepted_at", "started_at", "finished_at"}
    assert set(live_receipt) == set(kept_receipt)
    assert {k: v for k, v in live_receipt.items() if k not in different} == {
        k: v for k, v in kept_receipt.items() if k not in different}


@pytest.mark.parametrize("code", sorted(answer_release.NOT_SERVED_NOW))
def test_every_not_served_now_refusal_defers_a_kept_ask_and_no_other_ask(answers_node, service, monkeypatch, code):
    node = answers_node

    def refused(**_kwargs):
        raise PolicyError(code)

    monkeypatch.setattr(node.search, "retrieve_for_answer", refused)
    kept = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    live = _ended(service, _ask(node, service))
    assert kept.deferred and kept.body is None and _receipt(node, kept)[0]["outcome"] == "deferred"
    assert not live.deferred and live.body.outcome == "no_answer" and _receipt(node, live)[0]["outcome"] == "no_answer"
    # The receipt's reason is the closed word it always was (`receipt_reason`), deferred or not.
    assert _receipt(node, kept)[0]["reason"] == _receipt(node, live)[0]["reason"] == answer_release.receipt_reason(code)


@pytest.mark.parametrize("code", ["search_index_over_cap", "search_index_integrity", "search_index_binding",
                                  "grant_inactive", "authority_stale", "policy_time"])
def test_a_refusal_waiting_cannot_cure_ends_a_kept_ask_no_answer(answers_node, service, monkeypatch, code):
    """An index over its cap, a corrupt one, a mismatched binding, or a share that is not on: no retry would answer."""
    node = answers_node

    def refused(**_kwargs):
        raise PolicyError(code)

    monkeypatch.setattr(node.search, "retrieve_for_answer", refused)
    kept = _ended(service, _ask(node, service, keep_until=node.now[0] + DAY))
    assert not kept.deferred and kept.body.outcome == "no_answer"
    assert _receipt(node, kept)[0]["outcome"] == "no_answer"


def test_a_deferred_receipt_adds_nothing_to_the_owners_week(answers_node, service):
    from topos.permissions_v2.share_week import week
    node = answers_node

    def counted():
        return week(node.ledger.path, ["grant-search"], since=0, until=node.now[0] + DAY)

    before = counted()
    _unserve(node)
    assert _ended(service, _ask(node, service, keep_until=node.now[0] + DAY)).deferred
    assert counted() == before
    assert not _ended(service, _ask(node, service)).deferred      # not vacuous: a live no_answer is counted
    assert counted() == {**before, "no_answer": before["no_answer"] + 1}


def test_a_deferred_ask_is_remembered_for_an_unkept_answers_window(answers_node, service):
    node = answers_node
    _unserve(node)
    answer_id = _ask(node, service, keep_until=node.now[0] + 5 * DAY)
    job = _ended(service, answer_id)
    assert answer_release.keep_end(job) == job.ended_at + answer_release.UNFETCHED_SECONDS
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS - 1
    with pytest.raises(AnswerDeferred):
        _fetch(node, service, answer_id)
    node.now[0] = job.ended_at + answer_release.UNFETCHED_SECONDS
    with pytest.raises(PolicyError):
        _fetch(node, service, answer_id)
    assert answer_id not in service._jobs


def test_a_deferred_ask_whose_share_moved_is_refused_and_dropped(answers_node, service):
    """Every fetch-time check runs before the deferral: a moved authority is the uniform refusal, as for any job."""
    node = answers_node
    _unserve(node)
    answer_id = _ask(node, service, keep_until=node.now[0] + DAY)
    assert _ended(service, answer_id).deferred
    node.activate(node.search_raw, generation=2)
    with pytest.raises(PolicyError):
        _fetch(node, service, answer_id)
    assert answer_id not in service._jobs


def test_a_kept_ask_and_its_deferred_fetch_on_the_wire(answers_node, service, monkeypatch):
    """Through the relay dispatch: the stamped ask is accepted (pending), and the fetch of the deferred job is 429
    `answer_deferred`, in the frame lane 190's busy has (WS-OFF, Q5)."""
    node = answers_node
    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="actor-1", client_id="client-2"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: service))
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(PUBLIC).decode())
    _unserve(node)
    socket = Socket()
    intent = {"question": QUESTION}
    signed = node._envelope("grant-search", ASK, intent, node.next_id("answer"))
    asyncio.run(answer_transport.dispatch_answer(socket, {
        "id": signed.request_id, "type": answer_transport.SUBMIT_TYPE,
        "payload": {"envelope": signed.model_dump(), "intent": intent},
        "kept_stamp": ks.sign_stamp(node.now[0] + DAY, KQ, msg_id=signed.request_id, msg_type=ks.MESSAGE_TYPE,
                                    signer=KEY)}))
    assert socket.frames[0]["status"] == "ok", socket.frames[0]
    answer_id = socket.frames[0]["payload"]["output"]["answer_id"]
    assert _ended(service, answer_id).deferred
    fetch = {"answer_id": answer_id}
    signed = node._envelope("grant-search", FETCH, fetch, node.next_id("fetch"))
    asyncio.run(answer_transport.dispatch_answer(socket, {
        "id": signed.request_id, "type": answer_transport.FETCH_TYPE,
        "payload": {"envelope": signed.model_dump(), "intent": fetch}}))
    assert socket.frames[1] == {"id": signed.request_id, "type": answer_transport.FETCH_TYPE, "status": "error",
                                "code": 429, "error": "answer_deferred"}


@pytest.mark.parametrize("raised, word", [(AnswerDeferred("search_index_stale"), "answer_deferred"),
                                          (AnswerBusy(), "answer_busy")], ids=["deferred", "busy"])
def test_a_deferred_fetch_has_its_own_word_and_a_busy_one_keeps_lane_190s(monkeypatch, raised, word):
    """WS-OFF, 23 Oct (Q5): `answer_busy` on a fetch means the job is still being written, so the control plane keeps
    polling; `answer_deferred` means this kept delivery is over, so it ends the attempt and asks again later. The same
    status and frame shape; askers see neither."""
    class Service:
        def fetch(self, **_kwargs):
            raise raised

    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: Service()))
    socket = Socket()
    payload = {"envelope": {}, "intent": {"answer_id": "ans_a"}}
    asyncio.run(answer_transport.dispatch_answer(socket, {"id": "req-1", "type": answer_transport.FETCH_TYPE,
                                                          "payload": payload}))
    assert socket.frames == [{"id": "req-1", "type": answer_transport.FETCH_TYPE, "status": "error", "code": 429,
                              "error": word}]
    assert not answer_transport._FETCHES_WAITING


# --- the capability -----------------------------------------------------------------------------------------------

def test_the_node_says_it_keeps_kept_answers(monkeypatch):
    from topos.engine import registration
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    assert registration.build_engine_capabilities()["permissions_v2_kept_answers"] == 1 == ks.CAPABILITY_VERSION
