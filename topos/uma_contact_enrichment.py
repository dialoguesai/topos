"""
Stage 11: UMA message contact participation + display name resolution.

Requires dataset_id and DB connection. Skips all logic when dataset_id is missing.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from shared.filtering import FilterManifest

from topos.analytics.messenger_labels import _identifier_candidates, resolve_participant_labels
from topos.contacts.identity import normalize_contact_key
from topos.storage.canonical.conversations_tables import CONTACT_IDENTIFIERS_TABLE, CONTACTS_TABLE
from topos.storage.user_identity import get_user_identity

logger = logging.getLogger("topos.uma_contact_enrichment")

DEFAULT_SHARING_POLICY = {"name_visibility": "normal", "row_visibility": "exclude_from_grants"}


def _table_exists(conn, table_name: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def strip_contact_runtime_filters(manifest: Optional[FilterManifest]) -> Optional[FilterManifest]:
    """Remove filters handled in this module so apply_filter_manifest does not double-apply or error."""
    if manifest is None:
        return None
    skip = {"message_contact_participation", "contact_display_names"}
    kept = [f for f in manifest.filters if f.filter_id not in skip]
    if len(kept) == len(manifest.filters):
        return manifest
    return manifest.model_copy(update={"filters": kept})


def _parse_sharing_policy(raw: Any) -> Dict[str, str]:
    if raw is None or raw == "":
        return dict(DEFAULT_SHARING_POLICY)
    if isinstance(raw, dict):
        base = dict(DEFAULT_SHARING_POLICY)
        base.update({k: str(v) for k, v in raw.items() if k in ("name_visibility", "row_visibility")})
        return base
    try:
        d = json.loads(str(raw))
        if isinstance(d, dict):
            base = dict(DEFAULT_SHARING_POLICY)
            base.update({k: str(v) for k, v in d.items() if k in ("name_visibility", "row_visibility")})
            return base
    except Exception:
        pass
    return dict(DEFAULT_SHARING_POLICY)


def _contact_ids_for_literal_self_sender(id_mm: Dict[str, Set[str]]) -> Set[str]:
    """Contact IDs tied to the iMessage sender handle ``self`` (when not using ``is_self`` row)."""
    out: Set[str] = set()
    for key in _identifier_candidates("self"):
        got = id_mm.get(key)
        if got:
            out.update(got)
    return out


def build_identifier_contact_multimap(
    conn, dataset_id: str, _source_ids: Optional[Set[str]] = None
) -> Dict[str, Set[str]]:
    """
    Map each lookup key -> set(contact_id) for resolving message sender_id.

    Uses the same phone/email candidate expansion as the social graph (``_identifier_candidates``)
    so E.164 (+1…), 10-digit NANP, and stored identifier strings align.

    **Multimap (not single-valued):** duplicate imports often create two ``contact_id`` rows for
    the same NANP phone (e.g. ``+1512…`` vs ``512…``). Those rows share expanded keys; a
    first-wins ``Dict[str, str]`` drops one side so ``pick_representative_contact_id`` never sees
    the named card. Every ``(key, contact_id)`` pair from identifiers is recorded here.

    Loads **all** ``contact_identifiers`` rows for ``dataset_id``. ``_source_ids`` is ignored.
    """
    if not conn or not dataset_id:
        return {}
    if not _table_exists(conn, CONTACT_IDENTIFIERS_TABLE):
        return {}
    mm: Dict[str, Set[str]] = defaultdict(set)
    rows = conn.execute(
        f"""
        SELECT identifier, contact_id
        FROM {CONTACT_IDENTIFIERS_TABLE}
        WHERE dataset_id = ?
        """,
        (dataset_id,),
    ).fetchall()
    for ident, cid in rows:
        if not ident or not cid:
            continue
        i = str(ident).strip()
        c = str(cid).strip()
        for key in _identifier_candidates(i):
            if key:
                mm[key].add(c)
        nk = normalize_contact_key(i)
        if nk:
            mm[nk].add(c)
        mm[i].add(c)
    return dict(mm)


def _nanp_digit_lookup_keys(digits: str) -> Set[str]:
    """Link US NANP handles that differ only by formatting or leading country code 1."""
    d = digits
    if len(d) < 10:
        return set()
    keys: Set[str] = {d[-10:]}
    if len(d) == 11 and d[0] == "1":
        keys.add(d[1:])
    keys.add(d)
    return keys


def _nanp_lookup_keys_for_value(value: Any) -> Set[str]:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())
    return _nanp_digit_lookup_keys(d)


def build_nanp_digit_contact_index(conn, dataset_id: str) -> Dict[str, Set[str]]:
    """
    Map digit-derived keys -> contact_ids so differently formatted phone rows still merge
    (e.g. ``+1512…`` vs ``(512) …`` vs ``512…``) when string keys in the multimap diverge.
    """
    if not conn or not dataset_id:
        return {}
    idx: Dict[str, Set[str]] = defaultdict(set)
    try:
        rows = conn.execute(
            f"""
            SELECT identifier, contact_id
            FROM {CONTACT_IDENTIFIERS_TABLE}
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_nanp_digit_contact_index failed: %s", exc)
        return {}
    for ident, cid in rows:
        if not ident or not cid:
            continue
        i = str(ident).strip()
        c = str(cid).strip()
        for k in _nanp_lookup_keys_for_value(i):
            idx[k].add(c)
    return dict(idx)


