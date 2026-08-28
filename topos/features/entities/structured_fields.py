"""Mentions from DECLARED structured columns, not just extracted prose.

NER runs over a record's ``content``. A journal entry's place lives in
``place_name``, a declared column — so the record that names the place never got
a mention for it, and the fan-out child minted from that same column got one
instead.

Measured on the live node 2026-08-27: **333 of 362 ``journal_entries`` rows carry
a non-empty ``place_name`` and have no place mention of their own**, while the
children hold 178 place mentions against the parents' 648 across all sources. 51
of the 69 distinct place names already resolve to a place entity, so the
resolution mostly exists — it is attached to the wrong row.

Why this and not a wider black-hole rule. Protecting a place should not hide a
whole day's journal entry because a *sibling row* named it — that was decided
against. But these records are not siblings of the evidence: they contain it, in
their own column. Attributing the mention to the record whose content carries it
means a protected place blocks the parent **because the parent names it**, and a
record that never names the entity is still never blocked. Strictly narrower than
unit-scoping, and it closes the same leak.

The same correction fixes the graph: co-occurrence groups by record, so a person
extracted from the entry's prose and the place named in its column now land in
one bucket instead of two.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("topos.features.entities.structured_fields")

#: ``canonical_table -> ((column, spine entity_type), ...)``
#:
#: A column belongs here when its value IS an entity rather than prose that
#: happens to contain one — the whole cell is the name, so there is nothing for
#: NER to find and nothing to be uncertain about. Free-text columns must not be
#: added: they need extraction, and asserting the whole cell as one entity would
#: mint garbage.
STRUCTURED_ENTITY_FIELDS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "journal_entries": (("place_name", "place"), ("people", "person")),
    "location_events": (("place_name", "place"),),
    "calendar_events": (("location", "place"),),
}

#: ``(table, column)`` whose cell holds a LIST of entities rather than one.
#:
#: ``journal_entries.people`` is the owner's declared participant list, and it is
#: the reason this concept exists: the cell reads "Rowan", or "Rowan, Nadia", or
#: "Rowan, Nadia, Suri". Resolving it whole would mint a person named
#: "Rowan, Nadia" — the exact garbage the rule above warns against — so the list
#: is split, and each name resolves on its own.
#:
#: Measured on the live node 2026-08-28, before this: 13 ``journal_entries`` rows
#: declare one participant by name and the entity spine held **2** mentions for
#: them, both from prose. The declared column is the higher-confidence witness of
#: the two and it was the one being ignored.
MULTI_VALUE_FIELDS: frozenset = frozenset({("journal_entries", "people")})

#: Structured values are the record's own declared data, not a model's guess, so
#: they do not carry NER's uncertainty. Kept explicit so a reader can tell a
#: declared mention from an extracted one in the table.
STRUCTURED_CONFIDENCE = 1.0


def structured_fields_for(table: Optional[str]) -> Tuple[Tuple[str, str], ...]:
    return STRUCTURED_ENTITY_FIELDS.get(str(table or ""), ())


def surfaces_for(table: str, column: str, raw: Any) -> List[str]:
    """The entity surfaces a declared cell holds — one, or a list.

    The participant parser is IMPORTED, not reimplemented: the artifact lane
    already reads this same column, and the "Solo"/"Group" sentinels that name
    nobody are encoded there. A second copy of that rule is how one lane starts
    minting a person called "Solo" while the other does not.
    """
    if (table, column) in MULTI_VALUE_FIELDS:
        from ..signal.extraction.rule_extractors import parse_participant_names

        return parse_participant_names(raw)
    surface = str(raw or "").strip()
    return [surface] if surface else []


def record_structured_mentions(
    conn: sqlite3.Connection,
    resolver: Any,
    canonical_messages: Iterable[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Record a mention for every declared structured field that holds a value.

    Returns ``{record_id: [entity_id, ...]}`` so the caller can fold these into
    the same co-occurrence pass as the extracted mentions — which is the point:
    the person in the prose and the place in the column belong to one record.

    ``queue_review=False``: a declared column is not a candidate sighting for a
    human to adjudicate, it is the record stating what it is.
    """
    from ...storage.db.migrations.entity_mentions_authored_v1 import authored_flag_for_row

    by_record: Dict[str, List[str]] = {}
    for msg in canonical_messages:
        if not isinstance(msg, dict):
            continue
        table = str(msg.get("_table") or msg.get("canonical_table") or "")
        fields = structured_fields_for(table)
        if not fields:
            continue
        record_id = str(
            msg.get("record_id")
            or msg.get("message_id")
            or msg.get("entry_id")
            or msg.get("event_id")
            or ""
        ).strip()
        if not record_id:
            continue
        for column, entity_type in fields:
            for surface in surfaces_for(table, column, msg.get(column)):
                try:
                    entity_id, _tier = resolver.resolve(
                        surface,
                        entity_type=entity_type,
                        record_id=record_id,
                        queue_review=False,
                    )
                except ValueError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "structured mention skipped for %s.%s: %s", table, column, exc
                    )
                    continue
                if not entity_id:
                    continue
                try:
                    authored = authored_flag_for_row(msg, table=table)
                except Exception:  # noqa: BLE001
                    authored = None
                resolver.record_mention(
                    entity_id,
                    record_id=record_id,
                    surface_text=surface,
                    source_id=msg.get("source_id"),
                    canonical_table=table,
                    confidence=STRUCTURED_CONFIDENCE,
                    event_at=msg.get("event_at"),
                    authored_by_owner=authored,
                )
                # `record_mention` is INSERT OR IGNORE, so where NER already found this
                # name in the prose the row keeps the MODEL's confidence and the fact that
                # the owner declared it is lost. The column is the stronger witness of the
                # two and the stored row has to say so — anything reading for declared
                # evidence would otherwise skip exactly the records that have both.
                #
                # Measured on the live journal: 3 of the 10 entries that name Rowan as a
                # participant AND carry category='Topos' were stuck at NER's 0.99, so a
                # count of declared sessions read 7 where the owner had logged 10.
                try:
                    conn.execute(
                        "UPDATE entity_mentions SET confidence=?"
                        " WHERE entity_id=? AND record_id=? AND COALESCE(confidence,0)<?",
                        (STRUCTURED_CONFIDENCE, entity_id, record_id, STRUCTURED_CONFIDENCE),
                    )
                except sqlite3.Error as exc:
                    logger.debug("declared-confidence upgrade skipped for %s: %s", record_id, exc)
                by_record.setdefault(record_id, []).append(entity_id)
    return by_record
