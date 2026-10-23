"""The owner's inbound truth lane: its settings and its pause, persisted in ``engine_config``.

The lane (``topos/ingestion/truth_inbound.py``) checks new messages, transcripts and journal entries with
verify_claim v2 and keeps what it found for the owner to label. Its settings live where the node keeps its other
owner settings (``ui_config``, ``facts_llm_model``): one JSON value under the ``truth_inbound`` key, read per job, so
a change takes effect at the next batch with no restart. No new store and no migration.

- ``enabled`` (default ``False``): nothing is checked, queued or stored while it is off.
- ``sources``: a subset of ``imessage``, ``signal``, ``transcripts``, ``journal`` (default all four).
- ``model``: ``local`` (default; the node's pinned local model, nothing leaves the Mac) or ``hosted`` (the node's Red
  Pill key: it spends credits and sends the checked text, contacts' messages included, to a provider, so it is a
  deliberate choice).
- ``rate_per_minute`` (default 6, 1-60): the most model checks the lane starts in a minute.

The pause is a separate key, ``truth_inbound_paused_until`` (an ISO time in UTC, empty when not paused): a hosted 402
or an exhausted wallet pauses the lane for ten minutes, and it resumes only after a probe passes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

ENGINE_CONFIG_KEY = "truth_inbound"
PAUSE_KEY = "truth_inbound_paused_until"

SOURCES = ("imessage", "signal", "transcripts", "journal")
MODELS = ("local", "hosted")
DEFAULT_RATE_PER_MINUTE = 6
MAX_RATE_PER_MINUTE = 60
FIELDS = ("enabled", "sources", "model", "rate_per_minute")


class InvalidTruthSettings(ValueError):
    """A settings change the lane will not store. Carries no part of the input."""


def defaults() -> Dict[str, Any]:
    return {"enabled": False, "sources": list(SOURCES), "model": "local", "rate_per_minute": DEFAULT_RATE_PER_MINUTE}


def _valid_rate(value: Any) -> bool:
    return type(value) is int and 1 <= value <= MAX_RATE_PER_MINUTE


def _valid_sources(value: Any) -> bool:
    return (isinstance(value, list) and all(isinstance(s, str) and s in SOURCES for s in value)
            and len(set(value)) == len(value))


def normalize(stored: Any) -> Dict[str, Any]:
    """The full settings from what is stored. A key that is missing or malformed keeps its default, so a damaged
    value can never switch the lane on or widen it."""
    out = defaults()
    if not isinstance(stored, Mapping):
        return out
    if type(stored.get("enabled")) is bool:
        out["enabled"] = stored["enabled"]
    if _valid_sources(stored.get("sources")):
        out["sources"] = [s for s in SOURCES if s in stored["sources"]]
    if stored.get("model") in MODELS:
        out["model"] = stored["model"]
    if _valid_rate(stored.get("rate_per_minute")):
        out["rate_per_minute"] = stored["rate_per_minute"]
    return out


def validate_patch(patch: Any) -> Dict[str, Any]:
    """A ``set`` payload: any subset of the four fields, each exactly typed. Raises InvalidTruthSettings."""
    if not isinstance(patch, Mapping) or not patch or not set(patch) <= set(FIELDS):
        raise InvalidTruthSettings("invalid_truth_settings")
    out: Dict[str, Any] = {}
    if "enabled" in patch:
        if type(patch["enabled"]) is not bool:
            raise InvalidTruthSettings("invalid_truth_settings")
        out["enabled"] = patch["enabled"]
    if "sources" in patch:
        if not _valid_sources(patch["sources"]):
            raise InvalidTruthSettings("invalid_truth_settings")
        out["sources"] = [s for s in SOURCES if s in patch["sources"]]
    if "model" in patch:
        if patch["model"] not in MODELS:
            raise InvalidTruthSettings("invalid_truth_settings")
        out["model"] = patch["model"]
    if "rate_per_minute" in patch:
        if not _valid_rate(patch["rate_per_minute"]):
            raise InvalidTruthSettings("invalid_truth_settings")
        out["rate_per_minute"] = patch["rate_per_minute"]
    return out


def _read(conn: Optional[sqlite3.Connection], key: str) -> Optional[str]:
    """Read engine_config without creating it (a fresh database has no row and no table: the defaults hold)."""
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return str(row["value"])
    except (TypeError, IndexError, KeyError):
        return str(row[0])


def load(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    raw = _read(conn, ENGINE_CONFIG_KEY)
    try:
        return normalize(json.loads(raw) if raw else None)
    except ValueError:
        return defaults()


def save(conn: sqlite3.Connection, patch: Any) -> Dict[str, Any]:
    """Merge a validated change into the stored settings and return the result. The write takes the write gate."""
    from ..core.state import set_engine_config_value

    merged = {**load(conn), **validate_patch(patch)}
    set_engine_config_value(conn, ENGINE_CONFIG_KEY, json.dumps(merged, sort_keys=True))
    return normalize(merged)


def paused_until(conn: Optional[sqlite3.Connection]) -> Optional[datetime]:
    raw = (_read(conn, PAUSE_KEY) or "").strip()
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def set_paused_until(conn: sqlite3.Connection, when: Optional[datetime]) -> None:
    from ..core.state import set_engine_config_value

    set_engine_config_value(conn, PAUSE_KEY, when.astimezone(timezone.utc).isoformat() if when else "")


def public_view(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """What ``truth_inbound_settings`` answers: the four settings, plus ``paused_until`` (null unless a hosted lane is
    waiting out a 402) so the owner's screen can say why nothing is being checked."""
    settings = load(conn)
    until = paused_until(conn)
    active = until is not None and settings["model"] == "hosted" and until > datetime.now(timezone.utc)
    return {**settings, "paused_until": until.isoformat() if active else None}
