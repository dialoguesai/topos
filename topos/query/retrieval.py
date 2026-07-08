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

# --- Query routing vocabulary ---------------------------------------------------------
# A query that names a surface ("what's on my calendar", "show my messages") makes that
# surface topically relevant per se: with no further content tokens it is a BROWSE of
# that surface (recent rows). Content tokens beyond the surface words are the actual
# ask and must match rows — an unmatched specific ask contributes nothing (absence
# honesty; the negative-control lane).
_SURFACE_INTENT_TERMS: Dict[str, Tuple[str, ...]] = {
    "conversation_messages": (
        "message", "texted", "text message", "chat", "conversation", "sms", "imessage",
    ),
    "ai_chat_messages": (
        "ai conversation", "ai chat", "assistant", "chatgpt", "prompt", "ai message",
    ),
    "calendar_events": (
        "calendar", "meeting", "schedule", "event", "appointment", "standup",
        "agenda", "busy", "free", "availability",
    ),
    "journal_entries": ("journal", "diary", "mood", "wrote", "writing"),
    "location_events": (
        "place", "where", "location", "visit", "went", "travel", "city", "cities",
    ),
    # NB: content words like 'github' are deliberately NOT surface terms — they
    # must stay in the residual so rows are matched by them (C21).
    "activity_events": (
        "brows", "website", "site", "url", "reading", "read", "looked", "looking",
        "visit", "online", "activity", "watched",
    ),
    "financial_transactions": (
        "spend", "spent", "transaction", "purchase", "bought", "money", "cost",
        "paid", "finance", "financial", "expense",
    ),
    "contacts": ("contact", "phone", "number", "email", "reach", "person", "people"),
    "contact_identifiers": ("contact", "phone", "number", "email", "identifier"),
    "profile_records": (
        "profile", "bio", "experience", "job", "work history", "resume", "employer",
        "certification", "education",
    ),
}
# Non-canonical surfaces that participate in the same routing decision.
_EXTRA_SURFACE_TERMS: Tuple[str, ...] = ("goal", "objective", "priorit", "working on")
_RECENCY_TERMS = frozenset(
    {"recent", "recently", "latest", "newest", "last", "today", "yesterday",
     "now", "current", "currently"}
)


def _surface_intent(table: str, query_lower: str) -> bool:
    return any(term in query_lower for term in _SURFACE_INTENT_TERMS.get(table, ()))


def _residual_content_tokens(tokens: List[str], tables: Optional[List[str]] = None) -> List[str]:
    """Query tokens that are the *content* of the ask — not surface names, not
    recency framing. These are what retrieved rows must actually match."""
    blobs = [
        " ".join(terms)
        for tbl, terms in _SURFACE_INTENT_TERMS.items()
        if tables is None or tbl in tables
    ]
    blobs.append(" ".join(_EXTRA_SURFACE_TERMS))
    surface_blob = " ".join(blobs)
    out: List[str] = []
    for token in tokens:
        if token in _RECENCY_TERMS:
            continue
        # Plural-insensitive: "goals"/"meetings" name the same surface as
        # "goal"/"meeting".
        if token in surface_blob or token.rstrip("s") in surface_blob:
            continue
        out.append(token)
    return out


def _rare_tokens(conn, tokens: List[str]) -> Dict[str, int]:
    """Tokens with low document frequency in the FTS index, mapped to their df —
    the discriminative part of a specific ask. df==0 means the term appears
    nowhere in the indexed corpus (fabricated topics). Porter stemming on both
    sides makes 'committed' meet 'commitment'. Iteration yields the tokens, so
    callers that only need membership can treat the result like a list."""
    if conn is None or not tokens:
        return {}
    from ..features.signal.vector_settings import rare_token_df_max

    df_max = rare_token_df_max()
    try:
        total_row = conn.execute("SELECT count(*) FROM signal_embeddings_fts").fetchone()
        if not total_row or int(total_row[0]) < df_max * 10:
            # An empty/tiny FTS index carries no frequency signal — treating
            # every token as rare would abstain on everything (fresh DBs,
            # seeded test corpora).
            return {}
    except Exception:
        return {}
    rare: Dict[str, int] = {}
    for token in tokens:
        clean = re.sub(r"[^a-z0-9]", "", token.lower())
        if len(clean) < 3:
            continue
        try:
            row = conn.execute(
                "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                (f'"{clean}"',),
            ).fetchone()
        except Exception:
            return {}  # no FTS index (fresh DB) — treat nothing as rare
        df = int(row[0]) if row else 0
        if df < df_max:
            rare[token] = df
    return rare


