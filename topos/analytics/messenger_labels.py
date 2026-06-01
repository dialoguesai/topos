"""Helpers for resolving participant labels in messenger analytics."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Sequence, Set, Tuple


def _rows_to_dicts(rows: Sequence[Any], cursor: Any = None) -> List[Dict[str, Any]]:
    """Map DB rows to dicts. Plain sqlite3 connections return tuples; use ``cursor.description``."""
    out: List[Dict[str, Any]] = []
    col_names: List[str] | None = None
    if cursor is not None and getattr(cursor, "description", None):
        col_names = [d[0] for d in cursor.description if d is not None]
    for row in rows:
        if hasattr(row, "keys"):
            out.append({k: row[k] for k in row.keys()})
        elif col_names is not None and isinstance(row, (tuple, list)) and len(row) == len(col_names):
            out.append({col_names[i]: row[i] for i in range(len(col_names))})
        else:
            out.append(dict(row))
    return out


def _in_clause(values: Sequence[str]) -> tuple[str, List[str]]:
    placeholders = ",".join(["?"] * len(values))
    return f"({placeholders})", list(values)


def _normalize_contact_key(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low == "self":
        return "self"
    if "@" in low:
        return low
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        return f"+{digits}" if s.startswith("+") else digits
    return low


def sender_matches_focus_identifier(sender_id: str, profile_identifier: str) -> bool:
    """True if message ``sender_id`` refers to the same party as the profile row's primary identifier."""
    a = str(sender_id or "").strip()
    b = str(profile_identifier or "").strip()
    if not a or not b:
        return False
    if _identifier_candidates(a) & _identifier_candidates(b):
        return True
    na, nb = _normalize_contact_key(a), _normalize_contact_key(b)
    return bool(na and nb and na == nb)


def _identifier_candidates(value: str) -> Set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    out = {raw, raw.lower()}
    normalized = _normalize_contact_key(raw)
    if normalized:
        out.add(normalized)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        out.add(digits)
        out.add(f"+{digits}")
        # Common NANP variant: some imports drop leading country code 1.
        if len(digits) == 11 and digits.startswith("1"):
            local10 = digits[1:]
            out.add(local10)
            out.add(f"+{local10}")
    return {v for v in out if v}


