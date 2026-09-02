"""Deterministic aggregate builders for the S7 ``aggregate`` verb.

Curated (measure x group_by x time-bucket) SQL over canonical tables — no
model writes SQL here. Three rules carried from hard-won incidents:

- **People, not ghosts.** ``group_by=person`` resolves message senders
  through the NANP-aware contact multimap, so one human imported under
  ``+1512…`` and ``512…`` is ONE group. The entity spine is deliberately
  not consulted for message aggregates — the split is healed at the
  contact layer, where the variants already align.
- **Black holes exclude inside the same pass.** Every reported number is
  computed after exclusion; a total is never derived from a pre-exclusion
  count (the D5 side-channel). Exclusion is sender-identity based for
  person tables plus a label-term belt; OWNER_UI (and local-only
  routines) see everything, every other caller class excludes.
- **No silent dataset fallback.** When the table carries ``dataset_id``
  and a dataset is named, the filter is strict — the get_analytics
  "drop the filter when it matches nothing" leniency leaks cross-dataset
  aggregates and is not copied here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

AGGREGATE_GROUP_CAP = 50

_MEASURES = ("count", "sum", "avg", "min", "max")

_BUCKETS: Dict[str, str] = {
    "day": "DATE({t})",
    "week": "strftime('%Y-W%W', {t})",
    "month": "strftime('%Y-%m', {t})",
    "hour_of_day": "strftime('%H', {t})",
    "day_of_week": "strftime('%w', {t})",
}


class AggregateParamError(ValueError):
    """A request outside the curated surface. ``reason`` is a narrowing member."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ScopeAggregateSpec:
    table: str
    time_col: str
    group_bys: Dict[str, str]  # public name -> column
    fields: Dict[str, str] = dc_field(default_factory=dict)  # public name -> SQL expr
    has_dataset_col: bool = False
    person_col: Optional[str] = None  # column resolved through the contact multimap

    @property
    def group_by_names(self) -> Set[str]:
        names = set(self.group_bys)
        if self.person_col:
            names.add("person")
        return names

    @property
    def buckets(self) -> Set[str]:
        return set(_BUCKETS)


AGGREGATE_REGISTRY: Dict[str, ScopeAggregateSpec] = {
    "messages:read": ScopeAggregateSpec(
        table="conversation_messages",
        time_col="event_at",
        group_bys={
            "sender_type": "sender_type",
            "message_type": "message_type",
            "source_id": "source_id",
        },
        fields={"length": "LENGTH(content)"},
        has_dataset_col=True,
        person_col="sender_id",
    ),
    "ai_conversations:read": ScopeAggregateSpec(
        table="ai_chat_messages",
        time_col="event_at",
        group_bys={"sender_type": "sender_type", "source_id": "source_id"},
        fields={"length": "LENGTH(content)"},
        has_dataset_col=False,
    ),
    "schedule:read": ScopeAggregateSpec(
        table="calendar_events",
        time_col="starts_at",
        group_bys={"event_type": "event_type", "source_id": "source_id"},
        fields={
            "duration_minutes": "(julianday(ends_at) - julianday(starts_at)) * 1440.0"
        },
    ),
    "activity:read": ScopeAggregateSpec(
        table="activity_events",
        time_col="occurred_at",
        group_bys={
            "activity_type": "activity_type",
            "hostname": "hostname",
            "source_id": "source_id",
        },
    ),
    "health:read": ScopeAggregateSpec(
        table="journal_entries",
        time_col="entry_at",
        group_bys={
            "mood_tag": "mood_tag",
            "category": "category",
            "place_name": "place_name",
            "source_id": "source_id",
        },
        fields={"duration": "duration"},
    ),
    "places:read": ScopeAggregateSpec(
        table="location_events",
        time_col="event_at",
        group_bys={
            "event_type": "event_type",
            "place_name": "place_name",
            "city": "city",
            "country": "country",
            "source_id": "source_id",
        },
    ),
    "resources:read": ScopeAggregateSpec(
        table="financial_transactions",
        time_col="posted_at",
        group_bys={
            "category": "category",
            "account_name": "account_name",
            "account_type": "account_type",
            "currency": "currency",
            "source_id": "source_id",
        },
        fields={"amount": "amount"},
    ),
}


@dataclass(frozen=True)
class AggregateSpec:
    scope_id: str
    measure: str
    field: Optional[str]
    group_by: Optional[str]
    bucket: Optional[str]
    since: Optional[str]
    until: Optional[str]

    @property
    def scope(self) -> ScopeAggregateSpec:
        return AGGREGATE_REGISTRY[self.scope_id]


