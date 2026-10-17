"""One content-free timing row per owner home-chat turn, kept on the owner's node (HT lane, 15 Oct 2026).

Until this, the owner's home-chat latency was recorded nowhere durable. The app kept its ``[latency]`` lines in
browser localStorage and reset them when a turn finished; the control plane's usage rows are per model call, skip
calls that produced no tokens (so timeouts never appear) and carry no turn. No fix could be measured before and
after. The app now sends one row per turn after the turn ends, through a control-plane route that stores nothing.

PRIVACY. A row holds numbers, epoch-millisecond times, words from closed sets, random ids, a model tag and a build
stamp, and JSON lists of the same. Nothing else can be stored, because :func:`validate_record` refuses it: every key
is from a fixed list, every word from a fixed set, every number a whole non-negative integer under a bound, and the
two open-ended strings (model tag, build stamp) are held to a charset that a sentence cannot pass. A refusal names
the FIELD, from this module's own vocabulary, and never repeats the value, so a smuggled sentence does not come back
in the answer or in a log line. The owner ruled on the counts (15 Oct 2026): token counts per model call, rows per
scope query and the answer's length in characters and sentences are recorded; their text never is.

The node stamps what only it can know: its own version and clock (``node_version``, ``recorded_at``) and, later
(M5), the write-gate wait (``node_gate_wait_ms``). A client that sends one of those is refused, not ignored.

Read back as aggregates only (:func:`summarize`): nearest-rank p50/p90 per day, model and route, with outcome rates.
``scripts/query_latency_percentiles.py`` computes the same series from a census copy, and a test holds the two to the
same numbers.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..storage.db.migrations.home_chat_turn_timings_v1 import create_home_chat_turn_timings
from ..storage.db.write_gate import commit_connection, with_db_write

TABLE = "home_chat_turn_timings"
SCHEMA_VERSION = 1
MAX_RECORDS_PER_BATCH = 20
MAX_LLM_ITEMS = 16
MAX_TOOL_ITEMS = 32
MAX_SCOPE_QUERY_ITEMS = 32
SUMMARY_MAX_DAYS = 90

OUTCOMES = ("ok", "fallback", "timeout", "abort", "error")
#: The app's ``HomeMcpErrorCategory`` (next/lib/mcp/mcpErrorCategories.ts), plus ``other`` for anything it adds later.
ERROR_CLASSES = frozenset({
    "auth", "permission", "validation", "rate_limit", "network", "upstream", "empty_data",
    "remote_auth_required", "engine_timeout", "engine_unreachable", "other",
})
PROVIDERS = frozenset({"ollama", "platform", "redpill", "openai", "anthropic", "grok", "other"})
ROUTES = frozenset({"local", "hosted"})
LLM_PURPOSES = frozenset({"decide", "synthesize", "repair", "other"})
#: The node's own MCP tools by name. A remote connector's tool (``remote__<connector>__<tool>``) is sent as
#: ``remote``, so no connector id is stored, and anything else as ``other``.
TOOL_NAMES = frozenset({
    "query_scope", "shared_query_scope", "aggregate_scope", "get_analytics", "get_messages", "get_oplog",
    "get_table_rows", "get_table_schema", "list_connectors", "list_database_tables", "list_scopes",
    "describe_connector", "mcp_session_context", "save_to_topos", "remote", "other",
})
SCOPE_MODES = frozenset({"summary", "raw", "inference", "other"})

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: A random UUID, or the app store's ``<epoch ms>-<base36>`` turn id (homeChatStore.ts ``runTurn``).
TURN_ID_RE = re.compile(rf"^(?:{_UUID}|\d{{12,14}}-[0-9a-z]{{4,16}})$")
SESSION_ID_RE = re.compile(rf"^{_UUID}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
BUILD_RE = re.compile(r"^[0-9a-f]{7,40}$")

MAX_MS = 24 * 3600 * 1000
MAX_COUNT = 100_000
MAX_TOKENS = 10_000_000
MAX_CHARS = 10_000_000
#: 2020-01-01T00:00:00Z. A submitted_at before it, or more than a day ahead of the node, is a broken clock.
EARLIEST_MS = 1_577_836_800_000
FUTURE_SLACK_MS = 24 * 3600 * 1000


class TimingRecordRefused(ValueError):
    """A record that is not exactly the closed shape. Carries the field's name, never its value."""

    def __init__(self, field: str):
        super().__init__(f"INVALID_TIMING_RECORD:{field}")
        self.field = field


def now_ms() -> int:
    return int(time.time() * 1000)


