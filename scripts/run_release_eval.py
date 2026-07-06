#!/usr/bin/env python3
"""Per-release privacy evaluation → a version-stamped scorecard.

Stitches the privacy eval harnesses (UAR, CER, minimality, negotiation A/B, latency) into one
report, stamped with the release version, git sha, corpus version, and model config, and
writes it to `eval_reports/<version>.json` (+ appends `eval_reports/history.jsonl`).

This is the DETERMINISTIC lane: it does not call the live LLM, so the numbers are reproducible
and safe to publish. The live-LLM lane (semantic judge / live requester agent) is a separate,
internal-only run because it drifts run-to-run.

Usage:
    python3 scripts/run_release_eval.py                 # write report, exit non-zero if a gate fails
    python3 scripts/run_release_eval.py --no-gate       # write report, always exit 0
    python3 scripts/run_release_eval.py --print         # also print the marketing summary

Wire into the release-topos skill right after the version bump so the report is stamped with
the version being shipped and included in the release commit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tests" / "gap" / "qq" / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Deny-path p95 budget (ms) — the tier-1 latency gate (a slow deny is a side-channel).
DENY_P95_BUDGET_MS = 500.0


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _version() -> str:
    try:
        import topos

        return str(getattr(topos, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _model_config() -> Dict[str, str]:
    try:
        from topos.config.settings import settings

        return {
            "minimizer": getattr(settings, "disclosure_minimizer_model", "?"),
            "judge": getattr(settings, "privacy_judge_model", "?"),
        }
    except Exception:
        return {"minimizer": "?", "judge": "?"}


def _run_uar() -> Dict[str, Any]:
    from tests.evals.privacy.uar.test_uar_engine import run_uar_battery

    scorecard, _ = run_uar_battery()
    return scorecard


def _run_cer() -> Dict[str, Any]:
    from tests.evals.privacy.cer.test_cer_engine import run_cer

    scorecard, owner = run_cer()
    scorecard["owner_visibility"] = owner
    return scorecard


def _run_negotiation_and_minimality() -> Dict[str, Any]:
    from tests.evals.privacy.negotiation.ab_harness import DEFAULT_TASK, build_scorecard, run_ab
    from tests.evals.privacy.common.minimality import score_facts

    ab = build_scorecard()
    ab.pop("arm_results", None)

    res = run_ab()
    minimality = {
        arm: score_facts(
            res[arm].facts, gold=[DEFAULT_TASK.necessary_token], sensitive_markers=DEFAULT_TASK.sensitive_markers
        ).to_dict()
        for arm in ("open", "firewall", "negotiated")
    }
    return {"negotiation": ab, "minimality": minimality}


def _run_latency() -> Dict[str, Any]:
    from tests.evals.privacy.perf.perf_harness import build_perf_report

    return build_perf_report(n=8)


def _run_pytest_gate() -> Dict[str, Any]:
    """Run the full privacy gate suite (the invariant tests the scorecard doesn't cover:
    subset/injection, idempotence, existence parity, phone/date redaction, cache isolation,
    dense sparsification). A failure blocks the release."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / "evals" / "privacy"),
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else ""
    return {"passed": proc.returncode == 0, "returncode": proc.returncode, "summary": summary}


def _collect(*, run_pytest: bool = True) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "version": _version(),
        "code_sha": _git_sha(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lane": "deterministic",
        "models": _model_config(),
        "errors": {},
    }
    try:
        from tests.evals.privacy.common.corpus import CORPUS_VERSION

        report["corpus_version"] = CORPUS_VERSION
    except Exception:
        report["corpus_version"] = "unknown"

    for name, fn in (
        ("uar", _run_uar),
        ("cer", _run_cer),
        ("negotiation_minimality", _run_negotiation_and_minimality),
        ("latency", _run_latency),
    ):
        try:
            result = fn()
            if name == "negotiation_minimality":
                report["negotiation"] = result["negotiation"]
                report["minimality"] = result["minimality"]
            else:
                report[name] = result
        except Exception as exc:  # noqa: BLE001 — a harness failure is a gate failure (fail closed)
            report["errors"][name] = f"{type(exc).__name__}: {exc}"

    if run_pytest:
        try:
            report["pytest_gate"] = _run_pytest_gate()
        except Exception as exc:  # noqa: BLE001
            report["pytest_gate"] = {"passed": False, "returncode": -1, "summary": f"{type(exc).__name__}: {exc}"}

    report["gates"] = _evaluate_gates(report)
    return report


