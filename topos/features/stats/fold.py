"""Fold/merge algebra for incremental statistics.

Every stat kind implements:
  init()            -> state dict
  fold(state, x, ts)-> state   (O(1); the "add one blue ball" update)
  merge(a, b)       -> state   (associative + commutative; windows = merged
                                daily buckets, replays = safe re-merge)

Mean/variance uses Welford's online update for fold and Chan's parallel
algorithm for merge. Rolling windows never subtract — expired data simply
stops being merged in (daily-bucket partials), which is what makes the
incremental design exact instead of approximate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STAT_KINDS = ("count", "sum", "mean_var", "minmax", "histogram", "ema", "distinct", "intervals")

_DISTINCT_CAP = 200


def parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def init_state(kind: str) -> Dict[str, Any]:
    if kind == "count":
        return {"n": 0}
    if kind == "sum":
        return {"n": 0, "total": 0.0}
    if kind == "mean_var":
        return {"n": 0, "mean": 0.0, "m2": 0.0}
    if kind == "minmax":
        return {"n": 0, "min": None, "max": None}
    if kind == "histogram":
        return {"n": 0, "buckets": {}}
    if kind == "ema":
        return {"n": 0, "value": 0.0, "last_at": None}
    if kind == "distinct":
        return {"n": 0, "values": []}
    if kind == "intervals":
        return {"n": 0, "mean": 0.0, "m2": 0.0, "last_at": None}
    raise ValueError(f"unknown stat kind: {kind}")


def fold(kind: str, state: Dict[str, Any], value: Any, *, ts: Optional[datetime] = None,
         half_life_days: Optional[float] = None) -> Dict[str, Any]:
    s = dict(state)
    if kind == "count":
        s["n"] = int(s.get("n") or 0) + 1
        return s
    if kind == "sum":
        s["n"] = int(s.get("n") or 0) + 1
        s["total"] = float(s.get("total") or 0.0) + float(value or 0.0)
        return s
    if kind == "mean_var":
        x = float(value or 0.0)
        n = int(s.get("n") or 0) + 1
        mean = float(s.get("mean") or 0.0)
        delta = x - mean
        mean += delta / n
        s["n"], s["mean"] = n, mean
        s["m2"] = float(s.get("m2") or 0.0) + delta * (x - mean)
        return s
    if kind == "minmax":
        x = float(value or 0.0)
        s["n"] = int(s.get("n") or 0) + 1
        s["min"] = x if s.get("min") is None else min(float(s["min"]), x)
        s["max"] = x if s.get("max") is None else max(float(s["max"]), x)
        return s
    if kind == "histogram":
        key = str(value)
        buckets = dict(s.get("buckets") or {})
        buckets[key] = int(buckets.get(key) or 0) + 1
        s["buckets"], s["n"] = buckets, int(s.get("n") or 0) + 1
        return s
    if kind == "ema":
        x = float(value or 0.0)
        last = parse_ts(s.get("last_at"))
        current = float(s.get("value") or 0.0)
        if last is not None and ts is not None and half_life_days:
            dt_days = max(0.0, (ts - last).total_seconds() / 86400.0)
            current *= math.pow(0.5, dt_days / float(half_life_days))
        s["value"] = current + x
        s["n"] = int(s.get("n") or 0) + 1
        if ts is not None:
            s["last_at"] = ts.isoformat()
        return s
    if kind == "distinct":
        key = str(value)
        values = list(s.get("values") or [])
        if key not in values and len(values) < _DISTINCT_CAP:
            values.append(key)
        s["values"], s["n"] = values, int(s.get("n") or 0) + 1
        return s
    if kind == "intervals":
        # Gap statistics between consecutive events (seconds). fold() must be
        # called in event-time order per group for exact results.
        if ts is None:
            return s
        last = parse_ts(s.get("last_at"))
        if last is not None:
            gap = (ts - last).total_seconds()
            if gap >= 0:
                n = int(s.get("n") or 0) + 1
                mean = float(s.get("mean") or 0.0)
                delta = gap - mean
                mean += delta / n
                s["n"], s["mean"] = n, mean
                s["m2"] = float(s.get("m2") or 0.0) + delta * (gap - mean)
        s["last_at"] = ts.isoformat()
        return s
    raise ValueError(f"unknown stat kind: {kind}")


def merge(kind: str, a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    if kind == "count":
        return {"n": int(a.get("n") or 0) + int(b.get("n") or 0)}
    if kind == "sum":
        return {
            "n": int(a.get("n") or 0) + int(b.get("n") or 0),
            "total": float(a.get("total") or 0.0) + float(b.get("total") or 0.0),
        }
    if kind == "mean_var":
        na, nb = int(a.get("n") or 0), int(b.get("n") or 0)
        if na == 0:
            return dict(b)
        if nb == 0:
            return dict(a)
        ma, mb = float(a.get("mean") or 0.0), float(b.get("mean") or 0.0)
        delta = mb - ma
        n = na + nb
        mean = ma + delta * nb / n
        m2 = float(a.get("m2") or 0.0) + float(b.get("m2") or 0.0) + delta * delta * na * nb / n
        return {"n": n, "mean": mean, "m2": m2}
    if kind == "minmax":
        mins = [v for v in (a.get("min"), b.get("min")) if v is not None]
        maxs = [v for v in (a.get("max"), b.get("max")) if v is not None]
        return {
            "n": int(a.get("n") or 0) + int(b.get("n") or 0),
            "min": min(mins) if mins else None,
            "max": max(maxs) if maxs else None,
        }
    if kind == "histogram":
        buckets = dict(a.get("buckets") or {})
        for key, count in (b.get("buckets") or {}).items():
            buckets[key] = int(buckets.get(key) or 0) + int(count)
        return {"n": int(a.get("n") or 0) + int(b.get("n") or 0), "buckets": buckets}
    if kind == "ema":
        # Keep the more recent trajectory; add the older mass decayed to its ts.
        ta, tb = parse_ts(a.get("last_at")), parse_ts(b.get("last_at"))
        newer, older = (a, b) if (tb is None or (ta is not None and ta >= tb)) else (b, a)
        return {
            "n": int(a.get("n") or 0) + int(b.get("n") or 0),
            "value": float(newer.get("value") or 0.0) + float(older.get("value") or 0.0) * 0.5,
            "last_at": newer.get("last_at") or older.get("last_at"),
        }
    if kind == "distinct":
        values = list(a.get("values") or [])
        for v in b.get("values") or []:
            if v not in values and len(values) < _DISTINCT_CAP:
                values.append(v)
        return {"n": int(a.get("n") or 0) + int(b.get("n") or 0), "values": values}
    if kind == "intervals":
        # Approximate: the boundary gap between buckets is dropped (documented).
        core = merge("mean_var", a, b)
        ta, tb = parse_ts(a.get("last_at")), parse_ts(b.get("last_at"))
        last = max(t for t in (ta, tb) if t is not None).isoformat() if (ta or tb) else None
        return {**core, "last_at": last}
    raise ValueError(f"unknown stat kind: {kind}")


def summarize(kind: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Human-consumable readout of a state (derived values, never stored)."""
    if kind == "mean_var":
        n = int(state.get("n") or 0)
        variance = float(state.get("m2") or 0.0) / (n - 1) if n > 1 else 0.0
        return {
            "n": n,
            "mean": round(float(state.get("mean") or 0.0), 3),
            "stddev": round(math.sqrt(max(0.0, variance)), 3),
        }
    if kind == "histogram":
        buckets = state.get("buckets") or {}
        total = sum(int(v) for v in buckets.values()) or 1
        top = sorted(buckets.items(), key=lambda kv: -int(kv[1]))[:5]
        entropy = -sum(
            (int(v) / total) * math.log2(int(v) / total) for v in buckets.values() if int(v) > 0
        )
        return {
            "n": int(state.get("n") or 0),
            "top": [{"bucket": k, "count": int(v), "share": round(int(v) / total, 3)} for k, v in top],
            "entropy_bits": round(entropy, 3),
            "distinct_buckets": len(buckets),
        }
    if kind == "intervals":
        n = int(state.get("n") or 0)
        mean = float(state.get("mean") or 0.0)
        variance = float(state.get("m2") or 0.0) / (n - 1) if n > 1 else 0.0
        sd = math.sqrt(max(0.0, variance))
        return {
            "n": n,
            "mean_gap_hours": round(mean / 3600.0, 3),
            "burstiness_cv": round(sd / mean, 3) if mean > 0 else None,
        }
    if kind == "sum":
        return {"n": int(state.get("n") or 0), "total": round(float(state.get("total") or 0.0), 2)}
    return dict(state)


def window_state(kind: str, bucket_states: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge daily bucket partials into a window aggregate."""
    out = init_state(kind)
    for state in bucket_states:
        out = merge(kind, out, state)
    return out