def _uma_graph_display_name_for_row(
    graph_labels: Dict[str, Dict[str, str]],
    row: Dict[str, Any],
) -> str:
    """Best-effort display_name from :func:`resolve_participant_labels` for this message's handles."""
    sid = str(row.get("sender_id") or "").strip()
    if not sid or sid.lower() == "self":
        return ""
    keys: List[str] = [sid]
    alt = _metadata_chat_identifier(row)
    if alt:
        a = str(alt).strip()
        if a and a not in keys:
            keys.append(a)
    for key in keys:
        g = graph_labels.get(key, {})
        dn = (g.get("display_name") or "").strip()
        if dn:
            return dn
    return ""


def _graph_display_name_respects_name_policy(
    graph_dn: str,
    cids: Set[str],
    display_names: Dict[str, Optional[str]],
    name_block: Set[str],
) -> str:
    """
    Only use graph-resolved names that correspond to a contact on this row whose name may be shown.

    ``resolve_participant_labels`` ignores sharing_policy; we still must not surface a name marked hidden.
    """
    g = (graph_dn or "").strip()
    if not g or not cids:
        return ""
    holders = {c for c in cids if (display_names.get(c) or "").strip() == g}
    if not holders:
        return ""
    if holders & name_block:
        return ""
    return g


def _owner_graph_display_name(
    graph_labels: Dict[str, Dict[str, str]],
    self_contact_id: str,
) -> str:
    """
    ``resolve_participant_labels`` result keyed by the owner's ``contact_id``.

    Include ``self_contact_id`` in the graph batch so duplicate cards / identifier promotion
    can supply a name when ``contacts.display_name`` on the ``is_self`` row is empty.
    """
    g = graph_labels.get(self_contact_id, {})
    dn = (g.get("display_name") or "").strip()
    if dn and not _is_imessage_self_sentinel_label(dn):
        return dn
    lab = (g.get("label") or "").strip()
    if not lab or _is_imessage_self_sentinel_label(lab) or lab == self_contact_id:
        return ""
    return lab


def _metadata_chat_identifier(row: Dict[str, Any]) -> Optional[str]:
    """iMessage-style metadata often duplicates the peer handle in ``chat_identifier``."""
    mj = row.get("metadata_json")
    if mj is None:
        return None
    if isinstance(mj, str):
        try:
            mj = json.loads(mj)
        except Exception:
            return None
    if not isinstance(mj, dict):
        return None
    for key in ("chat_identifier", "handle"):
        v = mj.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def message_row_contact_id(
    row: Dict[str, Any],
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str] = None,
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
) -> Optional[str]:
    """Resolve contact_id from sender_id, then from metadata_json when needed."""
    cids = collect_message_contact_ids(row, id_mm, self_contact_id=self_contact_id, nanp_idx=nanp_idx)
    if not cids:
        return None
    if len(cids) == 1:
        return next(iter(cids))
    # Ambiguous without contact meta; callers that need a single id should use
    # pick_representative_contact_id after load_contact_meta.
    for key in _ordered_identifier_lookup_keys(row.get("sender_id")):
        got = id_mm.get(key)
        if got:
            return min(got)
    alt = _metadata_chat_identifier(row)
    if alt:
        for key in _ordered_identifier_lookup_keys(alt):
            got = id_mm.get(key)
            if got:
                return min(got)
    return min(cids)


