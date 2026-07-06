"""Mode-aware signal retrieval (PRD §8.5–8.7)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..storage.adapters.factory import AdapterBundle
from .manifest import ScopeResolutionManifest
from .types import (
    MODE_RANK,
    AccessMode,
    RetrievalBundle,
    RetrievalError,
    RetrievalRequest,
)

logger = logging.getLogger(__name__)

_INFERENCE_EXCLUDED_KEYS = frozenset({"content", "text", "body"})
# Canonical rows carry the raw record text in topic/summary_text (see _load_canonical_summary_items).
# Inference must expose only the existence/relevance signal — never the raw text — so canonical
# score items are stripped of these too. Derived items (briefs, facts, clusters) keep topic/
# summary_text because those are computed labels, not raw content.
_INFERENCE_CANONICAL_EXCLUDED_KEYS = _INFERENCE_EXCLUDED_KEYS | frozenset({"topic", "summary_text"})
# Semantic hits carry raw chunk previews; inference keeps only the similarity/id signal.
_INFERENCE_SEMANTIC_EXCLUDED_KEYS = frozenset(
    {"content", "text", "body", "content_preview", "text_preview", "title"}
)
_SUMMARY_ITEM_CAP = 25
_SEMANTIC_HIT_LIMIT = 20
_CLUSTER_LIMIT = 5
_GOAL_SUMMARY_BOOST = 0.88
_VECTOR_WORK_SCOPE_DAMPEN = 0.55


def resolve_retrieval_source_ids(
    manifest: ScopeResolutionManifest,
    installed_source_ids: Optional[List[str]] = None,
) -> List[str]:
    ids = [str(s).strip() for s in (manifest.default_source_ids or []) if str(s).strip()]
    if not ids and manifest.default_source_id:
        ids = [str(manifest.default_source_id)]
    if not installed_source_ids:
        return ids
    installed = {str(s).strip() for s in installed_source_ids if str(s).strip()}
    if not installed:
        return ids
    filtered = [sid for sid in ids if sid in installed]
    if filtered:
        return filtered
    logger.debug(
        "No installed sources intersect manifest for scope=%s; using manifest defaults",
        manifest.scope_id,
    )
    return ids


def _resolve_source_ids(
    manifest: ScopeResolutionManifest,
    installed_source_ids: Optional[List[str]] = None,
) -> List[str]:
    return resolve_retrieval_source_ids(manifest, installed_source_ids)


def _parse_row_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    for field in ("event_at", "ts", "occurred_at", "created_at"):
        raw = row.get(field)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except ValueError:
            continue
    return None


def _apply_filter_manifest_rows(
    rows: List[Dict[str, Any]],
    filter_manifest: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not filter_manifest:
        return rows
    window = filter_manifest.get("rolling_window") or {}
    days = int(window.get("days") or 0)
    if days <= 0:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: List[Dict[str, Any]] = []
    for row in rows:
        ts = _parse_row_timestamp(row)
        if ts is None or ts >= cutoff:
            kept.append(row)
    return kept


def _goal_relevance(goal_text: str, query_text: str) -> float:
    text = str(goal_text or "").strip()
    if not text:
        return 0.0
    tokens = _query_tokens(query_text)
    if not tokens:
        return _GOAL_SUMMARY_BOOST
    blob = text.lower()
    overlap = sum(1 for token in tokens if token in blob)
    if overlap == 0:
        return 0.72
    return min(1.0, 0.75 + overlap / len(tokens) * 0.25)


def _load_user_goal_summaries(
    query_text: str,
    *,
    source_ids: Optional[List[str]] = None,
    limit: int = _SUMMARY_ITEM_CAP,
) -> List[Dict[str, Any]]:
    try:
        from ..core.state import get_db_connection

        conn = get_db_connection()
        if conn is None:
            return []
        params: List[Any] = []
        query = "SELECT goal_id, record_id, source_id, goal_text FROM user_goals"
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            query += f" WHERE source_id IN ({placeholders})"
            params.extend(source_ids)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(limit * 3, 50))
        rows = conn.execute(query, tuple(params)).fetchall()
        items: List[Dict[str, Any]] = []
        tokens = _query_tokens(query_text)
        for goal_id, record_id, source_id, goal_text in rows:
            text = str(goal_text or "").strip()
            if not text:
                continue
            if tokens and not any(token in text.lower() for token in tokens):
                continue
            items.append(
                {
                    "topic": text,
                    "summary_text": text,
                    "goal_id": goal_id,
                    "record_id": record_id,
                    "source_id": source_id,
                    "dimension": "work",
                    "relevance_score": round(_goal_relevance(text, query_text), 4),
                    "retrieval_source": "user_goal",
                }
            )
        items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
        if not items and rows:
            for goal_id, record_id, source_id, goal_text in rows[:limit]:
                text = str(goal_text or "").strip()
                if not text:
                    continue
                items.append(
                    {
                        "topic": text,
                        "summary_text": text,
                        "goal_id": goal_id,
                        "record_id": record_id,
                        "source_id": source_id,
                        "dimension": "work",
                        "relevance_score": round(_goal_relevance(text, query_text), 4),
                        "retrieval_source": "user_goal",
                    }
                )
        items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
        return items[:limit]
    except Exception as exc:
        logger.debug("user_goals load skipped: %s", exc)
        return []


def _list_canonical_rows(
    adapters: AdapterBundle,
    table: str,
    *,
    source_ids: List[str],
    limit: int = 100,
    disclosure_tier: str = "owner_raw",
) -> List[Dict[str, Any]]:
    # canonical.list() already applies the disclosure tier (SQL adapters via the
    # per-table _disclosure spec; in-memory fake via apply_disclosure_tier_to_rows), so the
    # rows returned here are ALREADY disclosed to `disclosure_tier`. Re-applying the swap
    # would over-redact: SQL-disclosed rows arrive as redacted text with no disclosure
    # column, which the swap mistakes for pending raw and overwrites with the placeholder.
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    candidates = source_ids or [None]
    for source_id in candidates:
        page = adapters.canonical.list(
            table,
            limit=limit,
            offset=0,
            source_id=source_id,
            disclosure_tier=disclosure_tier,
        )
        for row in page.items:
            record_id = str(row.get("record_id") or row.get("message_id") or "")
            key = record_id or str(row)
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item.setdefault("_table", table)
            rows.append(item)
            if len(rows) >= limit:
                return rows
    return rows[:limit]


def _row_summary_text(table: str, row: Dict[str, Any], *, scope_id: str = "") -> str:
    import json as _json

    if table == "profile_records":
        parts = [row.get("record_type"), row.get("title"), row.get("organization"), row.get("description")]
        return " — ".join(str(p).strip() for p in parts if p)
    if table == "calendar_events":
        if scope_id == "availability:read":
            meta = row.get("metadata_json") or {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except _json.JSONDecodeError:
                    meta = {}
            busy = meta.get("is_busy", True)
            label = "open window" if busy is False else "busy block"
            return f"{label} {row.get('starts_at')} — {row.get('ends_at')}"
        title = str(row.get("title") or "").strip()
        human_date = _human_date_from_iso(str(row.get("starts_at") or ""))
        parts = [title]
        if human_date:
            parts.append(human_date)
        parts.append(f"{row.get('starts_at')} — {row.get('ends_at')}")
        return " ".join(p for p in parts if p).strip()
    if table == "contacts":
        return str(row.get("display_name") or "")
    if table == "contact_identifiers":
        return f"{row.get('identifier_type')}: {row.get('identifier')}"
    if table == "financial_transactions":
        return " — ".join(
            str(row.get(field) or "")
            for field in ("description", "category", "amount", "account")
            if row.get(field)
        )
    if table == "location_events":
        parts = [
            str(row.get(field) or "")
            for field in ("place_name", "city", "event_type", "occurred_at")
            if row.get(field)
        ]
        human_date = _human_date_from_iso(
            str(row.get("occurred_at") or row.get("starts_at") or "")
        )
        if human_date:
            parts.append(human_date)
        return " — ".join(parts)
    if table == "journal_entries":
        parts = [str(row.get(field) or "") for field in ("content", "mood_tag", "category", "people", "place_name") if row.get(field)]
        human_date = _human_date_from_iso(str(row.get("entry_at") or ""))
        if human_date:
            parts.append(human_date)
        meta = row.get("metadata_json")
        if isinstance(meta, str):
            try:
                import json as _json

                meta = _json.loads(meta)
            except _json.JSONDecodeError:
                meta = {}
        if isinstance(meta, dict):
            ends_at = str(row.get("ends_at") or meta.get("ends_at") or "").strip()
            if ends_at and row.get("entry_at"):
                parts.insert(0, f"{row.get('entry_at')} — {ends_at}")
            duration = meta.get("duration_minutes")
            if duration:
                parts.append(f"{duration} min")
        return " — ".join(parts)
    return " ".join(
        str(row.get(field) or "")
        for field in ("title", "description", "content", "place_name", "display_name")
        if row.get(field)
    ).strip()


def _redact_row_for_scope(scope_id: str, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json

    if scope_id != "availability:read":
        return row
    out = dict(row)
    out.pop("title", None)
    meta = out.get("metadata_json")
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k not in ("attendees",)}
    elif isinstance(meta, str):
        try:
            parsed = _json.loads(meta)
            parsed.pop("attendees", None)
            meta = parsed
        except _json.JSONDecodeError:
            pass
    out["metadata_json"] = meta
    out.pop("content", None)
    return out


def _canonical_relevance(text: str, query_text: str) -> float:
    tokens = _query_tokens(query_text)
    blob = text.lower()
    if not tokens:
        return 0.55
    overlap = sum(1 for token in tokens if token in blob)
    if overlap == 0:
        return 0.35
    return min(1.0, 0.6 + overlap / len(tokens) * 0.4)


def _load_canonical_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str = "owner_raw",
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for table in manifest.canonical_tables or []:
        rows = _list_canonical_rows(
            adapters,
            table,
            source_ids=source_ids,
            limit=50,
            disclosure_tier=disclosure_tier,
        )
        all_rows = list(rows)
        if query_text and not (
            manifest.scope_id == "work_context:read" and table == "profile_records"
        ):
            filtered = _filter_rows_by_query(rows, query_text)
            if filtered:
                rows = filtered
            if table == "calendar_events" and filtered:
                rows = _expand_calendar_week_context(filtered, all_rows, query_text)
        for row in rows:
            clean = _redact_row_for_scope(manifest.scope_id, table, row)
            text = _row_summary_text(table, clean, scope_id=manifest.scope_id)
            if not text:
                continue
            items.append(
                {
                    "topic": text[:120],
                    "summary_text": text,
                    "record_id": clean.get("record_id")
                    or clean.get("event_id")
                    or clean.get("contact_id")
                    or clean.get("message_id"),
                    "source_id": clean.get("source_id"),
                    "relevance_score": round(_canonical_relevance(text, query_text), 4),
                    "retrieval_source": f"canonical:{table}",
                }
            )
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    return items[:_SUMMARY_ITEM_CAP]


def _load_brief_summary_items(dimensions: List[str]) -> List[Dict[str, Any]]:
    try:
        from ..core.state import get_db_connection

        conn = get_db_connection()
        if conn is None:
            return []
        items: List[Dict[str, Any]] = []
        for dim in dimensions:
            dim_key = str(dim or "").strip().lower()
            if not dim_key:
                continue
            row = conn.execute(
                "SELECT markdown_body FROM signal_dimension_briefs WHERE signal_dimension=?",
                (dim_key,),
            ).fetchone()
            if not row or not row[0]:
                continue
            text = str(row[0])[:2500]
            items.append(
                {
                    "topic": f"{dim_key} brief",
                    "summary_text": text,
                    "dimension": dim_key,
                    "relevance_score": 0.82,
                    "retrieval_source": "dimension_brief",
                }
            )
        return items
    except Exception as exc:
        logger.debug("dimension brief load skipped: %s", exc)
        return []


def _mode_allowed(requested: AccessMode, ceiling: str) -> bool:
    req_rank = MODE_RANK.get(str(requested))
    if req_rank is None:
        return False
    return req_rank <= MODE_RANK.get(str(ceiling), MODE_RANK["inference"])


def _strip_forbidden(data: Any, forbidden: List[str]) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in forbidden or any(f in k for f in forbidden):
                continue
            out[k] = _strip_forbidden(v, forbidden)
        return out
    if isinstance(data, list):
        return [_strip_forbidden(item, forbidden) for item in data]
    return data


def _query_tokens(query_text: str) -> List[str]:
    stop = frozenset(
        {
            "the",
            "for",
            "what",
            "who",
            "whom",
            "how",
            "any",
            "are",
            "was",
            "did",
            "has",
            "have",
            "this",
            "that",
            "with",
            "from",
            "about",
            "when",
            "where",
            "give",
            "name",
            "one",
            "line",
            "mid",
            "day",
            "free",
            "busy",
            "can",
            "you",
            "their",
            "them",
            "there",
            "and",
            "not",
            "based",
            "into",
            "without",
        }
    )
    return [
        token
        for token in dict.fromkeys(re.findall(r"[a-z0-9]{3,}", (query_text or "").lower()))
        if token not in stop
    ] + _calendar_day_tokens(query_text)


def _calendar_day_tokens(query_text: str) -> List[str]:
    """Include short day numbers when a month name is present (e.g. March 13)."""
    text = query_text or ""
    if not re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
        text,
        re.I,
    ):
        return []
    return re.findall(r"\b(\d{1,2})\b", text)


def _filter_rows_by_query(rows: List[Dict[str, Any]], query_text: str) -> List[Dict[str, Any]]:
    tokens = _query_tokens(query_text)
    if not tokens:
        return rows
    matched: List[Dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(
            str(row.get(field) or "")
            for field in (
                "content",
                "content_preview",
                "title",
                "text",
                "body",
                "description",
                "organization",
                "record_type",
                "display_name",
                "starts_at",
                "ends_at",
                "entry_at",
                "occurred_at",
                "place_name",
                "category",
                "amount",
                "mood_tag",
            )
        ).lower()
        if any(token in haystack for token in tokens):
            matched.append(row)
    if matched:
        return matched
    date_hints = _iso_date_hints(query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(
                hint in str(row.get("starts_at") or row.get("entry_at") or row.get("occurred_at") or "")
                for hint in date_hints
            )
        ]
        if dated:
            return dated
    return matched


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _iso_date_hint(query_text: str) -> Optional[str]:
    hints = _iso_date_hints(query_text)
    return hints[0] if hints else None


def _iso_date_hints(query_text: str) -> List[str]:
    text = query_text or ""
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
    hints: List[str] = []
    for month_match in re.finditer(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
        text,
        re.I,
    ):
        month = _MONTHS[month_match.group(1).lower()]
        day = int(month_match.group(2))
        iso = f"{year}-{month:02d}-{day:02d}"
        if iso not in hints:
            hints.append(iso)
    if hints:
        return hints
    # Abbreviated month + day ("Mar 13", "jan 5"), skipping bare "may" ambiguity guard:
    # only fires when the abbreviation is unambiguous month usage followed by a day.
    for abbrev_match in re.finditer(
        r"\b(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{1,2})\b",
        text,
        re.I,
    ):
        abbrev = abbrev_match.group(1).lower()[:3]
        month = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
            "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }[abbrev]
        day = int(abbrev_match.group(2))
        iso = f"{year}-{month:02d}-{day:02d}"
        if iso not in hints:
            hints.append(iso)
    for iso_match in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        iso = iso_match.group(0)
        if iso not in hints:
            hints.append(iso)
    # A bare day number without any month is ambiguous — return nothing rather
    # than guessing a month (the old behavior defaulted to March).
    return hints


def _human_date_from_iso(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_ts)
    if not match:
        return ""
    year, month_num, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    month_names = {v: k for k, v in _MONTHS.items()}
    month_name = month_names.get(month_num, "")
    if not month_name:
        return ""
    return f"{month_name} {day}, {year}"


def _expand_calendar_week_context(
    rows: List[Dict[str, Any]], all_rows: List[Dict[str, Any]], query_text: str
) -> List[Dict[str, Any]]:
    lowered = (query_text or "").lower()
    if not rows or not any(token in lowered for token in ("week", "busy", "density", "compare", "consistent")):
        return rows
    seed_dates = {
        str(row.get("starts_at") or "")[:10]
        for row in rows
        if row.get("starts_at")
    }
    date_hints = set(_iso_date_hints(query_text))
    if any(token in lowered for token in ("compare", "versus", " vs ", "density")) and date_hints:
        seed_dates |= date_hints
    if not seed_dates:
        return rows
    expanded = list(rows)
    seen = {str(row.get("event_id") or row.get("record_id") or id(row)) for row in rows}
    for row in all_rows:
        row_key = str(row.get("event_id") or row.get("record_id") or id(row))
        if row_key in seen:
            continue
        starts = str(row.get("starts_at") or "")
        if starts[:10] in seed_dates:
            expanded.append(row)
            seen.add(row_key)
    return expanded


def _filter_calendar_rows(
    rows: List[Dict[str, Any]], query_text: str
) -> List[Dict[str, Any]]:
    """Date-aware calendar filter — handles compare/density queries with short day tokens."""
    date_hints = _iso_date_hints(query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(hint in str(row.get("starts_at") or "") for hint in date_hints)
        ]
        if dated:
            return _expand_calendar_week_context(dated, rows, query_text)
    filtered = _filter_rows_by_query(rows, query_text)
    if filtered:
        return _expand_calendar_week_context(filtered, rows, query_text)
    if date_hints:
        dated = [
            row
            for row in rows
            if any(hint in str(row.get("starts_at") or "") for hint in date_hints)
        ]
        if dated:
            return dated
    return filtered


def _semantic_hits(
    query_text: str,
    *,
    source_id: Optional[str] = None,
    limit: int = _SEMANTIC_HIT_LIMIT,
    time_range: Optional[Tuple[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    q = str(query_text or "").strip()
    if not q:
        return [], None
    try:
        from ..features.signal.service import get_signal_service

        result = get_signal_service().search_vectors(
            query=q,
            limit=limit,
            source_id=source_id,
            event_after=time_range[0] if time_range else None,
            event_before=time_range[1] if time_range else None,
        )
        hits: List[Dict[str, Any]] = []
        for item in result.get("items") or []:
            hits.append(
                {
                    "record_id": item.get("record_id"),
                    "text_preview": item.get("text_preview"),
                    "similarity": item.get("similarity"),
                    "source_id": item.get("source_id"),
                    "signal_dimension": item.get("signal_dimension"),
                }
            )
        return hits, result.get("error")
    except Exception as exc:
        logger.debug("semantic vector search skipped: %s", exc)
        return [], str(exc)


def _load_ranked_clusters(
    query_text: str,
    *,
    limit: int = _CLUSTER_LIMIT,
    primary_dimensions: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    try:
        from ..core.state import get_db_connection
        from ..features.signal.topic_clustering import (
            filter_clusters_by_dimensions,
            load_topic_clusters_for_query,
            rank_topic_clusters_for_query,
        )

        conn = get_db_connection()
        if conn is None:
            return []
        clusters = load_topic_clusters_for_query(conn, limit=50)
        clusters = filter_clusters_by_dimensions(clusters, primary_dimensions)
        if not clusters:
            return []
        query_vector = None
        if str(query_text or "").strip():
            from ..features.signal.topic_clustering import embed_query_text_for_ranking

            query_vector = embed_query_text_for_ranking(query_text)
            return rank_topic_clusters_for_query(
                clusters,
                query_text,
                limit=limit,
                query_vector=query_vector,
            )
        ranked = sorted(clusters, key=lambda c: int(c.get("member_count") or 0), reverse=True)
        return [{**c, "relevance_score": 0.0} for c in ranked[:limit]]
    except Exception as exc:
        logger.debug("topic cluster load skipped: %s", exc)
        return []


# Owner-only artifact classes and the manifest grant that unlocks each for
# non-owner tiers. Dense rollups (stats, dossiers) are computed unconditionally
# but packaged only where a scope explicitly asks for them.
_OWNER_ONLY_GRANTS = {
    "stat_insight": "stat_insights",
    "entity_dossier": "entity_dossiers",
    "fact": "owner_facts",
}


def _fact_disclosure_allowed(
    fact: Dict[str, Any],
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
) -> bool:
    """Owner-only facts never leave the owner tier without an explicit grant."""
    if str(fact.get("disclosure") or "") != "owner_only":
        return True
    if disclosure_tier == "owner_raw":
        return True
    grant = _OWNER_ONLY_GRANTS.get(str(fact.get("object_type") or ""), "stat_insights")
    return grant in (manifest.signal_objects or [])


def _load_fact_store_items(
    conn,
    query_text: str,
    linked_entities: List[Dict[str, Any]],
    *,
    disclosure_tier: str,
    manifest: ScopeResolutionManifest,
) -> List[Dict[str, Any]]:
    """Atomic facts: subject-first for linked entities, then token search."""
    try:
        from ..features.facts.store import FactStore
    except Exception:
        return []
    store = FactStore(conn)
    facts: List[Dict[str, Any]] = []
    seen_ids: set = set()
    try:
        subject_ids = [e["entity_id"] for e in linked_entities]
        self_row = conn.execute("SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1").fetchone()
        if self_row and self_row[0] not in subject_ids:
            subject_ids.append(str(self_row[0]))
        for subject_id in subject_ids:
            for fact in store.facts_for_subject(subject_id):
                if fact["object_id"] not in seen_ids:
                    facts.append(fact)
                    seen_ids.add(fact["object_id"])
        for fact in store.search(_query_tokens(query_text)):
            if fact["object_id"] not in seen_ids:
                facts.append(fact)
                seen_ids.add(fact["object_id"])
    except Exception as exc:
        logger.debug("fact store load skipped: %s", exc)
        return []

    tokens = set(_query_tokens(query_text))
    items: List[Dict[str, Any]] = []
    for fact in facts:
        payload = fact.get("payload") or {}
        gate_item = {"object_type": "fact", "disclosure": payload.get("disclosure")}
        if not _fact_disclosure_allowed(gate_item, disclosure_tier, manifest):
            continue
        text = FactStore.render(fact)
        blob = text.lower()
        overlap = sum(1 for t in tokens if t in blob)
        items.append(
            {
                "topic": text[:120],
                "summary_text": text,
                "record_id": fact["object_id"],
                "predicate": payload.get("predicate"),
                "retrieval_source": "fact",
                "relevance_score": round(min(1.0, 0.6 + 0.1 * overlap), 4),
                "_overlap": overlap,
            }
        )
    items.sort(key=lambda i: i.pop("_overlap"), reverse=True)
    return items[:10]


def _load_stat_insight_items(
    conn,
    query_text: str,
    *,
    dimensions: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    manifest: Optional[ScopeResolutionManifest] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Aggregate-intent queries answer best from stat insights, not chunks."""
    import json as _json

    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:%' ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    except Exception:
        return []
    tokens = set(_query_tokens(query_text))
    wanted_dims = {d.lower() for d in (dimensions or [])}
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for (payload_json,) in rows:
        try:
            fact = _json.loads(payload_json)
        except _json.JSONDecodeError:
            continue
        if manifest is not None and not _fact_disclosure_allowed(fact, disclosure_tier, manifest):
            continue
        text = str(fact.get("tag") or fact.get("summary_text") or "").strip()
        if not text:
            continue
        blob = f"{text} {fact.get('group_key') or ''} {fact.get('record_id') or ''}".lower()
        overlap = sum(1 for t in tokens if t in blob)
        dim_bonus = 1.0 if str(fact.get("dimension") or "").lower() in wanted_dims else 0.0
        score = overlap + dim_bonus
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "topic": text[:120],
                    "summary_text": text,
                    "record_id": fact.get("fact_id"),
                    "dimension": fact.get("dimension"),
                    "retrieval_source": "stat_insight",
                    "relevance_score": round(min(1.0, 0.5 + 0.15 * score), 4),
                },
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def _fusion_item_key(item: Dict[str, Any]) -> str:
    record_id = str(item.get("record_id") or "")
    if record_id:
        return f"rec:{record_id}"
    cluster_id = str(item.get("cluster_id") or "")
    if cluster_id:
        return f"cluster:{cluster_id}"
    return f"txt:{str(item.get('retrieval_source') or '')}:{str(item.get('topic') or '')[:80]}"


