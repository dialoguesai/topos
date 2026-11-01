"""Opt-in loopback provider gateway. Never imported by the normal node startup.

Run with ``python -m topos.compute.gateway --help``. This initial deployment is
local-only and requires explicit authentication. It is not a transparent proxy
for every existing caller or a remote-compute deployment recipe.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx

from .journal import OwnershipJournal, disk
from .provider import ProviderAuthority, Rejected


class ProviderResponse(StreamingResponse):
    def __init__(self, authority, job, **kwargs):
        self.authority, self.job = authority, job
        super().__init__(job.chunks(), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also covers disconnect before the iterator starts and send errors.
            self.authority.detach(self.job)


def local_provider_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("This gateway requires a literal loopback HTTP provider")
    port = parsed.port or 80
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def create_app(*, upstream: str, state_dir: Path, token: str, active=1, waiting=4,
               queue_seconds=30.0, execution_seconds=300.0, transport=None,
               shutdown_grace=5.0) -> FastAPI:
    upstream = local_provider_url(upstream)
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("An explicit ASCII gateway token of at least 32 characters is required")

    @asynccontextmanager
    async def lifespan(app):
        journal = await disk(OwnershipJournal, state_dir, hashlib.sha256(upstream.encode()).hexdigest())
        authority = None
        try:
            async with httpx.AsyncClient(base_url=upstream, transport=transport, trust_env=False,
                                         follow_redirects=False, timeout=httpx.Timeout(60, connect=5)) as client:
                authority = ProviderAuthority(journal, client, active=active, waiting=waiting,
                                              queue_seconds=queue_seconds, execution_seconds=execution_seconds)
                app.state.provider_authority = authority
                try:
                    yield
                finally:
                    await authority.close(shutdown_grace)
        finally:
            if authority is None:
                await disk(journal.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request):
        received = request.headers.get("authorization", "").encode()
        return hmac.compare_digest(received, ("Bearer " + token).encode())

    def error(reason):
        return JSONResponse({"error": str(reason), "error_type": reason.code,
                             "detail": reason.detail(), "admission_reason": reason.reason}, status_code=503)

    @app.get("/admission")
    async def diagnostic(request: Request):
        if not authorized(request):
            return Response(status_code=401)
        return app.state.provider_authority.snapshot()

    @app.post("/api/{kind}")
    async def generate(kind: str, request: Request):
        if not authorized(request):
            return Response(status_code=401)
        if kind not in {"chat", "generate"}:
            return Response(status_code=404)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 1048576:
                        return Response(status_code=413)
            data = json.loads(body)
            if (not isinstance(data, dict) or not isinstance(data.get("model"), str)
                    or not data["model"].strip() or type(data.get("stream", True)) is not bool):
                return Response(status_code=400)
        except (ValueError, TimeoutError):
            return Response(status_code=400)
        authority = app.state.provider_authority
        try:
            job = authority.submit(f"/api/{kind}", bytes(body), data.get("stream", True))
        except Rejected as reason:
            return error(reason)

        async def disconnected():
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            await asyncio.wait((job.headers, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher.done():
                authority.detach(job)
                return Response(status_code=499)
            status, content_type = job.headers.result()
        except Rejected as reason:
            return error(reason)
        except BaseException:
            authority.detach(job)
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        return ProviderResponse(authority, job, status_code=status, media_type=content_type,
                                headers={"X-Topos-Operation-Id": job.id})

    return app


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Experimental local provider admission; never changes node routing")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--active", type=int, default=1)
    parser.add_argument("--waiting", type=int, default=4)
    args = parser.parse_args()
    upstream = local_provider_url(args.upstream)
    if urlsplit(upstream).port == args.port:
        parser.error("Gateway and provider ports must differ")
    app = create_app(upstream=upstream, state_dir=args.state_dir,
                     token=os.environ.get("TOPOS_PROVIDER_GATEWAY_TOKEN", ""),
                     active=args.active, waiting=args.waiting)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
