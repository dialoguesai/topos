from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from .config.settings import settings

logger = logging.getLogger("topos.hosted_pool_lease")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _control_plane_http_base(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"Unsupported control plane websocket URL scheme: {parsed.scheme}")
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    netloc = parsed.netloc
    if not netloc:
        raise ValueError("Control plane websocket URL missing host")
    return f"{http_scheme}://{netloc}"


def _metadata_identity_token(audience: str) -> str:
    metadata_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
    )
    params = {"audience": audience, "format": "full"}
    headers = {"Metadata-Flavor": "Google"}
    with httpx.Client(timeout=5.0) as client:
        resp = client.get(metadata_url, params=params, headers=headers)
        resp.raise_for_status()
        token = str(resp.text or "").strip()
        if not token:
            raise RuntimeError("Metadata server returned an empty identity token")
        return token


@dataclass
class HostedPoolLease:
    connector_key: str
    lease_expires_at: Optional[datetime]
    lease_ttl_seconds: int


class HostedPoolLeaseClient:
    def __init__(self, *, control_plane_ws_url: str) -> None:
        self.control_plane_ws_url = control_plane_ws_url
        self.control_plane_http_base = _control_plane_http_base(control_plane_ws_url)
        self.issue_path = str(settings.hosted_pool_lease_issue_path or "/v1/system/pool-connectors/lease/issue")
        self.renew_path = str(settings.hosted_pool_lease_renew_path or "/v1/system/pool-connectors/lease/renew")
        self.revoke_path = str(settings.hosted_pool_lease_revoke_path or "/v1/system/pool-connectors/lease/revoke")
        self.pool_group = str(settings.hosted_pool_lease_pool_group or "default")
        self.instance_id = str(os.getenv("HOSTNAME") or "unknown-instance").strip()
        self.service_name = str(os.getenv("K_SERVICE") or "unknown-service").strip()
        self.revision = str(os.getenv("K_REVISION") or "").strip() or None
        self.lease: Optional[HostedPoolLease] = None

    def _build_url(self, path: str) -> str:
        normalized = f"/{str(path or '').lstrip('/')}"
        return f"{self.control_plane_http_base}{normalized}"

    def _audience(self) -> str:
        configured = str(settings.hosted_pool_lease_audience or "").strip()
        return configured or self.control_plane_http_base

    def _identity_token(self) -> str:
        return _metadata_identity_token(self._audience())

    async def issue(self) -> HostedPoolLease:
        url = self._build_url(self.issue_path)
        token = await asyncio.to_thread(self._identity_token)
        payload: Dict[str, Any] = {
            "service_name": self.service_name,
            "revision": self.revision,
            "instance_id": self.instance_id,
            "pool_group": self.pool_group,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            resp.raise_for_status()
            body = resp.json()
        connector_key = str(body.get("connector_key") or "").strip()
        if not connector_key:
            raise RuntimeError("Lease issue response missing connector_key")
        ttl = int(body.get("lease_ttl_seconds") or 300)
        lease = HostedPoolLease(
            connector_key=connector_key,
            lease_expires_at=_parse_iso_datetime(body.get("lease_expires_at")),
            lease_ttl_seconds=max(30, ttl),
        )
        self.lease = lease
        return lease

    async def renew(self) -> HostedPoolLease:
        if not self.lease:
            return await self.issue()
        url = self._build_url(self.renew_path)
        token = await asyncio.to_thread(self._identity_token)
        payload = {
            "connector_key": self.lease.connector_key,
            "service_name": self.service_name,
            "revision": self.revision,
            "instance_id": self.instance_id,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            resp.raise_for_status()
            body = resp.json()
        ttl = int(body.get("lease_ttl_seconds") or self.lease.lease_ttl_seconds)
        self.lease = HostedPoolLease(
            connector_key=self.lease.connector_key,
            lease_expires_at=_parse_iso_datetime(body.get("lease_expires_at")),
            lease_ttl_seconds=max(30, ttl),
        )
        return self.lease

    async def revoke(self) -> None:
        if not self.lease:
            return
        url = self._build_url(self.revoke_path)
        token = await asyncio.to_thread(self._identity_token)
        payload = {
            "connector_key": self.lease.connector_key,
            "service_name": self.service_name,
            "instance_id": self.instance_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hosted pool lease revoke failed: %s", exc)
        finally:
            self.lease = None
