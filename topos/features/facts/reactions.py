"""Rows whose text quotes a message someone else sent.

An iMessage reaction ("tapback") is a row authored by whoever reacted, but its
text reproduces the message it reacts to, for example 'Loved “I work at X”'.
When the owner reacts to a correspondent, that text is the correspondent's, not
the owner's. Native readers mark such a row with ``associated_message_guid``
and a non-zero ``associated_message_type`` (0 is an ordinary message). Neither
fact extractor may read a first-person statement out of one.

Permission evidence refuses the same keys independently
(``permissions_v2/evidence.py``); this module decides only extraction.

A row can also arrive with no metadata at all (a lane that hands on text and
sender only, an import that kept none). Messages writes a reaction's text in a
fixed form, so ``reads_as_reaction`` recognises it from the words, and
``is_reaction`` takes either sign.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict

REACTION_METADATA_KEYS = ("associated_message_guid", "associated_message_type")


def _metadata(row: Dict[str, Any]) -> list:
    found = []
    for key in ("metadata_json", "_metadata", "metadata"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                # Unreadable metadata cannot show the row is ordinary.
                return [None]
        if isinstance(value, dict):
            found.append(value)
    return found


def quotes_another_message(row: Dict[str, Any]) -> bool:
    """True when a row carries reaction metadata, or metadata too malformed to rule it out."""
    for metadata in _metadata(row):
        if metadata is None:
            return True
        if any(metadata.get(key) not in (None, False, 0, "0", "", [], {}) for key in REACTION_METADATA_KEYS):
            return True
    return False


#: The text Messages writes for a reaction, English forms: the six tapbacks, the emoji and
#: sticker form, and the removal of either, each followed by the whole message reacted to
#: in curly quotes and nothing after it. Capitalised as Messages writes it, and curly
#: quotes only: a sentence the owner typed that merely opens with "Loved" or "Liked", or
#: that goes on after the quotation, is not one. The optional two characters in front are
#: the length byte older decodes left glued to a body (`derivation/template.py` strips the
#: same prefix before a prompt).
_REACTION_TEXT = re.compile(
    r"\A\s*(?:\+\S)?(?:"
    r"(?:Liked|Loved|Disliked|Emphasized|Questioned|Laughed at)"
    r"|Reacted \S.{0,40}? to"
    r"|Removed \S.{0,40}? from"
    r") “.*”\s*\Z",
    re.DOTALL,
)


def reads_as_reaction(text: Any) -> bool:
    """True when the text is, in full, the form Messages writes for a reaction."""
    return bool(_REACTION_TEXT.match(str(text or "")))


def is_reaction(row: Dict[str, Any], text: Any) -> bool:
    """Whether a message row is a reaction: its metadata says so, or its words do.

    Either is enough. The metadata is the native marker and covers every form and language;
    the words cover a row that carries none. A row the reader recorded as ordinary is still
    refused when its whole text is a reaction's: what a wrong refusal costs is one short
    message that holds nothing but a quotation, and what a wrong read costs is another
    person's words filed as the reactor's."""
    return quotes_another_message(row) or reads_as_reaction(text)


def stored_metadata_column(conn: Any, table: str = "conversation_messages") -> str:
    """``metadata_json`` when the table has that column, else ``NULL``: a SELECT that asks
    for a missing column fails whole, and a reader would then lose every row with it."""
    try:
        columns = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        columns = set()
    return "metadata_json" if "metadata_json" in columns else "NULL"