def _item_text_blob(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(f) or "")
        for f in ("topic", "summary_text", "content", "text_preview", "content_preview")
    ).lower()


def _sqlite_main_path(conn) -> str:
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                import os as _os

                return _os.path.realpath(str(row[2] or ""))
    except Exception:
        pass
    return ""


def _bundle_is_global_db(adapters: AdapterBundle) -> bool:
    """True when this query's adapter bundle targets the same database as the
    global singleton — i.e. the global-connection layers (vector search, topic
    clusters) actually describe THIS query's data. On mismatch (seeded eval
    corpora, multi-db verification) those layers must not contribute: they
    would silently serve another database's content."""
    bundle_conn = getattr(adapters.signal, "_conn", None)
    if bundle_conn is None:
        return True  # non-sqlite/fake bundles: nothing to compare
    try:
        from ..core.state import get_db_connection

        global_conn = get_db_connection()
    except Exception:
        return True
    if global_conn is None:
        return True
    bundle_path = _sqlite_main_path(bundle_conn)
    global_path = _sqlite_main_path(global_conn)
    if not bundle_path or not global_path:
        return True
    return bundle_path == global_path


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
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    try:
        # Prefer the query's own connection (multi-db verification, seeded evals);
        # the global singleton may point at a different database.
        if conn is None:
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
        query_lower = (query_text or "").lower()
        goal_intent = any(term in query_lower for term in _EXTRA_SURFACE_TERMS)
        seen_texts: set = set()
        for goal_id, record_id, source_id, goal_text in rows:
            text = str(goal_text or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_texts:
                continue
            token_match = bool(tokens) and any(token in key for token in tokens)
            # A goal rides on token overlap OR on explicit goal intent ("what are
            # my goals") — never as unconditional filler.
            if tokens and not token_match and not goal_intent:
                continue
            seen_texts.add(key)
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
    contains: Optional[List[str]] = None,
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
        try:
            page = adapters.canonical.list(
                table,
                limit=limit,
                offset=0,
                source_id=source_id,
                disclosure_tier=disclosure_tier,
                contains=contains,
            )
        except TypeError:
            # Adapter predates the contains filter — fall back to a plain page.
            page = adapters.canonical.list(
                table,
                limit=limit,
                offset=0,
                source_id=source_id,
                disclosure_tier=disclosure_tier,
            )
        for row in page.items:
            record_id = str(row.get("record_id") or row.get("message_id") or "")
            # contact_identifiers rows share record_id (= contact_id) across
            # distinct identifiers — the identifier is part of the row identity.
            key = (record_id + "|" + str(row.get("identifier") or "")) if record_id else str(row)
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item.setdefault("_table", table)
            rows.append(item)
    # Global recency: each source's page is already newest-first, but sources
    # are concatenated in manifest order — without a merge sort the first
    # source's (often demo-fixture) rows outrank every real source's newest.
    def _ts(row: Dict[str, Any]) -> str:
        return str(row.get("event_at") or row.get("starts_at") or row.get("entry_at") or "")

    if any(_ts(r) for r in rows):
        rows.sort(key=_ts, reverse=True)
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


def _route_canonical_rows(
    adapters: AdapterBundle,
    table: str,
    *,
    manifest: ScopeResolutionManifest,
    query_text: str,
    source_ids: List[str],
    limit: int,
    disclosure_tier: str,
    rare_query_tokens: Optional[List[str]] = None,
    browse_fallback: bool = False,
) -> List[Dict[str, Any]]:
    """Router: content tokens must MATCH rows (full-table SQL filter); a query
    that only names the surface (or has no tokens) BROWSES recent rows; a
    specific ask that matches nothing contributes nothing — no unfiltered
    fallback page. Fabricated topics must come back empty.

    browse_fallback=True (inference mode) keeps the recency browse even without
    surface intent: the inference packet is derived existence signal, not
    content, and the answerer needs candidates — but the rare-token honesty
    check still applies, so unanswerable specifics stay empty."""
    query_lower = (query_text or "").lower()
    tokens = _query_tokens(query_text)
    residual = _residual_content_tokens(tokens, tables=[table])
    date_hints = _iso_date_hints(query_text)

    if not tokens and not date_hints:
        # No query content at all — recency browse.
        return _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
        )

    matched: List[Dict[str, Any]] = []
    if residual or date_hints:
        matched = _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
            contains=[*residual, *date_hints],
        )
    if matched:
        if table == "calendar_events":
            all_rows = _list_canonical_rows(
                adapters, table, source_ids=source_ids, limit=max(limit, 100),
                disclosure_tier=disclosure_tier,
            )
            matched = _expand_calendar_week_context(matched, all_rows, query_text)
        return matched

    # Identifiers join their contact: "find the contact record for Jessica"
    # must surface Jessica's phone/email rows even though those rows don't
    # contain her name — match contacts by name, then identifiers by contact id.
    if table == "contact_identifiers" and residual:
        contact_rows = _list_canonical_rows(
            adapters, "contacts", source_ids=source_ids, limit=20,
            disclosure_tier=disclosure_tier, contains=residual,
        )
        ids = [str(r.get("record_id") or "") for r in contact_rows if r.get("record_id")]
        if ids:
            # No source filter here: identifier rows carry the provenance of the
            # channel that observed them (imessage/signal/'*'), not the contact's
            # source — the contact itself is already scope-authorized.
            ident_rows = _list_canonical_rows(
                adapters, table, source_ids=[], limit=limit,
                disclosure_tier=disclosure_tier, contains=ids,
            )
            if ident_rows:
                return ident_rows

    # Nothing matched the content tokens. Browse is honest only when the surface
    # itself was asked for AND the ask carried no effectively-absent token
    # (df ≤ 2 — zero, or a porter-stem collision) — a term the corpus does not
    # contain means the specific thing isn't there. Weakly-rare df>2 framing
    # ('spend') and answer-shape words (stoplisted upstream) must not block.
    rare_dfs = (
        dict(rare_query_tokens)
        if isinstance(rare_query_tokens, dict)
        else {t: 0 for t in (rare_query_tokens or [])}
    )
    if any(df <= 2 and t in residual for t, df in rare_dfs.items()):
        return []
    work_profile = manifest.scope_id == "work_context:read" and table == "profile_records"
    if _surface_intent(table, query_lower) or work_profile or browse_fallback:
        return _list_canonical_rows(
            adapters, table, source_ids=source_ids, limit=limit,
            disclosure_tier=disclosure_tier,
        )
    return []