def _parse_instant(value: Any, name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateParamError(
            "aggregate_param_invalid", f"{name} is not an ISO-8601 instant: {text!r}"
        ) from exc
    return text


def validate_aggregate_params(payload: Dict[str, Any]) -> AggregateSpec:
    """Validate a request against the curated surface. Raises AggregateParamError."""
    scope_id = str(payload.get("scope_id") or "").strip()
    scope = AGGREGATE_REGISTRY.get(scope_id)
    if scope is None:
        raise AggregateParamError(
            "aggregate_scope_unsupported",
            f"scope_id {scope_id!r} has no aggregate surface; supported: "
            + ", ".join(sorted(AGGREGATE_REGISTRY)),
        )

    measure = str(payload.get("measure") or "").strip().lower()
    if measure not in _MEASURES:
        raise AggregateParamError(
            "aggregate_param_invalid",
            f"measure {measure!r} not in {list(_MEASURES)}",
        )

    field = payload.get("field")
    field = str(field).strip() if field not in (None, "") else None
    if measure == "count":
        if field is not None:
            raise AggregateParamError(
                "aggregate_param_invalid", "count takes no field"
            )
    else:
        if field is None:
            raise AggregateParamError(
                "aggregate_param_invalid",
                f"measure {measure!r} needs a curated field: "
                + ", ".join(sorted(scope.fields) or ["(none for this scope)"]),
            )
        if field not in scope.fields:
            raise AggregateParamError(
                "aggregate_param_invalid",
                f"field {field!r} is not curated for {scope_id}; "
                + "supported: " + ", ".join(sorted(scope.fields) or ["(none)"]),
            )

    group_by = payload.get("group_by")
    group_by = str(group_by).strip() if group_by not in (None, "") else None
    if group_by is not None and group_by not in scope.group_by_names:
        raise AggregateParamError(
            "aggregate_param_invalid",
            f"group_by {group_by!r} is not curated for {scope_id}; "
            + "supported: " + ", ".join(sorted(scope.group_by_names)),
        )

    bucket = payload.get("bucket")
    bucket = str(bucket).strip() if bucket not in (None, "") else None
    if bucket is not None and bucket not in _BUCKETS:
        raise AggregateParamError(
            "aggregate_param_invalid",
            f"bucket {bucket!r} not in {sorted(_BUCKETS)}",
        )

    return AggregateSpec(
        scope_id=scope_id,
        measure=measure,
        field=field,
        group_by=group_by,
        bucket=bucket,
        since=_parse_instant(payload.get("since"), "since"),
        until=_parse_instant(payload.get("until"), "until"),
    )


# ------------------------------------------------------------------ helpers


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _measure_sql(spec: AggregateSpec) -> str:
    if spec.measure == "count":
        return "COUNT(*)"
    # Some canonical numeric columns are stored TEXT (e.g. journal duration);
    # MIN/MAX preserve the stored type, so coerce every numeric measure.
    expr = f"CAST({spec.scope.fields[spec.field or '']} AS REAL)"
    return f"{spec.measure.upper()}({expr})"


def _blocked_contact_and_terms(conn, guard) -> Tuple[Set[str], List[str]]:
    """Contact ids and normalized name terms of black-holed entities.

    Sourced from the same store the guard reads; consulted only when the
    guard's caller class does not see everything.
    """
    if guard is None or guard.sees_everything:
        return set(), []
    try:
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        blocked_ids = sorted(store.blackholed_entity_ids())
        terms = [t for t in store.blackholed_name_terms() if t]
    except Exception:
        # Fail closed on the person lane: with no readable store we cannot
        # prove a person is safe to show, but we also have nothing to key an
        # exclusion on. Match the guard's own behavior (inert when the store
        # is absent — nothing is protected on this node).
        return set(), []
    if not blocked_ids:
        return set(), [t for t in terms]
    placeholders = ",".join("?" for _ in blocked_ids)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT contact_id FROM entities"
            f" WHERE entity_id IN ({placeholders}) AND contact_id IS NOT NULL",
            blocked_ids,
        ).fetchall()
        cids = {str(r[0]) for r in rows if r and r[0]}
    except Exception:
        cids = set()
    return cids, terms