#: The functions below take a ``now_ms`` argument for tests, which shadows the function inside them.
_clock_ms = now_ms


# --- one field at a time ----------------------------------------------------------------------------------------


def _whole(value: Any, field: str, *, upper: int, nullable: bool = True) -> Optional[int]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper:
        raise TimingRecordRefused(field)
    return value


def _word(value: Any, field: str, allowed: Iterable[str], *, nullable: bool = True) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise TimingRecordRefused(field)
    return value


def _match(value: Any, field: str, pattern: "re.Pattern[str]", *, nullable: bool = True) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TimingRecordRefused(field)
    return value


def _flag(value: Any, field: str, *, nullable: bool = True) -> Optional[bool]:
    if value is None and nullable:
        return None
    if not isinstance(value, bool):
        raise TimingRecordRefused(field)
    return value


def _items(value: Any, field: str, *, cap: int, count: int, keys: frozenset, check) -> List[Dict[str, Any]]:
    """A capped list of closed items, never longer than the count it itemizes (the app may truncate, not pad)."""
    if not isinstance(value, list) or len(value) > cap or len(value) > count:
        raise TimingRecordRefused(field)
    out = []
    for item in value:
        if not isinstance(item, Mapping) or not set(item) <= keys:
            raise TimingRecordRefused(field)
        try:
            out.append(check(item))
        except TimingRecordRefused:
            raise TimingRecordRefused(field) from None
    return out


_LLM_KEYS = frozenset({"purpose", "provider", "model", "ms", "ttft_ms", "load_ms", "prefill_ms", "decode_ms",
                       "prompt_tokens", "completion_tokens", "cold"})
_TOOL_KEYS = frozenset({"name", "ms", "ok"})
_SCOPE_QUERY_KEYS = frozenset({"ms", "rows", "mode", "ok"})


def _llm_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "purpose": _word(item.get("purpose"), "purpose", LLM_PURPOSES, nullable=False),
        "provider": _word(item.get("provider"), "provider", PROVIDERS),
        "model": _match(item.get("model"), "model", MODEL_RE),
        "cold": _flag(item.get("cold"), "cold"),
    }
    for key in ("ms", "ttft_ms", "load_ms", "prefill_ms", "decode_ms"):
        out[key] = _whole(item.get(key), key, upper=MAX_MS)
    for key in ("prompt_tokens", "completion_tokens"):
        out[key] = _whole(item.get(key), key, upper=MAX_TOKENS)
    return {k: out[k] for k in sorted(out)}


def _tool_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": _word(item.get("name"), "name", TOOL_NAMES, nullable=False),
        "ms": _whole(item.get("ms"), "ms", upper=MAX_MS),
        "ok": _flag(item.get("ok"), "ok", nullable=False),
    }


def _scope_query_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "ms": _whole(item.get("ms"), "ms", upper=MAX_MS),
        "rows": _whole(item.get("rows"), "rows", upper=MAX_COUNT),
        "mode": _word(item.get("mode"), "mode", SCOPE_MODES),
        "ok": _flag(item.get("ok"), "ok", nullable=False),
    }


RECORD_KEYS = frozenset({
    "schema_version", "turn_id", "session_id", "submitted_at", "ttft_ms", "total_ms", "outcome", "error_class",
    "model", "provider", "route", "llm_calls", "llm_ms", "llm", "tool_calls", "tool_ms", "tools", "scope_queries",
    "scope_query_ms_total", "scope_query_list", "answer_chars", "answer_sentences", "app_build",
})


