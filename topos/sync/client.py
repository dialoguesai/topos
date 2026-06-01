"""Sync client for connecting to control plane sync relay."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from typing import Any, Callable, Dict, Optional

import certifi
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ..config.settings import settings
from ..core.connection_resilience import (
    ConnectionSnapshot,
    ConnectionState,
    ExponentialBackoff,
    FailureCategory,
    ResilienceConfig,
    classify_connection_error,
    is_fatal_connection_category,
    utc_now_iso,
)

logger = logging.getLogger("topos.sync")


class SyncClient:
    """Client for syncing encrypted ops with the control plane relay."""

    def __init__(
        self,
        sync_url: str,
        api_key: str,
        user_id: str,
        dataset_id: str,
        on_op_received: Callable[[Dict[str, Any]], Any],
        verify_ssl: bool = True,
    ):
        self.sync_url = sync_url
        self.api_key = api_key
        self.user_id = user_id
        self.dataset_id = dataset_id
        self.on_op_received = on_op_received
        self.verify_ssl = verify_ssl

        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_op_ts: Optional[str] = None
        self._state: ConnectionState = "idle"
        self._state_changed_at: str | None = utc_now_iso()
        self._last_connected_at: str | None = None
        self._last_disconnected_at: str | None = None
        self._last_failure_category: FailureCategory = "none"
        self._last_failure_reason: str = ""
        self._attempt = 0
        self._consecutive_failures = 0
        self._ready = asyncio.Event()
        self._backoff = ExponentialBackoff(
            ResilienceConfig(
                initial_backoff_s=max(0.1, float(settings.connection_retry_initial_seconds)),
                max_backoff_s=max(1.0, float(settings.connection_retry_max_seconds)),
                jitter_ratio=max(0.0, float(settings.connection_retry_jitter_ratio)),
            )
        )

    def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        self._state_changed_at = utc_now_iso()

    def get_connection_status(self) -> dict[str, Any]:
        snapshot = ConnectionSnapshot(
            state=self._state,
            connected=self._connected,
            attempt=self._attempt,
            consecutive_failures=self._consecutive_failures,
            last_failure_category=self._last_failure_category,
            last_failure_reason=self._last_failure_reason,
            last_state_change_at=self._state_changed_at,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
        )
        return snapshot.to_dict()

    def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("Sync client already started")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("Sync client started")

    async def wait_until_connected(self, timeout_s: float | None = None) -> bool:
        timeout = float(timeout_s) if timeout_s is not None else float(settings.connection_readiness_timeout_seconds)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.1, timeout))
            return True
        except TimeoutError:
            return False

    async def stop(self) -> None:
        self._set_state("stopping")
        self._stop.set()
        if self._ws:
            await self._ws.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        self._connected = False
        self._ready.clear()
        self._set_state("idle")
        logger.info("Sync client stopped")

    async def _run(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        ssl_context = None
        if self.sync_url.startswith("wss://"):
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            if not self.verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        while not self._stop.is_set():
            self._set_state("connecting")
            self._attempt += 1
            try:
                async with connect(self.sync_url, additional_headers=headers, ssl=ssl_context) as ws:
                    self._ws = ws
                    self._connected = True
                    self._set_state("connected")
                    self._ready.set()
                    self._backoff.reset()
                    self._last_failure_category = "none"
                    self._last_failure_reason = ""
                    self._consecutive_failures = 0
                    self._last_connected_at = utc_now_iso()
                    logger.info("Sync client connected to relay")

                    await self._send_connect()

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            await self._handle_message(data)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Failed to handle sync message: %s", exc)
            except ConnectionClosed as exc:
                self._record_failure(exc)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(exc)
            finally:
                self._ws = None
                self._connected = False
                self._ready.clear()
                if not self._stop.is_set() and self._state != "stopping":
                    self._last_disconnected_at = utc_now_iso()
            if self._stop.is_set():
                break
            delay = self._backoff.next_delay()
            self._set_state("degraded" if is_fatal_connection_category(self._last_failure_category) else "backing_off")
            logger.warning(
                "Sync reconnect scheduled state=%s attempt=%d failures=%d category=%s delay_s=%.2f reason=%s",
                self._state,
                self._attempt,
                self._consecutive_failures,
                self._last_failure_category,
                delay,
                self._last_failure_reason,
            )
            await self._wait_for_stop_or_timeout(delay)
        self._set_state("idle")

    def _record_failure(self, exc: BaseException) -> None:
        category, reason = classify_connection_error(exc)
        self._last_failure_category = category
        self._last_failure_reason = reason
        self._consecutive_failures += 1
        if not self._stop.is_set():
            logger.warning(
                "Sync connection failed category=%s failures=%d reason=%s",
                category,
                self._consecutive_failures,
                reason,
            )

    async def _wait_for_stop_or_timeout(self, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=timeout_s)
        except TimeoutError:
            return

    async def _send_connect(self) -> None:
        message = {
            "type": "sync_connect",
            "user_id": self.user_id,
            "dataset_id": self.dataset_id,
            "last_op_ts": self._last_op_ts,
        }
        await self._ws.send(json.dumps(message))
        logger.debug("Sent sync_connect for user: %s, dataset: %s", self.user_id, self.dataset_id)

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        msg_type = data.get("type")

        if msg_type == "sync_connected":
            logger.info("Sync connected for dataset: %s", data.get("dataset_id"))
        elif msg_type == "sync_op":
            op = data.get("op")
            if op:
                result = self.on_op_received(op)
                if asyncio.iscoroutine(result):
                    await result
                self._last_op_ts = op.get("hlc_ts")
                try:
                    await self._send_json_with_retry({"type": "sync_cursor", "op": op})
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to send sync_cursor: %s", exc)
        elif msg_type == "sync_ack":
            logger.debug("Op acknowledged: %s", data.get("op_id"))
        elif msg_type == "error":
            logger.error("Sync error: %s", data.get("error"))

    async def _send_json_with_retry(self, payload: Dict[str, Any]) -> None:
        attempts = max(1, int(settings.sync_cursor_retry_attempts))
        delay = max(0.0, float(settings.sync_cursor_retry_delay_seconds))
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if not self._ws:
                break
            try:
                await self._ws.send(json.dumps(payload))
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(delay)
        if last_exc:
            raise last_exc

    async def send_op(self, op: Dict[str, Any]) -> None:
        if not self._connected or not self._ws:
            logger.warning("Sync client not connected, cannot send op")
            return

        op_copy = op.copy()
        if "ciphertext" in op_copy and isinstance(op_copy["ciphertext"], bytes):
            op_copy["ciphertext"] = base64.b64encode(op_copy["ciphertext"]).decode("utf-8")

        message = {"type": "sync_op", "op": op_copy}
        try:
            await self._send_json_with_retry(message)
            logger.debug("Sent op to relay: %s", op.get("op_id", "unknown")[:8])
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send op to relay: %s", exc)

    def is_connected(self) -> bool:
        return self._connected
