"""The ``aggregate`` verb — deterministic numbers over curated scope surfaces.

The third verb of the query architecture: ``query`` retrieves, ``aggregate``
counts. No model writes SQL; the surface is the curated registry in
:mod:`topos.query.aggregate`. The handler owns its whole envelope because it
bypasses the retrieval orchestrator: it builds the narrowing ledger, stamps
honesty fields inside ``public_result`` (the model reads public_result, not
the transport envelope), and walks ``validate_public_result`` itself.

Principal policy (channel-verified, never read from the payload): grantee
requests and THIRD_PARTY principals are denied — aggregates are statistical
reads over the raw substrate, and the raw substrate is deny-by-default for
non-native callers (S1). owner_app and owner_automation are served; the
black-hole guard still excludes protected people for every caller class
except the owner's own UI.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

import topos.core.handlers as hub

from ...principal import THIRD_PARTY, current_principal
from ...features.lifecycle.blackhole_guard import guard_from_message
from ...query.aggregate import (
    AggregateParamError,
    run_aggregate,
    validate_aggregate_params,
)
from ...query.narrowing import (
    CAUSE_GATE_VETOED,
    CAUSE_NO_MATCH,
    CAUSE_SCOPE_DENIED,
    CAUSE_STORE_EMPTY,
    STAGE_GRANT,
    STAGE_RETRIEVAL,
    NarrowingLedger,
)
from ...query.session_utils import validate_public_result
from .registry import handles

logger = logging.getLogger(__name__)


def _deny(
    req_id: Any,
    reason: str,
    *,
    cause: str,
    query_session_id: str,
    detail: str = "",
) -> Dict[str, Any]:
    ledger = NarrowingLedger()
    ledger.empty(cause, stage=STAGE_GRANT, reason=reason, detail=detail)
    return {
        "id": req_id,
        "status": "ok",
        "payload": {
            "turn_outcome": "denied",
            "deny_reason": reason,
            "public_result": None,
            "query_session_id": query_session_id,
            "narrowing": {**ledger.as_public(), "result_empty": True},
        },
    }


@handles("aggregate")
async def handle_aggregate(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    query_session_id = str(
        payload.get("query_session_id") or f"agg_{uuid.uuid4()}"
    )

    # Grantee requests never reach this verb, and the flag is checked before
    # anything else so the denial carries no scope-shaped information.
    if bool(payload.get("is_grantee_request")) or payload.get("resource_id"):
        return _deny(
            req_id,
            "aggregate_principal_denied",
            cause=CAUSE_SCOPE_DENIED,
            query_session_id=query_session_id,
        )
    principal = current_principal()
    if principal is not None and principal.cls == THIRD_PARTY:
        return _deny(
            req_id,
            "aggregate_principal_denied",
            cause=CAUSE_SCOPE_DENIED,
            query_session_id=query_session_id,
        )

    try:
        spec = validate_aggregate_params(payload)
    except AggregateParamError as exc:
        cause = (
            CAUSE_SCOPE_DENIED
            if exc.reason == "aggregate_scope_unsupported"
            else CAUSE_GATE_VETOED
        )
        return _deny(
            req_id,
            exc.reason,
            cause=cause,
            query_session_id=query_session_id,
            detail=str(exc),
        )

    try:
        conn = hub.get_db_connection()
        if conn is None:
            return {"id": req_id, "status": "error", "error": "no database"}

        dataset_id = str(payload.get("dataset_id") or "")
        if not dataset_id:
            try:
                from .common import get_user_id, settings

                user_id = get_user_id(conn)
                if user_id:
                    dataset_id = f"{user_id}:{settings.topos_default_dataset_id}"
            except Exception:  # noqa: BLE001
                dataset_id = ""

        guard = guard_from_message(conn, message)
        ledger = NarrowingLedger()
        core = run_aggregate(conn, spec, guard=guard, dataset_id=dataset_id)

        store_empty = bool(core.pop("store_empty", False))
        if store_empty:
            ledger.empty(
                CAUSE_STORE_EMPTY,
                stage=STAGE_RETRIEVAL,
                reason="scope_stores_hold_no_rows",
            )
            core["empty_cause"] = CAUSE_STORE_EMPTY
        elif not core.get("rows"):
            ledger.empty(
                CAUSE_NO_MATCH,
                stage=STAGE_RETRIEVAL,
                reason="no_row_matched_the_request",
            )
            core["empty_cause"] = CAUSE_NO_MATCH
        else:
            ledger.record(STAGE_RETRIEVAL, "contributed", "aggregate_lane")

        validate_public_result(core)

        try:
            from .common import record_mcp_request

            record_mcp_request(
                conn,
                "aggregate",
                source="control_plane",
                requester_id=str(payload.get("requester_id") or "") or None,
            )
        except Exception:  # noqa: BLE001
            logger.debug("aggregate: audit record failed", exc_info=True)

        result_empty = not core.get("rows")
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "turn_outcome": "live_query",
                "public_result": core,
                "scope_id": spec.scope_id,
                "query_session_id": query_session_id,
                "narrowing": {**ledger.as_public(), "result_empty": result_empty},
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("aggregate failed")
        return {"id": req_id, "status": "error", "error": str(exc)}
