"""Render stat_state into human-readable insight facts."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from .fold import summarize

_MIN_N = 3  # below this, an "insight" is noise


def _confidence(n: int, *, saturation: int = 20) -> float:
    return round(min(1.0, n / float(saturation)), 3)


def render_insights(engine) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    from . import definitions as defs

    for defn in defs.load_enabled_definitions(engine._conn):
        stat_id = defn["stat_id"]
        kind = defn["stat_kind"]
        dimension = defn["dimension"]

        if defn["group_by"] == "none":
            state = engine.read_state(stat_id)
            n = int(state.get("n") or 0)
            if n < _MIN_N:
                continue
            summary = summarize(kind, state)
            text = _render_text(stat_id, kind, "", summary, defn)
            if text:
                out.append(
                    {
                        "stat_id": stat_id,
                        "group_key": "",
                        "dimension": dimension,
                        "text": text,
                        "summary": summary,
                        "confidence": _confidence(n),
                    }
                )
            continue

        for group_key, state in engine.group_states(stat_id):
            n = int(state.get("n") or 0)
            if n < _MIN_N or not group_key:
                continue
            summary = summarize(kind, state)
            text = _render_text(stat_id, kind, group_key, summary, defn)
            if text:
                out.append(
                    {
                        "stat_id": stat_id,
                        "group_key": group_key,
                        "dimension": dimension,
                        "text": text,
                        "summary": summary,
                        "confidence": _confidence(n),
                    }
                )
    return out


def _fmt_minutes(minutes: float) -> str:
    if minutes >= 90:
        return f"{minutes / 60.0:.1f} h"
    return f"{minutes:.0f} min"


def _render_text(
    stat_id: str,
    kind: str,
    group_key: str,
    summary: Dict[str, Any],
    defn: Dict[str, Any],
) -> str:
    if kind == "histogram":
        top = summary.get("top") or []
        if not top:
            return ""
        if str(defn.get("value_expr")) in ("hour_of_week", "hour_of_day", "weekday"):
            bands = ", ".join(f"{t['bucket']}" for t in top[:3])
            subject = "web activity" if stat_id.startswith("activity") else "AI chat activity"
            return f"Most {subject} happens around: {bands} (n={summary.get('n')})."
        label = str(defn["canonical_table"]).replace("_", " ")
        mix = ", ".join(f"{t['bucket']} {t['share'] * 100:.0f}%" for t in top[:3])
        return (
            f"{label} category mix: {mix}"
            f" (focus entropy {summary.get('entropy_bits')} bits, n={summary.get('n')})."
        )
    if kind == "mean_var":
        mean = float(summary.get("mean") or 0.0)
        sd = float(summary.get("stddev") or 0.0)
        if stat_id.startswith("journal.duration"):
            return (
                f"Typical '{group_key}' session: {_fmt_minutes(mean)}"
                f" ± {_fmt_minutes(sd)} (n={summary.get('n')})."
            )
        if stat_id.startswith("calendar"):
            return f"Typical calendar commitment: {_fmt_minutes(mean)} ± {_fmt_minutes(sd)} (n={summary.get('n')})."
        return f"{stat_id} [{group_key}]: mean {mean:.1f} ± {sd:.1f} (n={summary.get('n')})."
    if kind == "count":
        return f"{summary.get('n')} messages with {group_key}."
    if kind == "intervals":
        gap_h = summary.get("mean_gap_hours")
        cv = summary.get("burstiness_cv")
        if gap_h is None:
            return ""
        gap_txt = f"{gap_h:.1f} h" if float(gap_h) < 72 else f"{float(gap_h) / 24.0:.1f} days"
        style = ""
        if cv is not None:
            style = " (bursty)" if float(cv) > 1.2 else " (steady)" if float(cv) < 0.6 else ""
        return f"Messages with {group_key} roughly every {gap_txt}{style} (n={summary.get('n')})."
    if kind == "sum":
        total = float(summary.get("total") or 0.0)
        return f"Total {group_key} spend: {total:.2f} across {summary.get('n')} transactions."
    return ""