def validate_record(raw: Any, *, now: int) -> Dict[str, Any]:
    """The record as it will be stored, or :class:`TimingRecordRefused` naming the first field that is not closed."""
    if not isinstance(raw, Mapping):
        raise TimingRecordRefused("record")
    if not set(raw) <= RECORD_KEYS:
        # Never the key itself: a key is as good a place to smuggle a sentence as a value.
        raise TimingRecordRefused("unknown_key")
    if raw.get("schema_version") != SCHEMA_VERSION or isinstance(raw.get("schema_version"), bool):
        raise TimingRecordRefused("schema_version")
    rec: Dict[str, Any] = {
        "turn_id": _match(raw.get("turn_id"), "turn_id", TURN_ID_RE, nullable=False),
        "session_id": _match(raw.get("session_id"), "session_id", SESSION_ID_RE, nullable=False),
        "submitted_at": _whole(raw.get("submitted_at"), "submitted_at", upper=now + FUTURE_SLACK_MS, nullable=False),
        "ttft_ms": _whole(raw.get("ttft_ms"), "ttft_ms", upper=MAX_MS),
        "total_ms": _whole(raw.get("total_ms"), "total_ms", upper=MAX_MS, nullable=False),
        "outcome": _word(raw.get("outcome"), "outcome", OUTCOMES, nullable=False),
        "error_class": _word(raw.get("error_class"), "error_class", ERROR_CLASSES),
        "model": _match(raw.get("model"), "model", MODEL_RE),
        "provider": _word(raw.get("provider"), "provider", PROVIDERS),
        "route": _word(raw.get("route"), "route", ROUTES),
        "llm_calls": _whole(raw.get("llm_calls"), "llm_calls", upper=MAX_COUNT, nullable=False),
        "llm_ms": _whole(raw.get("llm_ms"), "llm_ms", upper=MAX_MS),
        "tool_calls": _whole(raw.get("tool_calls"), "tool_calls", upper=MAX_COUNT, nullable=False),
        "tool_ms": _whole(raw.get("tool_ms"), "tool_ms", upper=MAX_MS),
        "scope_queries": _whole(raw.get("scope_queries"), "scope_queries", upper=MAX_COUNT, nullable=False),
        "scope_query_ms_total": _whole(raw.get("scope_query_ms_total"), "scope_query_ms_total", upper=MAX_MS),
        "answer_chars": _whole(raw.get("answer_chars"), "answer_chars", upper=MAX_CHARS),
        "answer_sentences": _whole(raw.get("answer_sentences"), "answer_sentences", upper=MAX_COUNT),
        "app_build": _match(raw.get("app_build"), "app_build", BUILD_RE),
    }
    if rec["submitted_at"] < EARLIEST_MS:
        raise TimingRecordRefused("submitted_at")
    if rec["ttft_ms"] is not None and rec["ttft_ms"] > rec["total_ms"]:
        raise TimingRecordRefused("ttft_ms")
    if rec["outcome"] == "ok" and rec["error_class"] is not None:
        raise TimingRecordRefused("error_class")
    rec["llm"] = _items(raw.get("llm", []), "llm", cap=MAX_LLM_ITEMS, count=rec["llm_calls"], keys=_LLM_KEYS,
                        check=_llm_item)
    rec["tools"] = _items(raw.get("tools", []), "tools", cap=MAX_TOOL_ITEMS, count=rec["tool_calls"],
                          keys=_TOOL_KEYS, check=_tool_item)
    rec["scope_query_list"] = _items(raw.get("scope_query_list", []), "scope_query_list", cap=MAX_SCOPE_QUERY_ITEMS,
                                     count=rec["scope_queries"], keys=_SCOPE_QUERY_KEYS, check=_scope_query_item)
    return rec


# --- the table ----------------------------------------------------------------------------------------------------


def table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure_table(conn: sqlite3.Connection) -> None:
    """Migration 86 makes the table; this covers a node whose ledger never ran. One read when it exists; the write
    gate only when it is missing (DDL is a write, and per-request DDL under the gate is what stalled home-chat
    saves on 1.5.9)."""
    if table_exists(conn):
        return
    with with_db_write():
        create_home_chat_turn_timings(conn)
        commit_connection(conn)


_COLUMNS = (
    "turn_id", "session_id", "schema_version", "submitted_at", "recorded_at", "ttft_ms", "total_ms", "outcome",
    "error_class", "model", "provider", "route", "llm_calls", "llm_ms", "tool_calls", "tool_ms", "scope_queries",
    "scope_query_ms_total", "answer_chars", "answer_sentences", "app_build", "node_version", "llm_json",
    "tools_json", "scope_queries_json",
)


def _dumps(items: List[Dict[str, Any]]) -> str:
    return json.dumps(items, separators=(",", ":"), sort_keys=True)


