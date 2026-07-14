"""StatsEngine: fold canonical batches into stat_state; promote insights to signal_facts.

Fold is O(batch) and runs on every ingest. Promotion renders human-readable
rollups with stable fact_ids (upsert — no churn) and is debounced by the job.

Privacy: promoted stat_insight facts carry disclosure="owner_only". Aggregate
rhythms (work hours, spend, contact cadence) can fingerprint a person, so they
are computed densely but excluded from non-owner packets unless a scope
explicitly grants "stat_insights" (see retrieval._build_summary_items).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import definitions as defs
from .fold import fold, init_state, merge, parse_ts, summarize, window_state

logger = logging.getLogger("topos.features.stats.engine")

_SEEN_RETENTION_DAYS = 45


# Canonical ai_chat_messages sender_type values ('human' = owner, per
# chatgpt_parser / ai_chat model); 'user' survives in legacy rows.
_AI_CHAT_SENDER_TYPES = frozenset({"human", "user", "assistant", "system"})

# Keys that only conversation_messages rows carry. sender_type CANNOT tell the
# families apart: the messenger mapper writes 'human' for other people's
# messages too. Checked by presence (dict(sqlite3.Row) keeps NULLs as None).
_CONVERSATION_MARKER_KEYS = (
    "is_from_self",
    "event_type",
    "message_type",
    "reply_to_message_id",
)


def _table_for_row(row: Dict[str, Any]) -> str:
    explicit = row.get("_table") or row.get("canonical_table")
    if explicit:
        return str(explicit)
    if row.get("entry_at") is not None or row.get("mood_tag") is not None:
        return "journal_entries"
    if row.get("starts_at") is not None and row.get("title") is not None:
        return "calendar_events"
    if row.get("url") is not None:
        return "activity_events"
    if row.get("amount") is not None:
        return "financial_transactions"
    sender_type = str(row.get("sender_type") or "").strip().lower()
    if row.get("sender_id") is not None or row.get("conversation_id") is not None:
        if any(key in row for key in _CONVERSATION_MARKER_KEYS):
            return "conversation_messages"
        if sender_type in _AI_CHAT_SENDER_TYPES:
            return "ai_chat_messages"
        return "conversation_messages"
    if sender_type in _AI_CHAT_SENDER_TYPES:
        return "ai_chat_messages"
    return ""


def _record_id(row: Dict[str, Any]) -> str:
    return str(
        row.get("record_id")
        or row.get("message_id")
        or row.get("event_id")
        or row.get("entry_id")
        or row.get("transaction_id")
        or row.get("id")
        or ""
    )


class StatsEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        defs.seed_definitions(conn)

    # ------------------------------------------------------------- folding

    def fold_batch(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Fold a canonical batch into all matching stat definitions."""
        from ..lifecycle.exclusions import excluded_record_ids

        excluded = excluded_record_ids(self._conn)
        by_table: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            if excluded and _record_id(row) in excluded:
                continue
            table = _table_for_row(row)
            if table:
                by_table.setdefault(table, []).append(row)

        folded = 0
        skipped_seen = 0
        for defn in defs.load_enabled_definitions(self._conn):
            table_rows = by_table.get(defn["canonical_table"]) or []
            if not table_rows:
                continue
            # intervals require event-time order per group
            if defn["stat_kind"] == "intervals":
                table_rows = sorted(
                    table_rows, key=lambda r: (defs.row_event_ts(r) or datetime.min.replace(tzinfo=timezone.utc))
                )
            for row in table_rows:
                record_id = _record_id(row)
                if record_id and self._already_seen(defn["stat_id"], record_id):
                    skipped_seen += 1
                    continue
                if self._fold_row(defn, row):
                    folded += 1
                    if record_id:
                        self._mark_seen(defn["stat_id"], record_id)
        self._prune_seen()
        self._conn.commit()
        return {"rows_folded": folded, "rows_skipped_seen": skipped_seen}

    def _fold_row(self, defn: Dict[str, Any], row: Dict[str, Any]) -> bool:
        kind = defn["stat_kind"]
        ts = defs.row_event_ts(row)

        group_key = defs.group_key_for(row, defn["group_by"])
        if group_key is None:
            return False

        if kind == "histogram":
            value: Any = defs.histogram_value(row, str(defn["value_expr"] or "category"))
            if value is None:
                return False
        elif kind in ("count", "intervals"):
            value = 1.0
        else:
            extractor = defs.VALUE_EXTRACTORS.get(str(defn["value_expr"] or "one"))
            if extractor is None:
                return False
            value = extractor(row)
            if value is None:
                return False

        bucket_dates = [""]
        windows = defn.get("windows") or ["all"]
        if any(w != "all" for w in windows) and ts is not None:
            bucket_dates.append(ts.date().isoformat())

        for bucket_date in bucket_dates:
            state = self._load_state(defn["stat_id"], group_key, bucket_date, kind)
            state = fold(
                kind,
                state,
                value,
                ts=ts,
                half_life_days=defn.get("decay_half_life_days"),
            )
            self._save_state(defn["stat_id"], group_key, bucket_date, state, watermark=ts)
        return True

    # ------------------------------------------------------------ storage

    def _load_state(self, stat_id: str, group_key: str, bucket_date: str, kind: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT state_json FROM stat_state WHERE stat_id=? AND group_key=? AND bucket_date=?",
            (stat_id, group_key, bucket_date),
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        return init_state(kind)

    def _save_state(
        self,
        stat_id: str,
        group_key: str,
        bucket_date: str,
        state: Dict[str, Any],
        *,
        watermark: Optional[datetime],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO stat_state (stat_id, group_key, bucket_date, state_json, watermark, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(stat_id, group_key, bucket_date) DO UPDATE SET
                state_json=excluded.state_json,
                watermark=MAX(COALESCE(stat_state.watermark, ''), COALESCE(excluded.watermark, '')),
                updated_at=datetime('now')
            """,
            (stat_id, group_key, bucket_date, json.dumps(state), watermark.isoformat() if watermark else None),
        )

    def _already_seen(self, stat_id: str, record_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stat_seen WHERE stat_id=? AND record_id=?",
            (stat_id, record_id),
        ).fetchone()
        return row is not None

    def _mark_seen(self, stat_id: str, record_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO stat_seen (stat_id, record_id) VALUES (?, ?)",
            (stat_id, record_id),
        )

    def _prune_seen(self) -> None:
        self._conn.execute(
            "DELETE FROM stat_seen WHERE seen_at < datetime('now', ?)",
            (f"-{_SEEN_RETENTION_DAYS} days",),
        )

    # ------------------------------------------------------------- reads

    def read_state(self, stat_id: str, *, group_key: str = "", window: str = "all") -> Dict[str, Any]:
        defn = next(
            (d for d in defs.load_enabled_definitions(self._conn) if d["stat_id"] == stat_id),
            None,
        )
        if defn is None:
            raise ValueError(f"unknown stat: {stat_id}")
        kind = defn["stat_kind"]
        if window == "all":
            return self._load_state(stat_id, group_key, "", kind)
        days = int(window.lstrip("d") or 0)
        rows = self._conn.execute(
            """
            SELECT state_json FROM stat_state
            WHERE stat_id=? AND group_key=? AND bucket_date != ''
              AND bucket_date >= date('now', ?)
            """,
            (stat_id, group_key, f"-{days} days"),
        ).fetchall()
        states = []
        for row in rows:
            try:
                states.append(json.loads(row[0]))
            except json.JSONDecodeError:
                continue
        return window_state(kind, states)

    def group_states(self, stat_id: str, *, bucket_date: str = "") -> List[Tuple[str, Dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT group_key, state_json FROM stat_state WHERE stat_id=? AND bucket_date=?",
            (stat_id, bucket_date),
        ).fetchall()
        out = []
        for group_key, state_json in rows:
            try:
                out.append((str(group_key), json.loads(state_json)))
            except json.JSONDecodeError:
                continue
        return out

    # ---------------------------------------------------------- promotion

    def _promotion_period(self, insight: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
        """(period_start, period_end, as_of) for a promoted stat insight (B2.3).

        Staleness honesty (T8): every stat artifact must carry its OWN event
        period, not just a value. Windowed insights ('dN') are bounded by the
        promotion clock (the read window is relative to now); all-time
        insights are bounded by the folded evidence itself (stat_state
        watermark = latest event folded, earliest daily bucket when present).
        All values are ISO dates (YYYY-MM-DD).
        """
        today = datetime.now(timezone.utc).date()
        as_of = today.isoformat()
        window = str(insight.get("window") or "")
        if window.startswith("d") and window[1:].isdigit():
            days = int(window[1:])
            return (today - timedelta(days=days)).isoformat(), as_of, as_of
        try:
            row = self._conn.execute(
                """SELECT MIN(NULLIF(bucket_date, '')), MAX(watermark)
                   FROM stat_state WHERE stat_id=? AND group_key=?""",
                (insight["stat_id"], insight.get("group_key") or ""),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        period_start = str(row[0])[:10] if row and row[0] else None
        period_end = str(row[1])[:10] if row and row[1] else as_of
        return period_start, period_end, as_of

    def promote_insights(self, adapters) -> int:
        """Render insight facts into signal_facts (stable ids; owner-only)."""
        from .insights import render_insights

        insights = render_insights(self)
        written = 0
        excluded_keys: set = set()
        try:
            excluded_keys = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='stat_insight'"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            pass
        for insight in insights:
            insight_key = f"{insight['stat_id']}:{insight.get('group_key') or 'all'}"
            window = str(insight.get("window") or "")
            if window:
                # Windowed variants get their own stable fact id; the base
                # key stays unchanged so existing exclusions keep working.
                insight_key = f"{insight_key}:{window}"
            if insight_key in excluded_keys:
                continue  # owner-excluded: never re-promote
            fact_id = f"stat:{insight_key}"
            # B2.3 date-stamping (data side of T8): the artifact carries its
            # own period in the payload AND the summary text carries the
            # window's end date in ISO form, so a rendered stat can never
            # masquerade as current.
            period_start, period_end, as_of = self._promotion_period(insight)
            fact = {
                "fact_id": fact_id,
                "dimension": insight.get("dimension") or "memory",
                "source_id": "stats_engine",
                "record_id": insight["stat_id"],
                "object_type": "stat_insight",
                "tag": insight["text"],
                "summary_text": f"{insight['text']} (as of {period_end})",
                "stat_summary": insight.get("summary"),
                "group_key": insight.get("group_key"),
                "window": insight.get("window"),
                "period_start": period_start,
                "period_end": period_end,
                "as_of": as_of,
                "confidence": insight.get("confidence"),
                "disclosure": "owner_only",
                "provider": "topos",
                "model": "stats_engine_v1",
            }
            # Exposure-profile ledger marker (plan contract: query-side stat
            # selection separates behavioral ledgers from authored ones).
            if insight.get("ledger"):
                fact["ledger"] = str(insight["ledger"])
            adapters.signal.put_fact(fact)
            written += 1
        return written
