"""BL-159: the owner node logs one counts-only line per answer fetch: the outcome word, the refusal's code, seconds.

Never a question, an answer, an answer id or a record id. Invented evidence only; no model, network or personal data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_answer_release import _ask, _service
from tests.permissions_v2.test_bl159_answer_delivery import (  # noqa: F401 -- fixtures
    ANSWER, QUESTION, Refusals, answers_node, assess, fetched, first_day, journal_database, rebuild, service, written,
)
from topos.permissions_v2 import answer_transport
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public
LINE = re.compile(r"permissions answer fetch: outcome=(answered|no_answer|pending|held|refused) "
                  r"reason=([a-z0-9_]+|-) seconds=\d+\.\d")
LOGGER = "topos.permissions_v2.answer_release"


def lines(caplog, level=logging.INFO):
    found = []
    for record in caplog.records:
        if record.name == LOGGER and record.levelno >= level and record.getMessage().startswith("permissions answer fetch"):
            match = LINE.fullmatch(record.getMessage())
            assert match, f"not the counts-only shape: {record.getMessage()!r}"
            found.append((match.group(1), match.group(2)))
    return found


def no_content(caplog, *secrets):
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in text


def test_a_delivered_answer_logs_answered_and_nothing_of_it(answers_node, service, monkeypatch, caplog):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    answer_id = written(node, service)
    record_ids = service._jobs[answer_id].record_ids
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and body["outcome"] == "answered"
    assert lines(caplog) == [("answered", "-")]
    no_content(caplog, QUESTION, body["answer"], answer_id, answer_id[4:], *record_ids, "synthetic")


def test_a_second_fetch_logs_refused_answer_unknown(answers_node, service, monkeypatch, caplog):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    fetched(node, service, answer_id, refusals)
    caplog.set_level(logging.INFO, logger=LOGGER)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "answer_unknown"
    assert lines(caplog) == [("refused", "answer_unknown")]


def test_a_fetch_while_the_index_is_not_served_logs_held_with_the_code(answers_node, service, monkeypatch, caplog):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    index_path(node.index.root, "grant-search").unlink()          # as a sweep's drop leaves it
    caplog.set_level(logging.INFO, logger=LOGGER)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and body["state"] == "pending"
    assert lines(caplog) == [("held", "search_index_stale")]


def test_a_moved_answer_logs_refused_authority_moved(answers_node, service, monkeypatch, caplog):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    original = node.search.retrieve_for_answer

    def reworded(**kwargs):
        current, policy, output, decision = original(**kwargs)
        records = [record.model_copy(update={"content": record.content + " Changed."}) for record in output.records]
        return current, policy, output.model_copy(update={"records": records}), decision

    monkeypatch.setattr(node.search, "retrieve_for_answer", reworded)
    caplog.set_level(logging.INFO, logger=LOGGER)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "authority_moved"
    assert lines(caplog) == [("refused", "authority_moved")]


def test_an_authority_change_logs_refused_authority_stale(answers_node, service, monkeypatch, caplog):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    node.activate(node.search_raw, generation=2)
    caplog.set_level(logging.INFO, logger=LOGGER)
    fetched(node, service, answer_id, refusals)
    assert lines(caplog) == [("refused", "authority_stale")]


def test_a_fast_pending_poll_is_debug_and_a_no_answer_is_logged(answers_node, monkeypatch, caplog):
    node = answers_node
    release = threading.Event()

    async def generate(_prompt, *, deadline):
        await asyncio.to_thread(release.wait)
        return "Nothing here cites anything."

    service = _service(node, "unused")
    service.generate = generate
    refusals = Refusals(node, monkeypatch)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    try:
        answer_id = _ask(node, service, QUESTION)
        body, refused = fetched(node, service, answer_id, refusals)
        assert refused is None and body["state"] == "pending"
        assert lines(caplog) == [] and lines(caplog, logging.DEBUG) == [("pending", "-")]
        release.set()
        for _ in range(500):
            body, refused = fetched(node, service, answer_id, refusals)
            if body is None or "outcome" in body:
                break
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
        assert lines(caplog) == [("no_answer", "-")]
    finally:
        release.set()
        service.close()


def test_the_relay_logs_its_own_wait_running_out_without_an_id(monkeypatch, caplog):
    started = threading.Event()
    finish = threading.Event()

    class Socket:
        async def send(self, frame):
            self.frame = json.loads(frame)

    class Service:
        def fetch(self, **_kwargs):
            started.set()
            finish.wait(5)
            return {}, {}

    monkeypatch.setattr(answer_transport, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: Service()))
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.answer_transport")
    socket = Socket()
    try:
        asyncio.run(answer_transport.dispatch_answer(socket, {"id": "req-secret-id", "type": answer_transport.FETCH_TYPE,
                                                              "payload": {"envelope": {}, "intent": {}}}))
    finally:
        finish.set()
    assert socket.frame["code"] == 403
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("permissions answer relay: no result within") and message.endswith("(fetch)")
               for message in messages)
    assert not any("req-secret-id" in message for message in messages)
