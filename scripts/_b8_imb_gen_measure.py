#!/usr/bin/env python3
"""B8 BEFORE/AFTER helper: IMB queries in inference mode on the IMB corpus.

Runs without the full QQ suite. When imb_generative_eval_cases is available,
uses those anchors + judge; otherwise runs bare IMB queries for a BEFORE scout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
ENGINE_QQ = ROOT / "topos" / "tests" / "gap" / "qq" / "engine"
EVAL_SRC = ROOT / "topos-eval" / "src"
sys.path.insert(0, str(ROOT / "topos"))
sys.path.insert(0, str(ENGINE_QQ))
sys.path.insert(0, str(ENGINE_QQ / "adapter"))
sys.path.insert(0, str(EVAL_SRC))


def _ollama_ok() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
        return True
    except Exception:
        return False


def _pin_engine_database(db_path: Path) -> None:
    """Make `db_path` the database the engine opens. Call before anything opens one.

    `AdapterFactory.create("local_database", db_path=X)` does not isolate a run:
    it first asks `core.state.get_db_connection()` for the process handle, and
    that opens AND MIGRATES the settings database (TOPOS_DATABASE_PATH, else
    ~/.topos/database.db) whatever X is. Measured 2026-09-24: with
    TOPOS_DATABASE_PATH naming a scratch file that did not exist, a run created
    it at user_version 78 and wrote a pre-migration backup, while the corpus
    sat in a temp dir. Scope resolution and retrieval read that handle too, not
    the corpus: the manifest's runtime installs, the planner's entity links and
    the profile brief. The settings singleton read the environment at import,
    so both are set, as tests/conftest.py's `_no_live_db_guard` does.
    """
    from topos.config.settings import settings

    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    settings.topos_database_path = str(db_path)


async def _execute_as_owner(orch: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run one turn as the owner's app, as scripts/run_query_eval.py's engine lane does.

    These are the owner's questions about their own data. Since 860efe5f the
    pipeline refuses an inference turn outside availability:read before
    retrieval unless the principal is OWNER_APP. With no principal, all ten
    IMBG turns came back denied / inference_view_unsupported, and the grading
    below scored refusals.
    """
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

    token = set_principal(Principal(cls=OWNER_APP, channel="uds"))
    try:
        return await orch.execute(**kwargs)
    finally:
        reset_principal(token)


