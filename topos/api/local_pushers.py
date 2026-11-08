"""The local pusher doors: a batch write with ``write_id`` dedupe, and ``me``.

``POST /sources/{source_id}/ingest/batch`` is what the browser extension (and any
other app the owner enrolled with ``topos-node pushers add``) calls on a node
that runs with no account, instead of the control plane's
``/v1/ingestion/app_ingest``. It takes the same body shape minus the resource
(``{"records": [...], "write_id": "..."}``; ``Idempotency-Key`` is read as the
write id too, as the control plane reads it) and answers the relay's payload
(``write_id``, ``records_processed``, ``records_total``, ``errors``,
``deduplicated``). The dataset is the node's own (``resolve_owner_scope``): a
pusher never names one.

Who may call: ``auth.resolve_local_pusher`` — an enrolled app's key over loopback
TCP, or the owner socket. Then the app's allowlist: the source must be one the
owner named at enrolment (the control plane's ``check_app_ingest_source``). The
rows are written as the owner's capture app (``writer_class = owner_app``,
``writer_app_id = <app>``), the stamp the control plane gives the same app.

``GET /local-pushers/me`` tells an app what its key is good for; the extension's
options page uses it to confirm a pasted key.

CORS for these two paths is answered by :class:`LocalPusherCORS` for the exact
extension origins the owner enrolled, read on every request so a disconnect
closes the origin at once. It never answers ``*`` or a pattern; an origin that
is not enrolled is left to the app's ordinary CORS middleware, which does not
know it either.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..auth import resolve_local_pusher
from ..local_pushers import MAX_BATCH_RECORDS, PUSHER_CHANNEL, PusherGrant, source_not_allowed_detail

logger = logging.getLogger("topos.api.local_pushers")

router = APIRouter(tags=["local-pushers"])

_NO_STORE = {"Cache-Control": "no-store"}
#: The paths the extension-origin CORS layer answers for.
PUSHER_PATH_RE = re.compile(r"^/(sources/[^/]+/ingest/batch|local-pushers/me)$")
_ALLOW_HEADERS = "Authorization, Content-Type, Idempotency-Key"
_ALLOW_METHODS = "GET, POST, OPTIONS"


def _refusal(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"status": "error", "error": code, "message": message, **extra},
                        status_code=status, headers=_NO_STORE)


def _grant_of(principal) -> Optional[PusherGrant]:
    """The pusher's grant, or None for the owner on the socket (who may push any source)."""
    if getattr(principal, "channel", "") != PUSHER_CHANNEL:
        return None
    grant = getattr(principal, "app_grant", None)
    return grant if isinstance(grant, PusherGrant) else PusherGrant()


def _scope() -> Optional[Dict[str, str]]:
    from ..core.state import get_db_connection
    from ..ingestion.ai_tool_sync import resolve_owner_scope

    conn = get_db_connection()
    return resolve_owner_scope(conn) if conn is not None else None


@router.post("/sources/{source_id}/ingest/batch")
async def ingest_batch(
    source_id: str,
    request: Request,
    body: Optional[dict] = Body(default=None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    principal=Depends(resolve_local_pusher),
):
    from ..features.provenance.writer_class import WRITER_OWNER_APP
    from ..ingestion.inbox_drain import run_inbox_app_ingest
    from ..ingestion.local_push import ingest_push_batch
    from ..sources.definitions import accepts_app_ingest
    from ..sources.registry import REGISTRY

    source = REGISTRY.get(source_id)
    if source is None:
        return _refusal(404, "unknown_source", f"This Topos has no source named {source_id!r}.")
    if not accepts_app_ingest(source):
        return _refusal(400, "source_not_pushable",
                        f"Source {source_id!r} is not one an app pushes (delivery={source.delivery!r}).")
    grant = _grant_of(principal)
    app = str(getattr(principal, "client_id", "") or "") or None
    if grant is not None and not grant.may_write(source_id):
        return _refusal(403, "source_not_allowed", source_not_allowed_detail(app or "", source_id, grant))

    payload = body if isinstance(body, dict) else {}
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return _refusal(400, "records_required", "Send {\"records\": [...]} with at least one record.")
    if len(records) > MAX_BATCH_RECORDS:
        return _refusal(413, "batch_too_large", f"At most {MAX_BATCH_RECORDS} records per batch.")
    write_id = str(payload.get("write_id") or idempotency_key or "").strip() or None

    try:
        scope = await asyncio.to_thread(_scope)
    except Exception:  # noqa: BLE001 — no path or exception text leaves the node
        return _refusal(503, "owner_scope_unavailable", "The node's identity could not be read.")
    if scope is None:
        return _refusal(409, "not_paired", "This Topos has no identity yet; start it once and retry.")

    async def _run() -> Dict[str, Any]:
        return await ingest_push_batch(
            source_def=source,
            records=records,
            owner_user_id=scope["user_id"],
            dataset_id=scope["dataset_id"],
            writer_class=WRITER_OWNER_APP,
            writer_app_id=app,
            write_id=write_id,
        )

    result = await run_inbox_app_ingest(_run, write_id=write_id)
    if result["records_processed"] == 0 and not result["deduplicated"]:
        return _refusal(422, "all_records_failed",
                        f"All {result['records_total']} record(s) failed ingestion for source_id={source_id}.",
                        **result)
    return JSONResponse({"status": "ok", "source_id": source_id, **result}, headers=_NO_STORE)


@router.get("/local-pushers/me")
async def pusher_me(principal=Depends(resolve_local_pusher)):
    grant = _grant_of(principal)
    if grant is None:
        return JSONResponse({"status": "ok", "app": None, "owner": True, "sources": None, "origins": []},
                            headers=_NO_STORE)
    return JSONResponse(
        {"status": "ok", "app": principal.client_id, "owner": False,
         "sources": list(grant.sources), "origins": list(grant.origins)},
        headers=_NO_STORE,
    )


class LocalPusherCORS:
    """Pure-ASGI CORS for the pusher paths, keyed on the enrolled extension origins.

    Outermost of the app's middleware so a preflight from an enrolled origin is
    answered here and never reaches the app's static ``CORSMiddleware`` (which
    would 400 an origin it does not list). An origin that is not enrolled, a
    request with no ``Origin``, or any other path passes through untouched. The
    allow header is always the one exact origin that matched; ``Vary: Origin``
    keeps a cache from replaying it to another.
    """

    def __init__(self, app, *, origins=None) -> None:
        self.app = app
        self._origins = origins  # a callable for tests; None reads the database

    def _enrolled(self) -> frozenset:
        if self._origins is not None:
            return frozenset(self._origins())
        from ..core.state import get_db_connection
        from ..local_pushers import enrolled_origins

        try:
            return enrolled_origins(get_db_connection())
        except Exception:  # noqa: BLE001 — fail closed
            return frozenset()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not PUSHER_PATH_RE.match(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        origin = headers.get("origin", "").strip()
        if not origin or origin not in self._enrolled():
            await self.app(scope, receive, send)
            return
        cors = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (b"vary", b"Origin"),
        ]
        if scope.get("method") == "OPTIONS" and "access-control-request-method" in headers:
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": cors + [
                    (b"access-control-allow-methods", _ALLOW_METHODS.encode()),
                    (b"access-control-allow-headers", _ALLOW_HEADERS.encode()),
                    (b"access-control-max-age", b"600"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message.get("type") == "http.response.start":
                message = {**message, "headers": list(message.get("headers") or []) + cors}
            await send(message)

        await self.app(scope, receive, send_with_cors)
