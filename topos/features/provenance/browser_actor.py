"""Who operated a browser, independently of the authenticated capture writer.

The extension's declaration is evidence, not a new principal or an access grant.
Missing/legacy attribution remains unknown; absence of bot signals is not human
evidence. Conversation IDs are opaque source-scoped references, never timestamps
or similarity matches. A missing parent may arrive on a later source sync.
"""
from __future__ import annotations

import json
from typing import Any

BROWSER_SOURCES = frozenset({"browser_visits", "browser_events", "starred_websites"})
KINDS = frozenset({"human", "agent", "automation", "unknown"})
METHODS = frozenset({"manual", "controller_declared", "inherited_tab", "followup", "webdriver", "synthetic_event", "tab_group_declared", "tab_group_hint", "unresolved"})
IDENTITY_FIELDS = ("provider", "name", "session_id", "conversation_source", "conversation_id", "turn_id")


def _object(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def browser_actor(row: dict) -> dict:
    """Return a bounded, normalized actor from raw, flat, or canonical shape."""
    meta = _object(row.get("metadata_json"))
    raw = _object(row.get("actor") or row.get("actor_json") or meta.get("browser_actor"))
    kind = raw.get("kind") if isinstance(raw.get("kind"), str) and raw.get("kind") in KINDS else "unknown"
    method = raw.get("method") if isinstance(raw.get("method"), str) and raw.get("method") in METHODS else "unresolved"
    out: dict = {"version": 1, "kind": kind, "method": method}
    for key in (*IDENTITY_FIELDS, "assigned_at", "observed_at", "identity_method"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip() and len(val.strip()) <= 256:
            out[key] = val.strip()
    signals = _object(raw.get("signals"))
    out["signals"] = {key: True for key in ("webdriver", "synthetic_event", "agent_tab_group") if signals.get(key) is True}
    # Unsubstantiated human declarations must not open the personal-interest lane.
    declared = method in {"manual", "controller_declared", "inherited_tab", "tab_group_declared"} or (
        method == "followup" and out.get("identity_method") in {"manual", "controller_declared", "inherited_tab", "tab_group_declared"})
    if kind == "human" and (not declared or out["signals"]):
        out["kind"] = "automation" if out["signals"] else "unknown"
    if kind == "agent" and not all(out.get(key) for key in ("provider", "name", "conversation_source", "conversation_id")):
        out["kind"] = "automation"  # Preserve partial identity, never invent a parent.
    return out


def is_browser_record(row: dict) -> bool:
    meta = _object(row.get("metadata_json"))
    return bool(
        row.get("source_id") in BROWSER_SOURCES or row.get("_table") in {"browser_visits", "browser_events"}
        or "browser_actor" in meta or "actor_json" in row
        or str(row.get("event_id") or row.get("record_id") or "").startswith("browser:")
        or str(row.get("activity_type") or row.get("event_type") or "").lower() in
        {"visit", "browser_visit", "tab_activated", "browser_highlight"}
    )


def timeline_controller(event_id, source_id, activity_type, metadata_json):
    """SQL read predicate sharing the exact same normalization as derivation."""
    row = dict(event_id=event_id, source_id=source_id,
               activity_type=activity_type, metadata_json=metadata_json)
    if not is_browser_record(row):
        return None
    kind = browser_actor(row)["kind"]
    return "ai" if kind in {"agent", "automation"} else kind


def personal_browser_activity(row: dict) -> bool:
    """Non-browser records are unaffected; browser evidence needs explicit human attribution."""
    return not is_browser_record(row) or browser_actor(row)["kind"] == "human"


def actor_label(row: dict) -> str:
    actor = browser_actor(row)
    if actor["kind"] == "agent":
        return f'{actor["name"]} ({actor["provider"]})'
    return {"human": "owner", "automation": "unidentified automation", "unknown": "unknown browser controller"}[actor["kind"]]


def conversation_activity(conn, *, source_id: str, conversation_id: str) -> list[dict]:
    """Local owner-side exact lookup; does not traverse or disclose parent content.

    Call through an already-authorized owner read. The two identifiers must match
    the chat source's canonical identifiers, including any namespace prefix.
    No foreign key: browser events can arrive before the parent conversation.
    """
    cursor = conn.execute(
        "SELECT * FROM activity_events WHERE json_valid(metadata_json) "
        "AND json_extract(metadata_json, '$.browser_actor.conversation_source')=? "
        "AND json_extract(metadata_json, '$.browser_actor.conversation_id')=? ORDER BY occurred_at, event_id",
        (source_id, conversation_id),
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def exclude_nonhuman_browser_embeddings(conn, records: list[dict]) -> list[dict]:
    """Keep recall vectors, but keep nonhuman browsing out of personal clusters."""
    candidates = [r for r in records if is_browser_record(r)]
    if not candidates:
        return records
    allowed = set()
    ids = list({str(r.get("record_id") or "") for r in candidates})
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        try:
            rows = conn.execute("SELECT event_id, source_id, metadata_json FROM activity_events WHERE event_id IN ("
                                + ",".join("?" for _ in chunk) + ")", chunk).fetchall()
        except Exception:
            continue  # Cannot establish the actor: do not derive an interest.
        for rid, source, meta in rows:
            if personal_browser_activity({"event_id": rid, "source_id": source, "metadata_json": meta}):
                allowed.add(rid)
    return [r for r in records if not is_browser_record(r) or r.get("record_id") in allowed]