def _load_canonical_summary_items(
    *,
    manifest: ScopeResolutionManifest,
    adapters: AdapterBundle,
    query_text: str,
    source_ids: List[str],
    disclosure_tier: str = "owner_raw",
    rare_query_tokens: Optional[List[str]] = None,
    browse_fallback: bool = False,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for table in manifest.canonical_tables or []:
        rows = _route_canonical_rows(
            adapters,
            table,
            manifest=manifest,
            query_text=query_text,
            source_ids=source_ids,
            limit=50,
            disclosure_tier=disclosure_tier,
            rare_query_tokens=rare_query_tokens,
            browse_fallback=browse_fallback,
        )
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


def _load_brief_summary_items(
    dimensions: List[str], *, conn: Optional[Any] = None
) -> List[Dict[str, Any]]:
    try:
        if conn is None:
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
            # query framing — never content
            "which",
            "does",
            "show",
            "find",
            "tell",
            "know",
            "most",
            "often",
            "usually",
            "typically",
            "involving",
            "list",
            "whats",
            "record",
            "records",
            "long",
            "much",
            "many",
            "time",
            "say",
            "says",
            "everything",
            "anything",
            "something",
            "stuff",
            "year",
            "years",
            # answer-shape words: they describe the KIND of aggregate wanted,
            # never row content — derived layers answer them without containing
            # them ('cadence' df 1 in a corpus whose stat tags say "every 5.8 h")
            "cadence",
            "frequency",
            "frequently",
            "rhythm",
            "pattern",
            "patterns",
            "habit",
            "habits",
            "routine",
            "routines",
            "trend",
            "trends",
            "average",
            "typical",
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
    temporal_shift: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Atomic facts: subject-first for linked entities, then token search.

    temporal_shift='past' (the planner's before/prior/used-to signal) widens the
    read to superseded revisions — the bi-temporal store keeps closed facts and
    a past-tense question is exactly what they answer. Superseded facts are
    rendered with an explicit no-longer-current marker so they can never read
    as present-tense truth."""
    try:
        from ..features.facts.store import FactStore
    except Exception:
        return []
    store = FactStore(conn)
    include_closed = temporal_shift == "past"
    facts: List[Dict[str, Any]] = []
    subject_linked: set = set()
    seen_ids: set = set()
    try:
        # Subject-first ONLY for entities the query actually names. The old
        # behavior also dumped every self-entity fact into every query — the
        # "owner lives in San Francisco" padding on all results.
        for entity in linked_entities:
            for fact in store.facts_for_subject(
                entity["entity_id"], include_closed=include_closed
            ):
                if fact["object_id"] not in seen_ids:
                    facts.append(fact)
                    seen_ids.add(fact["object_id"])
                    subject_linked.add(fact["object_id"])
        for fact in store.search(_query_tokens(query_text), include_closed=include_closed):
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
        valid_to = fact.get("valid_to")
        if valid_to:
            text += f" (no longer current — superseded {str(valid_to)[:10]})"
        # Overlap graded on the fact's own content — the rendered "owner …"
        # subject prefix guaranteed fake overlap on owner-phrased queries.
        content_blob = " ".join(
            [
                str(payload.get("predicate") or "").replace("_", " "),
                str(payload.get("object_value") or ""),
            ]
        ).lower()
        overlap = sum(1 for t in tokens if t in content_blob)
        if overlap == 0 and fact["object_id"] not in subject_linked and tokens:
            continue
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
        # No recency window: stats are keyed artifacts, not a stream — a
        # LIMIT-by-created_at made older stat families permanently unreachable
        # (calendar.commitment sat at rank 558/558). The safety cap is generous.
        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:%' ORDER BY created_at DESC LIMIT 5000"
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
        # Prefix matching bridges morphology ("committed"→"commitment",
        # "journaling"→"journal") — stat tags are terse, exact-token overlap
        # missed them.
        overlap = sum(1 for t in tokens if (t[:5] if len(t) >= 5 else t) in blob)
        dim_bonus = 1.0 if str(fact.get("dimension") or "").lower() in wanted_dims else 0.0
        # Token evidence is required; the dimension bonus only reorders. A
        # dimension match alone must not qualify stats for a query about a
        # topic that does not exist.
        if overlap <= 0:
            continue
        score = overlap + dim_bonus
        # Tie-break equal lexical scores by the stat's own sample size: for
        # per-group count families ("Most visited: …") hundreds of groups
        # match the same query words, and the high-n groups ARE the answer.
        try:
            n_weight = float((fact.get("stat_summary") or {}).get("n") or 0.0)
        except (TypeError, ValueError):
            n_weight = 0.0
        scored.append(
            (
                (score, n_weight),
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


_RECENT_WINDOW_DAYS = 14
_RECENT_ITEM_LIMIT = 10


def _default_conn():
    try:
        from ..core.state import get_db_connection

        return get_db_connection()
    except Exception:
        return None


def _load_recent_summary_items(
    conn,
    *,
    source_ids: Optional[List[str]] = None,
    days: int = _RECENT_WINDOW_DAYS,
    limit: int = _RECENT_ITEM_LIMIT,
) -> List[Dict[str, Any]]:
    """Freshest records as an ordered fusion contributor.

    Guarantees the last two weeks are always *representable* in the summary
    regardless of semantic similarity — recency is a first-class relevance
    signal, not a tiebreaker.
    """
    if conn is None:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    params: List[Any] = [cutoff]
    source_sql = ""
    ids = [str(s) for s in (source_ids or []) if str(s).strip()]
    if ids:
        source_sql = f" AND source_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    params.append(max(1, limit))
    try:
        rows = conn.execute(
            f"""
            SELECT record_id, source_id, signal_dimension, text_preview, event_at
            FROM signal_embeddings
            WHERE chunk_index = 0 AND event_at IS NOT NULL AND event_at >= ?{source_sql}
            ORDER BY event_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as exc:
        logger.debug("recent summary items skipped: %s", exc)
        return []
    items: List[Dict[str, Any]] = []
    for record_id, source_id, dimension, preview, event_at in rows:
        text = str(preview or "").strip()
        if not text:
            continue
        items.append(
            {
                "topic": text[:120],
                "summary_text": text,
                "record_id": record_id,
                "source_id": source_id,
                "signal_dimension": dimension,
                "event_at": event_at,
                "retrieval_source": "recent",
            }
        )
    return items


def _fusion_item_key(item: Dict[str, Any]) -> str:
    record_id = str(item.get("record_id") or "")
    if record_id:
        return f"rec:{record_id}"
    cluster_id = str(item.get("cluster_id") or "")
    if cluster_id:
        return f"cluster:{cluster_id}"
    return f"txt:{str(item.get('retrieval_source') or '')}:{str(item.get('topic') or '')[:80]}"


# Contributors whose items describe *current state* rather than events in
# time: facts carry their own validity intervals, stats fold their own
# windows, briefs and dossiers are maintained snapshots. Decaying these by
# created_at would punish exactly the artifacts built to stay current.
_NO_DECAY_FUSION_SOURCES = frozenset(
    {"stat_insights", "facts_store", "entities", "briefs", "goals"}
)


def _recency_decay_factor(
    item: Dict[str, Any],
    *,
    now: datetime,
    half_life_days: float,
    floor: float,
) -> float:
    ts = _parse_row_timestamp(item)
    if ts is None:
        return 1.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return max(floor, 0.5 ** (age_days / half_life_days))


def _rrf_fuse_summary_lists(
    lists: List[Tuple[str, float, List[Dict[str, Any]]]],
    *,
    k: int = 60,
    cap: int = _SUMMARY_ITEM_CAP,
    now: Optional[datetime] = None,
    context_sources: frozenset = frozenset(),
    rare_tokens: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Fuse ordered contributor lists with weighted reciprocal rank fusion.

    Each entry is (source_name, weight, ordered_items). Scores from different
    contributors are never compared directly — only ranks are, which is the
    whole point: cosine similarities, keyword overlaps, and fixed brief scores
    live on incomparable scales.

    Time-stamped event items (vector hits, canonical rows) additionally decay
    by 2^(-age/half_life) toward a floor, so "relevant" drifts with the
    present instead of treating a message from last year and last night as
    interchangeable. Current-state contributors are exempt (see
    _NO_DECAY_FUSION_SOURCES).

    Abstention: `context_sources` name contributors that only add color around
    real findings (briefs, recent window, dimension filler) — they can never
    justify a non-empty result by themselves. And when the query carried
    `rare_tokens` (a specific ask), at least one evidence item must actually
    contain one of them, or the honest answer is nothing.
    """
    from ..features.signal.vector_settings import (
        fusion_recency_enabled,
        fusion_recency_floor,
        fusion_recency_half_life_days,
    )

    evidence_items = [
        item for source_name, _, ordered in lists
        if source_name not in context_sources
        for item in ordered
    ]
    if not evidence_items:
        return []
    if rare_tokens:
        rare_dfs: Dict[str, int] = (
            dict(rare_tokens) if isinstance(rare_tokens, dict)
            else {t: 1 for t in rare_tokens}
        )
        blobs = [_item_text_blob(item) for item in evidence_items]

        def _evidenced(token: str) -> bool:
            t = token.lower()
            return any(t in blob for blob in blobs)

        # Every effectively-absent token (df ≤ 2: zero, or a porter-stem
        # collision like 'falconer'→'falcon' df 1) must be evidenced by the
        # returned items themselves, or the ask is about something that does
        # not exist. Answer-shape vocabulary ('cadence', 'frequency') is
        # excluded upstream by the token stoplist — it describes the aggregate
        # wanted, not row content.
        if any(df <= 2 and not _evidenced(t) for t, df in rare_dfs.items()):
            return []
        # A query with SEVERAL rare tokens is a specific ask even when stem
        # collisions keep each df nonzero ('years as a competitive falconer':
        # falconer→'falcon' df 1, competitive df 26): if NONE of them is
        # evidenced, nothing retrieved is about the ask. A single weakly-rare
        # token ('journaling' df 5, 'cadence' df 1) never vetoes alone — the
        # derived layers may answer it without containing the word.
        if len(rare_dfs) >= 2 and not any(_evidenced(t) for t in rare_dfs):
            return []

    decay_on = fusion_recency_enabled()
    half_life = fusion_recency_half_life_days()
    floor = fusion_recency_floor()
    now = now or datetime.now(timezone.utc)

    scores: Dict[str, float] = {}
    best_item: Dict[str, Dict[str, Any]] = {}
    contributors: Dict[str, List[str]] = {}
    for source_name, weight, ordered in lists:
        apply_decay = decay_on and source_name not in _NO_DECAY_FUSION_SOURCES
        for rank, item in enumerate(ordered):
            key = _fusion_item_key(item)
            decay = (
                _recency_decay_factor(item, now=now, half_life_days=half_life, floor=floor)
                if apply_decay
                else 1.0
            )
            scores[key] = scores.get(key, 0.0) + weight * decay / (k + rank + 1)
            contributors.setdefault(key, []).append(source_name)
            if key not in best_item:
                best_item[key] = dict(item)
                if apply_decay and decay < 1.0:
                    best_item[key]["recency_factor"] = round(decay, 4)

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
    from ..features.signal.vector_settings import fusion_rrf_enabled, vector_evidence_min

    hit_record_ids = {str(h.get("record_id")) for h in semantic_hits if h.get("record_id")}
    prefer_goals = "user_goals" in (manifest.signal_objects or [])
    work_scope = manifest.scope_id == "work_context:read"
    source_ids = _resolve_source_ids(manifest, installed_source_ids)

    # The query's own connection — goal/brief contributors must read the same
    # database the query targets (the global singleton may point elsewhere in
    # multi-db verification runs and seeded evals).
    bundle_conn = getattr(adapters.signal, "_conn", None)

    # Specific-ask detection: the discriminative (rare-in-corpus) content tokens
    # of the query. If the query carries these and nothing matches them, the
    # honest result is empty — see the rare gate in _rrf_fuse_summary_lists.
    query_tokens = _query_tokens(query_text)
    residual_tokens = _residual_content_tokens(query_tokens)
    rare_query_tokens = _rare_tokens(bundle_conn, residual_tokens)

    goal_items: List[Dict[str, Any]] = []
    if prefer_goals or work_scope:
        goal_items = _load_user_goal_summaries(
            query_text, source_ids=source_ids or None, conn=bundle_conn
        )

    canonical_items = _load_canonical_summary_items(
        manifest=manifest,
        adapters=adapters,
        query_text=query_text,
        source_ids=source_ids,
        disclosure_tier=disclosure_tier,
        rare_query_tokens=rare_query_tokens,
    )

    brief_dims = list(manifest.primary_dimensions)
    if manifest.scope_id == "activity:read":
        brief_dims.append("Profile")
    brief_items = _load_brief_summary_items(brief_dims, conn=bundle_conn)

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

    # Zero-scored clusters are unranked filler, not findings.
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
        if float(cluster.get("relevance_score") or 0.0) > 0.0
    ]

    # Vector hits split by strength: a strong hit (cosine ≥ evidence floor) or a
    # lexical match on the query's content tokens is evidence; a weak FTS-OR or
    # low-cosine hit only rides along when real evidence exists.
    evidence_floor = vector_evidence_min()
    vector_items: List[Dict[str, Any]] = []
    vector_context_items: List[Dict[str, Any]] = []
    for hit in semantic_hits:
        preview_lower = str(hit.get("text_preview") or "").lower()
        similarity = float(hit.get("similarity") or 0.0)
        item = {
            "topic": hit.get("text_preview"),
            "summary_text": hit.get("text_preview"),
            "record_id": hit.get("record_id"),
            "source_id": hit.get("source_id"),
            "signal_dimension": hit.get("signal_dimension"),
            "relevance_score": round(similarity, 4),
            "retrieval_source": "vector",
        }
        lexical = any(t in preview_lower for t in residual_tokens)
        if similarity >= evidence_floor or lexical:
            vector_items.append(item)
        else:
            vector_context_items.append(item)

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
            if fact.get("goal_text"):
                # Same rule as the goals contributor: a goal rides on token
                # overlap or explicit goal intent — never as filler. Undated
                # goal texts dodge recency decay and were outranking on-topic
                # evidence on niche queries (C8's top-5).
                goal_lower = str(fact.get("goal_text")).lower()
                tokens_q = _query_tokens(query_text)
                goal_intent = any(term in (query_text or "").lower() for term in _EXTRA_SURFACE_TERMS)
                if tokens_q and not goal_intent and not any(t in goal_lower for t in tokens_q):
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
    # Entity/fact/stat contributors need a raw sqlite handle; reuse the bundle
    # connection resolved above for the goal/brief contributors.
    raw_conn = bundle_conn

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
                    conn, query_text, linked, disclosure_tier=disclosure_tier,
                    manifest=manifest,
                    temporal_shift=getattr(plan, "temporal_shift", None) if plan else None,
                )
        except Exception as exc:
            logger.debug("entity linking skipped: %s", exc)

    # The statistics layer is a first-class surface, not an intent special
    # case: frequency questions ("what cities…", "which moods…") often carry
    # no aggregate keyword yet answer best from stat insights, and the loader
    # self-qualifies on token overlap (the rare gate protects negatives). The
    # planner's aggregate flag remains a dimension hint only.
    stat_items: List[Dict[str, Any]] = []
    if query_text:
        try:
            from ..core.state import get_db_connection

            stat_items = _load_stat_insight_items(
                raw_conn if raw_conn is not None else get_db_connection(),
                query_text,
                dimensions=getattr(plan, "dimensions", None) if plan else None,
                disclosure_tier=disclosure_tier,
                manifest=manifest,
            )
        except Exception as exc:
            logger.debug("stat insight load skipped: %s", exc)

    if fusion_rrf_enabled():
        recent_items = _load_recent_summary_items(
            raw_conn if raw_conn is not None else _default_conn(),
            source_ids=source_ids or None,
        )
        recency_intent = any(t in (query_text or "").lower() for t in _RECENCY_TERMS)
        canonical_weight = 2.0 if work_scope else 1.0
        vector_weight = 0.6 if work_scope else 1.0
        # NOTE: cosine similarity must NOT waive the zero-df gate — a strong hit
        # on the generic half of a query ("compiler rewrite") cannot evidence a
        # name the corpus does not contain ("Threnody-7"). The N-lane found
        # exactly this leak when a strong-vector waiver existed here.
        return _rrf_fuse_summary_lists(
            [
                ("stat_insights", 2.0, stat_items),
                ("facts_store", 1.5, fact_store_items),
                ("entities", 1.5, entity_items),
                ("goals", 1.0, goal_items),
                ("canonical", canonical_weight, canonical_items),
                ("briefs", 0.8, brief_items),
                ("clusters", 0.8, cluster_items),
                ("vector", vector_weight, vector_items),
                ("vector_context", vector_weight * 0.8, vector_context_items),
                ("signal_facts", 1.0, fact_items),
                ("recent", 1.0, recent_items),
            ],
            context_sources=frozenset(
                {"briefs", "signal_facts", "vector_context"}
                | (set() if recency_intent else {"recent"})
            ),
            rare_tokens=rare_query_tokens,
        )

    # Legacy path (TOPOS_FUSION_RRF=off): incomparable absolute scores.
    for item in entity_items + fact_store_items + stat_items:
        item.setdefault("relevance_score", 0.9)
    items = stat_items + fact_store_items + entity_items + goal_items + canonical_items + brief_items + cluster_items + vector_items + vector_context_items + fact_items
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

        # The vector/cluster services read the GLOBAL db connection. When this
        # query's adapter bundle targets a different database (seeded eval
        # corpora, multi-db verification), those layers would silently serve
        # another database's content — the cross-db leak class of bce067a.
        global_layers_apply = _bundle_is_global_db(self._adapters)

        semantic_hits: List[Dict[str, Any]] = []
        vector_error: Optional[str] = None
        if query_text and global_layers_apply and request.access_mode in ("summary", "inference"):
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
        if global_layers_apply and request.access_mode in ("summary", "inference"):
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
            raw_rare_tokens: List[str] = []
            if query_text:
                try:
                    raw_conn_for_df = getattr(self._adapters.signal, "_conn", None)
                    raw_rare_tokens = _rare_tokens(
                        raw_conn_for_df, _residual_content_tokens(_query_tokens(query_text))
                    )
                except Exception:
                    raw_rare_tokens = []
            for table in manifest.canonical_tables:
                table_rows = _route_canonical_rows(
                    self._adapters,
                    table,
                    manifest=manifest,
                    query_text=query_text,
                    source_ids=source_ids,
                    limit=100,
                    disclosure_tier=request.disclosure_tier,
                    rare_query_tokens=raw_rare_tokens,
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
                # Query-token filtering (incl. calendar date awareness) happened
                # in _route_canonical_rows — SQL-side, over the full table, with
                # no unfiltered-fallback page.
                if query_text and table_rows:
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
            # Abstention is a complete answer: when a query found nothing, do not
            # dress the empty result with semantic/cluster/graph furniture that
            # reads as confident content about a topic that does not exist.
            abstained = bool(query_text) and not summaries
            if semantic_hits and not abstained:
                packet["semantic_hits"] = semantic_hits
            if ranked_clusters and not abstained:
                packet["topic_clusters"] = ranked_clusters
            if manifest.scope_id in ("relationship_context:read", "messages:read") and not abstained:
                graph = self._adapters.graph.list_graph(limit_nodes=50, limit_edges=100)
                if graph.get("edges") or graph.get("nodes"):
                    touched.append("graph")
                    packet["graph"] = {
                        "nodes": graph.get("nodes") or [],
                        "edges": graph.get("edges") or [],
                    }
        elif mode == "inference":
            scores: List[Dict[str, Any]] = []
            inference_rare: List[str] = []
            if query_text:
                try:
                    inference_rare = _rare_tokens(
                        getattr(self._adapters.signal, "_conn", None),
                        _residual_content_tokens(_query_tokens(query_text)),
                    )
                except Exception:
                    inference_rare = []
            canon_items = _load_canonical_summary_items(
                manifest=manifest,
                adapters=self._adapters,
                query_text=query_text,
                source_ids=source_ids,
                disclosure_tier=request.disclosure_tier,
                rare_query_tokens=inference_rare,
                browse_fallback=True,
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
