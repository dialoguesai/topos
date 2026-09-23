"""Owner facts from the rows one owner-attested snapshot job has just proved.

This is the only producer that writes fact references complete enough for the
p2b evidence chain ({table, record_id, source_id, dataset_id}). It is not a
widening of the shared extractors: ``extract._source_ref`` and every loader stay
as they are, so a legacy or forged row can never gain a dataset reference.

It runs inside the job's batch transaction (``context.batch``), after the
canonical rows and their provenance links are written and before the job is
finished, so rows, links, facts and completion commit or roll back as one unit.
It runs the rules floor only; the LLM pass never runs here.

Three things are decided here rather than trusted from elsewhere:

- The subject is the one entity that is both ``is_self`` and actively attested
  by the owner. With none, or more than one, nothing is extracted: the
  extractor's own choice of self entity (the one with the most facts, or a newly
  created one) is exactly what an attested contract refuses to accept.
- A value that is not one atomic label (``fact_contract.atomic_label_syntax``)
  is refused before it is written, so no unreleasable owner fact is minted.
- Evidence time: a row linked to this job, or to an earlier job that still
  validates, has a native source clock. That is the only trust handed to the
  store's supersession guard.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict

from ..features.facts.extract import extract_rules_facts
from ..features.facts.store import FactStore
from ..features.temporal.points import parse_point
from ..features.temporal.records import fact_temporal
from ..storage.db.write_gate import joined_transaction
from .fact_contract import atomic_label_syntax
from .identity import attested_subjects, self_entity_ids

ORIGIN_VERSION = "owner-attested-snapshot/v1"


def _native_point(row: Dict[str, Any]):
    point = parse_point(row.get("event_at"), provenance="native_source_clock")
    return point if point.precision == "instant" and point.basis == "utc" else None


class LinkedRowTrust:
    """Vouches for a row's event time only while its provenance link validates."""

    def __init__(self, service, context):
        self.service, self.context = service, context

    def trusted_event_point(self, conn, row):
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            return None
        origin = metadata.get("topos_owner_ingest") if type(metadata) is dict else None
        if type(origin) is not dict:
            return None
        if origin == {"version": ORIGIN_VERSION, "enrollment_id": self.context.enrollment_id, "job_id": self.context.job_id}:
            if not self.context.existing_record(conn, row["message_id"]):
                return None
        else:
            self.service.validate_record_origin(conn, message_id=row["message_id"], origin=origin)
        return _native_point(row)

    def existed_by(self, conn, row):
        """When the owner attested the snapshot the row was read from.

        The enrolled snapshot's exact bytes were pinned at that moment, so no
        message in it can have been sent later. That is earlier than the row's
        ``ingested_at`` (stamped when the job ran), so it catches a device clock
        running ahead that the ingestion time alone would let through.
        """
        found = conn.execute(
            "SELECT e.authorized_at FROM ingest_provenance_records r JOIN ingest_provenance_enrollments e "
            "ON e.enrollment_id = r.enrollment_id WHERE r.message_id=?", (row["message_id"],)).fetchone()
        if found is None or type(found[0]) is not int:
            return None
        moment = datetime.fromtimestamp(found[0], tz=timezone.utc).isoformat(timespec="microseconds")
        return parse_point(moment, provenance="native_source_clock")


def _attested_self(conn):
    try:
        subjects = attested_subjects(conn) & self_entity_ids(conn)
    except Exception:  # noqa: BLE001 — unreadable identity state is "unattested", never a guess
        return None
    return next(iter(subjects)) if len(subjects) == 1 else None


def _atomic(spec) -> bool:
    try:
        atomic_label_syntax(spec["object_value"])
    except (TypeError, ValueError):
        return False
    return True


def extract_snapshot_facts(conn, service, context) -> Dict[str, int]:
    """Assert owner facts from this job's linked rows, inside its batch. Returns counts only."""
    context.require_batch(conn)
    stats: Dict[str, int] = {}
    cursor = conn.execute(
        "SELECT m.* FROM conversation_messages m JOIN ingest_provenance_records r ON r.message_id = m.message_id "
        "WHERE r.enrollment_id=? AND r.enrollment_revision=? AND r.job_id=? AND m.dataset_id=? AND m.source_id=? "
        "ORDER BY m.event_at, m.message_id",
        (context.enrollment_id, context.enrollment_revision, context.job_id, context.dataset_id, context.source_id),
    )
    names = [column[0] for column in cursor.description]
    rows = [{**dict(zip(names, values)), "_table": "conversation_messages"} for values in cursor.fetchall()]
    stats["rows_linked"] = len(rows)
    for row in rows:
        # Raises on a changed identity; the batch then rolls back as a whole.
        if not context.existing_record(conn, row["message_id"]):
            raise ValueError("ingest_link_missing")
    subject = _attested_self(conn)
    if subject is None:
        stats["owner_subject_unattested"] = len(rows)
        return stats
    trust = LinkedRowTrust(service, context)
    store = FactStore(conn, evidence_trust=trust)
    with joined_transaction(conn):
        extract_rules_facts(
            conn, rows, store=store, subject_entity_id=subject,
            source_ref=lambda row: {"table": "conversation_messages", "record_id": str(row["message_id"]),
                                    "source_id": str(row["source_id"]), "dataset_id": str(row["dataset_id"])},
            temporal_for=lambda row, spec: fact_temporal(evidence=_native_point(row) or parse_point(
                row.get("event_at"), provenance="unverified_producer")),
            accept_value=_atomic, stats=stats,
        )
    for outcome, count in store.outcomes.items():
        stats[outcome] = count
    return stats
