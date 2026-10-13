"""Purpose-specific readiness judgments over normalized, source-backed observations.

This module does not extract meaning from messages or infer availability from warmth.
An adapter must resolve identity, audience, purpose, attribution and source event clocks
before supplying observations. Missing adapter coverage is not missing source data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

POLICY_VERSION = "bench-readiness-1"
# An upper bound on observation freshness, not an invented promise expiry. Explicit
# validity must also cover the assessment time; changing this policy needs a new version.
MAX_AGE_DAYS = 30
_VALUES = {
    "willingness": {"offered", "withdrawn", "fulfilled"},
    "availability": {"available", "unavailable"},
}


def _instant(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def assess_readiness(*, person_id: str, purpose_id: str, dataset_id: str,
                     observations: Iterable[Mapping[str, Any]] = (),
                     as_of: datetime, coverage: str = "not_integrated") -> dict[str, Any]:
    """Evaluate willingness and availability independently of ability and purpose fit.

    Observations must name dimension/value, person/purpose/dataset, speaker=person,
    recipient=owner, assertion_mode=direct, event_at, valid_until, conditions_status
    (none/met/unmet/unknown), and source {table, record_id}. Duplicate observations
    cannot create confidence. The newest attributable observation wins per dimension;
    tied contradictory observations remain conflicted. Expired newer evidence never
    revives an older offer. All intervals are [event_at, valid_until).

    coverage describes adapter execution, not whether the corpus contains an answer.
    Only a completed adapter read can support a positive readiness judgment.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if coverage not in {"not_integrated", "partial", "complete"}:
        raise ValueError("unknown readiness coverage")
    now = as_of.astimezone(timezone.utc)
    by_dimension: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {
        key: [] for key in _VALUES
    }
    rejected = []
    for observation in observations:
        reason = None
        dimension = observation.get("dimension")
        source = observation.get("source")
        event_at = _instant(observation.get("event_at"))
        if (not person_id or not purpose_id or not dataset_id or
                any(observation.get(key) != value for key, value in (
                    ("person_id", person_id), ("purpose_id", purpose_id),
                    ("dataset_id", dataset_id)))):
            reason = "scope_mismatch"
        elif observation.get("speaker_id") != person_id or observation.get("recipient") != "owner":
            reason = "not_person_to_owner"
        elif observation.get("assertion_mode") != "direct":
            reason = "not_direct_assertion"
        elif dimension not in _VALUES or observation.get("value") not in _VALUES[dimension]:
            reason = "unsupported_observation"
        elif not isinstance(source, Mapping) or not source.get("table") or not source.get("record_id"):
            reason = "missing_source_reference"
        elif event_at is None:
            reason = "unqualified_event_clock"
        elif event_at > now:
            reason = "future_observation"
        if reason:
            # Do not echo out-of-scope record IDs or source contents in diagnostics.
            rejected.append(reason)
        else:
            by_dimension[dimension].append((event_at, observation))

    dimensions = {}
    for dimension, rows in by_dimension.items():
        if not rows:
            dimensions[dimension] = {"status": "unknown", "reason": "no_qualified_observation", "evidence": []}
            continue
        latest = max(at for at, _ in rows)
        newest = [row for at, row in rows if at == latest]
        judgments = []
        evidence = []
        for row in newest:
            end = _instant(row.get("valid_until"))
            conditions = row.get("conditions_status")
            if latest < now - timedelta(days=MAX_AGE_DAYS):
                status, reason = "unknown", "stale_observation"
            elif end is None or end <= latest:
                status, reason = "unknown", "unqualified_validity_window"
            elif now >= end:
                status, reason = "unknown", "expired_observation"
            elif conditions not in {"none", "met"}:
                status, reason = "conditional", "conditions_unresolved"
            elif row["value"] in {"withdrawn", "fulfilled", "unavailable"}:
                status, reason = "not_ready", row["value"]
            else:
                status, reason = "supported", row["value"]
            judgments.append((status, reason))
            ref = {"table": row["source"]["table"], "record_id": row["source"]["record_id"],
                   "event_at": row["event_at"], "valid_until": row.get("valid_until")}
            if ref not in evidence:
                evidence.append(ref)
        if len(set(judgments)) > 1:
            status, reason = "conflicted", "contradictory_latest_observations"
        else:
            status, reason = judgments[0]
        dimensions[dimension] = {"status": status, "reason": reason, "evidence": evidence}

    states = {value["status"] for value in dimensions.values()}
    # Gaps and rejected observations can conceal a later retraction. They must never
    # become a positive assertion merely because an older positive remains readable.
    if "conflicted" in states:
        status = "conflicted"
    elif "not_ready" in states:
        status = "not_ready"
    elif coverage != "complete" or rejected:
        status = "unknown"
    elif "conditional" in states:
        status = "conditional"
    elif states == {"supported"}:
        status = "supported"
    else:
        status = "unknown"
    reasons = [value["reason"] for value in dimensions.values() if value["status"] != "supported"]
    if coverage != "complete":
        reasons.append("readiness_adapter_" + coverage)
    if rejected:
        reasons.append("observations_rejected")
    return {"policy_version": POLICY_VERSION, "status": status,
            "person_id": person_id, "purpose_id": purpose_id, "dataset_id": dataset_id,
            "as_of": now.isoformat(), "coverage": coverage,
            "dimensions": dimensions, "reason_codes": sorted(set(reasons)),
            "rejected_reasons": sorted(set(rejected))}


def assess_bench_candidate(purpose: Mapping[str, Any], candidate: Mapping[str, Any],
                           readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Keep an inferred shortlist distinct from a qualified, ready bench member.

    'supported' purpose/fit inputs must come from a qualified adapter; today's goal
    and lexical-match adapters intentionally emit unverified/inferred statuses.
    """
    reasons = []
    if (not candidate.get("person_id") or not purpose.get("role_shape_id") or
            readiness.get("person_id") != candidate.get("person_id") or
            readiness.get("purpose_id") != purpose.get("role_shape_id")):
        reasons.append("readiness_scope_mismatch")
    if purpose.get("need_status") != "supported_current_need":
        reasons.append("current_need_unverified")
    if candidate.get("fit_status") != "supported_purpose_match":
        reasons.append("purpose_fit_unverified")
    if candidate.get("capability_status") != "demonstrated":
        reasons.append("capability_unverified")
    if readiness.get("status") != "supported":
        reasons.append("readiness_" + str(readiness.get("status", "unknown")))
    return {"status": "candidate" if reasons else "ready", "reason_codes": reasons}
