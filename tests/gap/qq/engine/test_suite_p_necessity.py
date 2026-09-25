"""SUITE-P necessity leg: the old lane, measured on its corpus, as the owner.

protects: the kill-switch comparison `scripts/run_query_eval.py --aggregate`
prints ("old lane N/9") measures today's inference lane. Measured 2026-09-24,
three things each made it measure something else:

- the door: `AdapterFactory.create(db_path=corpus)` first opened and MIGRATED
  the settings database, so with TOPOS_DATABASE_PATH unset the "throwaway" run
  stamped ~/.topos/database.db;
- the principal: the leg sent inference with none, and since 860efe5f all nine
  turns were refused before retrieval (`inference_view_unsupported`);
- the rubric: it searched the whole response for digits, so a refused turn
  passed on the random hex in its session id (81 and 98 of 200 refused P-07
  turns, in two runs).

The live-model half of the leg still runs only via the script.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from topos.config.settings import settings
from topos.principal import OWNER_APP, Principal, current_principal
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

from query_eval_cases import AGGREGATE_CASES, manifest_for_scope, necessity_answer_contains
from tests.fixtures.query_eval_seed.apply_aggregate_seed import apply_aggregate_seed

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "run_query_eval.py"
CASES = {c.id: c for c in AGGREGATE_CASES}


def _first_expected(case) -> str:
    return f"{next(iter(case.expect.values())):g}"


# --- the rubric ---------------------------------------------------------------


def _answered(answer, items=None):
    return {
        "turn_outcome": "live_query",
        "session_id": "suitep-old-P-07-8a3f0c12",
        "public_result": {"answer_type": "yes_no", "answer": answer, "items": items or [],
                          "confidence": 0.8},
    }


def test_a_refused_turn_fails_even_with_the_number_in_its_session_id():
    # _execute_turn's refusal, verbatim in shape.
    refused = {
        "turn_outcome": "denied", "public_result": None,
        "deny_reason": "inference_view_unsupported",
        "session_id": "suitep-old-P-07-8a3f0c12", "query_session_id": "suitep-old-P-07-8a3f0c12",
        "supported_inference_scopes": ["availability:read"], "supported_derived_filter_ids": [],
        "audit": {"deny_reason": "inference_view_unsupported", "stores_touched": []},
    }
    assert necessity_answer_contains(CASES["P-07"], refused) == (
        False, "denied: inference_view_unsupported",
    )


def test_the_envelope_is_not_graded():
    ok, reason = necessity_answer_contains(CASES["P-07"], _answered("unknown"))
    assert not ok, reason


@pytest.mark.parametrize(
    ("case_id", "answer", "passes"),
    [
        ("P-07", "You logged 25 calm entries.", True),
        ("P-07", "calm: 25, anxious: 15", True),
        ("P-07", "It was 128 entries.", False),  # 8 and 12 inside a bigger number
        ("P-07", "Between 2026-08-24 and 2026-08-28.", False),  # dates
        ("P-07", "The last one was at 10:25.", False),  # a clock time
        ("P-01", "You sent 5,200 messages.", True),
        ("P-01", "5200.0", True),
        ("P-01", "About 52,000.", False),
        ("P-04", "You spent $1,000.00 on groceries.", True),
        ("P-06", "Roughly 12000 a month.", True),
    ],
)
def test_only_a_number_stated_on_its_own_counts(case_id, answer, passes):
    ok, reason = necessity_answer_contains(CASES[case_id], _answered(answer))
    assert ok is passes, reason


def test_items_and_structured_answers_are_the_answer_too():
    assert necessity_answer_contains(CASES["P-09"], _answered("list", ["July 1st: 40 events"]))[0]
    assert necessity_answer_contains(CASES["P-07"], _answered({"calm": 25}))[0]


# --- the principal --------------------------------------------------------------


@pytest.fixture(scope="module")
def run_query_eval() -> ModuleType:
    """The script, loaded by path. It defaults TOPOS_SCOPE_SHADOW to 0 at import when
    the variable is unset; importing with it set keeps that out of the session."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TOPOS_SCOPE_SHADOW", os.environ.get("TOPOS_SCOPE_SHADOW") or "0")
        spec = importlib.util.spec_from_file_location("_run_query_eval_under_test", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def corpus(tmp_path, monkeypatch) -> Path:
    """The SUITE-P corpus, made the engine's database the way main() pins it."""
    db = tmp_path / "suitep.db"
    conn = sqlite3.connect(str(db))
    apply_all_migrations(conn)
    apply_aggregate_seed(conn)
    conn.close()
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))
    monkeypatch.setattr(settings, "topos_database_path", str(db))
    return db