def _evaluate_gates(report: Dict[str, Any]) -> Dict[str, Any]:
    """Tier-1 hard gates: any failure blocks the release."""
    failures: List[str] = []

    if report.get("errors"):
        for name, err in report["errors"].items():
            failures.append(f"harness_error:{name}: {err}")

    uar = report.get("uar") or {}
    if uar.get("uar", 1.0) != 0.0:
        failures.append(f"UAR != 0 (leaks={uar.get('leaks')})")

    cer = report.get("cer") or {}
    if cer.get("cer", 1.0) != 0.0:
        failures.append(f"CER != 0 (leaked={cer.get('leaked_tokens')})")

    minim = report.get("minimality") or {}
    for arm in ("firewall", "negotiated"):
        se = (minim.get(arm) or {}).get("sensitive_excess", 1)
        if se != 0:
            failures.append(f"sensitive_excess != 0 for grantee arm '{arm}' (={se})")

    deny_p95 = ((report.get("latency") or {}).get("paths") or {}).get("deny", {}).get("p95")
    if deny_p95 is not None and deny_p95 > DENY_P95_BUDGET_MS:
        failures.append(f"deny p95 {deny_p95}ms > {DENY_P95_BUDGET_MS}ms")

    pytest_gate = report.get("pytest_gate")
    if pytest_gate is not None and not pytest_gate.get("passed"):
        failures.append(f"privacy pytest suite failed ({pytest_gate.get('summary')})")

    return {"passed": not failures, "failures": failures}


def _write(report: Dict[str, Any]) -> Path:
    out_dir = ROOT / "eval_reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{report['version']}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")

    # Append a compact one-line-per-version trend record.
    hist = out_dir / "history.jsonl"
    summary = _marketing_summary(report)
    with hist.open("a") as f:
        f.write(json.dumps(summary, default=str) + "\n")
    return path


def _marketing_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    neg = report.get("negotiation") or {}
    minim = report.get("minimality") or {}
    lat = (report.get("latency") or {}).get("paths") or {}
    return {
        "version": report.get("version"),
        "code_sha": report.get("code_sha"),
        "corpus_version": report.get("corpus_version"),
        "generated_at": report.get("generated_at"),
        "unauthorized_access_rate": (report.get("uar") or {}).get("uar"),
        "canary_extraction_rate": (report.get("cer") or {}).get("cer"),
        "facts_reduction_open_over_negotiated": neg.get("facts_reduction_ratio_open_over_negotiated"),
        "negotiated_disclosure_precision": (minim.get("negotiated") or {}).get("disclosure_precision"),
        "negotiated_sensitive_excess": (minim.get("negotiated") or {}).get("sensitive_excess"),
        "specificity_delta": neg.get("specificity_delta"),
        "deny_p95_ms": (lat.get("deny") or {}).get("p95"),
        "gates_passed": (report.get("gates") or {}).get("passed"),
    }


def _print_summary(report: Dict[str, Any]) -> None:
    s = _marketing_summary(report)
    g = report.get("gates") or {}
    print("\n" + "=" * 62)
    print(f"  TOPOS PRIVACY EVAL — v{s['version']} ({s['code_sha']}) — {report['lane']} lane")
    print(f"  corpus={s['corpus_version']}  models={report.get('models')}")
    print("=" * 62)
    print(f"  Unauthorized access rate : {s['unauthorized_access_rate']}   (target 0)")
    print(f"  Canary extraction rate   : {s['canary_extraction_rate']}   (target 0)")
    print(f"  Facts reduction (open→neg): {s['facts_reduction_open_over_negotiated']}x")
    print(f"  Negotiated precision      : {s['negotiated_disclosure_precision']}  (sensitive excess {s['negotiated_sensitive_excess']})")
    print(f"  Intent specificity delta  : +{s['specificity_delta']}")
    print(f"  Deny-path p95             : {s['deny_p95_ms']} ms  (budget {DENY_P95_BUDGET_MS})")
    pg = report.get("pytest_gate")
    if pg is not None:
        print(f"  Privacy pytest suite      : {'PASS' if pg.get('passed') else 'FAIL'}  ({pg.get('summary')})")
    print("-" * 62)
    print(f"  GATES: {'PASS' if g.get('passed') else 'FAIL'}")
    for f in g.get("failures", []):
        print(f"    - {f}")
    print("=" * 62 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-gate", action="store_true", help="always exit 0 (report-only)")
    parser.add_argument("--no-pytest", action="store_true", help="skip the privacy pytest gate (scorecard only)")
    parser.add_argument("--print", dest="do_print", action="store_true", help="print the summary")
    parser.add_argument("--json", action="store_true", help="print the full report JSON to stdout")
    args = parser.parse_args()

    report = _collect(run_pytest=not args.no_pytest)
    path = _write(report)

    if args.do_print or not args.json:
        _print_summary(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    print(f"[release-eval] wrote {path.relative_to(ROOT)}")

    if args.no_gate:
        return 0
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
