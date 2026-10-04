"""``permissions_v2_share_catalog`` (A2A-3 §7.1; N4): the sources an owner can share from, and the kinds this node releases.

Until now the control plane built the share editor's source list from its scope->table mapping, joined with the
node's install listing (``get_sources``) and a static mirror of bundled sources. The node's listing names installs
only, so a bundled source the owner never installs (iMessage) existed on the page only through that mirror. This
answer comes from the node's own rows instead:

- ``sources``: one entry per (source, canonical table) for every source with at least one row in
  ``conversation_messages``, ``ai_chat_messages``, ``journal_entries`` or ``activity_events``, bundled or installed;
  plus every active install under the scopes the control plane names (``owner_install_scopes``) whose source feeds
  one of those tables, with ``rows`` 0 when nothing has come in yet. ``installed`` says whether an active install
  of the source exists under those scopes. ``label`` is the source definition's display name: the install's own
  definition first, else the bundled one, else the source id.
- ``kinds``: what this node releases when bound (``share_kinds.released_kinds``), never facts.

Counts, ids and fixed names only: no row is read beyond its source id. Every read is one read-only transaction on
the database this node serves; nothing is written and no install record is touched.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .share_kinds import released_kinds

VERSION = "topos-share-catalog/v1"
TABLES = ("conversation_messages", "ai_chat_messages", "journal_entries", "activity_events")
#: A source definition's canonical group -> the canonical table its rows land in (the control plane's
#: ``source_tables.FAMILY_TABLES_BY_GROUP``, the same four).
GROUP_TABLES = {"conversations": "conversation_messages", "ai_messages": "ai_chat_messages",
                "journal": "journal_entries", "activity": "activity_events"}
#: Install states that count as live (``ai_chat_capture.INSTALL_LIVE``).
INSTALL_LIVE = ("installed", "active", "ready")
#: At most this many scopes per request; the control plane names two (``owner_install_scopes``).
MAX_SCOPES = 8
SCOPE_FIELDS = ("user_id", "topos_id", "dataset_id")


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn, name: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def _object(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bundled(source_id: str):
    try:
        from topos.sources.registry import BUNDLED_REGISTRY
    except Exception:  # noqa: BLE001 -- no registry: no bundled names
        return None
    return BUNDLED_REGISTRY.get(source_id)


def _bundled_table(source_id: str):
    source = _bundled(source_id)
    return GROUP_TABLES.get(_text(getattr(source, "canonical_group_id", None))) if source is not None else None


def _label(source_id: str, definitions: dict) -> str:
    """The display name a source definition gives, the install's own first; the id when none names one."""
    for definition in definitions.get(source_id, ()):
        for key in ("display_name", "name"):
            name = _text(definition.get(key))
            if name:
                return name
    source = _bundled(source_id)
    name = _text(getattr(source, "display_name", None)) if source is not None else ""
    return name or source_id


def _row_counts(conn) -> dict:
    """(source, table) -> rows, over the four shareable tables."""
    counts = {}
    for table in TABLES:
        if not _table_exists(conn, table) or "source_id" not in _columns(conn, table):
            continue
        for source_id, rows in conn.execute(f"SELECT source_id, COUNT(*) FROM {table} GROUP BY source_id"):
            if isinstance(source_id, str) and source_id.strip() and rows:
                counts[(source_id, table)] = int(rows)
    return counts


def _matches(scope: dict, wanted: dict) -> bool:
    """An install's scope under one requested scope: the owner, the Topos (``topos_id`` or its other spelling
    ``app_id``) and the dataset exactly, any device (``install_service.list_installs_any_device``'s rule)."""
    topos = _text(scope.get("topos_id")) or _text(scope.get("app_id"))
    return (_text(scope.get("user_id")) == wanted["user_id"] and topos == wanted["topos_id"]
            and _text(scope.get("dataset_id")) == wanted["dataset_id"])


def _installs(conn, scopes: list) -> tuple[set, dict]:
    """(the (source, table) pairs active installs under ``scopes`` feed, source -> those installs' definitions)."""
    if not scopes or not _table_exists(conn, "source_runtime_installs"):
        return set(), {}
    columns = _columns(conn, "source_runtime_installs")
    if not {"source_id", "scope_key", "is_active", "source_definition_json"} <= columns:
        return set(), {}
    status = "status" if "status" in columns else "NULL"
    pairs, definitions = set(), {}
    for source_id, scope_key, is_active, state, definition_json in conn.execute(
            f"SELECT source_id, scope_key, is_active, {status}, source_definition_json FROM source_runtime_installs"):
        source_id = _text(source_id)
        scope = _object(scope_key)
        if not source_id or scope is None or is_active != 1 or (state is not None and state not in INSTALL_LIVE):
            continue
        if not any(_matches(scope, wanted) for wanted in scopes):
            continue
        definition = _object(definition_json) or {}
        table = GROUP_TABLES.get(_text(definition.get("canonical_group_id"))) or _bundled_table(source_id)
        if table is None:
            continue
        pairs.add((source_id, table))
        definitions.setdefault(source_id, []).append(definition)
    return pairs, definitions


def catalog(database: Path, scopes: list, *, now: int, env=None) -> dict:
    """The catalog reply for the database this node serves. ``scopes`` are already checked (the handler's job)."""
    conn = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("BEGIN")
        counts = _row_counts(conn)
        installed, definitions = _installs(conn, scopes)
    finally:
        conn.close()
    sources = []
    for source_id, table in sorted(set(counts) | installed, key=lambda pair: (pair[0], TABLES.index(pair[1]))):
        sources.append({"source_id": source_id, "label": _label(source_id, definitions), "table": table,
                        "installed": source_id in definitions, "rows": counts.get((source_id, table), 0)})
    return {"version": VERSION, "as_of": int(now), "kinds": released_kinds(env), "sources": sources}
