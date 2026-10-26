"""Participant-keyed latest text retrieval. Mentions are never identity evidence.

Owner-only caller: resolve an exact contact name/identifier, join the participant
roster using dataset AND source, and order the complete matching history before
capping. No writes, name inference, embedding rank or ingestion-clock fallback.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone


def latest_text_participant(query: str) -> str | None:
    if not re.search(r"\b(latest|most recent|newest|last)\b", query, re.I):
        return None
    if not re.search(r"\b(text messages?|texts|imessages?|sms)\b", query, re.I):
        return None
    match = re.search(r"\b(?:exchange|conversation|messages?|texts|imessages?|sms)\s+(?:with|from|to)\s+(.+?)(?=[!?;\n]|\.(?:\s|$)|,?\s+(?:and\s+)?(?:give|include|summarize)\b|$)", query, re.I)
    if not match:
        return None
    return match[1].strip().strip('"“”') or None


def _instant(raw):
    try:
        value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except (ValueError, TypeError):
        return None


def latest_exchange_rows(conn, participant, *, source_ids, dataset_id=None, limit=8):
    """Return (rows, receipt). Empty/ambiguous identity fails closed.

    Dataset is supplied by the selected Topos. Without one, only a single
    message dataset is accepted; joins never cross datasets. Recency describes stored dated messages, never unsynced history.
    """
    receipt = {"status": "identity_unresolved", "participant": participant,
               "identity_basis": "exact_contact_name_or_identifier",
               "ordering": "event_at_desc", "coverage": "stored_messages"}
    if conn is None or not source_ids:
        return [], receipt
    try:
        if dataset_id is None:
            datasets = [r[0] for r in conn.execute(
                f"SELECT DISTINCT dataset_id FROM conversation_messages WHERE source_id IN ({','.join('?' for _ in source_ids)})",
                source_ids)]
            if len(datasets) > 1:
                receipt['status'] = 'dataset_required'
                return [], receipt
            if datasets:
                dataset_id = datasets[0]
        if dataset_id is not None:
            receipt['dataset_id'] = dataset_id
        contacts = conn.execute(
            "SELECT contact_id, dataset_id, display_name FROM contacts WHERE coalesce(is_self,0)=0"
        ).fetchall()
        matches = {(str(r[0]), str(r[1])) for r in contacts
                   if str(r[2] or '').strip().casefold() == participant.casefold()
                   and (dataset_id is None or r[1] == dataset_id)}
        # Only exact identifiers, not phone suffixes, message body text or entity mentions.
        for r in conn.execute("SELECT contact_id, dataset_id, identifier FROM contact_identifiers"):
            if str(r[2] or '').strip().casefold() == participant.casefold() and (dataset_id is None or r[1] == dataset_id):
                matches.add((str(r[0]), str(r[1])))
        if len(matches) != 1:
            receipt['status'] = 'identity_ambiguous' if matches else 'identity_unresolved'
            return [], receipt
        contact_id, matched_dataset = next(iter(matches))
        receipt['dataset_id'] = matched_dataset
        cursor = conn.execute(
            "SELECT m.* FROM conversation_messages m WHERE m.dataset_id=? "
            f"AND m.source_id IN ({','.join('?' for _ in source_ids)}) "
            "AND EXISTS (SELECT 1 FROM conversation_participants p "
            "WHERE p.contact_id=? AND p.conversation_id=m.conversation_id "
            "AND p.dataset_id=m.dataset_id AND p.source_id=m.source_id)",
            [matched_dataset, *source_ids, contact_id],
        )
        columns = [d[0] for d in cursor.description]
        dated, undated = [], 0
        for values in cursor:
            row = dict(zip(columns, values))
            instant = _instant(row.get('event_at'))
            if instant is None:
                undated += 1
            else:
                dated.append((instant, str(row['message_id']), row))
        dated.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        receipt.update(status='resolved' if dated else 'no_dated_messages',
                       undated_messages=undated)
        if not dated:
            return [], receipt
        # The newest conversation with the participant, including group exchanges;
        # do not mix disconnected threads into a single purported exchange.
        latest = dated[0][2]
        thread = (latest['conversation_id'], latest['source_id'])
        rows = [row for _, _, row in dated
                if (row['conversation_id'], row['source_id']) == thread][:limit]
        receipt['latest_record_id'] = latest['message_id']
        receipt['conversation_id'] = latest['conversation_id']
        return rows, receipt
    except sqlite3.OperationalError:
        receipt['status'] = 'identity_lookup_unavailable'
        return [], receipt
