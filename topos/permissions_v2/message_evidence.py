"""Independent, owner-reviewed whole messages. No inferred fact is required.

Classification is explicit owner review, not a model's release authority. Native
origin, current exclusions and Off-limits are independently rechecked. Reviews
share the enrolled evidence store's rollback pin, clock and publication gate;
the private review key has a separate namespace and creates no canonical fact.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field

from .canonical import PolicyError, canonical_bytes, digest, parse_json
from .contract import Hash, Identifier, Number, StrictModel
from .evidence import (EvidenceBinding, EvidenceIdentity, EvidenceResolver, EvidenceRevision,
    EvidenceReviewStore, _key, _owner, _row_revision, _json, _source_posture)
from .identity import restriction_subjects
from .message_review_contract import MessageSnapshot, MessageClassification, OwnerMessageReview
from topos.disclosure.content_policy import is_record_nsfw
from topos.features.provenance.roles import record_role
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lineage_keys

MESSAGE_REVIEW = "topos-owner-message-review/v1"
MESSAGE_CONTRACT = "owner_authored_message_v1"
MESSAGE_KEY_PREFIX = "message-review:"
DOMAINS = frozenset({"work", "plans", "hobbies", "health", "family", "finance", "relationships", "home"})


def message_key(identity: EvidenceIdentity) -> str:
    return MESSAGE_KEY_PREFIX + digest(identity.model_dump())


class QualifiedMessage(StrictModel):
    family: Literal["owner_authored_message/v1"]
    snapshot: MessageSnapshot
    review_id: Identifier
    review_revision: Hash
    classifications: list[MessageClassification]
    subject_contract: Literal["owner_authored_message_v1"]
    execution_enabled: Literal[False]


def parse_review(raw):
    from .evidence import OwnerEvidenceReview
    from .automatic_message_review import MachineMessageReview, VERSION
    value = parse_json(raw) if isinstance(raw, (str, bytes)) else raw
    version = value.get("version") if isinstance(value, dict) else None
    model = {MESSAGE_REVIEW: OwnerMessageReview, VERSION: MachineMessageReview}.get(version, OwnerEvidenceReview)
    return model.parse(value)


def _source_checks(resolver, conn, identity, row):
    if identity.table not in ("conversation_messages", "ai_chat_messages"):
        raise PolicyError("unsupported_message_table")
    # A writable canonical role/owner column never substitutes for live origin.
    if not resolver._validate_native_origin(conn, identity, row):
        raise PolicyError("native_owner_provenance_unavailable")
    if identity.table == "conversation_messages":
        if type(row.get("is_from_self")) is not int or row["is_from_self"] != 1:
            raise PolicyError("not_owner_authored")
    elif not resolver._ai_chat_owner_proven(conn, identity, row):
        raise PolicyError("not_owner_authored")
    posture, _ = _source_posture(conn, identity)
    if record_role(row, table=identity.table, posture=posture) != "authored":
        raise PolicyError("not_owner_authored")
    metadata = _json(row["metadata_json"], dict) if row.get("metadata_json") not in (None, "") else {}
    if any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in
           ("is_forwarded", "forwarded_from", "quoted_message", "quoted_text", "quote", "quoted_message_id", "quoted_sender", "is_quoted",
            "quoteText", "quoteBody", "quoteAuthor", "quoteAuthorAci", "quoteAuthorUuid", "quoteId", "quotedMessageId",
            "storyReplyContext", "associated_message_guid", "associated_message_type")):
        raise PolicyError("not_original_message")
    content = row.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 100_000 or is_record_nsfw(row):
        raise PolicyError("unsupported_message_content")
    if resolver._known_copies(conn, identity, row):
        raise PolicyError("independent_copy_lineage")


def snapshot_message(resolver, conn, floor, identity):
    if identity.binding != resolver.binding or identity.table == "signal_objects":
        raise PolicyError("evidence_owner_binding")
    row = resolver._load(conn, identity)
    _source_checks(resolver, conn, identity, row)
    reference = EvidenceRevision(identity=identity, revision=_row_revision(row, table=identity.table))
    # Labels concern this exact message. Current Off-limits and sibling-fact
    # restrictions are vetoes rechecked on every qualification, not claims that
    # the owner must re-review when an unrelated protected identity changes.
    from .protection_clock import clock_state
    protected = list(conn.execute("SELECT canonical_table,record_id FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                                  (identity.table, identity.record_id)))
    excluded = list(conn.execute("SELECT artifact_type,artifact_key FROM intelligence_exclusions WHERE artifact_type='record' AND artifact_key=?",
                                 (identity.record_id,)))
    # The human absence assessment is made against the owner's current
    # Off-limits list. Adding/changing that list requires a fresh assessment;
    # unrelated record restrictions do not. A nonempty list itself is no veto.
    protected_scope = [list(r) for r in conn.execute("SELECT blackhole_id,entity_id,normalized_name,canonical_name,aliases_json FROM entity_blackholes ORDER BY blackhole_id")]
    protection = digest({"clock_id": clock_state(conn)[0], "protected": [list(r) for r in protected],
                         "excluded": [list(r) for r in excluded], "protected_scope": protected_scope})
    return MessageSnapshot(binding=resolver.binding, canonical_file_revision=resolver._file_revision(),
        message=reference, protection_revision=protection), {_key(identity): row}


def facts_naming(conn, leaves: dict):
    """(object_id, payload_json, source_refs_json) of every fact whose references name a leaf, in rowid order.

    `EvidenceResolver._names_a_leaf` decides, as it always has. With the migration-78 keys
    installed it decides only over their candidates, a superset of the facts it matches
    (the sibling floor reads the same set), instead of over every fact on the node.
    Rowid order is the order the table walk below reads, so a caller that stops at the
    first fact that refuses stops at the same one. Without the keys, the walk runs.
    """
    if lineage_keys.installed(conn):
        candidates = conn.execute(
            "SELECT rowid,object_id,payload_json,source_refs_json FROM signal_objects WHERE object_type='fact' "
            f"AND object_id IN ({lineage_keys.SIBLING_CANDIDATES.format(marks=','.join('?' * len(leaves)), ranges=' OR '.join(['(key>=? AND key<?)'] * len(leaves)))})",
            lineage_keys.sibling_arguments(leaves)).fetchall()
        facts = [(row[1], row[2], row[3]) for row in sorted(candidates, key=lambda row: row[0])]
    else:
        facts = conn.execute("SELECT object_id,payload_json,source_refs_json FROM signal_objects WHERE object_type='fact'")
    return (fact for fact in facts if EvidenceResolver._names_a_leaf(fact[2], leaves))


def _floors(resolver, conn, snapshot, rows, opted_out):
    from .exclusion_floor import exclusions, fact_excluded
    identity = snapshot.message.identity
    if message_key(identity) in opted_out:
        raise PolicyError("owner_opted_out")
    tombstones = exclusions(conn)
    if tombstones["entity"]:
        raise PolicyError("entity_exclusion_lineage_unavailable")
    if identity.record_id in tombstones["record"]:
        raise PolicyError("intelligence_excluded")
    if conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",
                    (identity.table, identity.record_id)).fetchone():
        raise PolicyError("owner_only")
    resolver.entity_boundary(conn).check(table=identity.table, record_id=identity.record_id,
        source_id=identity.source_id, dataset_id=identity.dataset_id, row=rows[_key(identity)])
    resolver._source_sibling_floor(conn, snapshot, opted_out=opted_out)
    # A fact tombstone or protected fact also restricts its backing text, even
    # though a direct message does not need any *qualifying* fact.
    for fact in facts_naming(conn, {identity.record_id: {identity.table}}):
        fact_identity = resolver._identity("signal_objects", fact[0])
        fact_row = resolver._load(conn, fact_identity)
        resolver.entity_boundary(conn).check(table="signal_objects", record_id=fact[0],
            source_id=None, dataset_id=None, row=fact_row)
        if fact_excluded(_json(fact[1], dict), tombstones["fact"], restriction_subjects(conn)):
            raise PolicyError("intelligence_excluded")
        if fact[0] in tombstones["record"] or conn.execute(
            "SELECT 1 FROM owner_only_records WHERE canonical_table='signal_objects' AND record_id=? LIMIT 1", (fact[0],)).fetchone():
            raise PolicyError("owner_only")


def qualify_message(resolver, conn, floor, identity, reviews, review_db):
    snapshot, rows = snapshot_message(resolver, conn, floor, identity)
    _floors(resolver, conn, snapshot, rows, reviews._opt_outs_in(review_db))
    review = reviews._current_in(review_db, message_key(identity))
    if not isinstance(review, OwnerMessageReview):
        raise PolicyError("message_review_required")
    if review.owner_id != resolver.binding.owner_id or review.snapshot != snapshot:
        raise PolicyError("review_stale")
    return _qualified_classification(snapshot, rows, review.classifications[0], review.review_id,
                                    digest(review.model_dump()))


def _qualified_classification(snapshot, rows, item, review_id, review_revision):
    identity = snapshot.message.identity
    if item.evidence != snapshot.message:
        raise PolicyError("review_stale")
    if not item.domains or len(set(item.domains)) != len(item.domains) or not set(item.domains) <= DOMAINS or item.sensitivity == "unknown":
        raise PolicyError("classification_unknown_or_mixed")
    if item.authorship != "owner_authored" or item.speech != "original_message":
        raise PolicyError("not_original_message")
    if item.protected_content != "none":
        raise PolicyError("protected_content_unresolved")
    if item.independent_copies != "none_known":
        raise PolicyError("independent_copy_lineage")
    # Preserve any previously established whole-message ceiling. An explicit
    # review adds coverage for unlabeled native rows but cannot lower a ceiling.
    row = rows[_key(identity)]
    if row.get('_p2b_native_classification') is not None:
        from .reconciliation_facts import validated_classification
        ceiling = validated_classification(_json(row['_p2b_native_classification'], dict))
        ranks = {'none': 0, 'personal': 1, 'special': 2, 'unknown': 3}
        item = item.model_copy(update={'domains': sorted(set(item.domains) | set(ceiling['domains'])),
            'sensitivity': max((item.sensitivity, ceiling['sensitivity']), key=ranks.__getitem__)})
    return QualifiedMessage(family="owner_authored_message/v1", snapshot=snapshot, review_id=review_id,
        review_revision=review_revision, classifications=[item],
        subject_contract=MESSAGE_CONTRACT, execution_enabled=False), rows


def qualify_automatic_message(resolver, conn, floor, identity, reviews, review_db):
    """New-capability input only. An existing explicit correction takes precedence.

    The old qualify_message function remains human-review-only. All callers of
    this helper must still evaluate the signed grant and recheck at final read.
    """
    from .automatic_message_review import (MachineMessageReview, machine_key, context_for,
        is_current, apply_floors)
    snapshot, rows = snapshot_message(resolver, conn, floor, identity)
    _floors(resolver, conn, snapshot, rows, reviews._opt_outs_in(review_db))
    correction = reviews._current_in(review_db, message_key(identity))
    if isinstance(correction, OwnerMessageReview):
        # A stale correction must not silently disappear behind a machine label.
        return qualify_message(resolver, conn, floor, identity, reviews, review_db)
    review = reviews._current_in(review_db, machine_key(identity))
    if not isinstance(review, MachineMessageReview):
        raise PolicyError("machine_review_required")
    row = rows[_key(identity)]
    context_revision, context = context_for(conn, identity, row, boundary=resolver.entity_boundary(conn))
    if not is_current(review, {"snapshot":snapshot, "context_revision":context_revision,
                               "owner_review_revision":None}):
        raise PolicyError("review_stale")
    item = apply_floors(review.classifications[0], {"target":row['content'], **context})
    return _qualified_classification(snapshot, rows, item, review.review_id, digest(review.model_dump()))


def _preview_labels(resolver, conn, reviews, db, snapshot, rows):
    from .automatic_message_review import MachineMessageReview, machine_key, context_for, MODEL_REVISION, rubric_revision
    identity = snapshot.message.identity
    review = reviews._current_in(db, message_key(identity))
    labels, origin = None, "pending"
    if isinstance(review, OwnerMessageReview) and review.snapshot == snapshot:
        labels, origin = review.classifications[0], "owner"
    elif review is None:
        machine = reviews._current_in(db, machine_key(identity))
        if (isinstance(machine, MachineMessageReview) and machine.snapshot == snapshot
            and machine.owner_review_revision is None and machine.model_revision == MODEL_REVISION
            and machine.rubric_revision == rubric_revision()
            and machine.context_revision == context_for(conn, identity, rows[_key(identity)], boundary=resolver.entity_boundary(conn))[0]):
            labels, origin = machine.classifications[0], "automatic"
    return {"current_review_revision": digest(review.model_dump()) if review else None,
            "classification": labels.model_dump() if labels else None, "classification_origin": origin,
            "opted_out": message_key(identity) in reviews._opt_outs_in(db)}


def preview_message(resolver, reviews, identity):
    _owner(resolver.binding)
    with resolver._read() as (conn, floor):
        reviews._observe_clock(conn)
        snapshot, rows = snapshot_message(resolver, conn, floor, identity)
        with reviews._db() as db:
            _floors(resolver, conn, snapshot, rows, reviews._opt_outs_in(db) - {message_key(identity)})
            labels = _preview_labels(resolver, conn, reviews, db, snapshot, rows)
        return {"snapshot": snapshot.model_dump(), "content": rows[_key(identity)]["content"], **labels}


def record_message_review(resolver, reviews, *, review_id, expected_snapshot, classification,
                          expected_current_review_revision, reviewed_at):
    _owner(resolver.binding)
    if reviews.binding != resolver.binding or reviews.canonical_file_revision != resolver._file_revision():
        raise PolicyError("review_database_binding")
    expected_snapshot = MessageSnapshot.parse(expected_snapshot)
    classification = MessageClassification.parse(classification)
    with resolver._read() as (conn, floor):
        reviews._observe_clock(conn)
        current, rows = snapshot_message(resolver, conn, floor, expected_snapshot.message.identity)
        if current != expected_snapshot or classification.evidence != current.message:
            raise PolicyError("review_stale")
        key = message_key(current.message.identity)
        review = OwnerMessageReview(version=MESSAGE_REVIEW, review_id=review_id, owner_id=resolver.binding.owner_id,
            reviewed_at=reviewed_at, rubric="whole-message-owner-review/v1", snapshot=current, classifications=[classification])
        with reviews._db() as db:
            prior = reviews._current_in(db, key)
            if (digest(prior.model_dump()) if prior else None) != expected_current_review_revision:
                raise PolicyError("review_conflict")
            if db.execute("SELECT 1 FROM fact_reviews WHERE review_id=?", (review_id,)).fetchone():
                raise PolicyError("review_id_conflict")
            db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=? AND active=1", (key,))
            db.execute("INSERT INTO fact_reviews VALUES(?,?,?,1)",
                       (review_id, key, canonical_bytes(review.model_dump()).decode("ascii")))
        return review


def queue_messages(resolver, reviews, request, *, now):
    from .message_review_contract import MessageReviewPage
    from .fact_eligibility import canonical_utc_microseconds
    _owner(resolver.binding)
    if request.before > now or request.before <= request.after or request.before - request.after > 31 * 86400:
        raise PolicyError("message_review_window_invalid")
    records, scanned = [], 0
    with resolver._read() as (conn, floor):
        reviews._observe_clock(conn)
        identities = []
        # Only enrolled records are candidates. No arbitrary metadata or role
        # flag can put an unproven imported message in the review queue.
        rows = conn.execute("SELECT m.message_id,m.source_id,m.dataset_id,m.event_at FROM conversation_messages m "
                            "JOIN ingest_provenance_records p ON p.message_id=m.message_id ORDER BY m.event_at DESC LIMIT 201").fetchall()
        with reviews._db() as db:
            opted_out = reviews._opt_outs_in(db)
            for row in rows[:200]:
                scanned += 1
                event = canonical_utc_microseconds(row[3])
                if event is None or not request.after * 1000000 <= event <= request.before * 1000000:
                    continue
                identity = resolver._identity("conversation_messages", row[0], row[1], row[2])
                try:
                    snapshot, loaded = snapshot_message(resolver, conn, floor, identity)
                    _floors(resolver, conn, snapshot, loaded, opted_out - {message_key(identity)})
                except PolicyError:
                    continue
                labels = _preview_labels(resolver, conn, reviews, db, snapshot, loaded)
                records.append({"snapshot": snapshot.model_dump(), "content": loaded[_key(identity)]["content"], **labels})
                if len(records) == request.limit:
                    break
    return MessageReviewPage(records=records, scanned=scanned, truncated=scanned < len(rows))
