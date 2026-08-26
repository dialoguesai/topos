"""Messenger graph extraction from canonical conversation tables (Sprint 01).

Nodes are strictly chat participants from canonical membership/senders.
Edges combine:
- co-participation in conversations
- direct links from reply-to relationships
- direct links from @mentions in message content (only when mention resolves to a participant)
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


MENTION_PATTERN = re.compile(r"(?<!\w)@([A-Za-z0-9_.+\-]{2,64})")


SOURCE_DIRECT_LINK_INVESTIGATION: Dict[str, Dict[str, Any]] = {
    "imessage": {
        "reply_fields": [
            "conversation_messages.reply_to_message_id",
            "conversation_messages.metadata_json.thread_originator_guid",
            "conversation_messages.metadata_json.associated_message_guid",
        ],
        "mention_fields": [
            "message content @token (regex extraction)",
        ],
        "notes": (
            "iMessage ingestion maps thread-originator/associated context into "
            "reply_to_message_id and metadata_json. No structured mention field is "
            "currently ingested; mentions are extracted from content."
        ),
    },
    "signal": {
        "reply_fields": [
            "conversation_messages.reply_to_message_id",
            "conversation_messages.metadata_json.quoteId",
            "conversation_messages.metadata_json.quotedMessageId",
            "conversation_messages.metadata_json.replyToMessageId",
        ],
        "mention_fields": [
            "message content @token (regex extraction)",
            "metadata_json may include quoteAuthor* fields (used as context only)",
        ],
        "notes": (
            "Signal ingestion resolves quote/reply context into reply_to_message_id "
            "when possible. No canonical structured @mention list is currently stored."
        ),
    },
    "whatsapp": {
        "reply_fields": [
            "not implemented yet",
        ],
        "mention_fields": [
            "not implemented yet",
        ],
        "notes": "Reserved for future source integration.",
    },
}


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _period_key(ts: str, granularity: str) -> str:
    dt = _parse_ts(ts)
    if dt is None:
        return "unknown"
    if granularity == "quarter":
        quarter = ((dt.month - 1) // 3) + 1
        return f"{dt.year}-Q{quarter}"
    if granularity == "year":
        return f"{dt.year}"
    return f"{dt.year:04d}-{dt.month:02d}"


def _extract_mentions(content: Optional[str]) -> Set[str]:
    if not content:
        return set()
    return {m.group(1).lower() for m in MENTION_PATTERN.finditer(content)}


def _normalize_source_ids(source_ids: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not source_ids:
        return None
    norm = sorted({str(s).strip() for s in source_ids if str(s).strip()})
    return norm or None


def _sql_in_clause(values: Sequence[str]) -> Tuple[str, List[str]]:
    placeholders = ",".join(["?"] * len(values))
    return f"({placeholders})", list(values)


def _rows_to_dicts(cursor_rows: Iterable[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in cursor_rows:
        if isinstance(row, dict):
            out.append(dict(row))
            continue
        if hasattr(row, "keys"):
            out.append({k: row[k] for k in row.keys()})
            continue
        raise TypeError("Expected sqlite Row/dict rows; ensure connection.row_factory is set")
    return out


def _load_contact_profiles(
    conn: Any,
    *,
    dataset_id: str,
) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT contact_id, display_name, known_usernames_json
        FROM contacts
        WHERE dataset_id = ?
        """,
        (dataset_id,),
    ).fetchall()
    profiles: Dict[str, Dict[str, Any]] = {}
    for row in _rows_to_dicts(rows):
        known_usernames_raw = row.get("known_usernames_json")
        known_usernames: List[str] = []
        if isinstance(known_usernames_raw, str) and known_usernames_raw.strip():
            try:
                parsed = json.loads(known_usernames_raw)
                if isinstance(parsed, list):
                    known_usernames = [str(v).strip() for v in parsed if str(v).strip()]
            except Exception:
                known_usernames = []
        profiles[str(row["contact_id"])] = {
            "display_name": row.get("display_name"),
            "known_usernames": known_usernames,
        }
    return profiles


