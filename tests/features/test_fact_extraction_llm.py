"""Role-gated LLM fact extraction (B4 / PLAN_PROVENANCE_SPLIT P4.3).

STUB-ONLY: every test injects a canned extractor (no Ollama, no network) so the
lane is machine-independent. Pins the load-bearing invariants:

  * ROLE GATE: authored => owner facts; assistant/contact addressed =>
    ATTRIBUTED (asserted_by != 'owner'); observed/ambient => ZERO facts even
    with the LLM on (safety-critical A1 class).
  * PARSER: clean JSON, messy fenced JSON, rigid line format, and drops
    non-owner-subject triples.
  * assert_fact integration: facts land with the right asserted_by.
  * TOPOS_FACTS_LLM off => the additive pass is a no-op.
  * Ollama unreachable => graceful fallback: the rules floor still ran, the LLM
    pass wrote nothing, and NOTHING crashed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.facts.extract import extract_facts_from_batch
from topos.features.facts.llm_extract import (
    build_extraction_prompt,
    extract_owner_facts_llm,
    facts_llm_enabled,
    parse_triples,
)
from topos.features.facts.store import FactStore
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "facts.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    db.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)"
    )
    db.commit()
    yield db
    db.close()


def _active_facts(conn) -> list:
    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _facts_by_predicate(conn, predicate: str) -> list:
    return [f for f in _active_facts(conn) if f.get("predicate") == predicate]


# --------------------------------------------------------------------------
# Stub extractors (NO network). A stub inspects row['content'] and returns
# canned TRIPLE dicts, exactly like the real parser would.
# --------------------------------------------------------------------------


def _stub_by_content(mapping):
    """Build an extractor that returns canned triples keyed by a content substring."""

    def _extract(prompt, row):
        content = str(row.get("content") or "")
        for needle, triples in mapping.items():
            if needle in content:
                return [dict(t) for t in triples]
        return []

    return _extract


# --------------------------------------------------------------------------
# Row builders matching canonical batch shapes.
# --------------------------------------------------------------------------


def _ai_chat_owner(content, mid="a-owner"):
    return {
        "message_id": mid,
        "conversation_id": "chatgpt:conv-1",
        "sender_type": "human",  # canonical owner sender for ai_chat
        "sender_id": None,
        "content": content,
        "event_at": "2026-06-01T10:00:00+00:00",
        "_table": "ai_chat_messages",
    }


def _ai_chat_assistant(content, mid="a-model"):
    return {
        "message_id": mid,
        "conversation_id": "chatgpt:conv-1",
        "sender_type": "assistant",
        "sender_id": None,
        "content": content,
        "event_at": "2026-06-01T10:00:01+00:00",
        "_table": "ai_chat_messages",
    }


def _messenger_contact(content, sender_id="+15551234567", mid="m-them"):
    return {
        "message_id": mid,
        "conversation_id": "c1",
        "sender_type": "human",  # messenger writes 'human' for everyone
        "sender_id": sender_id,
        "content": content,
        "event_at": "2026-06-01T10:00:00+00:00",
        "is_from_self": 0,
        "_table": "conversation_messages",
    }


def _messenger_owner(content, mid="m-me"):
    return {
        "message_id": mid,
        "conversation_id": "c1",
        "sender_type": "human",
        "sender_id": "self",
        "content": content,
        "event_at": "2026-06-01T10:00:00+00:00",
        "is_from_self": 1,
        "_table": "conversation_messages",
    }


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class TestParser:
    def test_clean_json_array(self):
        out = parse_triples('[{"predicate":"lives_in","object":"Berlin"}]')
        assert out == [{"predicate": "lives_in", "object": "Berlin"}]

    def test_messy_fenced_json_with_prose(self):
        raw = 'Sure! Here:\n```json\n[{"predicate":"prefers","object":"cold brew","period_start":"2026"}]\n```'
        out = parse_triples(raw)
        assert out == [
            {"predicate": "prefers", "object": "cold brew", "period_start": "2026"}
        ]

    def test_rigid_line_format(self):
        out = parse_triples("lives_in | Berlin | 2024 |\npractices | yoga")
        assert {"predicate": "lives_in", "object": "Berlin", "period_start": "2024"} in out
        assert {"predicate": "practices", "object": "yoga"} in out

    def test_single_object_and_aliases(self):
        # model emits a bare object with an aliased key ("value" instead of "object")
        out = parse_triples('{"pred":"skilled_in","value":"Python"}')
        assert out == [{"predicate": "skilled_in", "object": "Python"}]

    def test_junk_returns_empty(self):
        assert parse_triples("I could not find any facts.") == []
        assert parse_triples("") == []
        assert parse_triples("[]") == []

    def test_incomplete_triples_dropped(self):
        assert parse_triples('[{"predicate":"lives_in"},{"object":"orphan"}]') == []

    def test_prompt_is_owner_scoped_and_bounded(self):
        prompt = build_extraction_prompt("x" * 10000)
        assert "SPEAKER" in prompt or "speaker" in prompt
        assert "lives_in" in prompt  # known-predicate hint present
        assert len(prompt) < 6000  # content clamped


# --------------------------------------------------------------------------
# Role gate (the load-bearing invariant)
# --------------------------------------------------------------------------


class TestRoleGate:
    def test_authored_owner_row_writes_owner_facts(self, conn):
        rows = [_ai_chat_owner("been vegetarian since college")]
        stub = _stub_by_content(
            {"vegetarian": [{"predicate": "prefers", "object": "vegetarian", "period_start": "college"}]}
        )
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        assert written == 1
        facts = _facts_by_predicate(conn, "prefers")
        assert facts[0]["object_value"] == "vegetarian"
        assert facts[0]["asserted_by"] == "owner"
        assert facts[0]["period_start"] == "college"

    def test_assistant_addressed_row_is_attributed_not_owner(self, conn):
        rows = [_ai_chat_assistant("I live in Berlin")]
        stub = _stub_by_content({"Berlin": [{"predicate": "lives_in", "object": "Berlin"}]})
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        assert written == 1
        facts = _facts_by_predicate(conn, "lives_in")
        assert facts[0]["asserted_by"] == "assistant"  # NEVER 'owner'

    def test_contact_addressed_row_attributed_to_contact(self, conn):
        rows = [_messenger_contact("I moved to Lisbon", sender_id="ana")]
        stub = _stub_by_content({"Lisbon": [{"predicate": "lives_in", "object": "Lisbon"}]})
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        # v1 conversation_messages: non-self rows resolve to 'observed', NOT
        # 'addressed' (participated/addressed heuristic deferred) — so a contact
        # DM yields ZERO owner facts. This is the safe floor: no misattribution.
        assert written == 0
        assert _facts_by_predicate(conn, "lives_in") == []

    def test_witnessed_ambient_row_yields_zero_owner_facts(self, conn):
        # Safety-critical A1: a witnessed medical/allergy statement from another
        # sender must never become an owner fact, even with the LLM on.
        rows = [_messenger_contact("I'm deathly allergic to shellfish", sender_id="bob")]
        stub = _stub_by_content(
            {"shellfish": [{"predicate": "allergic_to", "object": "shellfish"}]}
        )
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        assert written == 0
        assert _active_facts(conn) == []

    def test_ambient_posture_caps_owner_row(self, conn):
        # An ambient-posture source caps even an owner-authored row below
        # 'authored' — no belief-grade fact can be minted.
        row = _ai_chat_owner("been vegetarian since college")
        row["_posture"] = "ambient"
        stub = _stub_by_content(
            {"vegetarian": [{"predicate": "prefers", "object": "vegetarian"}]}
        )
        written = extract_owner_facts_llm(conn, [row], extractor=stub)
        assert written == 0

    def test_observed_conversation_row_skipped(self, conn):
        rows = [_messenger_contact("I run marathons", sender_id="carol")]
        stub = _stub_by_content(
            {"marathons": [{"predicate": "training_for", "object": "marathon"}]}
        )
        assert extract_owner_facts_llm(conn, rows, extractor=stub) == 0

    def test_messenger_owner_row_writes_owner_fact(self, conn):
        rows = [_messenger_owner("I switched from tea to cold brew this year")]
        stub = _stub_by_content(
            {"cold brew": [{"predicate": "prefers", "object": "cold brew", "period_start": "2026"}]}
        )
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        assert written == 1
        assert _facts_by_predicate(conn, "prefers")[0]["asserted_by"] == "owner"


# --------------------------------------------------------------------------
# Subject filter
# --------------------------------------------------------------------------


class TestSubjectFilter:
    def test_non_owner_subject_triple_dropped(self, conn):
        # The LLM leaks a fact about the owner's sister; subject != owner => drop.
        rows = [_ai_chat_owner("my sister Nadia moved to Berlin for a design job")]
        stub = _stub_by_content(
            {
                "Nadia": [
                    {"predicate": "lives_in", "object": "Berlin", "subject": "Nadia"},
                    {"predicate": "works_on", "object": "design"},  # owner-subject: kept
                ]
            }
        )
        written = extract_owner_facts_llm(conn, rows, extractor=stub)
        assert written == 1  # only the owner-subject triple
        assert _facts_by_predicate(conn, "lives_in") == []
        assert _facts_by_predicate(conn, "works_on")[0]["object_value"] == "design"


# --------------------------------------------------------------------------
# Flag + graceful degradation
# --------------------------------------------------------------------------


class TestFlag:
    def test_disabled_flag_makes_batch_llm_a_noop(self, conn, monkeypatch):
        monkeypatch.setenv("TOPOS_FACTS_LLM", "off")
        from topos.config import settings as settings_mod

        # Rebuild the Settings singleton so the env var is picked up.
        monkeypatch.setattr(settings_mod, "settings", settings_mod.Settings())
        assert facts_llm_enabled(settings_mod.settings) is False

    def test_auto_is_inert_under_pytest(self, monkeypatch):
        # AUTO (flag unset) resolves OFF under pytest so extract_facts_from_batch
        # never hits real Ollama in the suite — even though a model is set.
        assert monkeypatch  # noqa: placeholder to keep signature explicit

        class _AutoModelSet:
            topos_facts_llm = None
            ollama_extraction_model = "qwen3.5:9b-mlx"
            ollama_query_model = "llama3.2:latest"

        # PYTEST_CURRENT_TEST is set while this runs => auto path is inert.
        assert facts_llm_enabled(_AutoModelSet()) is False

    def test_auto_on_when_model_set_outside_pytest(self, monkeypatch):
        # Simulate a non-test process: the auto default is ON when a model resolves.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        class _AutoModelSet:
            topos_facts_llm = None
            ollama_extraction_model = "qwen3.5:9b-mlx"
            ollama_query_model = "llama3.2:latest"

        assert facts_llm_enabled(_AutoModelSet()) is True

    def test_auto_off_when_no_model(self):
        # No extraction/query model resolves (both empty) + flag unset => auto-off.
        # (pydantic coerces empty env vars to field defaults, so exercise the
        # resolver against an explicit no-model settings-like object.)
        class _NoModel:
            topos_facts_llm = None
            ollama_extraction_model = ""
            ollama_query_model = ""

        assert facts_llm_enabled(_NoModel()) is False

    def test_explicit_enable_overrides_missing_model(self):
        # Forcing on ("1") enables even with no model — the pass then degrades
        # to inert at runtime (no extractor => no facts), but the flag is ON.
        class _ForcedOnNoModel:
            topos_facts_llm = "1"
            ollama_extraction_model = ""
            ollama_query_model = ""

        assert facts_llm_enabled(_ForcedOnNoModel()) is True

    def test_batch_llm_off_leaves_only_rules_facts(self, conn, monkeypatch):
        monkeypatch.setenv("TOPOS_FACTS_LLM", "off")
        from topos.config import settings as settings_mod

        monkeypatch.setattr(settings_mod, "settings", settings_mod.Settings())
        # A rules-extractable owner message: rules floor writes works_at.
        rows = [_ai_chat_owner("I work at Dialogues these days")]
        written = extract_facts_from_batch(conn, rows)
        assert written == 1  # rules only, LLM pass skipped
        assert _facts_by_predicate(conn, "works_at")[0]["object_value"] == "Dialogues"


class TestGracefulDegradation:
    def test_ollama_unreachable_falls_back_to_rules(self, conn, monkeypatch):
        """The real extractor raises (server down); the rules floor still ran and
        nothing crashed. Wired through the batch entry to prove ingestion is safe."""
        monkeypatch.setenv("TOPOS_FACTS_LLM", "on")
        from topos.config import settings as settings_mod

        monkeypatch.setattr(settings_mod, "settings", settings_mod.Settings())

        # Force the LLM path to use a "real" extractor that simulates a dead server.
        import topos.features.facts.llm_extract as llm

        def _dead_extractor(prompt, row):
            raise RuntimeError("Ollama request failed: connection refused")

        monkeypatch.setattr(llm, "_make_ollama_extractor", lambda model: _dead_extractor)

        rows = [_ai_chat_owner("I work at Dialogues these days")]
        # Must not raise; rules floor writes works_at.
        written = extract_facts_from_batch(conn, rows)
        assert written == 1
        assert _facts_by_predicate(conn, "works_at")[0]["object_value"] == "Dialogues"

    def test_unreachable_stops_the_pass_early(self, conn, monkeypatch):
        import topos.features.facts.llm_extract as llm

        calls = {"n": 0}

        def _dead(prompt, row):
            calls["n"] += 1
            raise RuntimeError("urlopen error [Errno 61] Connection refused")

        monkeypatch.setattr(llm, "_make_ollama_extractor", lambda model: _dead)
        rows = [_ai_chat_owner("a", "1"), _ai_chat_owner("b", "2"), _ai_chat_owner("c", "3")]
        written = extract_owner_facts_llm(conn, rows, model="fake-model")
        assert written == 0
        assert calls["n"] == 1  # stopped after the first unreachable error

    def test_per_row_error_is_non_fatal(self, conn):
        # A non-transport error on one row is logged and skipped; other rows still process.
        def _flaky(prompt, row):
            if "boom" in str(row.get("content")):
                raise ValueError("garbled model output")
            return [{"predicate": "lives_in", "object": "Berlin"}]

        rows = [
            _ai_chat_owner("boom bad row", "1"),
            _ai_chat_owner("I live in Berlin now", "2"),
        ]
        written = extract_owner_facts_llm(conn, rows, extractor=_flaky)
        assert written == 1
        assert _facts_by_predicate(conn, "lives_in")[0]["object_value"] == "Berlin"


# --------------------------------------------------------------------------
# assert_fact integration + additivity with the rules floor
# --------------------------------------------------------------------------


class TestAssertIntegration:
    def test_llm_and_rules_are_additive_and_deduped(self, conn, monkeypatch):
        monkeypatch.setenv("TOPOS_FACTS_LLM", "on")
        from topos.config import settings as settings_mod

        monkeypatch.setattr(settings_mod, "settings", settings_mod.Settings())

        import topos.features.facts.llm_extract as llm

        # LLM returns the SAME works_at the rules floor extracts (dedup) plus a
        # NEW paraphrased fact the rules miss.
        def _extractor(prompt, row):
            return [
                {"predicate": "works_at", "object": "Dialogues"},  # dup of rules
                {"predicate": "prefers", "object": "cold brew"},  # new
            ]

        monkeypatch.setattr(llm, "_make_ollama_extractor", lambda model: _extractor)

        rows = [_ai_chat_owner("I work at Dialogues these days")]
        extract_facts_from_batch(conn, rows)

        works = _facts_by_predicate(conn, "works_at")
        assert len(works) == 1  # rules + LLM collapsed to one active row
        assert works[0]["object_value"] == "Dialogues"
        assert _facts_by_predicate(conn, "prefers")[0]["object_value"] == "cold brew"

    def test_confidence_defaults_below_resume(self, conn):
        from topos.features.facts.llm_extract import DEFAULT_CONFIDENCE
        from topos.features.facts.store import CONFLICT_CONFIDENCE_MARGIN

        # A chat-derived LLM fact must not silently supersede a resume incumbent.
        assert DEFAULT_CONFIDENCE + CONFLICT_CONFIDENCE_MARGIN < 0.9

        rows = [_ai_chat_owner("I switched to cold brew")]
        stub = _stub_by_content({"cold brew": [{"predicate": "prefers", "object": "cold brew"}]})
        extract_owner_facts_llm(conn, rows, extractor=stub)
        fact = _facts_by_predicate(conn, "prefers")[0]
        assert fact["confidence"] == pytest.approx(DEFAULT_CONFIDENCE, abs=0.01)

    def test_source_ref_records_table_and_record(self, conn):
        rows = [_ai_chat_owner("been vegetarian since college", mid="rec-42")]
        stub = _stub_by_content({"vegetarian": [{"predicate": "prefers", "object": "vegetarian"}]})
        extract_owner_facts_llm(conn, rows, extractor=stub)
        refs = conn.execute(
            "SELECT source_refs_json FROM signal_objects WHERE object_type='fact'"
            " AND valid_to IS NULL AND payload_json LIKE '%vegetarian%'"
        ).fetchone()[0]
        refs = json.loads(refs)
        assert refs[0]["table"] == "ai_chat_messages"
        assert refs[0]["record_id"] == "rec-42"
