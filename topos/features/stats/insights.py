"""Render stat_state into human-readable insight facts."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from .fold import summarize

_MIN_N = 3  # below this, a frequency/count "insight" is noise

# The count floor guards frequency/count/distribution stats, where a handful of
# observations cannot establish a pattern. A SUM aggregate ("you spent $X on
# dining") is a running total, meaningful from a single transaction — the floor
# would silently drop legitimate spend stats whose categories naturally hold few
# rows (financial.spend.by_category). Kinds exempt from the count floor:
_LOW_N_KINDS = frozenset({"sum"})


def _confidence(n: int, *, saturation: int = 20) -> float:
    return round(min(1.0, n / float(saturation)), 3)


_WINDOW_LABELS = {"d7": "last 7 days", "d30": "last 30 days", "d90": "last 90 days"}
_WINDOWED_GROUP_CAP = 20  # windowed variants only for the most active groups


def _summary_with_payload(defn: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the definition's code-side payload markers (defs.STAT_PAYLOADS,
    e.g. {"ledger": "exposure"}) into the insight summary. promote_insights
    forwards the summary as the fact's stat_summary, which is the only
    payload channel the engine exposes — query-side stat selection reads the
    marker from there (PLAN_PROVENANCE_SPLIT P1.3 / contract 5)."""
    payload = defn.get("payload")
    if payload:
        return {**summary, **payload}
    return summary


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
    label_map: Dict[str, str] | None = None,
) -> Dict[str, Any] | None:
    state = engine.read_state(defn["stat_id"], group_key=group_key, window=window)
    n = int(state.get("n") or 0)
    # Match all-time path: sum aggregates (e.g. financial.spend.by_category) are
    # meaningful at n=1–2; do not drop windowed variants under the count floor.
    if n < _MIN_N and defn.get("stat_kind") not in _LOW_N_KINDS:
        return None
    summary = _summary_with_payload(defn, summarize(defn["stat_kind"], state))
    text = _render_text(defn["stat_id"], defn["stat_kind"], group_key, summary, defn, label_map or {})
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


def _identifier_lookup_keys(ident: str) -> List[str]:
    """Normalize phone-like identifiers so +1… and bare digits share a label."""
    raw = str(ident or "").strip()
    if not raw:
        return []
    keys = [raw]
    digits = "".join(c for c in raw if c.isdigit())
    if digits and digits != raw:
        keys.append(digits)
    if digits.startswith("1") and len(digits) == 11:
        keys.append(digits[1:])
        keys.append(f"+{digits}")
    elif len(digits) == 10:
        keys.append(f"+1{digits}")
        keys.append(f"1{digits}")
    # Dedup preserving order
    out: List[str] = []
    seen: set[str] = set()
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _contact_label_map(conn) -> Dict[str, str]:
    """identifier -> display name for message/cadence stat groups (plan C6). Stat tags
    read 'Messages with +17184834576'; a name is both more useful and closes an
    oracle↔human agreement class. Best-effort: returns {} if the tables are absent."""
    try:
        rows = conn.execute(
            """SELECT ci.identifier, c.display_name FROM contact_identifiers ci
               JOIN contacts c ON c.contact_id = ci.contact_id
               WHERE c.display_name IS NOT NULL AND length(c.display_name) > 1
                 AND c.is_self = 0"""
        ).fetchall()
    except Exception:
        return {}
    label: Dict[str, str] = {}
    for ident, name in rows:
        display = str(name).strip()
        if not display:
            continue
        # First non-empty display name wins; map common identifier variants (C6).
        for key in _identifier_lookup_keys(str(ident or "").strip()):
            if key not in label:
                label[key] = display
    return label


def _group_label(group_key: str, label_map: Dict[str, str]) -> str:
    """Render a message-group key as a human label: display name (with identifier for
    disambiguation) when resolvable, 'myself' for the self bucket, else the raw key."""
    if group_key == "self":
        return "myself"
    name = label_map.get(group_key)
    if not name:
        for key in _identifier_lookup_keys(group_key):
            name = label_map.get(key)
            if name:
                break
    return f"{name} ({group_key})" if name else group_key


def render_insights(engine) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    from . import definitions as defs

    label_map = _contact_label_map(engine._conn)
    for defn in defs.load_enabled_definitions(engine._conn):
        stat_id = defn["stat_id"]
        kind = defn["stat_kind"]
        dimension = defn["dimension"]
        window = _recent_window(defn)

        low_n_ok = kind in _LOW_N_KINDS

        if defn["group_by"] == "none":
            state = engine.read_state(stat_id)
            n = int(state.get("n") or 0)
            if n < _MIN_N and not low_n_ok:
                continue
            summary = _summary_with_payload(defn, summarize(kind, state))
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
            if not group_key or (n < _MIN_N and not low_n_ok):
                continue
            summary = _summary_with_payload(defn, summarize(kind, state))
            text = _render_text(stat_id, kind, group_key, summary, defn, label_map)
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
                recent = _windowed_insight(engine, defn, group_key=group_key, window=window, label_map=label_map)
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
    label_map: Dict[str, str] | None = None,
) -> str:
    label_map = label_map or {}
    if kind == "histogram":
        top = summary.get("top") or []
        if not top:
            return ""
        if str(defn.get("value_expr")) in (
            "hour_of_week",
            "hour_of_week_authored",
            "hour_of_day",
            "weekday",
        ):
            bands = ", ".join(f"{t['bucket']}" for t in top[:3])
            subject = "web activity" if stat_id.startswith("activity") else "AI chat activity"
            return f"Most {subject} happens around: {bands} (n={summary.get('n')})."
        if str(defn.get("value_expr")) == "mood_tag":
            mix = ", ".join(f"{t['bucket']} {t['share'] * 100:.0f}%" for t in top[:3])
            return f"Journal moods recorded most often: {mix} (n={summary.get('n')})."
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
        # Sent family (PLAN_PROVENANCE_SPLIT P1.3): authored-gated counts must
        # read as the owner's OWN sending ("You sent N messages…") so
        # first-person volume asks select them over thread/exposure volume.
        if stat_id == "messages.volume.sent.total":
            return f"You sent {summary.get('n')} messages overall."
        if stat_id == "messages.volume.sent.by_thread":
            return f"You sent {summary.get('n')} messages in {group_key}."
        if stat_id == "places.visits.by_city":
            return f"Cities visited: {group_key} — {summary.get('n')} visits."
        if stat_id == "places.visits.by_place":
            return f"Places visited most: {group_key} — {summary.get('n')} visits."
        if stat_id == "activity.visits.by_title":
            return f"Most visited while browsing: {group_key} — {summary.get('n')} visits."
        # message volume: group_key is a contact identifier — render the name (C6).
        return f"{summary.get('n')} messages with {_group_label(group_key, label_map)}."
    if kind == "intervals":
        gap_h = summary.get("mean_gap_hours")
        cv = summary.get("burstiness_cv")
        if gap_h is None:
            return ""
        gap_txt = f"{gap_h:.1f} h" if float(gap_h) < 72 else f"{float(gap_h) / 24.0:.1f} days"
        style = ""
        if cv is not None:
            style = " (bursty)" if float(cv) > 1.2 else " (steady)" if float(cv) < 0.6 else ""
        return f"Messages with {_group_label(group_key, label_map)} roughly every {gap_txt}{style} (n={summary.get('n')})."
    if kind == "sum":
        total = float(summary.get("total") or 0.0)
        return f"Total {group_key} spend: {total:.2f} across {summary.get('n')} transactions."
    return ""
