"""Render stat_state into human-readable insight facts."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from .fold import summarize

_MIN_N = 3  # below this, an "insight" is noise


def _confidence(n: int, *, saturation: int = 20) -> float:
    return round(min(1.0, n / float(saturation)), 3)


_WINDOW_LABELS = {"d7": "last 7 days", "d30": "last 30 days", "d90": "last 90 days"}
_WINDOWED_GROUP_CAP = 20  # windowed variants only for the most active groups


def _recent_window(defn: Dict[str, Any]) -> str:
    """The window used for the 'recent' insight variant (prefers d30)."""
    windows = [str(w) for w in (defn.get("windows") or []) if str(w) != "all"]
    if not windows:
        return ""
    if "d30" in windows:
        return "d30"
    return sorted(windows, key=lambda w: int(w.lstrip("d") or 0))[0]


def _windowed_insight(
    engine,
    defn: Dict[str, Any],
    *,
    group_key: str,
    window: str,
) -> Dict[str, Any] | None:
    state = engine.read_state(defn["stat_id"], group_key=group_key, window=window)
    n = int(state.get("n") or 0)
    if n < _MIN_N:
        return None
    summary = summarize(defn["stat_kind"], state)
    text = _render_text(defn["stat_id"], defn["stat_kind"], group_key, summary, defn)
    if not text:
        return None
    label = _WINDOW_LABELS.get(window, window)
    return {
        "stat_id": defn["stat_id"],
        "group_key": group_key,
        "dimension": defn["dimension"],
        "text": f"{label.capitalize()}: {text}",
        "summary": summary,
        "confidence": _confidence(n),
        "window": window,
    }


def render_insights(engine) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    from . import definitions as defs

    for defn in defs.load_enabled_definitions(engine._conn):
        stat_id = defn["stat_id"]
        kind = defn["stat_kind"]
        dimension = defn["dimension"]
        window = _recent_window(defn)

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
                if window:
                    recent = _windowed_insight(engine, defn, group_key="", window=window)
                    if recent:
                        out.append(recent)
            continue

        emitted_groups: List[tuple[int, str]] = []
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
                emitted_groups.append((n, group_key))

        # Windowed variants for the most active groups only: a per-group
        # window read is one indexed query, so cap the fan-out.
        if window and emitted_groups:
            emitted_groups.sort(reverse=True)
            for _, group_key in emitted_groups[:_WINDOWED_GROUP_CAP]:
                recent = _windowed_insight(engine, defn, group_key=group_key, window=window)
                if recent:
                    out.append(recent)
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
