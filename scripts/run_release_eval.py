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
import html
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

    # Regenerate the self-contained trend dashboard. Best-effort: a rendering
    # slip must never fail a release (the scorecard + history are already written).
    try:
        _write_dashboard(out_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[release-eval] dashboard generation skipped: {exc}", file=sys.stderr)
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


# --- Self-contained trend dashboard (eval_reports/index.html) --------------------
# Reads the whole history.jsonl and bakes it into one static HTML file with
# server-rendered SVG sparklines — no JS, no CDN, opens with a double-click and
# works offline (file://). Regenerated on every release from _write().

# key, label, unit, decimals, direction ("low"/"high"/"info"), target-line value
_DASH_METRICS = [
    ("deny_p95_ms", "Deny-path p95", " ms", 2, "low", None),
    ("facts_reduction_open_over_negotiated", "Facts reduction", "×", 1, "high", None),
    ("unauthorized_access_rate", "Unauthorized access", "", 2, "low", 0.0),
    ("canary_extraction_rate", "Canary extraction", "", 2, "low", 0.0),
    ("negotiated_disclosure_precision", "Negotiated precision", "", 2, "high", 1.0),
    ("negotiated_sensitive_excess", "Sensitive excess", "", 0, "low", 0.0),
]


def _load_history(out_dir: Path) -> List[Dict[str, Any]]:
    hist = out_dir / "history.jsonl"
    if not hist.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in hist.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _fmt(v: Any, dec: int, unit: str = "") -> str:
    if v is None:
        return "—"
    try:
        return (str(int(round(float(v)))) if dec == 0 else f"{float(v):.{dec}f}") + unit
    except (TypeError, ValueError):
        return str(v)


def _domain(key: str, vals: List[float]) -> tuple[float, float]:
    mx = max(vals) if vals else 0.0
    if key == "negotiated_disclosure_precision":
        return 0.0, 1.05
    if key == "deny_p95_ms":
        return 0.0, max(0.3, mx * 1.3)
    if key in ("unauthorized_access_rate", "canary_extraction_rate"):
        return 0.0, max(0.1, mx * 1.3)
    return 0.0, max(1.0, mx * 1.25)


def _latest_ok(key: str, v: Any) -> bool | None:
    if v is None:
        return None
    if key == "deny_p95_ms":
        return float(v) <= DENY_P95_BUDGET_MS
    if key in ("unauthorized_access_rate", "canary_extraction_rate", "negotiated_sensitive_excess"):
        return float(v) <= 0
    if key == "negotiated_disclosure_precision":
        return float(v) >= 0.999
    return None  # facts_reduction: informational


def _svg_trend(vals: List[Any], lo: float, hi: float, target: Any, ok: bool | None) -> str:
    w, h, pad = 300.0, 60.0, 7.0
    xw, yh = w - 2 * pad, h - 2 * pad
    n = len(vals)
    span = (hi - lo) or 1.0

    def x_at(i: int) -> float:
        return pad + (i / (n - 1) * xw if n > 1 else xw / 2)

    def y_at(v: float) -> float:
        return pad + (1 - (v - lo) / span) * yh

    pts = [(x_at(i), y_at(float(v))) for i, v in enumerate(vals) if v is not None]
    parts = [f'<svg viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" class="spark" role="img" aria-hidden="true">']
    if target is not None and lo <= float(target) <= hi:
        ty = y_at(float(target))
        parts.append(f'<line x1="{pad:.1f}" y1="{ty:.1f}" x2="{w - pad:.1f}" y2="{ty:.1f}" class="tgt"/>')
    if len(pts) >= 2:
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area = f"{pts[0][0]:.1f},{h - pad:.1f} " + line + f" {pts[-1][0]:.1f},{h - pad:.1f}"
        parts.append(f'<polygon points="{area}" class="area"/>')
        parts.append(f'<polyline points="{line}" class="line"/>')
    for x, y in pts[:-1]:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.7" class="dot"/>')
    if pts:
        lx, ly = pts[-1]
        cls = "dot-ok" if ok else ("dot-bad" if ok is False else "dot-last")
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.2" class="{cls}"/>')
    parts.append("</svg>")
    return "".join(parts)


_DASH_CSS = """
:root{--bg:#faf9f5;--card:#fff;--ink:#141413;--sub:#6b6a65;--mut:#918f88;--line:#e6e4dc;
--ok:#0f6e56;--okbg:#e1f5ee;--bad:#a32d2d;--badbg:#fcebeb;--accent:#185fa5;--area:rgba(24,95,165,.10)}
@media(prefers-color-scheme:dark){:root{--bg:#181817;--card:#211f1d;--ink:#f5f4ef;--sub:#b7b5ab;
--mut:#8a8880;--line:#33312d;--ok:#5dcaa5;--okbg:#0f3f34;--bad:#f09595;--badbg:#4a1b1b;--accent:#6aa4e5;--area:rgba(106,164,229,.12)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}.wrap{max-width:940px;margin:0 auto;padding:32px 22px 56px}
.head{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:4px}
h1{font-size:20px;font-weight:500;margin:0}.sub{color:var(--sub);font-size:13px;margin:2px 0 22px}
.badge{font-size:13px;font-weight:500;padding:4px 12px;border-radius:7px}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.bad{background:var(--badbg);color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-bottom:26px}
.card{background:var(--card);border:.5px solid var(--line);border-radius:12px;padding:14px 15px}
.mrow{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.mlabel{font-size:13px;color:var(--sub)}.mval{font-size:20px;font-weight:500;font-variant-numeric:tabular-nums}
.mval.ok{color:var(--ok)}.mval.bad{color:var(--bad)}
.spark{width:100%;height:56px;margin:8px 0 4px;display:block}
.spark .line{fill:none;stroke:var(--accent);stroke-width:1.6;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.spark .area{fill:var(--area);stroke:none}
.spark .tgt{stroke:var(--mut);stroke-width:1;stroke-dasharray:3 3;vector-effect:non-scaling-stroke}
.spark .dot{fill:var(--accent)}.spark .dot-last{fill:var(--accent)}
.spark .dot-ok{fill:var(--ok)}.spark .dot-bad{fill:var(--bad)}
.mfoot{font-size:12px;color:var(--mut)}.mfoot .ok{color:var(--ok)}.mfoot .bad{color:var(--bad)}
h2{font-size:15px;font-weight:500;margin:30px 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 10px;border-bottom:.5px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--sub);font-weight:500}
td.v{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--sub)}
.pill{display:inline-block;width:8px;height:8px;border-radius:50%}.pill.ok{background:var(--ok)}.pill.bad{background:var(--bad)}
.note{color:var(--mut);font-size:12px;margin-top:18px;max-width:70ch}
"""


def _write_dashboard(out_dir: Path) -> Path:
    rows = _load_history(out_dir)
    latest = rows[-1] if rows else {}
    gates_ok = bool(latest.get("gates_passed"))
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    kpis = []
    for key, label, unit, dec, _dir, _t in _DASH_METRICS:
        v = latest.get(key)
        ok = _latest_ok(key, v)
        cls = " ok" if ok else (" bad" if ok is False else "")
        vals = [r.get(key) for r in rows]
        nn = [float(x) for x in vals if x is not None]
        lo, hi = _domain(key, nn)
        spark = _svg_trend(vals, lo, hi, _t, ok)
        if key == "deny_p95_ms":
            foot = f'budget {DENY_P95_BUDGET_MS:.0f} ms · <span class="{"ok" if ok else "bad"}">{"within" if ok else "over"}</span>'
        elif key == "facts_reduction_open_over_negotiated":
            foot = "open → negotiated · higher is better"
        elif ok is None:
            foot = "target " + _fmt(_t, dec)
        else:
            foot = f'target {_fmt(_t, dec)} · <span class="{"ok" if ok else "bad"}">{"held" if ok else "regressed"}</span>'
        kpis.append(
            f'<div class="card"><div class="mrow"><span class="mlabel">{html.escape(label)}</span>'
            f'<span class="mval{cls}">{_fmt(v, dec, unit)}</span></div>{spark}'
            f'<div class="mfoot">{foot}</div></div>'
        )

    head = [
        "version", "sha", "generated",
        "UAR", "CER", "facts×", "precision", "excess", "deny p95", "gate",
    ]
    trows = []
    for r in reversed(rows):
        when = str(r.get("generated_at") or "")[:16].replace("T", " ")
        g = bool(r.get("gates_passed"))
        trows.append(
            "<tr>"
            f'<td>{html.escape(str(r.get("version") or "—"))}</td>'
            f'<td class="v">{html.escape(str(r.get("code_sha") or "—"))}</td>'
            f"<td>{html.escape(when)}</td>"
            f'<td>{_fmt(r.get("unauthorized_access_rate"), 2)}</td>'
            f'<td>{_fmt(r.get("canary_extraction_rate"), 2)}</td>'
            f'<td>{_fmt(r.get("facts_reduction_open_over_negotiated"), 1)}×</td>'
            f'<td>{_fmt(r.get("negotiated_disclosure_precision"), 2)}</td>'
            f'<td>{_fmt(r.get("negotiated_sensitive_excess"), 0)}</td>'
            f'<td>{_fmt(r.get("deny_p95_ms"), 2)} ms</td>'
            f'<td><span class="pill {"ok" if g else "bad"}" title="{"passed" if g else "failed"}"></span></td>'
            "</tr>"
        )

    ver = html.escape(str(latest.get("version") or "—"))
    sha = html.escape(str(latest.get("code_sha") or "—"))
    corpus = html.escape(str(latest.get("corpus_version") or "—"))
    doc = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Topos privacy eval — v{ver}</title><style>{_DASH_CSS}</style></head><body><div class=\"wrap\">"
        f'<div class="head"><h1>Topos privacy eval — release trend</h1>'
        f'<span class="badge {"ok" if gates_ok else "bad"}">v{ver} · gates {"passed" if gates_ok else "FAILED"}</span></div>'
        f'<div class="sub">{len(rows)} run(s) · corpus {corpus} · latest {sha} · deterministic lane · generated {gen}</div>'
        f'<div class="grid">{"".join(kpis)}</div>'
        f'<h2>All runs</h2><table><thead><tr>{"".join(f"<th>{html.escape(h)}</th>" for h in head)}</tr></thead>'
        f'<tbody>{"".join(trows)}</tbody></table>'
        '<p class="note">Safety floors (unauthorized access, canary extraction, sensitive excess) '
        'are pass/fail gates pinned at target — a flat line there means no regression, which is the win. '
        'Deny-path latency and facts reduction are the metrics with headroom to move. '
        'Regenerated on every release by scripts/run_release_eval.py; deterministic lane, safe to publish.</p>'
        "</div></body></html>"
    )
    path = out_dir / "index.html"
    path.write_text(doc)
    return path


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
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="regenerate eval_reports/index.html from the existing history.jsonl and exit (no eval run)",
    )
    args = parser.parse_args()

    if args.dashboard_only:
        out_dir = ROOT / "eval_reports"
        path = _write_dashboard(out_dir)
        print(f"[release-eval] wrote {path.relative_to(ROOT)}")
        return 0

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