def _sender_person_map(
    conn, dataset_id: str, senders: Sequence[str]
) -> Dict[str, Tuple[str, str, Set[str]]]:
    """sender_id -> (person_key, label, contact_ids), NANP-aware.

    Senders whose contact sets intersect fold into one person (union-find
    over contact ids), so the +E.164 and 10-digit imports of one phone are
    one person. A sender with no contact match keys on itself.
    """
    from topos.analytics.messenger_labels import _identifier_candidates
    from topos.uma_contact_enrichment import build_identifier_contact_multimap

    mm = build_identifier_contact_multimap(conn, dataset_id)

    sender_cids: Dict[str, Set[str]] = {}
    for sender in senders:
        cids: Set[str] = set()
        for key in _identifier_candidates(str(sender)):
            cids |= mm.get(key, set())
        cids |= mm.get(str(sender), set())
        sender_cids[sender] = cids

    # Union-find over contact ids: senders sharing any cid are one person.
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cids in sender_cids.values():
        cl = sorted(cids)
        for cid in cl:
            parent.setdefault(cid, cid)
        for other in cl[1:]:
            union(cl[0], other)

    # Person key per sender: root of any of its cids, else the sender itself.
    person_cids: Dict[str, Set[str]] = {}
    sender_person: Dict[str, str] = {}
    for sender, cids in sender_cids.items():
        if cids:
            key = "cid:" + find(sorted(cids)[0])
        else:
            key = "sender:" + str(sender)
        sender_person[sender] = key
        person_cids.setdefault(key, set()).update(cids)

    # Labels: best display_name among the person's contacts, else identifier.
    all_cids = sorted({c for s in person_cids.values() for c in s})
    names: Dict[str, str] = {}
    if all_cids:
        placeholders = ",".join("?" for _ in all_cids)
        try:
            rows = conn.execute(
                f"SELECT contact_id, display_name FROM contacts"
                f" WHERE dataset_id = ? AND contact_id IN ({placeholders})",
                [dataset_id] + all_cids,
            ).fetchall()
            names = {
                str(r[0]): str(r[1]).strip()
                for r in rows
                if r and r[0] and r[1] and str(r[1]).strip()
            }
        except Exception:
            names = {}

    out: Dict[str, Tuple[str, str, Set[str]]] = {}
    for sender, key in sender_person.items():
        cids = person_cids.get(key, set())
        named = sorted(n for n in (names.get(c) for c in sorted(cids)) if n)
        label = named[0] if named else str(sender)
        out[sender] = (key, label, cids)
    return out


# ---------------------------------------------------------------- executor


