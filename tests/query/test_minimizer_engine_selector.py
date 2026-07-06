"""Tests for the bounded engine selector + fail-closed fallback (plan §D.2/§D.4)."""

from __future__ import annotations

import pytest

from topos.query.minimizer import (
    DisclosureMinimizer,
    EngineSelector,
    SelectorUnavailable,
    build_minimizer_prompt,
    parse_selected_ids,
)

pytestmark = [pytest.mark.private]

_FACTS = [
    {"fact_id": "rows:0", "text": "launch logistics with Alex"},
    {"fact_id": "rows:1", "text": "sourdough recipe"},
    {"fact_id": "rows:2", "text": "launch budget"},
]
_VALID = {"rows:0", "rows:1", "rows:2"}


# --- parsing -------------------------------------------------------------------------------

def test_parse_json_array():
    assert parse_selected_ids('["rows:0","rows:2"]', valid=_VALID) == {"rows:0", "rows:2"}


def test_parse_json_array_with_surrounding_text():
    text = 'Sure! The necessary facts are: ["rows:0"] — hope that helps.'
    assert parse_selected_ids(text, valid=_VALID) == {"rows:0"}


def test_parse_empty_array_is_valid_keep_nothing():
    assert parse_selected_ids("[]", valid=_VALID) == set()


def test_parse_id_pattern_fallback():
    assert parse_selected_ids("keep rows:1 and rows:2 please", valid=_VALID) == {"rows:1", "rows:2"}


def test_parse_filters_ids_not_in_valid_set():
    assert parse_selected_ids('["rows:0","rows:999","INJECTED"]', valid=_VALID) == {"rows:0"}


def test_parse_garbage_returns_none():
    assert parse_selected_ids("I cannot help with that.", valid=_VALID) is None
    assert parse_selected_ids("", valid=_VALID) is None


def test_prompt_contains_intent_and_facts():
    prompt = build_minimizer_prompt("find the launch info", _FACTS)
    assert "find the launch info" in prompt
    assert "rows:0" in prompt and "sourdough" in prompt


# --- engine selector with injected completion ----------------------------------------------

def test_engine_selector_selects_from_completion():
    sel = EngineSelector(complete=lambda prompt: '["rows:0","rows:2"]')
    assert sel.select(intent="launch", facts=_FACTS) == {"rows:0", "rows:2"}


def test_engine_selector_garbage_raises_unavailable():
    sel = EngineSelector(complete=lambda prompt: "nope")
    with pytest.raises(SelectorUnavailable):
        sel.select(intent="launch", facts=_FACTS)


def test_engine_selector_error_raises_unavailable():
    def _boom(prompt):
        raise RuntimeError("model crashed")

    sel = EngineSelector(complete=_boom)
    with pytest.raises(SelectorUnavailable):
        sel.select(intent="launch", facts=_FACTS)


def test_engine_selector_timeout_raises_unavailable():
    import time

    def _slow(prompt):
        time.sleep(2.0)
        return "[]"

    sel = EngineSelector(complete=_slow, timeout_sec=0.2)
    with pytest.raises(SelectorUnavailable):
        sel.select(intent="launch", facts=_FACTS)


# --- end-to-end through the minimizer with an engine selector ------------------------------

def test_minimizer_with_engine_selector_reduces():
    packet = {"rows": [
        {"record_id": "m0", "content": "launch logistics with Alex"},
        {"record_id": "m1", "content": "sourdough recipe"},
        {"record_id": "m2", "content": "launch budget"},
    ]}
    sel = EngineSelector(complete=lambda prompt: '["rows:0","rows:2"]')
    result = DisclosureMinimizer(selector=sel).minimize(
        packet, intent="launch info", disclosure_tier="default_disclosure"
    )
    contents = {r["content"] for r in result.packet["rows"]}
    assert contents == {"launch logistics with Alex", "launch budget"}
    assert result.used_fallback is False


def test_minimizer_engine_failure_uses_deterministic_fallback():
    def _boom(prompt):
        raise RuntimeError("down")

    packet = {"rows": [
        {"record_id": "m0", "content": "launch logistics with Alex"},
        {"record_id": "m1", "content": "sourdough recipe"},
    ]}
    result = DisclosureMinimizer(selector=EngineSelector(complete=_boom)).minimize(
        packet, intent="launch", disclosure_tier="default_disclosure"
    )
    assert result.used_fallback is True
    contents = {r["content"] for r in result.packet["rows"]}
    assert "sourdough recipe" not in contents  # fallback reduced, didn't pass the fuller candidate
