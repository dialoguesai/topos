"""Kept questions Wave 2 (lane OFF2N): the node's debounced person-readings pass waits as background work.

The readings pass runs on its own timer thread with no background mark, so its Ollama calls (the adapter waits only
inside that mark, ``yield_if_background``) went to the model host ahead of a share's answer job and the owner's chat.
The debounced pass (``_run_now``) is now background work: each model call waits for an answer job in hand and, under
the yield rule, an owner frame in flight, at most 110 s. The pass the owner asks for from the app is not marked and
never waits. The model host is the NR166 suite's stub: no model is called.
"""
from __future__ import annotations

import threading
import time

import pytest

from topos.features.derivation import person_reading
from topos.permissions_v2 import answer_gate, away_config
from tests.permissions_v2.test_nr166_answer_yield import call_the_model, host


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER", raising=False)
    away_config.apply(away_config.DEFAULT)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: object())
    monkeypatch.setattr("topos.core.state.close_thread_db_connection", lambda: None)
    yield
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()
    while answer_gate.active():
        answer_gate.delta(-1)


@pytest.fixture
def readings(monkeypatch):
    """The pass reduced to one model call through the Ollama adapter, as `_default_llm`'s readings make."""
    reached = host(monkeypatch)
    monkeypatch.setattr(person_reading, "refresh_person_readings",
                        lambda conn, dataset_id, **kwargs: (call_the_model(), {"read": 1})[1])
    return reached


def ended_after(seconds, end):
    ended = []
    threading.Timer(seconds, lambda: (ended.append(time.monotonic()), end())).start()
    return ended


def test_the_debounced_pass_waits_for_the_owners_frame(readings):
    answer_gate.owner_frame_began()
    ended = ended_after(0.3, answer_gate.owner_frame_ended)
    person_reading._run_now("an-invented-dataset")
    assert len(readings) == 1 and readings[0] >= ended[0]


def test_the_debounced_pass_waits_for_a_share_answer(readings):
    answer_gate.delta(1)
    ended = ended_after(0.3, lambda: answer_gate.delta(-1))
    person_reading._run_now("an-invented-dataset")
    assert readings[0] >= ended[0]


def test_a_pass_outside_the_debounce_never_waits(readings):
    answer_gate.owner_frame_began()
    answer_gate.delta(1)
    began = time.monotonic()
    person_reading.refresh_person_readings(object(), "an-invented-dataset")   # as the owner's route calls it
    assert readings and readings[0] - began < 0.2
