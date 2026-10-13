"""Lane LA164 (A2A-4 amendment 13, 13 Oct 2026): longer answers over more items, with sixteen sources on the wire.

A with-sources answer may run to eight short sentences over up to sixteen released items and carry up to sixteen
cited records. The wire cap is a closed, mirrored grammar, so it moves by answer protocol version: the node advertises
3 (`permissions_v2_answer_version`), and the asker's control plane says on the ask frame which version it accepts
(`answer_protocol.ACCEPTS_FIELD`). A control plane that says nothing is one from before the field: its ask is served
exactly as 1.5.9 served it (eight items retrieved, at most eight cited), so no body it cannot parse is ever written.
Nothing else about the checks moves. Invented names and text only.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_answer_quality_aq158 import Open, _many, _message
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_generation as ag, answer_protocol as ap, answer_release as ar
from topos.permissions_v2 import answer_transport, switches
from topos.permissions_v2.answer_checks import TEMPLATE_VERSION
from topos.permissions_v2.answer_generation import SYSTEM_PROMPT, build_prompt, k_answer, post_check_answer
from topos.permissions_v2.answer_protocol import (ACCEPTS_FIELD, ANSWER_PROTOCOL_VERSION, ASK, FETCH, K_ANSWER,
    K_ANSWER_LEGACY, LEGACY_ACCEPTED_VERSION, MAX_ANSWER_BYTES, VERSION, AnswerOnly, AnswerWithSources,
    parse_accepted_version, parse_answer_output, records_cap)
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.search_contract import MAX_SEARCH_BYTES

P = "TOPOS_PERMISSIONS_V2_"
LENGTH_SENTENCE = ("If the items establish an answer, write up to eight short sentences in one or two short "
                   "paragraphs, in your own words; each sentence states one supported fact and ends with the numbers "
                   "of the items that support it, such as [1] or [1, 2].")


@pytest.fixture(autouse=True)
def items_switch_unset(monkeypatch):
    monkeypatch.delenv(P + "ANSWER_ITEMS", raising=False)


def _sentences(count, records_each=1, start=1):
    """`count` sentences; sentence i cites `records_each` records from `start` on, so 8 sentences x 2 cite 16."""
    out, number = [], start
    for index in range(1, count + 1):
        numbers = ", ".join(str(number + offset) for offset in range(records_each))
        out.append(f"Step {index} of the lantern work was taken [{numbers}].")
        number += records_each
    return "\n".join(out)


# --- the mirrored grammar -------------------------------------------------------------------------------------------

def test_the_wire_cap_is_sixteen_by_protocol_version_and_eight_before_it():
    assert ANSWER_PROTOCOL_VERSION == 3 and K_ANSWER == 16 and K_ANSWER_LEGACY == 8 and LEGACY_ACCEPTED_VERSION == 2
    assert ACCEPTS_FIELD == "answer_version"
    assert records_cap(1) == records_cap(2) == K_ANSWER_LEGACY
    assert records_cap(3) == records_cap(4) == records_cap(99) == K_ANSWER
    assert VERSION == "topos-answer/v1"                 # the body's own version string does not move


def test_a_with_sources_body_carries_up_to_sixteen_records_and_never_seventeen():
    body = {"version": VERSION, "outcome": "answered", "answer": _sentences(8, 2),
            "records": [record.model_dump() for record in _many(16)]}
    parsed = parse_answer_output(body, request_type=FETCH, mode="with_sources")
    assert isinstance(parsed, AnswerWithSources) and len(parsed.records) == 16
    seventeen = {**body, "answer": _sentences(8, 2) + "\nOne more step [17].",
                 "records": [record.model_dump() for record in _many(17)]}
    with pytest.raises(PolicyError, match="answer_output_invalid"):
        parse_answer_output(seventeen, request_type=FETCH, mode="with_sources")
    # Every supplied record is still cited, and no citation reaches past the records (unchanged rules).
    with pytest.raises(PolicyError, match="answer_output_invalid"):
        parse_answer_output({**body, "answer": _sentences(7, 2)}, request_type=FETCH, mode="with_sources")


def test_a_body_within_the_old_cap_has_the_same_bytes_as_before():
    body = {"version": VERSION, "outcome": "answered", "answer": "The invented trip is on Friday [1].",
            "records": [_many(1)[0].model_dump()]}
    assert canonical_bytes(parse_answer_output(body, request_type=FETCH, mode="with_sources").model_dump()) == canonical_bytes(body)


@pytest.mark.parametrize("raw, expected", [(1, 1), (2, 2), (3, 3), (12, 12)])
def test_the_accepted_version_is_a_small_positive_integer(raw, expected):
    assert parse_accepted_version(raw) == expected


@pytest.mark.parametrize("raw", [None, "3", 0, -1, 100, 3.0, True, [3], {"v": 3}],
                         ids=["null", "string", "zero", "negative", "hundred", "float", "bool", "list", "dict"])
def test_anything_but_that_grammar_is_refused(raw):
    with pytest.raises(PolicyError, match="answer_version_invalid"):
        parse_accepted_version(raw)


# --- the ask frame --------------------------------------------------------------------------------------------------

class Socket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(json.loads(frame))


def _dispatch(monkeypatch, message):
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
    socket = Socket()
    asyncio.run(answer_transport.dispatch_answer(socket, message))
    return socket.frames[0], seen


def _frame(kind=answer_transport.SUBMIT_TYPE, **extra):
    return {"id": "req-1", "type": kind, "payload": {"envelope": {}, "intent": {}}, **extra}


def test_an_ask_without_the_field_is_served_as_a_control_plane_from_before_it(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _frame())
    assert frame["status"] == "ok"
    assert seen[0][1]["accepted_version"] == LEGACY_ACCEPTED_VERSION


def test_an_ask_that_says_it_accepts_three_reaches_the_service_with_three(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _frame(**{ACCEPTS_FIELD: 3}))
    assert frame["status"] == "ok"
    assert seen[0][1]["accepted_version"] == 3


@pytest.mark.parametrize("value", [None, "3", 0, 100, 3.0, [3]], ids=["null", "string", "zero", "hundred", "float", "list"])
def test_an_ask_whose_field_is_not_the_grammar_is_refused(monkeypatch, value):
    frame, seen = _dispatch(monkeypatch, _frame(**{ACCEPTS_FIELD: value}))
    assert frame == {"id": "req-1", "type": answer_transport.SUBMIT_TYPE, "status": "error", "code": 403,
                     "error": "permission_denied"}
    assert seen == []


def test_the_field_on_a_fetch_is_refused(monkeypatch):
    frame, seen = _dispatch(monkeypatch, _frame(answer_transport.FETCH_TYPE, **{ACCEPTS_FIELD: 3}))
    assert frame["status"] == "error" and frame["code"] == 403 and seen == []


# --- the service: what a legacy asker and a current asker are served -----------------------------------------------

def _spy_k(monkeypatch):
    from topos.permissions_v2 import search_release
    seen = []
    original = search_release.SearchIntent.parse

    def parse(value):
        seen.append(value.get("k"))
        return original(value)

    monkeypatch.setattr(search_release.SearchIntent, "parse", parse)
    return seen


def test_retrieval_for_a_legacy_asker_is_capped_at_eight_and_for_a_current_one_at_sixteen(legacy, tmp_path, monkeypatch):
    seen = _spy_k(monkeypatch)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=20, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
    for setting, cap, expected in (("16", K_ANSWER_LEGACY, 8), ("16", K_ANSWER, 16), ("24", K_ANSWER_LEGACY, 8),
                                   ("24", K_ANSWER, 16), ("4", K_ANSWER, 4), ("16", None, 16)):
        monkeypatch.setenv(P + "ANSWER_ITEMS", setting)
        seen.clear()
        with recipient():
            node.search.retrieve_for_answer(grant_id="grant-search", question="Synthetic message",
                                            admitted_authority=authority, records_cap=cap)
        assert seen[0] == expected, (setting, cap, seen)


def test_the_job_records_the_cap_the_asker_accepts(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.test_answer_release import _service
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=20, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        for accepted, cap in ((LEGACY_ACCEPTED_VERSION, K_ANSWER_LEGACY), (3, K_ANSWER)):
            payload = {"question": "Synthetic message"}
            signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
            with recipient():
                _result, output = service.submit(envelope=signed.model_dump(), payload=payload,
                                                 request_id=signed.request_id, accepted_version=accepted)
            job = service._jobs[output["answer_id"]]
            assert job.records_cap == cap
            # One job per share at a time: let it end before the next ask.
            for _ in range(500):
                if job.state == "ended":
                    break
                asyncio.run(asyncio.sleep(.01))
            assert job.state == "ended"
    finally:
        service.close()


def test_submit_without_the_keyword_is_the_legacy_cap(legacy, tmp_path, monkeypatch):
    """A caller written before the keyword (tests, batteries) is served as a control plane from before the field."""
    from tests.permissions_v2.test_answer_release import _ask, _service
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=20, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        answer_id = _ask(node, service, "Synthetic message")
        assert service._jobs[answer_id].records_cap == K_ANSWER_LEGACY
    finally:
        service.close()


# --- the checks over sixteen items ----------------------------------------------------------------------------------

def test_eight_sentences_over_sixteen_items_close_to_a_sixteen_record_body(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", "16")
    records = _many(16)
    prompt = build_prompt("What has the owner been doing?", records, precision="none")
    checked = post_check_answer(_sentences(8, 2), records, prompt, mode="with_sources", boundary=Open())
    assert checked.body.outcome == "answered" and checked.kept == 8 and checked.cited == 16
    assert len(checked.body.records) == 16 and [r.record_id for r in checked.body.records] == [r.record_id for r in records]
    only = post_check_answer(_sentences(8, 2), records, prompt, mode="only", boundary=Open())
    assert only.body.outcome == "answered" and only.kept == 8 and "[" not in only.body.answer


def test_seventeen_distinct_citations_are_still_a_body_the_grammar_refuses(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", "24")
    records = _many(17)
    prompt = build_prompt("What has the owner been doing?", records, precision="none")
    text = _sentences(8, 2) + "\nOne more step moved [17]."
    with pytest.raises(PolicyError):
        post_check_answer(text, records, prompt, mode="with_sources", boundary=Open())


def test_the_prompt_asks_for_up_to_eight_sentences_and_the_template_version_moved():
    assert LENGTH_SENTENCE in SYSTEM_PROMPT
    assert "write one to three short sentences" not in SYSTEM_PROMPT
    assert "A citation alone is not an answer." in SYSTEM_PROMPT and "Do not add a source list." in SYSTEM_PROMPT
    assert TEMPLATE_VERSION == "topos-answer-template/v8"


def test_the_items_switch_defaults_to_eight_takes_sixteen_by_setting_and_the_ceiling_stays(monkeypatch):
    """IN167 (WS0's decision at integration): the lane built 16 as the default and counted three times the false
    sentences there; the integrated default is 8 and 16 is the setting. The wire's cap stays 16."""
    assert switches.ANSWER_ITEMS.unbound == switches.ANSWER_ITEMS.bound == K_ANSWER_LEGACY == 8
    assert k_answer() == 8 and K_ANSWER == 16 and ag.K_ANSWER_CEILING == 24
    monkeypatch.setenv(P + "ANSWER_ITEMS", "16")
    assert k_answer() == K_ANSWER == 16


def test_the_longest_answers_fit_the_byte_limits():
    """Sixteen records at the prompt's clip size and eight long cited sentences stay inside the with-sources limit;
    eight long sentences alone stay inside the answer-only limit."""
    long_text = ("The harbour lamp project moved a step and the enclosure moulds were poured again this week. " * 5)[:420]
    records = [_message("0123456789abcdef"[i], f"{long_text} {i}") for i in range(16)]
    sentence = "The owner spent the week on the lantern boards, the harbour sketches and the market stall batch"
    cited = "\n".join(f"{sentence} {i} [{2 * i - 1}, {2 * i}]." for i in range(1, 9))
    body = AnswerWithSources.parse({"version": VERSION, "outcome": "answered", "answer": cited,
                                    "records": [record.model_dump() for record in records]})
    assert len(canonical_bytes(body.model_dump())) < MAX_SEARCH_BYTES // 10
    only = AnswerOnly.parse({"version": VERSION, "outcome": "answered",
                             "answer": "\n".join(f"{sentence} {i}." for i in range(1, 9))})
    assert len(canonical_bytes(only.model_dump())) < MAX_ANSWER_BYTES


@pytest.mark.asyncio
async def test_the_writer_is_given_room_for_eight_sentences(monkeypatch):
    from topos.permissions_v2 import shadow_labeler_local as sl
    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"model": sl.MODEL, "done": True, "message": {"role": "assistant", "content": "A step [1]."}}

    class Client:
        async def post(self, url, timeout=None, json=None):
            posted.append((url, timeout, json))
            return Response()

        async def aclose(self):
            return None

    class Transport:
        client = Client()
        base_url = "http://host.invalid"

        async def verify(self):
            return None

    monkeypatch.setattr(sl, "open_transport", lambda base_url=None: Transport())
    monkeypatch.setattr(sl, "assessment_base_url", lambda settings=None: "http://host.invalid")
    monkeypatch.setenv(P + "ANSWER_ITEMS", "16")              # the sixteen-item prompt is the switch's setting (IN167)
    prompt = build_prompt("What has the owner been doing?", _many(16), precision="none")
    assert await ar._generate_local(prompt, deadline=2_000_000_000) == "A step [1]."
    _url, _timeout, body = posted[0]
    assert ar.ANSWER_NUM_PREDICT == 1024
    assert body["options"] == {"temperature": 0.2, "num_predict": ar.ANSWER_NUM_PREDICT, "num_ctx": 8192}
    assert "keep_alive" not in body and body["think"] is False


def test_the_node_advertises_answer_protocol_three(monkeypatch):
    from topos.engine import registration
    monkeypatch.setattr(switches, "is_bound", lambda: True)
    monkeypatch.setattr(switches, "on", lambda _name: True)
    assert registration.ANSWER_VERSION == ANSWER_PROTOCOL_VERSION == 3
    assert registration._answer_version() == 3