async def _run() -> Dict[str, Any]:
    from imbalance_seed_corpus import IMB_CORPUS_VERSION, build_imbalance_corpus
    from imbalance_eval_cases import IMBALANCE_CASES
    from query_eval_cases import manifest_for_scope
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    os.environ.setdefault("TOPOS_QUERY_DDR", "1")

    judge = None
    cases_mod = None
    try:
        from imb_generative_eval_cases import (  # type: ignore
            IMB_GENERATIVE_CASES,
            IMB_GENERATIVE_CATALOG_VERSION,
        )
        cases_mod = IMB_GENERATIVE_CASES
        catalog = IMB_GENERATIVE_CATALOG_VERSION
    except ImportError:
        catalog = "before-no-lane"
        cases_mod = None

    if _ollama_ok():
        try:
            from adapter.llm_judge import OllamaJudge  # noqa: WPS433
            from topos_eval.protocols.corpus import IdealBadPair

            judge = OllamaJudge()
        except Exception as exc:
            print(f"judge unavailable: {exc}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="b8-imb-") as tmp:
        db = Path(tmp) / "imbalance.db"
        # Before anything opens a database. Otherwise the engine's own connection
        # opens the settings database (~/.topos/database.db when TOPOS_DATABASE_PATH
        # is unset), migrates it, and serves the pipeline's reads from it.
        _pin_engine_database(db)
        db = build_imbalance_corpus(db)
        adapters = AdapterFactory.create("local_database", db_path=db)
        orch = QueryPipelineOrchestrator(adapters=adapters)
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        out_cases: List[Dict[str, Any]] = []
        try:
            if cases_mod is not None:
                iterable = cases_mod
            else:
                # BEFORE scout: mirror IMB queries into inference.
                iterable = IMBALANCE_CASES

            for case in iterable:
                if cases_mod is not None:
                    qtext = case.query_text(ro)
                    scope = case.scope_id
                    case_id = case.id
                    layer = case.layer
                    answerable = case.answerable
                    poison = getattr(case, "poison_groups", ()) or ()
                    needles = case.answer_needles(ro) if case.answer_needles else None
                    anchors = case.anchors
                else:
                    qtext = case.query_text(ro)
                    scope = case.scope_id
                    case_id = case.id
                    layer = case.layer
                    answerable = not case.negative
                    poison = case.poison_groups or ()
                    needles = case.oracle(ro).needle_groups if answerable else None
                    anchors = None

                t0 = time.perf_counter()
                try:
                    raw = await _execute_as_owner(
                        orch,
                        query_text=qtext,
                        scope_id=scope,
                        access_mode="inference",
                        manifest=manifest_for_scope(scope),
                        query_session_id=f"b8-{case_id}-{uuid.uuid4().hex[:8]}",
                    )
                except Exception as exc:
                    raw = {
                        "turn_outcome": "error",
                        "deny_reason": f"{type(exc).__name__}: {exc}",
                        "public_result": None,
                    }
                elapsed = round((time.perf_counter() - t0) * 1000, 1)
                # No public_result: the turn was refused (every deny path returns
                # None) or raised, so it never reached an answer. Its empty answer
                # is not an abstention. It grades False and the judge skips it.
                answered = isinstance(raw.get("public_result"), dict)
                pr = raw["public_result"] if answered else {}
                answer = pr.get("answer")
                if isinstance(answer, dict):
                    answer = answer.get("answer") or answer.get("text") or str(answer)
                answer_s = "" if answer is None else str(answer)
                conf = pr.get("confidence")
                try:
                    conf_f = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    conf_f = 0.0
                blob = answer_s.lower()
                poison_hit = [
                    g[0] for g in poison if any(alt.lower() in blob for alt in g)
                ]
                correct: Optional[bool] = None
                if not answered:
                    correct = False
                elif answerable and needles:
                    correct = all(
                        any(alt.lower() in blob for alt in group) for group in needles
                    )
                elif not answerable:
                    correct = conf_f <= 0.5 or "unknown" in blob or not blob

                row: Dict[str, Any] = {
                    "case_id": case_id,
                    "layer": layer,
                    "scope_id": scope,
                    "query": qtext,
                    "answerable": answerable,
                    "answer": answer_s[:400],
                    "confidence": conf_f,
                    "correct": correct,
                    "poison_hit": poison_hit,
                    "latency_ms": elapsed,
                    "turn_outcome": raw.get("turn_outcome"),
                    "deny_reason": raw.get("deny_reason"),
                    "answered": answered,
                }

                # Score-packet attribution metadata (owner_authored / speaker_label).
                scores = (raw.get("disclosure_decision_record") or {}).get(
                    "filtered_packet", {}
                )
                if not scores:
                    # Fall back: re-read via DDR retrieval if present
                    ddr = raw.get("disclosure_decision_record") or {}
                    scores = (ddr.get("filtered") or ddr.get("filtered_packet") or {}).get(
                        "scores"
                    ) or []
                if isinstance(scores, list):
                    row["score_owner_flags"] = [
                        s.get("owner_authored")
                        for s in scores
                        if isinstance(s, dict) and "owner_authored" in s
                    ][:8]
                    row["speaker_labels"] = [
                        s.get("speaker_label")
                        for s in scores
                        if isinstance(s, dict) and s.get("speaker_label")
                    ][:8]

                if judge is not None and anchors is not None and answered:
                    from adapter.target_engine import normalize_result
                    from topos_eval.protocols.corpus import IdealBadPair

                    resp = normalize_result(raw, elapsed)
                    pair = IdealBadPair(
                        ideal=anchors.ideal,
                        bad_over_disclosure=anchors.bad_over_disclosure,
                        bad_confabulation=anchors.bad_confabulation,
                        bad_noise=anchors.bad_noise,
                    )
                    verdicts = [
                        judge.score(resp, resp.evidence, pair, sample_index=i)
                        for i in range(3)
                    ]
                    faith = [v.faithfulness for v in verdicts if v.faithfulness is not None]
                    role = [
                        v.role_appropriate
                        for v in verdicts
                        if v.role_appropriate is not None
                    ]
                    row["faithfulness_mean"] = (
                        round(sum(faith) / len(faith), 3) if faith else None
                    )
                    row["role_appropriate_mean"] = (
                        round(sum(role) / len(role), 3) if role else None
                    )
                out_cases.append(row)
        finally:
            ro.close()

    faith_all = [
        c["faithfulness_mean"]
        for c in out_cases
        if isinstance(c.get("faithfulness_mean"), (int, float))
    ]
    role_all = [
        c["role_appropriate_mean"]
        for c in out_cases
        if isinstance(c.get("role_appropriate_mean"), (int, float))
    ]
    poison_cases = [c for c in out_cases if c.get("poison_hit") is not None]
    # only count cases that had poison groups defined
    poison_n = sum(1 for c in out_cases if c.get("poison_hit"))
    return {
        "catalog_version": catalog,
        "imb_corpus_version": IMB_CORPUS_VERSION,
        "n_cases": len(out_cases),
        "faithfulness_mean": round(sum(faith_all) / len(faith_all), 3) if faith_all else None,
        "role_appropriate_mean": round(sum(role_all) / len(role_all), 3) if role_all else None,
        "poison_hit_cases": poison_n,
        "not_answered": [c["case_id"] for c in out_cases if not c["answered"]],
        "correct_rate": round(
            sum(1 for c in out_cases if c.get("correct") is True)
            / max(1, sum(1 for c in out_cases if c.get("correct") is not None)),
            3,
        ),
        "cases": out_cases,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    report = asyncio.run(_run())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2))
    for c in report["cases"]:
        print(
            f"  {c['case_id']:6} {c.get('turn_outcome')!s:10} correct={c.get('correct')!s:5} "
            f"poison={c.get('poison_hit')} conf={c.get('confidence')} "
            f"ans={c.get('answer', '')[:60]!r}"
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