def _rrf_fuse_summary_lists(
    lists: List[Tuple[str, float, List[Dict[str, Any]]]],
    *,
    k: int = 60,
    cap: int = _SUMMARY_ITEM_CAP,
) -> List[Dict[str, Any]]:
    """Fuse ordered contributor lists with weighted reciprocal rank fusion.

    Each entry is (source_name, weight, ordered_items). Scores from different
    contributors are never compared directly — only ranks are, which is the
    whole point: cosine similarities, keyword overlaps, and fixed brief scores
    live on incomparable scales.
    """
    scores: Dict[str, float] = {}
    best_item: Dict[str, Dict[str, Any]] = {}
    contributors: Dict[str, List[str]] = {}
    for source_name, weight, ordered in lists:
        for rank, item in enumerate(ordered):
            key = _fusion_item_key(item)
            scores[key] = scores.get(key, 0.0) + weight / (k + rank + 1)
            contributors.setdefault(key, []).append(source_name)
            if key not in best_item:
                best_item[key] = dict(item)

    if not scores:
        return []
    max_score = max(scores.values()) or 1.0
    fused: List[Dict[str, Any]] = []
    for key, score in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:cap]:
        item = best_item[key]
        item["relevance_score"] = round(score / max_score, 4)
        item["fusion_sources"] = sorted(set(contributors.get(key) or []))
        fused.append(item)
    return fused