def _ordered_identifier_lookup_keys(sender_id: Any) -> List[str]:
    """Deterministic key order for first-hit fallback (before meta-aware pick)."""
    if sender_id is None:
        return []
    s = str(sender_id).strip()
    if not s:
        return []
    keys: List[str] = []
    seen: Set[str] = set()

    def add(k: str) -> None:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    add(s)
    add(s.lower())
    nk = normalize_contact_key(s)
    add(nk)
    for k in sorted(_identifier_candidates(s)):
        add(k)
    return keys


def collect_contact_ids_for_sender(
    sender_id: Any,
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str] = None,
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
) -> Set[str]:
    """All contact_ids that match any stored identifier key for this sender."""
    if sender_id is None:
        return set()
    s = str(sender_id).strip()
    if not s:
        return set()
    if s.lower() == "self":
        out: Set[str] = set()
        if self_contact_id:
            out.add(self_contact_id)
        for key in _identifier_candidates(s):
            got = id_mm.get(key)
            if got:
                out.update(got)
        return out
    cids: Set[str] = set()
    for key in _identifier_candidates(s):
        got = id_mm.get(key)
        if got:
            cids.update(got)
    nk = normalize_contact_key(s)
    if nk:
        got = id_mm.get(nk)
        if got:
            cids.update(got)
    if nanp_idx:
        for k in _nanp_lookup_keys_for_value(s):
            got = nanp_idx.get(k)
            if got:
                cids.update(got)
    return cids


def collect_message_contact_ids(
    row: Dict[str, Any],
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str] = None,
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
) -> Set[str]:
    """Union of contact_ids from sender_id and metadata_json handles."""
    cids: Set[str] = set()
    cids.update(
        collect_contact_ids_for_sender(
            row.get("sender_id"),
            id_mm,
            self_contact_id=self_contact_id,
            nanp_idx=nanp_idx,
        )
    )
    alt = _metadata_chat_identifier(row)
    if alt:
        cids.update(
            collect_contact_ids_for_sender(alt, id_mm, self_contact_id=self_contact_id, nanp_idx=nanp_idx)
        )
    return cids


def sender_row_contact_id(
    sender_id: Any,
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str] = None,
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
) -> Optional[str]:
    if sender_id is None:
        return None
    s = str(sender_id).strip()
    if not s:
        return None
    cids = collect_contact_ids_for_sender(
        sender_id,
        id_mm,
        self_contact_id=self_contact_id,
        nanp_idx=nanp_idx,
    )
    if not cids:
        return None
    if len(cids) == 1:
        return next(iter(cids))
    for key in _ordered_identifier_lookup_keys(sender_id):
        got = id_mm.get(key)
        if got:
            return min(got)
    return min(cids)


def _has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def _is_imessage_self_sentinel_label(value: str) -> bool:
    """True for the iMessage owner placeholder string; not a human-readable display name."""
    return str(value or "").strip().lower() == "self"


def _contact_label_score(
    cid: str,
    *,
    display_names: Dict[str, Optional[str]],
    known_usernames_by_cid: Dict[str, List[str]],
    fallback_labels: Dict[str, str],
) -> Tuple[int, int, str]:
    """
    Return (tier, length, tie_breaker) with higher tier/length better.
    tier: 3 = human-looking display_name, 2 = human username, 1 = any display_name, 0 = phone-like fallback.
    """
    dn = (display_names.get(cid) or "").strip()
    if _is_imessage_self_sentinel_label(dn):
        dn = ""
    tier = 0
    best = ""
    if dn:
        if _has_letter(dn):
            tier = 3
            best = dn
        else:
            tier = 1
            best = dn
    if tier < 2:
        for u in known_usernames_by_cid.get(cid) or []:
            uu = str(u).strip()
            if uu and not _is_imessage_self_sentinel_label(uu) and _has_letter(uu):
                tier = max(tier, 2)
                if len(uu) > len(best):
                    best = uu
    if tier == 0:
        fb = (fallback_labels.get(cid) or "").strip()
        if fb and not _is_imessage_self_sentinel_label(fb):
            best = fb
    return (tier, len(best), best or cid)


