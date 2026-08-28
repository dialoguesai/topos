"""Deterministic ids for derived rows, so re-deriving a record REPLACES its rows.

The derived-table writers minted ``uuid4()`` whenever the incoming record dict
carried no id — and the enrichment jobs build fresh dicts on every pass, so a
record's id was different on every run. The write is ``INSERT OR REPLACE`` keyed
on that id, so a new id never conflicts and the row is INSERTED beside the old
one. Nothing deletes a record's prior rows first. Measured 2026-08-28 with the
writer in isolation: the same 5 records written three times produced 5, 10 and
then 15 rows over 5 distinct ``record_id``.

That is why a re-sync multiplied derived rows 2x-4.3x, and it is what makes a
``spec_version`` bump dangerous rather than routine: the bump exists to mark
every record stale so it re-derives, which under the old id is an instruction to
duplicate the table.

The fix is to key a derived row on what the row IS, not on when it was written.
``derived_row_id`` is a UUID5 over the row's identity fields, so the same entity
in the same record resolves to the same id forever, and the second write of it
is the REPLACE the statement always claimed to be.

**Every field is a fallback CHAIN, and that is not decoration.** The five writers
are fed by five jobs that do not agree on field names: the entities job says
``record_id`` and ``entity_text``, the emotions job says ``message_id`` and
``emotion_label``, the sentiment job may say ``label`` or ``sentiment``. A first
draft of this module declared ``("record_id", "label")`` for
``message_emotions``; both resolve to ``None`` on a real emotions record, so
every emotion row in the database would have collapsed onto ONE id and the table
would have ended up holding a single row. Caught before it ran, by a test that
writes what each job actually emits rather than what this module wished it did.

That near-miss is why the failure mode is what it is: **a missing required field
returns None and the caller keeps its per-run uuid.** Duplicate rows are a bug
worth fixing; silently merging unrelated rows is data loss no later pass can
undo, so the code refuses to guess. ``entity_type`` is the one field allowed to
be absent, because the NER type mapper legitimately returns None and an untyped
mention is still that mention.

Values are compared exactly, not case-folded: "Apple" and "apple" may be one
company and one fruit. Two rows differing only in case stay two rows, which is
the answer the extractor gave.

``migrations/derived_row_identity_v1`` re-keys existing rows through this same
function, which is why it lives here rather than in the writer — a migration
that computed the id its own way would collapse a different set of rows than the
writer goes on to produce, and the duplication would silently resume.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

#: Fixed namespace for derived-row ids. Never change it: every id in every
#: node's database is derived from it, and a new namespace would re-key the
#: whole derived layer and duplicate it once more.
DERIVED_ROW_NAMESPACE = uuid.UUID("6f1d4c4e-6a1f-5b2e-9c3a-1d5e7f9b2c40")

#: Unit separator. Cannot appear in extracted text, so it cannot forge a
#: collision between ("a\x1fb", "") and ("a", "b").
_SEP = "\x1f"

#: Sentinel for a field that is legitimately absent, distinct from "".
_NULL = "\x00"


class _Field(tuple):
    """One identity field: the names a job might emit it under, and whether it may be absent."""

    __slots__ = ()

    def __new__(cls, names: Tuple[str, ...], *, required: bool = True):
        return super().__new__(cls, (names, required))

    @property
    def names(self) -> Tuple[str, ...]:
        return self[0]

    @property
    def required(self) -> bool:
        return self[1]


#: The identity of a row per derived table: the fields that make two rows the
#: same fact rather than two facts. Each entry lists every key the producing job
#: is known to use, in preference order — see the module docstring for what
#: happens when that list is wrong.
IDENTITY_FIELDS: Dict[str, Tuple[_Field, ...]] = {
    "message_entities": (
        _Field(("record_id", "message_id")),
        _Field(("entity_text", "text")),
        # The NER type mapper returns None for types it does not cover; an
        # untyped mention is still that mention, so this one may be absent.
        _Field(("entity_type",), required=False),
    ),
    "user_goals": (
        _Field(("record_id", "message_id")),
        _Field(("goal_text", "text")),
    ),
    "message_topics": (
        _Field(("record_id", "message_id")),
        _Field(("topic", "label")),
    ),
    "message_sentiment": (
        _Field(("record_id", "message_id")),
        _Field(("label", "sentiment")),
    ),
    "message_emotions": (
        _Field(("record_id", "message_id")),
        _Field(("emotion_label", "label")),
    ),
}


def _resolve(row: Dict[str, Any], field: _Field) -> Tuple[Any, bool]:
    """This field's value from ``row``, and whether it was found at all.

    Empty string and whitespace count as absent: a row keyed on "" is a row
    keyed on nothing, and that is how a whole table collapses onto one id.
    """
    for name in field.names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text, True
    return None, False


def derived_row_id(table: str, identity: Sequence[Any]) -> str:
    """The stable id for a derived row of ``table`` with these identity values."""
    parts = [str(table or "")]
    for value in identity:
        parts.append(_NULL if value is None else str(value))
    return str(uuid.uuid5(DERIVED_ROW_NAMESPACE, _SEP.join(parts)))


def identity_from_row(table: str, row: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    """Identity values for ``row``, or None when this row cannot be identified.

    None on: a table with no declared identity, or any REQUIRED field missing.
    The caller then keeps a per-run uuid — a duplicate row, which is recoverable,
    rather than a merge, which is not.

    Used by both the writer (over an in-flight record dict) and the migration
    (over a database row), which is what keeps the two in agreement.
    """
    fields = IDENTITY_FIELDS.get(str(table or ""))
    if not fields:
        return None
    values = []
    for field in fields:
        value, found = _resolve(row, field)
        if not found and field.required:
            return None
        values.append(value)
    return tuple(values)


def derived_row_id_for(table: str, row: Dict[str, Any]) -> Optional[str]:
    """``derived_row_id`` resolved straight from a row/record dict, or None."""
    identity = identity_from_row(table, row)
    if identity is None:
        return None
    return derived_row_id(table, identity)


def identity_tables() -> Iterable[str]:
    """Tables with a declared row identity, in a stable order."""
    return tuple(IDENTITY_FIELDS.keys())