def _build_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    semantic_hits: List[Dict[str, Any]],
    ranked_clusters: List[Dict[str, Any]],
    installed_source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    plan=None,
) -> List[Dict[str, Any]]:
    from ..features.signal.vector_settings import fusion_rrf_enabled

    hit_record_ids = {str(h.get("record_id")) for h in semantic_hits if h.get("record_id")}
    prefer_goals = "user_goals" in (manifest.signal_objects or [])
    work_scope = manifest.scope_id == "work_context:read"
    source_ids = _resolve_source_ids(manifest, installed_source_ids)

    goal_items: List[Dict[str, Any]] = []
    if prefer_goals or work_scope:
        goal_items = _load_user_goal_summaries(query_text, source_ids=source_ids or None)

    canonical_items = _load_canonical_summary_items(
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        source_ids=source_ids,
        disclosure_tier=disclosure_tier,
    )

    brief_dims = list(manifest.primary_dimensions)
    if manifest.scope_id == "activity:read":
        brief_dims.append("Profile")
    brief_items = _load_brief_summary_items(brief_dims)

    # Legacy work-scope employer heuristic (scheduled for deletion once the
    # query planner covers it); contributes ordered items, not fake scores.
    if manifest.scope_id == "work_context:read" and query_text:
        lowered = query_text.lower()
        if any(token in lowered for token in ("employer", "company", "prior", "before", "previous")):
            for row in _list_canonical_rows(adapters, "profile_records", source_ids=source_ids, limit=50):
                if str(row.get("record_type") or "").lower() != "experience":
                    continue
                text = _row_summary_text("profile_records", row, scope_id=manifest.scope_id)
                if not text:
                    continue
                canonical_items.insert(
                    0,
                    {
                        "topic": text[:120],
                        "summary_text": text,
                        "record_id": row.get("record_id"),
                        "relevance_score": 0.94,
                        "retrieval_source": "canonical:profile_records",
                    },
                )

    cluster_items = [
        {
            "topic": cluster.get("label"),
            "summary_text": cluster.get("label"),
            "dimension": cluster.get("dimension"),
            "cluster_id": cluster.get("cluster_id"),
            "member_count": cluster.get("member_count"),
            "relevance_score": float(cluster.get("relevance_score") or 0.0),
            "retrieval_source": "cluster",
        }
        for cluster in ranked_clusters
    ]

    vector_items = [
        {
            "topic": hit.get("text_preview"),
            "summary_text": hit.get("text_preview"),
            "record_id": hit.get("record_id"),
            "source_id": hit.get("source_id"),
            "signal_dimension": hit.get("signal_dimension"),
            "relevance_score": round(float(hit.get("similarity") or 0.0), 4),
            "retrieval_source": "vector",
        }
        for hit in semantic_hits
    ]

    # Minimal-disclosure gate: owner-only facts (e.g. stat_insight aggregates —
    # work rhythms, spend patterns, contact cadence) are dense fingerprints of
    # the person. They are computed unconditionally but only *packaged* for the
    # owner tier, unless the scope manifest explicitly grants "stat_insights".
    fact_items: List[Dict[str, Any]] = []
    for dim in manifest.primary_dimensions:
        dim_key = dim.lower()
        page = adapters.signal.get_by_dimension(dim_key, limit=50, offset=0)
        for fact in page.items:
            if not _fact_disclosure_allowed(fact, disclosure_tier, manifest):
                continue
            label = fact.get("goal_text") or fact.get("summary_text") or fact.get("topic")
            if not label and not fact.get("dimension"):
                continue
            record_id = str(fact.get("record_id") or fact.get("fact_id") or "")
            if hit_record_ids and record_id and record_id not in hit_record_ids and not fact.get("goal_text"):
                continue
            score = (
                _goal_relevance(str(fact.get("goal_text")), query_text)
                if fact.get("goal_text")
                else (0.35 if hit_record_ids else 0.1)
            )
            fact_items.append(
                {
                    **{k: v for k, v in fact.items() if k != "content"},
                    "topic": label,
                    "summary_text": label,
                    "relevance_score": round(score, 4),
                    "retrieval_source": "signal_fact",
                }
            )
    fact_items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)

    # Entity spine: link query entities, contribute dossier/mention items.
    # Entity/fact/stat contributors need a raw sqlite handle. Use the bundle's
    # connection so they read the same database the query targets (the global
    # singleton may point at a different db when adapters were built from an
    # explicit conn/path — e.g. multi-db verification runs and seeded tests).
    raw_conn = getattr(adapters.signal, "_conn", None)

    entity_items: List[Dict[str, Any]] = []
    fact_store_items: List[Dict[str, Any]] = []
    if query_text:
        try:
            from ..core.state import get_db_connection
            from ..features.entities.linking import entity_context_items, link_query_entities

            conn = raw_conn if raw_conn is not None else get_db_connection()
            if conn is not None:
                linked = link_query_entities(conn, query_text)
                entity_items = [
                    item
                    for item in entity_context_items(conn, linked)
                    if _fact_disclosure_allowed(item, disclosure_tier, manifest)
                ]
                fact_store_items = _load_fact_store_items(
                    conn, query_text, linked, disclosure_tier=disclosure_tier, manifest=manifest
                )
        except Exception as exc:
            logger.debug("entity linking skipped: %s", exc)

    # Aggregate-shaped queries ("how often…", "average…") answer best from
    # the statistics layer; give those insights a heavyweight list.
    stat_items: List[Dict[str, Any]] = []
    if plan is not None and getattr(plan, "aggregate_intent", False):
        try:
            from ..core.state import get_db_connection

            stat_items = _load_stat_insight_items(
                raw_conn if raw_conn is not None else get_db_connection(),
                query_text,
                dimensions=getattr(plan, "dimensions", None),
                disclosure_tier=disclosure_tier,
                manifest=manifest,
            )
        except Exception as exc:
            logger.debug("stat insight load skipped: %s", exc)

    if fusion_rrf_enabled():
        canonical_weight = 2.0 if work_scope else 1.0
        return _rrf_fuse_summary_lists(
            [
                ("stat_insights", 2.0, stat_items),
                ("facts_store", 1.5, fact_store_items),
                ("entities", 1.5, entity_items),
                ("goals", 1.0, goal_items),
                ("canonical", canonical_weight, canonical_items),
                ("briefs", 0.8, brief_items),
                ("clusters", 0.8, cluster_items),
                ("vector", 0.6 if work_scope else 1.0, vector_items),
                ("signal_facts", 1.0, fact_items),
            ]
        )

    # Legacy path (TOPOS_FUSION_RRF=off): incomparable absolute scores.
    for item in entity_items + fact_store_items + stat_items:
        item.setdefault("relevance_score", 0.9)
    items = stat_items + fact_store_items + entity_items + goal_items + canonical_items + brief_items + cluster_items + vector_items + fact_items
    if work_scope:
        for item in items:
            if str(item.get("retrieval_source") or "").startswith("canonical:profile_records"):
                item["relevance_score"] = max(float(item.get("relevance_score") or 0.0), 0.96)
            if str(item.get("retrieval_source") or "") == "vector":
                item["relevance_score"] = round(
                    float(item.get("relevance_score") or 0.0) * _VECTOR_WORK_SCOPE_DAMPEN, 4
                )
    items.sort(key=lambda item: float(item.get("relevance_score") or 0.0), reverse=True)
    return items[:_SUMMARY_ITEM_CAP]