def _row_hidden_by_default_policy(
    cid: str,
    policies: Dict[str, Dict[str, str]],
    inherit_defaults: bool,
) -> bool:
    if not inherit_defaults:
        return False
    pol = policies.get(cid)
    if pol is None:
        return False
    return pol.get("row_visibility") == "exclude_from_grants"


def pick_representative_contact_id(
    cids: Set[str],
    *,
    display_names: Dict[str, Optional[str]],
    known_usernames_by_cid: Dict[str, List[str]],
    fallback_labels: Dict[str, str],
    policies: Dict[str, Dict[str, str]],
    inherit_defaults: bool,
) -> Optional[str]:
    """
    When multiple contact rows share the same phone (e.g. +E.164 vs 10-digit imports),
    prefer the card with a real display name / username over an unnamed duplicate.
    """
    if not cids:
        return None
    if len(cids) == 1:
        return next(iter(cids))
    ranked: List[Tuple[int, int, int, str]] = []
    for cid in cids:
        tier, length, tie = _contact_label_score(
            cid,
            display_names=display_names,
            known_usernames_by_cid=known_usernames_by_cid,
            fallback_labels=fallback_labels,
        )
        hidden = _row_hidden_by_default_policy(cid, policies, inherit_defaults)
        visibility_boost = 0 if hidden else 1
        ranked.append((visibility_boost, tier, length, cid))
    ranked.sort(reverse=True)
    return ranked[0][3]


def visible_label_for_contact(
    cid: str,
    *,
    display_names: Dict[str, Optional[str]],
    known_usernames_by_cid: Dict[str, List[str]],
    fallback_labels: Dict[str, str],
) -> str:
    """Single visible string for a contact_id (display_name → username → identifier)."""
    dn = (display_names.get(cid) or "").strip()
    if dn and not _is_imessage_self_sentinel_label(dn):
        return dn
    for u in known_usernames_by_cid.get(cid) or []:
        uu = str(u).strip()
        if uu and not _is_imessage_self_sentinel_label(uu):
            return uu
    fb = (fallback_labels.get(cid) or "").strip()
    if fb and not _is_imessage_self_sentinel_label(fb):
        return fb
    return ""