def resolve_participant_labels(
    conn: Any,
    *,
    dataset_id: str,
    participant_ids: Sequence[str],
) -> Dict[str, Dict[str, str]]:
    """Resolve display labels for participant contact IDs.

    Priority:
    1) contacts.display_name
    2) a contact identifier from contact_identifiers
    3) raw participant_id
    """
    normalized_participants = sorted({str(pid).strip() for pid in participant_ids if str(pid).strip()})
    if not normalized_participants:
        return {}

    contacts_in_clause, contacts_params = _in_clause(normalized_participants)
    participant_candidates: Dict[str, Set[str]] = {
        participant_id: _identifier_candidates(participant_id)
        for participant_id in normalized_participants
    }
    all_identifier_candidates = sorted({cand for cands in participant_candidates.values() for cand in cands})

    _cur_contacts = conn.execute(
        f"""
            SELECT contact_id, display_name
            FROM contacts
            WHERE dataset_id = ? AND contact_id IN {contacts_in_clause}
            """,
        tuple([dataset_id] + contacts_params),
    )
    contacts_rows = _rows_to_dicts(_cur_contacts.fetchall(), _cur_contacts)
    display_name_by_contact_id = {
        str(row["contact_id"]): str(row["display_name"]).strip()
        for row in contacts_rows
        if row.get("contact_id") and row.get("display_name") and str(row["display_name"]).strip()
    }

    # Keep identifier fallback for participants that are already contact_ids.
    _cur_cid = conn.execute(
        f"""
            SELECT contact_id, identifier, source_id
            FROM contact_identifiers
            WHERE dataset_id = ?
              AND contact_id IN {contacts_in_clause}
            ORDER BY CASE WHEN source_id = '*' THEN 1 ELSE 0 END, updated_at DESC
            """,
        tuple([dataset_id] + contacts_params),
    )
    contact_identifier_rows = _rows_to_dicts(_cur_cid.fetchall(), _cur_cid)

    identifier_rows: List[Dict[str, Any]] = []
    if all_identifier_candidates:
        identifiers_in_clause, identifiers_params = _in_clause(all_identifier_candidates)
        _cur_ident = conn.execute(
            f"""
                SELECT ci.contact_id, ci.identifier, ci.source_id, c.display_name
                FROM contact_identifiers ci
                LEFT JOIN contacts c
                  ON c.dataset_id = ci.dataset_id
                 AND c.contact_id = ci.contact_id
                WHERE ci.dataset_id = ?
                  AND ci.identifier IN {identifiers_in_clause}
                ORDER BY CASE WHEN ci.source_id = '*' THEN 1 ELSE 0 END, ci.updated_at DESC
                """,
            tuple([dataset_id] + identifiers_params),
        )
        identifier_rows = _rows_to_dicts(_cur_ident.fetchall(), _cur_ident)

    best_identifier_by_contact_id: Dict[str, str] = {}
    display_name_by_identifier: Dict[str, str] = {}
    contact_ids_by_identifier: Dict[str, List[str]] = defaultdict(list)

    def _index_identifier_rows(rows: Sequence[Dict[str, Any]]) -> None:
        for row in rows:
            contact_id = str(row.get("contact_id") or "").strip()
            identifier = str(row.get("identifier") or "").strip()
            display_name = str(row.get("display_name") or "").strip()
            if not contact_id or not identifier:
                continue
            if display_name and contact_id not in display_name_by_contact_id:
                display_name_by_contact_id[contact_id] = display_name
            if contact_id not in best_identifier_by_contact_id:
                best_identifier_by_contact_id[contact_id] = identifier
            for candidate in _identifier_candidates(identifier):
                if candidate and contact_id not in contact_ids_by_identifier[candidate]:
                    contact_ids_by_identifier[candidate].append(contact_id)
                if candidate and display_name and candidate not in display_name_by_identifier:
                    display_name_by_identifier[candidate] = display_name

    _index_identifier_rows(identifier_rows)

    # Also index identifiers that belong to participant contact_ids directly (used for fallback labeling).
    _index_identifier_rows(contact_identifier_rows)

    secondary_identifier_candidates = sorted(
        {
            candidate
            for identifier in best_identifier_by_contact_id.values()
            for candidate in _identifier_candidates(identifier)
        }
    )
    if secondary_identifier_candidates:
        secondary_in_clause, secondary_params = _in_clause(secondary_identifier_candidates)
        _cur_sec = conn.execute(
            f"""
                SELECT ci.contact_id, ci.identifier, ci.source_id, c.display_name
                FROM contact_identifiers ci
                LEFT JOIN contacts c
                  ON c.dataset_id = ci.dataset_id
                 AND c.contact_id = ci.contact_id
                WHERE ci.dataset_id = ?
                  AND ci.identifier IN {secondary_in_clause}
                ORDER BY CASE WHEN ci.source_id = '*' THEN 1 ELSE 0 END, ci.updated_at DESC
                """,
            tuple([dataset_id] + secondary_params),
        )
        secondary_rows = _rows_to_dicts(_cur_sec.fetchall(), _cur_sec)
        _index_identifier_rows(secondary_rows)

    for row in contact_identifier_rows:
        contact_id = str(row.get("contact_id") or "").strip()
        identifier = str(row.get("identifier") or "").strip()
        display_name = str(row.get("display_name") or "").strip()
        if contact_id and identifier and contact_id not in best_identifier_by_contact_id:
            best_identifier_by_contact_id[contact_id] = identifier

    out: Dict[str, Dict[str, str]] = {}
    for participant_id in normalized_participants:
        display_name = display_name_by_contact_id.get(participant_id, "")
        identifier = best_identifier_by_contact_id.get(participant_id, "")

        if not display_name:
            matched_contact_id = ""
            for candidate in participant_candidates.get(participant_id, set()):
                contact_ids = contact_ids_by_identifier.get(candidate, [])
                if not contact_ids:
                    continue
                matched_contact_id = contact_ids[0]
                if matched_contact_id:
                    break
            if matched_contact_id:
                display_name = display_name_by_contact_id.get(matched_contact_id, "") or display_name
                identifier = best_identifier_by_contact_id.get(matched_contact_id, "") or identifier
            if not display_name:
                # Fallback to identifier-level display mapping (e.g., when contact row has sparse data).
                for candidate in participant_candidates.get(participant_id, set()):
                    maybe_name = display_name_by_identifier.get(candidate, "")
                    if maybe_name:
                        display_name = maybe_name
                        break

        if not identifier:
            identifier = participant_id

        # If this participant maps to an unnamed contact_id but we do have an identifier,
        # try resolving that identifier to another contact with a display name
        # (common after contact import where normalized phone variants point to different contact_ids).
        if not display_name and identifier:
            identifier_matched_contact_id = ""
            fallback_contact_id = ""
            for candidate in _identifier_candidates(identifier):
                contact_ids = contact_ids_by_identifier.get(candidate, [])
                if not contact_ids:
                    continue
                named_ids = [cid for cid in contact_ids if display_name_by_contact_id.get(cid)]
                if named_ids:
                    identifier_matched_contact_id = named_ids[0]
                    break
                if not fallback_contact_id:
                    fallback_contact_id = contact_ids[0]
            if not identifier_matched_contact_id and fallback_contact_id:
                identifier_matched_contact_id = fallback_contact_id
            if identifier_matched_contact_id:
                display_name = display_name_by_contact_id.get(identifier_matched_contact_id, "") or display_name
                identifier = best_identifier_by_contact_id.get(identifier_matched_contact_id, "") or identifier
        label = display_name or identifier or participant_id
        out[participant_id] = {
            "label": label,
            "display_name": display_name,
            "identifier": identifier,
        }
    return out