class DefaultSignalRetrievalAdapter:
    """Retrieve minimum necessary data per access mode and manifest."""

    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters
        self._last_stores: List[str] = []
        self.retrieve_call_count = 0

    def reset_retrieve_call_count(self) -> None:
        self.retrieve_call_count = 0

    def stores_touched(self) -> List[str]:
        return list(self._last_stores)

    def retrieve(self, request: RetrievalRequest) -> RetrievalBundle:
        self.retrieve_call_count += 1
        manifest: ScopeResolutionManifest = request.manifest
        query_text = str(request.query_text or "").strip()
        if request.skip_retrieval:
            self._last_stores = []
            return RetrievalBundle(context_packet={}, stores_touched=[], record_counts={})

        if not _mode_allowed(request.access_mode, manifest.access_mode_ceiling):
            raise RetrievalError("mode_ceiling_exceeded", f"{request.access_mode} exceeds ceiling {manifest.access_mode_ceiling}")

        touched: List[str] = []
        counts: Dict[str, int] = {}
        retrieval_meta: Dict[str, Any] = {
            "retrieval_strategy": "dimension_dump",
            "disclosure_tier": request.disclosure_tier,
        }
        packet: Dict[str, Any] = {"scope_id": manifest.scope_id, "access_mode": request.access_mode}

        source_filter = manifest.default_source_id
        source_ids = _resolve_source_ids(manifest, request.installed_source_ids)

        # One structured parse ahead of retrieval (entities/time/aggregate).
        plan = None
        if query_text:
            try:
                from ..core.state import get_db_connection
                from .planner import build_query_plan, query_planner_enabled

                if query_planner_enabled():
                    plan = build_query_plan(get_db_connection(), query_text)
                    retrieval_meta["query_plan"] = plan.to_meta()
            except Exception as exc:
                logger.debug("query planner skipped: %s", exc)

        time_range = plan.time_range if plan else None
        semantic_query = query_text
        if plan and plan.semantic_residual and len(plan.semantic_residual) >= 6:
            semantic_query = plan.semantic_residual

        semantic_hits: List[Dict[str, Any]] = []
        vector_error: Optional[str] = None
        if query_text and request.access_mode in ("summary", "inference"):
            semantic_hits, vector_error = _semantic_hits(
                semantic_query, source_id=source_filter, time_range=time_range
            )
            if not semantic_hits and time_range:
                # Time scope can starve results (sparse corpora); retry unscoped.
                semantic_hits, vector_error = _semantic_hits(semantic_query, source_id=source_filter)
            if not semantic_hits and source_ids:
                for sid in source_ids:
                    if sid == source_filter:
                        continue
                    semantic_hits, vector_error = _semantic_hits(semantic_query, source_id=sid)
                    if semantic_hits:
                        break
            if semantic_hits:
                touched.append("vector")
                retrieval_meta["retrieval_strategy"] = "query_aware"
            elif vector_error:
                logger.debug("vector search unavailable: %s", vector_error)

        ranked_clusters: List[Dict[str, Any]] = []
        if request.access_mode in ("summary", "inference"):
            ranked_clusters = _load_ranked_clusters(
                query_text,
                primary_dimensions=manifest.primary_dimensions,
            )
            if ranked_clusters:
                touched.append("topic_clusters")
                if query_text:
                    retrieval_meta["retrieval_strategy"] = "query_aware"
                retrieval_meta["clusters_returned"] = [
                    {
                        "cluster_id": cluster.get("cluster_id"),
                        "relevance_score": cluster.get("relevance_score"),
                        "primary_dimension": cluster.get("primary_dimension") or cluster.get("dimension"),
                    }
                    for cluster in ranked_clusters
                ]
                retrieval_meta["cluster_rank_strategy"] = ranked_clusters[0].get(
                    "cluster_rank_strategy", "term_entity"
                )
                if manifest.primary_dimensions:
                    retrieval_meta["facet_filter_applied"] = list(manifest.primary_dimensions)

        mode = request.access_mode
        if mode == "raw":
            rows: List[Dict[str, Any]] = []
            for table in manifest.canonical_tables:
                table_rows = _list_canonical_rows(
                    self._adapters,
                    table,
                    source_ids=source_ids,
                    limit=100,
                    disclosure_tier=request.disclosure_tier,
                )
                table_rows = [_redact_row_for_scope(manifest.scope_id, table, row) for row in table_rows]
                touched.append("canonical")
                if table == "profile_records" and "certification" in (query_text or "").lower():
                    typed = [
                        row
                        for row in table_rows
                        if str(row.get("record_type") or "").lower() == "certification"
                    ]
                    if typed:
                        table_rows = typed
                if (
                    table == "profile_records"
                    and manifest.scope_id == "work_context:read"
                    and query_text
                    and any(w in query_text.lower() for w in ("prior", "before", "previous", "employer"))
                ):
                    typed = [
                        row
                        for row in table_rows
                        if str(row.get("record_type") or "").lower() == "experience"
                    ]
                    if "topos" in query_text.lower() and "before" in query_text.lower():
                        typed = [
                            row
                            for row in typed
                            if str(row.get("organization") or "").strip().lower() != "topos"
                        ]
                    if typed:
                        table_rows = typed
                if query_text:
                    if table == "calendar_events":
                        table_rows = _filter_calendar_rows(table_rows, query_text)
                        if table_rows:
                            retrieval_meta["retrieval_strategy"] = "raw_query_filter"
                    else:
                        filtered = _filter_rows_by_query(table_rows, query_text)
                        if table == "profile_records" and re.search(r"\b(list|show)\b", query_text, re.I):
                            table_rows = filtered if filtered else table_rows
                        else:
                            table_rows = filtered if filtered else table_rows
                        if filtered:
                            retrieval_meta["retrieval_strategy"] = "raw_query_filter"
                table_rows = _apply_filter_manifest_rows(table_rows, request.filter_manifest)
                max_rows = int((request.filter_manifest or {}).get("max_rows") or 0)
                if max_rows > 0:
                    table_rows = table_rows[:max_rows]
                counts[table] = len(table_rows)
                for row in table_rows:
                    rows.append({"_table": table, **row})
            packet["rows"] = _strip_forbidden(rows, manifest.must_not_retrieve)
        elif mode == "summary":
            if query_text or semantic_hits or ranked_clusters:
                summaries = _build_summary_items(
                    manifest=manifest,
                    adapters=self._adapters,
                    query_text=query_text,
                    semantic_hits=semantic_hits,
                    ranked_clusters=ranked_clusters,
                    installed_source_ids=request.installed_source_ids,
                    disclosure_tier=request.disclosure_tier,
                    plan=plan,
                )
                if summaries:
                    touched.append("signal")
            else:
                summaries = []
                for dim in manifest.primary_dimensions:
                    dim_key = dim.lower()
                    page = self._adapters.signal.get_by_dimension(dim_key, limit=50, offset=0)
                    touched.append("signal")
                    for item in page.items:
                        if not _fact_disclosure_allowed(item, request.disclosure_tier, manifest):
                            continue
                        if item.get("summary_text") or item.get("topic") or item.get("dimension"):
                            summaries.append({k: v for k, v in item.items() if k != "content"})
            packet["summaries"] = summaries
            counts["summaries"] = len(summaries)
            if semantic_hits:
                packet["semantic_hits"] = semantic_hits
            if ranked_clusters:
                packet["topic_clusters"] = ranked_clusters
            if manifest.scope_id in ("relationship_context:read", "messages:read"):
                graph = self._adapters.graph.list_graph(limit_nodes=50, limit_edges=100)
                if graph.get("edges") or graph.get("nodes"):
                    touched.append("graph")
                    packet["graph"] = {
                        "nodes": graph.get("nodes") or [],
                        "edges": graph.get("edges") or [],
                    }
        elif mode == "inference":
            scores: List[Dict[str, Any]] = []
            canon_items = _load_canonical_summary_items(
                manifest=manifest,
                adapters=self._adapters,
                query_text=query_text,
                source_ids=source_ids,
                disclosure_tier=request.disclosure_tier,
            )
            for item in canon_items:
                scores.append({k: v for k, v in item.items() if k not in _INFERENCE_CANONICAL_EXCLUDED_KEYS})
            if manifest.scope_id == "activity:read":
                for item in _load_brief_summary_items(["Profile"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            elif manifest.scope_id == "health:read":
                for item in _load_brief_summary_items(["Wellbeing"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            elif manifest.scope_id == "schedule:read":
                for item in _load_brief_summary_items(["Time"]):
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            for dim in manifest.primary_dimensions:
                page = self._adapters.signal.get_by_dimension(dim.lower(), limit=50, offset=0)
                touched.append("signal")
                for item in page.items:
                    if not _fact_disclosure_allowed(item, request.disclosure_tier, manifest):
                        continue
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            if ranked_clusters:
                packet["topic_clusters"] = ranked_clusters
                counts["topic_clusters"] = len(ranked_clusters)
            if semantic_hits:
                # Inference exposes only the similarity/id signal from semantic hits, never
                # the raw chunk preview text.
                packet["semantic_hits"] = [
                    {k: v for k, v in hit.items() if k not in _INFERENCE_SEMANTIC_EXCLUDED_KEYS}
                    for hit in semantic_hits
                ]
                counts["semantic_hits"] = len(semantic_hits)
            graph = self._adapters.graph.list_graph(limit_nodes=50, limit_edges=100)
            if graph.get("edges") or graph.get("nodes"):
                touched.append("graph")
                packet["graph"] = {
                    "nodes": graph.get("nodes") or [],
                    "edges": graph.get("edges") or [],
                }
            meta = self._adapters.vector.list_metadata(limit=20, offset=0)
            if meta.total:
                touched.append("vector")
            packet["scores"] = _strip_forbidden(scores, manifest.must_not_retrieve)
            counts["scores"] = len(scores)
            packet = _strip_forbidden(packet, manifest.must_not_retrieve)

        retrieval_meta["vector_hits"] = len(semantic_hits)
        retrieval_meta["clusters_returned"] = len(ranked_clusters)

        self._last_stores = sorted(set(touched))
        return RetrievalBundle(
            context_packet=packet,
            stores_touched=self._last_stores,
            record_counts=counts,
            retrieval_metadata=retrieval_meta,
        )


# Protocol alias for imports
SignalRetrievalAdapter = DefaultSignalRetrievalAdapter