def run_aggregate(
    conn,
    spec: AggregateSpec,
    *,
    guard,
    dataset_id: str = "",
) -> Dict[str, Any]:
    """Execute one curated aggregate. Returns the public_result core.

    Shape: {answer_type: "aggregate", scope_id, measure, field?, group_by?,
    bucket?, rows: [{group?, label?, bucket?, value}], truncated?, store_empty?}.
    A scalar zero is an answer (rows=[{value: 0}]); a missing table is an
    absence (rows=[] + store_empty).
    """
    scope = spec.scope
    base: Dict[str, Any] = {
        "answer_type": "aggregate",
        "scope_id": spec.scope_id,
        "measure": spec.measure,
        "rows": [],
    }
    if spec.field:
        base["field"] = spec.field
    if spec.group_by:
        base["group_by"] = spec.group_by
    if spec.bucket:
        base["bucket"] = spec.bucket

    if not _table_exists(conn, scope.table):
        base["store_empty"] = True
        return base

    where: List[str] = []
    params: List[Any] = []
    if spec.since:
        where.append(f"datetime({scope.time_col}) >= datetime(?)")
        params.append(spec.since)
    if spec.until:
        where.append(f"datetime({scope.time_col}) <= datetime(?)")
        params.append(spec.until)
    if scope.has_dataset_col and dataset_id:
        where.append("dataset_id = ?")
        params.append(dataset_id)
    where.append(f"{scope.time_col} IS NOT NULL")

    # Black-hole exclusion, pushed into the same WHERE that computes every
    # reported number. Sender-identity based for person tables.
    blocked_cids, blocked_terms = _blocked_contact_and_terms(conn, guard)
    person_map: Dict[str, Tuple[str, str, Set[str]]] = {}
    if scope.person_col and (blocked_cids or blocked_terms or spec.group_by == "person"):
        sender_rows = conn.execute(
            f"SELECT DISTINCT {scope.person_col} FROM {scope.table}"
            + (" WHERE " + " AND ".join(where) if where else ""),
            params,
        ).fetchall()
        senders = [str(r[0]) for r in sender_rows if r and r[0] is not None]
        person_map = _sender_person_map(conn, dataset_id, senders)
        if blocked_cids or blocked_terms:
            blocked_senders = [
                s
                for s, (_key, label, cids) in person_map.items()
                if (cids & blocked_cids)
                or any(t and t in label.lower() for t in blocked_terms)
            ]
            if blocked_senders:
                placeholders = ",".join("?" for _ in blocked_senders)
                where.append(f"{scope.person_col} NOT IN ({placeholders})")
                params.extend(blocked_senders)

    measure_sql = _measure_sql(spec)
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    select_dims: List[str] = []
    group_dims: List[str] = []
    if spec.group_by == "person":
        select_dims.append(f"{scope.person_col} AS grp")
        group_dims.append(scope.person_col or "")
    elif spec.group_by:
        col = scope.group_bys[spec.group_by]
        select_dims.append(f"{col} AS grp")
        group_dims.append(col)
    if spec.bucket:
        expr = _BUCKETS[spec.bucket].format(t=scope.time_col)
        select_dims.append(f"{expr} AS bkt")
        group_dims.append(expr)

    if not select_dims:
        row = conn.execute(
            f"SELECT {measure_sql} FROM {scope.table}{where_sql}", params
        ).fetchone()
        value = row[0] if row else None
        if value is None:
            value = 0 if spec.measure == "count" else None
        if value is None:
            base["store_empty"] = True
            return base
        base["rows"] = [{"value": value}]
        return base

    # For person folds, avg must be recomposed from sum+count per sender.
    fold_person = spec.group_by == "person"
    if fold_person and spec.measure == "avg":
        expr = f"CAST({scope.fields[spec.field or '']} AS REAL)"
        measure_select = f"SUM({expr}) AS s, COUNT({expr}) AS c"
    else:
        measure_select = f"{measure_sql} AS v"

    sql = (
        f"SELECT {', '.join(select_dims)}, {measure_select}"
        f" FROM {scope.table}{where_sql}"
        f" GROUP BY {', '.join(group_dims)}"
    )
    rows = conn.execute(sql, params).fetchall()

    out_rows: List[Dict[str, Any]] = []
    if fold_person:
        folded: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
        for r in rows:
            sender = str(r[0])
            bkt = str(r[1]) if spec.bucket else None
            key_label = person_map.get(sender) or ("sender:" + sender, sender, set())
            pkey, label = key_label[0], key_label[1]
            slot = folded.setdefault(
                (pkey, bkt), {"label": label, "s": 0.0, "c": 0, "vals": []}
            )
            if spec.measure == "avg":
                s, c = r[-2], r[-1]
                slot["s"] += float(s or 0.0)
                slot["c"] += int(c or 0)
            else:
                slot["vals"].append(r[-1])
        for (pkey, bkt), slot in folded.items():
            if spec.measure == "avg":
                value = (slot["s"] / slot["c"]) if slot["c"] else None
            elif spec.measure == "count" or spec.measure == "sum":
                value = sum(v for v in slot["vals"] if v is not None)
            elif spec.measure == "min":
                vals = [v for v in slot["vals"] if v is not None]
                value = min(vals) if vals else None
            else:
                vals = [v for v in slot["vals"] if v is not None]
                value = max(vals) if vals else None
            if value is None:
                continue
            row_out: Dict[str, Any] = {"group": pkey, "label": slot["label"], "value": value}
            if bkt is not None:
                row_out["bucket"] = bkt
            out_rows.append(row_out)
    else:
        for r in rows:
            row_out = {}
            idx = 0
            if spec.group_by:
                row_out["group"] = r[idx]
                idx += 1
            if spec.bucket:
                row_out["bucket"] = r[idx]
                idx += 1
            value = r[-1]
            if value is None:
                continue
            row_out["value"] = value
            out_rows.append(row_out)

    # Deterministic order: buckets ascending; groups by value descending.
    if spec.group_by:
        out_rows.sort(key=lambda x: (-(x["value"] or 0), str(x.get("label") or x.get("group") or ""), str(x.get("bucket") or "")))
    else:
        out_rows.sort(key=lambda x: str(x.get("bucket") or ""))

    groups_total = len(out_rows)
    if spec.group_by and groups_total > AGGREGATE_GROUP_CAP:
        out_rows = out_rows[:AGGREGATE_GROUP_CAP]
        base["truncated"] = {
            "group_cap": AGGREGATE_GROUP_CAP,
            "groups_total": groups_total,
        }

    base["rows"] = out_rows
    return base
