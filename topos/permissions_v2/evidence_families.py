"""Evidence families: the canonical tables whose rows can be evidence for a grant (IF-5 §1).

The evidence layer grew up knowing two leaf tables, the conversation and AI-chat messages, and spelled
them out wherever it touched a row: which column is the id, which is the time, how to read the time,
which columns are operational, who proves the row is the owner's. This registry says it once per table,
so a new family is one entry plus its owner proof rather than a sweep through every reader.

It grants nothing. A family's rows still pass every check a message passes (Off-limits, owner-only,
exclusions, NSFW, copies, consent, the signed policy's tables and sources, the window), plus the
family's own proof that the owner wrote the row. A family behind a flag is invisible with the flag off:
its identities do not load, so every path that meets one withholds.

v1 families: `message`, `ai_message` (unchanged) and `journal_entry` (OD-50/OD-52). `interest` records
are derived, not evidence rows, and are not a family here (IF-5 §1.3).
"""
from __future__ import annotations

from dataclasses import dataclass
import os

from .canonical import PolicyError
from .evidence_time import CANONICAL, STATED_DAY, event_bounds, released_time, row_time_text

JOURNAL_FLAG = "TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES"


@dataclass(frozen=True)
class Family:
    name: str                 # the family, and the raw result kind it releases as
    table: str                # the evidence identity's table
    id_column: str
    time_column: str
    time_semantics: str       # evidence_time rule
    dataset_kind: str         # "row_dataset" (source + dataset) | "node_resource" (source only)
    kind: str                 # the raw result kind a member releases as
    flag: str | None = None   # an env flag that must be "true" for the family to exist

    def enabled(self, env=None) -> bool:
        if self.flag is None:
            return True
        return str((os.environ if env is None else env).get(self.flag, "")).strip().lower() in ("1", "true", "yes", "on")


FAMILIES = {
    "conversation_messages": Family("message", "conversation_messages", "message_id", "event_at", CANONICAL,
                                    "row_dataset", "message"),
    "ai_chat_messages": Family("ai_message", "ai_chat_messages", "message_id", "event_at", CANONICAL,
                               "node_resource", "message"),
    "journal_entries": Family("journal_entry", "journal_entries", "entry_id", "entry_at", STATED_DAY,
                              "node_resource", "journal_entry", flag=JOURNAL_FLAG),
}
MESSAGE_TABLES = ("conversation_messages", "ai_chat_messages")
EVIDENCE_TABLES = tuple(FAMILIES)


def family(table) -> Family:
    """The family of an evidence table; refuses anything else (a closed list, never caller SQL)."""
    found = FAMILIES.get(table) if isinstance(table, str) else None
    if found is None:
        raise PolicyError("unsupported_evidence_table")
    return found


def enabled_family(table, env=None) -> Family:
    """The family of an evidence table that exists on this node now; a disabled family withholds."""
    found = family(table)
    if not found.enabled(env):
        raise PolicyError("evidence_family_disabled")
    return found


def enabled_tables(env=None) -> tuple:
    return tuple(table for table, item in FAMILIES.items() if item.enabled(env))


def time_text(table: str, row: dict):
    """The text that says when a row of this family happened (a journal row's event-time record, when its door wrote one)."""
    item = family(table)
    if item.table == "journal_entries":
        return row_time_text(row, column=item.time_column)
    return row.get(item.time_column)


def within(table: str, row: dict, lower_us: int, upper_us: int) -> bool:
    """Whether every instant the row can have happened lies inside the window, under its family's rule."""
    item = family(table)
    bounds = event_bounds(time_text(table, row), semantics=item.time_semantics)
    return bounds is not None and lower_us <= bounds[0] and bounds[1] <= upper_us


def rank_time_us(table: str, row: dict) -> int | None:
    """The time an index ranks a row by: an instant when the row states one, else the start of its stated day.

    Never later than the row can have happened, and never finer than the row states.
    """
    item = family(table)
    text = time_text(table, row)
    bounds = event_bounds(text, semantics=item.time_semantics)
    if bounds is None:
        return None
    if bounds[0] == bounds[1]:
        return bounds[0]
    day = released_time(text, semantics=item.time_semantics, precision="day")
    return None if day is None else day * 1_000_000


def released(table: str, row: dict, precision: str) -> int | None:
    """The event time a grant may release for a row of this family, at the grant's precision."""
    item = family(table)
    return released_time(time_text(table, row), semantics=item.time_semantics, precision=precision)