def record_batch(
    conn: sqlite3.Connection,
    records: Any,
    *,
    node_version: str,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Store a batch from the app's outbox. A resent turn is a duplicate, not a second row; one malformed record is
    refused without sinking the others. A batch that is not a short list is refused whole (``INVALID_TIMING_BATCH``).

    Returns counts and the names of refused fields (from :data:`RECORD_KEYS` and ``unknown_key`` / ``record`` only).
    """
    if not isinstance(records, list) or len(records) > MAX_RECORDS_PER_BATCH:
        raise ValueError("INVALID_TIMING_BATCH")
    now = _clock_ms() if now_ms is None else int(now_ms)
    accepted: List[Dict[str, Any]] = []
    refused_fields: set = set()
    for raw in records:
        try:
            accepted.append(validate_record(raw, now=now))
        except TimingRecordRefused as refused:
            refused_fields.add(refused.field)
    stored = 0
    if accepted:
        ensure_table(conn)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        sql = f"INSERT OR IGNORE INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        with with_db_write():
            for rec in accepted:
                cursor = conn.execute(sql, (
                    rec["turn_id"], rec["session_id"], SCHEMA_VERSION, rec["submitted_at"], now, rec["ttft_ms"],
                    rec["total_ms"], rec["outcome"], rec["error_class"], rec["model"], rec["provider"], rec["route"],
                    rec["llm_calls"], rec["llm_ms"], rec["tool_calls"], rec["tool_ms"], rec["scope_queries"],
                    rec["scope_query_ms_total"], rec["answer_chars"], rec["answer_sentences"], rec["app_build"],
                    str(node_version), _dumps(rec["llm"]), _dumps(rec["tools"]), _dumps(rec["scope_query_list"]),
                ))
                stored += int(cursor.rowcount or 0)
            commit_connection(conn)
    return {
        "stored": stored,
        "duplicate": len(accepted) - stored,
        "refused": len(records) - len(accepted),
        "refused_fields": sorted(refused_fields),
    }


# --- read back: aggregates only -----------------------------------------------------------------------------------


def percentile(values: Sequence[int], q: float) -> Optional[int]:
    """Nearest-rank, as in ``scripts/query_latency_percentiles.py``: every number reported is one a turn took."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def group_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    ttft = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
    total = [r["total_ms"] for r in rows]
    scope = [r["scope_query_ms_total"] for r in rows if r["scope_query_ms_total"] is not None]
    outcomes = {name: sum(1 for r in rows if r["outcome"] == name) for name in OUTCOMES}
    return {
        "n": n,
        "ttft_n": len(ttft),
        "ttft_p50_ms": percentile(ttft, 0.50),
        "ttft_p90_ms": percentile(ttft, 0.90),
        "total_p50_ms": percentile(total, 0.50),
        "total_p90_ms": percentile(total, 0.90),
        "scope_query_p50_ms": percentile(scope, 0.50),
        "scope_query_p90_ms": percentile(scope, 0.90),
        "outcomes": outcomes,
        "timeout_rate": _rate(outcomes["timeout"], n),
        "abort_rate": _rate(outcomes["abort"], n),
        "error_rate": _rate(outcomes["error"], n),
        "mean_llm_calls": round(sum(r["llm_calls"] for r in rows) / n, 2) if n else 0.0,
        "mean_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 2) if n else 0.0,
    }


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def summarize(conn: sqlite3.Connection, *, days: int = 30, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """p50/p90 per UTC day x model x route over the window, and per model x route for the whole window.

    Aggregates only: no turn id, session id or single row leaves through here. A turn with no TTFT (it produced no
    token) is left out of the TTFT percentiles and counted in ``ttft_n``'s shortfall, never counted as fast.
    """
    days = max(1, min(int(days), SUMMARY_MAX_DAYS))
    empty = {"window_days": days, "turns": 0, "groups": [], "totals": []}
    if not table_exists(conn):
        return empty
    now = _clock_ms() if now_ms is None else int(now_ms)
    since = now - days * 86_400_000
    cursor = conn.execute(
        f"SELECT submitted_at, ttft_ms, total_ms, outcome, model, route, llm_calls, tool_calls, scope_query_ms_total"
        f" FROM {TABLE} WHERE submitted_at >= ? AND submitted_at <= ?",
        (since, now + FUTURE_SLACK_MS),
    )
    names = [d[0] for d in cursor.description]
    rows = [dict(zip(names, r)) for r in cursor.fetchall()]
    if not rows:
        return empty
    by_group: Dict[tuple, List[Dict[str, Any]]] = {}
    by_total: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        model, route = r["model"] or "unknown", r["route"] or "unknown"
        by_group.setdefault((_day(r["submitted_at"]), model, route), []).append(r)
        by_total.setdefault((model, route), []).append(r)
    groups = [{"day": d, "model": m, "route": rt, **group_stats(g)} for (d, m, rt), g in sorted(by_group.items())]
    groups.sort(key=lambda g: g["day"], reverse=True)       # newest day first; stable, so model and route stay sorted
    totals = [{"model": m, "route": rt, **group_stats(g)} for (m, rt), g in sorted(by_total.items())]
    return {"window_days": days, "turns": len(rows), "groups": groups, "totals": totals}