def load_self_contact_info(conn: Any, dataset_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (contact_id, display label) for the dataset owner (is_self), if present."""
    if not conn or not dataset_id:
        return None, None
    try:
        row = conn.execute(
            f"""
            SELECT contact_id, display_name, known_usernames_json
            FROM {CONTACTS_TABLE}
            WHERE dataset_id = ? AND is_self = 1
            LIMIT 1
            """,
            (dataset_id,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_self_contact_info failed: %s", exc)
        return None, None
    if not row:
        return None, None
    cid = str(row[0] or "").strip() or None
    dn = str(row[1] or "").strip()
    if dn and not _is_imessage_self_sentinel_label(dn):
        return cid, dn
    raw = row[2]
    try:
        arr = json.loads(raw or "[]")
        if isinstance(arr, list):
            for u in arr:
                uu = str(u).strip()
                if uu and not _is_imessage_self_sentinel_label(uu):
                    return cid, uu
    except Exception:
        pass
    return cid, None


def load_user_identity_display_name(conn: Any, dataset_id: str) -> Optional[str]:
    """Return the canonical owner-authored display name for the dataset, if set."""
    if not conn or not dataset_id:
        return None
    try:
        identity = get_user_identity(conn, dataset_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_user_identity_display_name failed: %s", exc)
        return None
    if not identity:
        return None
    dn = str(identity.get("display_name") or "").strip()
    return dn or None


def prefetch_contact_ids_for_conversations(
    conn: Any,
    dataset_id: str,
    conversation_ids: Set[str],
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str],
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
) -> Set[str]:
    """Union of all contact_ids reachable from senders in the given conversations."""
    out: Set[str] = set()
    if not conn or not dataset_id or not conversation_ids:
        return out
    placeholders = ",".join("?" for _ in conversation_ids)
    params: List[Any] = [dataset_id, *sorted(conversation_ids)]
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT sender_id
            FROM conversation_messages
            WHERE dataset_id = ? AND conversation_id IN ({placeholders})
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prefetch_contact_ids_for_conversations failed: %s", exc)
        return out
    for (sender_id,) in rows:
        out.update(
            collect_contact_ids_for_sender(
                sender_id,
                id_mm,
                self_contact_id=self_contact_id,
                nanp_idx=nanp_idx,
            )
        )
    return out


def load_conversation_participant_contact_ids(
    conn: Any,
    dataset_id: str,
    conversation_ids: Set[str],
    id_mm: Dict[str, Set[str]],
    *,
    self_contact_id: Optional[str],
    nanp_idx: Optional[Dict[str, Set[str]]] = None,
    display_names: Dict[str, Optional[str]],
    known_usernames_by_cid: Dict[str, List[str]],
    fallback_labels: Dict[str, str],
    policies: Dict[str, Dict[str, str]],
    inherit_defaults: bool,
) -> Dict[str, Set[str]]:
    """
    Return mapping conversation_id -> set(contact_id) using sender_id values from
    conversation_messages within the same dataset.
    """
    out: Dict[str, Set[str]] = {}
    if not conn or not dataset_id or not conversation_ids:
        return out
    placeholders = ",".join("?" for _ in conversation_ids)
    params: List[Any] = [dataset_id, *sorted(conversation_ids)]
    try:
        rows = conn.execute(
            f"""
            SELECT conversation_id, sender_id
            FROM conversation_messages
            WHERE dataset_id = ? AND conversation_id IN ({placeholders})
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_conversation_participant_contact_ids failed: %s", exc)
        return out

    for conv_id, sender_id in rows:
        conv = str(conv_id or "").strip()
        if not conv:
            continue
        cids = collect_contact_ids_for_sender(
            sender_id,
            id_mm,
            self_contact_id=self_contact_id,
            nanp_idx=nanp_idx,
        )
        cid = (
            pick_representative_contact_id(
                cids,
                display_names=display_names,
                known_usernames_by_cid=known_usernames_by_cid,
                fallback_labels=fallback_labels,
                policies=policies,
                inherit_defaults=inherit_defaults,
            )
            if cids
            else None
        )
        if not cid:
            continue
        if conv not in out:
            out[conv] = set()
        out[conv].add(cid)
    return out


def load_contact_meta(
    conn,
    dataset_id: str,
    contact_ids: Set[str],
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Optional[str]], Dict[str, List[str]]]:
    """Return (sharing_policy_by_cid, display_name_by_cid, known_usernames_by_cid)."""
    policies: Dict[str, Dict[str, str]] = {}
    names: Dict[str, Optional[str]] = {}
    usernames: Dict[str, List[str]] = {}
    if not conn or not dataset_id or not contact_ids:
        return policies, names, usernames
    placeholders = ",".join("?" for _ in contact_ids)
    params: List[Any] = [dataset_id, *sorted(contact_ids)]
    try:
        rows = conn.execute(
            f"""
            SELECT contact_id, display_name, sharing_policy_json, known_usernames_json
            FROM {CONTACTS_TABLE}
            WHERE dataset_id = ? AND contact_id IN ({placeholders})
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_contact_meta failed: %s", exc)
        return policies, names, usernames
    for cid, dname, pol, raw_users in rows:
        c = str(cid or "").strip()
        if not c:
            continue
        names[c] = str(dname).strip() if dname else None
        policies[c] = _parse_sharing_policy(pol)
        parsed: List[str] = []
        try:
            arr = json.loads(raw_users or "[]")
            if isinstance(arr, list):
                parsed = [str(v).strip() for v in arr if str(v).strip()]
        except Exception:
            parsed = []
        usernames[c] = parsed
    return policies, names, usernames


def load_identifier_fallback_labels(
    conn: Any,
    dataset_id: str,
    contact_ids: Set[str],
) -> Dict[str, str]:
    """
    When ``contacts.display_name`` is empty, use a primary identifier row as the visible label.

    Avoids returning no ``sender_display_name`` when the address book row exists but the card
    name was never synced into ``display_name``.
    """
    if not conn or not dataset_id or not contact_ids:
        return {}
    out: Dict[str, str] = {}
    placeholders = ",".join("?" * len(contact_ids))
    params: List[Any] = [dataset_id, *sorted(contact_ids)]
    try:
        rows = conn.execute(
            f"""
            SELECT contact_id, identifier, source_id
            FROM {CONTACT_IDENTIFIERS_TABLE}
            WHERE dataset_id = ? AND contact_id IN ({placeholders})
            ORDER BY CASE WHEN source_id = '*' THEN 1 ELSE 0 END ASC, updated_at DESC
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_identifier_fallback_labels failed: %s", exc)
        return out
    for cid_raw, ident, _src in rows:
        c = str(cid_raw or "").strip()
        i = str(ident or "").strip()
        if not c or not i or c in out:
            continue
        out[c] = i
    return out


def _apply_blackhole_to_message_rows(rows: List[Dict[str, Any]], guard: Any) -> List[Dict[str, Any]]:
    """Drop messages that mention a black-holed entity.

    Applied on every exit from the pipeline, including the early one: a caller
    with no dataset_id still gets rows back, and those rows still carry names.
    """
    if guard is None:
        return rows
    return guard.filter_canonical_rows(
        rows,
        record_id_keys=("record_id", "message_id", "id"),
        text_keys=("content", "content_disclosure", "text", "body"),
    )


def apply_message_contact_pipeline(
    items: List[Dict[str, Any]],
    *,
    conn: Any,
    dataset_id: Optional[str],
    allowed_scopes: List[str],
    manifest: Optional[FilterManifest],
    filters: Optional[Dict[str, Any]],
    blackhole_guard: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Apply message_contact_participation, owner sharing_policy row exclusion, grant block/allow lists,
    and optional sender_display_name enrichment when contacts:resolve + contact_display_names.

    Returns ``(rows, sidecar)``. ``sidecar["message_owner"]`` describes the dataset owner so clients
    can label owner-authored rows (see per-row ``sender_is_owner`` and ``is_from_self``).

    ``blackhole_guard`` is required rather than defaulted: this is the messages
    read path, and a protected-entity filter that a call site can forget is one
    that will be forgotten. Pass an owner guard for an owner self-read.
    """
    if not items or not conn or not dataset_id:
        return _apply_blackhole_to_message_rows(items, blackhole_guard), {}

    scope_set = {str(s).strip() for s in (allowed_scopes or []) if s}
    can_resolve = "contacts:resolve" in scope_set

    participation = manifest.get_filter("message_contact_participation") if manifest else None
    name_filter = manifest.get_filter("contact_display_names") if manifest else None
    # If contacts:resolve is granted, enrich names by default unless the manifest explicitly disables it.
    names_enabled = can_resolve
    if name_filter is not None:
        names_enabled = bool(name_filter.params.get("enabled"))

    cgp = (filters or {}).get("contact_grant_policy") if isinstance(filters, dict) else None
    cgp = cgp if isinstance(cgp, dict) else {}
    inherit_defaults = bool(cgp.get("inherit_contact_defaults", True))
    grant_block: Set[str] = {str(x).strip() for x in (cgp.get("blocklist_contact_ids") or []) if str(x).strip()}
    grant_allow: Set[str] = {str(x).strip() for x in (cgp.get("allowlist_contact_ids") or []) if str(x).strip()}

    source_ids = {str(row.get("source_id") or "").strip() for row in items if row.get("source_id")}
    id_mm = build_identifier_contact_multimap(conn, dataset_id, source_ids)
    nanp_idx = build_nanp_digit_contact_index(conn, dataset_id)
    if not id_mm:
        logger.info(
            "UMA contact enrichment: no rows in %s for dataset_id=%s; cannot resolve senders to contacts",
            CONTACT_IDENTIFIERS_TABLE,
            dataset_id[:48] if dataset_id else "",
        )

    canonical_owner_display_name = load_user_identity_display_name(conn, dataset_id)
    self_contact_id, _self_label = load_self_contact_info(conn, dataset_id)
    literal_self_cids = _contact_ids_for_literal_self_sender(id_mm)

    conversation_ids_in_page: Set[str] = set()
    contact_ids_in_page: Set[str] = set()
    for row in items:
        conv = str(row.get("conversation_id") or row.get("thread_id") or "").strip()
        if conv:
            conversation_ids_in_page.add(conv)
        contact_ids_in_page.update(
            collect_message_contact_ids(row, id_mm, self_contact_id=self_contact_id, nanp_idx=nanp_idx)
        )
    if self_contact_id:
        contact_ids_in_page.add(self_contact_id)
    else:
        contact_ids_in_page.update(literal_self_cids)
    contact_ids_in_page.update(
        prefetch_contact_ids_for_conversations(
            conn,
            dataset_id,
            conversation_ids_in_page,
            id_mm,
            self_contact_id=self_contact_id,
            nanp_idx=nanp_idx,
        )
    )

    policies, display_names, known_usernames_by_cid = load_contact_meta(conn, dataset_id, contact_ids_in_page)
    fallback_labels = load_identifier_fallback_labels(conn, dataset_id, contact_ids_in_page)

    effective_owner_contact_id: Optional[str] = self_contact_id
    if not effective_owner_contact_id and literal_self_cids:
        effective_owner_contact_id = pick_representative_contact_id(
            literal_self_cids,
            display_names=display_names,
            known_usernames_by_cid=known_usernames_by_cid,
            fallback_labels=fallback_labels,
            policies=policies,
            inherit_defaults=inherit_defaults,
        )

    participant_map = load_conversation_participant_contact_ids(
        conn,
        dataset_id,
        conversation_ids_in_page,
        id_mm,
        self_contact_id=self_contact_id,
        nanp_idx=nanp_idx,
        display_names=display_names,
        known_usernames_by_cid=known_usernames_by_cid,
        fallback_labels=fallback_labels,
        policies=policies,
        inherit_defaults=inherit_defaults,
    )

    row_block: Set[str] = set(grant_block)
    name_block: Set[str] = set()
    for cid, pol in policies.items():
        if not inherit_defaults:
            continue
        if pol.get("row_visibility") == "exclude_from_grants":
            row_block.add(cid)
        if pol.get("name_visibility") == "hidden":
            name_block.add(cid)

    mode = "all"
    manifest_block_ids: Set[str] = set()
    manifest_allow_ids: Set[str] = set()
    if participation:
        mode = str(participation.params.get("mode") or "all")
        raw_ids = participation.params.get("contact_ids") or []
        if isinstance(raw_ids, list):
            if mode == "blocklist":
                manifest_block_ids = {str(x).strip() for x in raw_ids if str(x).strip()}
            elif mode == "allowlist":
                manifest_allow_ids = {str(x).strip() for x in raw_ids if str(x).strip()}
    match_mode = "thread_participants"
    if participation:
        match_mode = str(participation.params.get("match") or "thread_participants")

    row_block |= manifest_block_ids

    graph_labels: Dict[str, Dict[str, str]] = {}
    graph_pid: Set[str] = set()
    if can_resolve and names_enabled:
        for row in items:
            sid_g = str(row.get("sender_id") or "").strip()
            if sid_g and sid_g.lower() != "self":
                graph_pid.add(sid_g)
            alt_g = _metadata_chat_identifier(row)
            if alt_g:
                aa = str(alt_g).strip()
                if aa:
                    graph_pid.add(aa)
        if self_contact_id:
            graph_pid.add(self_contact_id)
        elif literal_self_cids:
            graph_pid.update(literal_self_cids)
        if graph_pid:
            try:
                graph_labels = resolve_participant_labels(
                    conn, dataset_id=dataset_id, participant_ids=sorted(graph_pid)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("resolve_participant_labels for UMA enrichment failed: %s", exc)
                graph_labels = {}

    out_rows: List[Dict[str, Any]] = []
    for row in items:
        sid = str(row.get("sender_id") or "").strip()
        cids = collect_message_contact_ids(row, id_mm, self_contact_id=self_contact_id, nanp_idx=nanp_idx)
        self_sender_cids = collect_contact_ids_for_sender(
            row.get("sender_id"),
            id_mm,
            self_contact_id=self_contact_id,
            nanp_idx=nanp_idx,
        )
        cid = (
            pick_representative_contact_id(
                cids,
                display_names=display_names,
                known_usernames_by_cid=known_usernames_by_cid,
                fallback_labels=fallback_labels,
                policies=policies,
                inherit_defaults=inherit_defaults,
            )
            if cids
            else None
        )
        # iMessage rows often duplicate the peer handle in metadata; that merges peer + owner into
        # ``cids``. Never treat the peer as the sender when ``sender_id`` is literally ``self``.
        if sid.lower() == "self":
            if self_contact_id and self_contact_id in cids:
                cid = self_contact_id
            elif self_sender_cids:
                cid = pick_representative_contact_id(
                    self_sender_cids,
                    display_names=display_names,
                    known_usernames_by_cid=known_usernames_by_cid,
                    fallback_labels=fallback_labels,
                    policies=policies,
                    inherit_defaults=inherit_defaults,
                )
            else:
                cid = None

        conv = str(row.get("conversation_id") or row.get("thread_id") or "").strip()
        if match_mode == "sender_only":
            row_contact_ids: Set[str] = {cid} if cid else set()
        else:
            row_contact_ids = set(participant_map.get(conv, set()))
            if cid:
                row_contact_ids.add(cid)

        if row_contact_ids and row_contact_ids.intersection(row_block):
            continue
        if mode == "allowlist":
            if not manifest_allow_ids:
                continue
            if not row_contact_ids.intersection(manifest_allow_ids):
                continue
        if grant_allow:
            if not row_contact_ids.intersection(grant_allow):
                continue

        new_row = dict(row)
        raw_graph = ""
        graph_dn = ""
        pipeline_dn = ""
        if can_resolve and names_enabled:
            if sid.lower() == "self" and canonical_owner_display_name:
                new_row["sender_display_name"] = canonical_owner_display_name
            elif cid:
                raw_graph = _uma_graph_display_name_for_row(graph_labels, row) if graph_labels else ""
                graph_dn = _graph_display_name_respects_name_policy(
                    raw_graph, cids, display_names, name_block
                )
                pipeline_dn = visible_label_for_contact(
                    cid,
                    display_names=display_names,
                    known_usernames_by_cid=known_usernames_by_cid,
                    fallback_labels=fallback_labels,
                )
                if cid not in name_block and cid not in grant_block:
                    dn = ""
                    owner_graph_key: Optional[str] = None
                    if sid.lower() == "self" and cid:
                        if self_contact_id is not None and cid == self_contact_id:
                            owner_graph_key = self_contact_id
                        elif self_contact_id is None and cid in self_sender_cids:
                            owner_graph_key = cid
                    if owner_graph_key is not None:
                        # Prefer graph resolution (identifier promotion across duplicate cards) over
                        # pipeline fallback labels, which are often raw phone strings.
                        dn = (_self_label or "").strip()
                        if not dn and graph_labels:
                            dn = _owner_graph_display_name(graph_labels, owner_graph_key)
                        if not dn:
                            dn = pipeline_dn
                    elif graph_dn:
                        dn = graph_dn
                    else:
                        dn = pipeline_dn
                    if dn:
                        new_row["sender_display_name"] = dn
        new_row["is_from_self"] = bool(new_row.get("is_from_self"))
        sid_lower = sid.strip().lower()
        new_row["sender_is_owner"] = bool(
            sid_lower == "self"
            or (effective_owner_contact_id is not None and cid == effective_owner_contact_id)
            or new_row.get("is_from_self")
        )

        out_rows.append(new_row)

    owner_messages_in_response = sum(1 for r in out_rows if r.get("sender_is_owner"))
    owner_display_for_response: Optional[str] = None
    if can_resolve and names_enabled:
        _ocand = (canonical_owner_display_name or "").strip()
        if not _ocand and effective_owner_contact_id:
            _ocand = (_self_label or "").strip()
            if not _ocand and graph_labels:
                _ocand = _owner_graph_display_name(graph_labels, effective_owner_contact_id)
            if not _ocand:
                _ocand = visible_label_for_contact(
                    effective_owner_contact_id,
                    display_names=display_names,
                    known_usernames_by_cid=known_usernames_by_cid,
                    fallback_labels=fallback_labels,
                )
        if _ocand and not _is_imessage_self_sentinel_label(_ocand):
            owner_display_for_response = _ocand
    owner_uid = (dataset_id or "").split(":", 1)[0] or None
    sidecar: Dict[str, Any] = {
        "message_owner": {
            "owner_user_id": owner_uid,
            "owner_contact_id": effective_owner_contact_id,
            "owner_display_name": owner_display_for_response,
            "owner_messages_in_this_response": owner_messages_in_response,
        }
    }

    return _apply_blackhole_to_message_rows(out_rows, blackhole_guard), sidecar