def enrich_conversation_thread_previews(
    conn: Any,
    *,
    dataset_id: str,
    profile_identifier: str,
    previews: List[Dict[str, Any]],
) -> None:
    """Mutates each message in ``previews``: adds ``sender_display_name`` and ``is_focus_contact``."""
    senders: List[str] = []
    for block in previews:
        for m in block.get("messages") or []:
            if not isinstance(m, dict):
                continue
            sid = str(m.get("sender_id") or "").strip()
            if sid:
                senders.append(sid)
    labels = resolve_participant_labels(conn, dataset_id=dataset_id, participant_ids=senders)
    for block in previews:
        for m in block.get("messages") or []:
            if not isinstance(m, dict):
                continue
            sid = str(m.get("sender_id") or "").strip()
            info = labels.get(sid, {}) if sid else {}
            label = str(info.get("label") or "").strip()
            m["sender_display_name"] = label or sid or "Unknown"
            m["is_focus_contact"] = bool(sid) and sender_matches_focus_identifier(sid, profile_identifier)


def enrich_contact_rows_with_resolved_display_names(
    conn: Any,
    *,
    dataset_id: str,
    contacts: List[Dict[str, Any]],
) -> None:
    """Fill empty ``display_name`` on owner/API contact rows (parity with messenger social graph).

    ``list_contacts`` returns ``contacts.display_name`` per row only. Analytics uses
    :func:`resolve_participant_labels` to promote names across identifier variants and
    duplicate contact_ids (e.g. iMessage sender vs address-book import). Apply the same
    resolution here so grant privacy UI and filters see the same labels as the graph.
    """
    participant_ids: List[str] = []
    for c in contacts:
        cid = str(c.get("contact_id") or "").strip()
        if cid:
            participant_ids.append(cid)
        ident = str(c.get("identifier") or "").strip()
        if ident:
            participant_ids.append(ident)
    if not participant_ids:
        return
    labels = resolve_participant_labels(conn, dataset_id=dataset_id, participant_ids=participant_ids)
    for c in contacts:
        if str(c.get("display_name") or "").strip():
            continue
        cid = str(c.get("contact_id") or "").strip()
        resolved = str((labels.get(cid) or {}).get("display_name") or "").strip()
        if resolved:
            c["display_name"] = resolved