@pytest.mark.asyncio
async def test_the_necessity_leg_asks_as_the_verb_legs_owner(run_query_eval, corpus, monkeypatch):
    asked = []

    async def record(self, **kwargs):
        asked.append(current_principal())
        return {"turn_outcome": "live_query", "public_result": {"answer": "unknown"}}

    monkeypatch.setattr(QueryPipelineOrchestrator, "execute", record)
    rows = await run_query_eval.run_aggregate_eval(corpus)

    # The verb leg read the pinned corpus through the engine's own connection.
    assert all(r["verb_pass"] for r in rows), [r["verb_reason"] for r in rows]
    assert asked == [Principal(cls=OWNER_APP, channel="cp_relay")] * len(AGGREGATE_CASES)


@pytest.mark.asyncio
async def test_without_it_every_necessity_turn_is_refused_before_retrieval(corpus):
    assert current_principal() is None
    orch = QueryPipelineOrchestrator(adapters=AdapterFactory.create("local_database", db_path=corpus))
    for case in AGGREGATE_CASES:
        out = await orch.execute(
            query_text=case.necessity_query,
            scope_id=case.necessity_scope,
            access_mode="inference",
            manifest=manifest_for_scope(case.necessity_scope),
            # The expected number in the id, where the old rubric used to find it.
            query_session_id=f"suitep-old-{case.id}-{_first_expected(case)}",
        )
        assert (out["turn_outcome"], out["deny_reason"]) == ("denied", "inference_view_unsupported")
        assert out["audit"]["stores_touched"] == []
        assert necessity_answer_contains(case, out) == (False, "denied: inference_view_unsupported")


@pytest.mark.asyncio
async def test_run_aggregate_eval_refuses_a_corpus_the_engine_does_not_open(run_query_eval, tmp_path):
    corpus = tmp_path / "unpinned.db"  # the settings path is conftest's guard file
    sqlite3.connect(str(corpus)).close()
    with pytest.raises(RuntimeError, match="the engine resolves"):
        await run_query_eval.run_aggregate_eval(corpus)


# --- the door -------------------------------------------------------------------


def _run_script(tmp_path: Path, settings_db: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TOPOS_DATABASE_PATH"] = str(settings_db)
    env["TMPDIR"] = str(tmp_path)  # --aggregate's mkdtemp corpus lands under tmp_path
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


def test_aggregate_never_opens_the_settings_database(tmp_path):
    """The sentinel method: name a settings database that does not exist, and the
    run must not create it. Before the pin this made it, at the migration head."""
    sentinel = tmp_path / "settings" / "database.db"
    result = _run_script(tmp_path, sentinel, "--aggregate", "--no-necessity")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUITE-P: verb 9/9 exact" in result.stdout
    assert not sentinel.parent.exists(), "an --aggregate run opened the settings database"


def test_the_default_lane_refuses_a_db_the_engine_does_not_open(tmp_path):
    sentinel = tmp_path / "settings" / "database.db"
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).close()
    before = other.read_bytes()
    result = _run_script(tmp_path, sentinel, "--db", str(other))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "is not the database the engine opens" in result.stderr
    assert not sentinel.parent.exists()
    assert other.read_bytes() == before