def _load_contact_identifiers(
    conn: Any,
    *,
    dataset_id: str,
    source_ids: Optional[Sequence[str]],
) -> Dict[str, Set[str]]:
    params: List[Any] = [dataset_id]
    where = "dataset_id = ?"
    if source_ids:
        in_clause, in_params = _sql_in_clause(source_ids)
        where = f"({where} AND source_id IN {in_clause}) OR (dataset_id = ? AND source_id = '*')"
        params.extend(in_params)
        params.append(dataset_id)
    rows = conn.execute(
        f"""
        SELECT contact_id, identifier
        FROM contact_identifiers
        WHERE {where}
        """,
        tuple(params),
    ).fetchall()
    aliases: Dict[str, Set[str]] = defaultdict(set)
    for row in _rows_to_dicts(rows):
        contact_id = str(row.get("contact_id") or "").strip()
        identifier = str(row.get("identifier") or "").strip()
        if contact_id and identifier:
            aliases[contact_id].add(identifier.lower())
    return aliases


def _participant_aliases_for_contacts(
    *,
    contact_ids: Iterable[str],
    contact_profiles: Dict[str, Dict[str, Any]],
    contact_identifiers: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
    aliases_by_contact: Dict[str, Set[str]] = {}
    for contact_id in contact_ids:
        aliases: Set[str] = set()
        profile = contact_profiles.get(contact_id) or {}
        display_name = str(profile.get("display_name") or "").strip()
        if display_name:
            aliases.add(display_name.lower())
            aliases.add(display_name.replace(" ", "").lower())
        for username in profile.get("known_usernames", []) or []:
            uname = str(username).strip().lower()
            if uname:
                aliases.add(uname)
        for identifier in contact_identifiers.get(contact_id, set()):
            aliases.add(identifier.lower())
        aliases_by_contact[contact_id] = {a for a in aliases if a}
    return aliases_by_contact


def _load_contact_id_lookup_by_identifier(
    conn: Any,
    *,
    dataset_id: str,
    source_ids: Optional[Sequence[str]],
) -> Dict[Tuple[str, str], str]:
    params: List[Any] = [dataset_id]
    where = ["dataset_id = ?"]
    if source_ids:
        in_clause, in_params = _sql_in_clause(source_ids)
        where.append(f"(source_id IN {in_clause} OR source_id = '*')")
        params.extend(in_params)
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT source_id, identifier, contact_id
            FROM contact_identifiers
            WHERE {" AND ".join(where)}
            """,
            tuple(params),
        ).fetchall()
    )
    lookup: Dict[Tuple[str, str], str] = {}
    for row in rows:
        src = str(row.get("source_id") or "").strip()
        identifier = str(row.get("identifier") or "").strip()
        contact_id = str(row.get("contact_id") or "").strip()
        if src and identifier and contact_id:
            lookup[(src, identifier)] = contact_id
    return lookup


def _build_unique_alias_lookup(aliases_by_contact: Dict[str, Set[str]]) -> Dict[str, str]:
    collisions: Dict[str, Set[str]] = defaultdict(set)
    for contact_id, aliases in aliases_by_contact.items():
        for alias in aliases:
            collisions[alias].add(contact_id)
    return {
        alias: next(iter(contact_ids))
        for alias, contact_ids in collisions.items()
        if len(contact_ids) == 1
    }


def extract_messenger_graph(
    *,
    dataset_id: str,
    conn: Optional[Any] = None,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    source_ids: Optional[Sequence[str]] = None,
    period_granularity: str = "month",
    cumulative: bool = False,
) -> Dict[str, Any]:
    """Extract messenger graph nodes/edges per period from canonical tables.

    Returns:
        {
          "period_granularity": "month|quarter|year",
          "source_ids": [...],
          "periods": [
             {
               "period_key": "YYYY-MM",
               "nodes": [{"id", "label", "source_ids"}],
               "edges": [{"source","target","weight","edge_type","edge_type_counts"}],
             },
          ],
          "investigation": SOURCE_DIRECT_LINK_INVESTIGATION,
        }
    """
    if not dataset_id:
        raise ValueError("dataset_id is required")
    if period_granularity not in {"month", "quarter", "year"}:
        raise ValueError("period_granularity must be one of: month, quarter, year")

    if conn is not None:
        db = conn
    else:
        from ..core.state import get_db_connection

        db = get_db_connection()
    if db is None:
        raise RuntimeError("Database connection not available")

    normalized_sources = _normalize_source_ids(source_ids)
    query_params: List[Any] = [dataset_id]
    where = ["m.dataset_id = ?"]
    if start_ts:
        where.append("m.event_at >= ?")
        query_params.append(start_ts)
    if end_ts:
        where.append("m.event_at <= ?")
        query_params.append(end_ts)
    if normalized_sources:
        in_clause, in_params = _sql_in_clause(normalized_sources)
        where.append(f"m.source_id IN {in_clause}")
        query_params.extend(in_params)

    rows = db.execute(
        f"""
        SELECT
            m.message_id,
            m.conversation_id,
            m.source_id,
            m.sender_id,
            m.reply_to_message_id,
            m.content,
            m.event_at
        FROM conversation_messages m
        WHERE {" AND ".join(where)}
        ORDER BY m.event_at ASC, m.message_id ASC
        """,
        tuple(query_params),
    ).fetchall()
    message_rows = _rows_to_dicts(rows)
    if not message_rows:
        return {
            "period_granularity": period_granularity,
            "source_ids": normalized_sources or [],
            "periods": [],
            "investigation": SOURCE_DIRECT_LINK_INVESTIGATION,
        }

    contact_lookup = _load_contact_id_lookup_by_identifier(
        db,
        dataset_id=dataset_id,
        source_ids=normalized_sources,
    )

    messages_by_period: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    conversation_keys_by_period: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    global_message_sender: Dict[str, str] = {}

    for row in message_rows:
        sender_id = str(row.get("sender_id") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        sender_contact_id = ""
        if sender_id and source_id:
            sender_contact_id = contact_lookup.get((source_id, sender_id), "") or contact_lookup.get(("*", sender_id), "")
        row["sender_contact_id"] = sender_contact_id

        period_key = _period_key(str(row.get("event_at") or ""), period_granularity)
        messages_by_period[period_key].append(row)
        conv_key = (str(row.get("conversation_id") or ""), str(row.get("source_id") or ""))
        conversation_keys_by_period[period_key].add(conv_key)
        message_id = str(row.get("message_id") or "")
        sender_contact_id = str(row.get("sender_contact_id") or "")
        if message_id and sender_contact_id:
            global_message_sender[message_id] = sender_contact_id

    cp_params: List[Any] = [dataset_id]
    cp_where = ["dataset_id = ?"]
    if normalized_sources:
        in_clause, in_params = _sql_in_clause(normalized_sources)
        cp_where.append(f"source_id IN {in_clause}")
        cp_params.extend(in_params)
    participant_rows = _rows_to_dicts(
        db.execute(
            f"""
            SELECT conversation_id, source_id, contact_id
            FROM conversation_participants
            WHERE {" AND ".join(cp_where)}
            """,
            tuple(cp_params),
        ).fetchall()
    )

    participants_by_conversation: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for row in participant_rows:
        conv_key = (str(row.get("conversation_id") or ""), str(row.get("source_id") or ""))
        contact_id = str(row.get("contact_id") or "")
        if contact_id:
            participants_by_conversation[conv_key].add(contact_id)

    contact_profiles = _load_contact_profiles(db, dataset_id=dataset_id)
    contact_identifiers = _load_contact_identifiers(
        db,
        dataset_id=dataset_id,
        source_ids=normalized_sources,
    )

    ordered_periods = sorted(messages_by_period.keys())
    period_payloads: List[Dict[str, Any]] = []
    cumulative_messages: List[Dict[str, Any]] = []
    cumulative_conv_keys: Set[Tuple[str, str]] = set()

    for period_key in ordered_periods:
        current_messages = messages_by_period[period_key]
        current_conv_keys = conversation_keys_by_period[period_key]
        if cumulative:
            cumulative_messages.extend(current_messages)
            cumulative_conv_keys |= current_conv_keys
            period_messages = cumulative_messages
            period_conv_keys = cumulative_conv_keys
        else:
            period_messages = current_messages
            period_conv_keys = current_conv_keys

        period_participants_by_conv: Dict[Tuple[str, str], Set[str]] = {}
        for conv_key in period_conv_keys:
            base = set(participants_by_conversation.get(conv_key, set()))
            period_participants_by_conv[conv_key] = base

        participant_ids: Set[str] = set()
        node_sources: Dict[str, Set[str]] = defaultdict(set)

        for conv_key, contact_ids in period_participants_by_conv.items():
            src = conv_key[1]
            for contact_id in contact_ids:
                participant_ids.add(contact_id)
                if src:
                    node_sources[contact_id].add(src)

        for msg in period_messages:
            contact_id = str(msg.get("sender_contact_id") or "").strip()
            conv_key = (str(msg.get("conversation_id") or ""), str(msg.get("source_id") or ""))
            if not contact_id:
                continue
            participant_ids.add(contact_id)
            if conv_key[1]:
                node_sources[contact_id].add(conv_key[1])
            period_participants_by_conv.setdefault(conv_key, set()).add(contact_id)

        aliases_by_contact = _participant_aliases_for_contacts(
            contact_ids=participant_ids,
            contact_profiles=contact_profiles,
            contact_identifiers=contact_identifiers,
        )

        co_edges: Counter[Tuple[str, str]] = Counter()
        reply_edges: Counter[Tuple[str, str]] = Counter()
        mention_edges: Counter[Tuple[str, str]] = Counter()
        # Which CONNECTOR produced each edge, and how much of it. The edge tables
        # partition on `source_scope` — a joined set like 'imessage,signal' — so an
        # edge in a multi-source partition could not say where it came from, and
        # asking for one connector's view meant writing a whole extra partition of
        # the same corpus (2^n of them for n sources). Provenance belongs on the
        # row: `conv_key` is (conversation_id, source_id), so the connector is
        # already in hand here and costs one Counter to keep.
        source_edges: Counter[Tuple[str, str, str]] = Counter()

        for conv_key, members in period_participants_by_conv.items():
            sorted_members = sorted(members)
            conv_source = str(conv_key[1] or "") or "unknown"
            for src_id, tgt_id in combinations(sorted_members, 2):
                if src_id and tgt_id:
                    co_edges[(src_id, tgt_id)] += 1
                    source_edges[(src_id, tgt_id, conv_source)] += 1

            conv_alias_lookup = _build_unique_alias_lookup(
                {cid: aliases_by_contact.get(cid, set()) for cid in members}
            )
            conv_messages = [
                m
                for m in period_messages
                if (str(m.get("conversation_id") or ""), str(m.get("source_id") or "")) == conv_key
            ]
            for msg in conv_messages:
                sender_id = str(msg.get("sender_contact_id") or "").strip()
                if not sender_id:
                    continue

                reply_to_message_id = str(msg.get("reply_to_message_id") or "").strip()
                if reply_to_message_id:
                    target_id = global_message_sender.get(reply_to_message_id)
                    if target_id and target_id in members and target_id != sender_id:
                        edge = tuple(sorted((sender_id, target_id)))
                        reply_edges[edge] += 1
                        source_edges[(edge[0], edge[1], conv_source)] += 1

                for mention in _extract_mentions(msg.get("content")):
                    target_id = conv_alias_lookup.get(mention)
                    if target_id and target_id != sender_id:
                        edge = tuple(sorted((sender_id, target_id)))
                        mention_edges[edge] += 1
                        source_edges[(edge[0], edge[1], conv_source)] += 1

        all_edges = set(co_edges.keys()) | set(reply_edges.keys()) | set(mention_edges.keys())
        edges_payload: List[Dict[str, Any]] = []
        for src_id, tgt_id in sorted(all_edges):
            edge_type_counts: Dict[str, int] = {}
            if co_edges.get((src_id, tgt_id), 0):
                edge_type_counts["co_participation"] = int(co_edges[(src_id, tgt_id)])
            if reply_edges.get((src_id, tgt_id), 0):
                edge_type_counts["direct_reply"] = int(reply_edges[(src_id, tgt_id)])
            if mention_edges.get((src_id, tgt_id), 0):
                edge_type_counts["direct_mention"] = int(mention_edges[(src_id, tgt_id)])
            total_weight = sum(edge_type_counts.values())
            if len(edge_type_counts) == 1:
                edge_type = next(iter(edge_type_counts.keys()))
            else:
                edge_type = "mixed"
            edges_payload.append(
                {
                    "source": src_id,
                    "target": tgt_id,
                    "weight": total_weight,
                    "edge_type": edge_type,
                    "edge_type_counts": edge_type_counts,
                    "source_counts": {
                        conn_id: int(n)
                        for (a, b, conn_id), n in source_edges.items()
                        if a == src_id and b == tgt_id and n
                    },
                }
            )

        nodes_payload: List[Dict[str, Any]] = []
        for contact_id in sorted(participant_ids):
            profile = contact_profiles.get(contact_id) or {}
            label = str(profile.get("display_name") or "").strip() or contact_id
            nodes_payload.append(
                {
                    "id": contact_id,
                    "label": label,
                    "source_ids": sorted(node_sources.get(contact_id, set())),
                }
            )

        period_payloads.append(
            {
                "period_key": period_key,
                "nodes": nodes_payload,
                "edges": edges_payload,
            }
        )

    return {
        "period_granularity": period_granularity,
        "source_ids": normalized_sources or [],
        "periods": period_payloads,
        "investigation": SOURCE_DIRECT_LINK_INVESTIGATION,
    }
