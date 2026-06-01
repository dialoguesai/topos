from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .__version__ import __version__
from .api import (
    analytics as analytics_routes,
    app_registry as app_registry_routes,
    backup as backup_routes,
    compute_remote as compute_remote_routes,
    data_commit as data_commit_routes,
    db as db_routes,
    device as device_routes,
    enrichment as enrichment_routes,
    health as health_routes,
    ingestion_compat as ingestion_compat_routes,
    ingestion_api as ingestion_routes,
    ingestion_sources as ingestion_sources_routes,
    local_mcp as local_mcp_routes,
    llm as llm_routes,
    messenger_analytics as messenger_analytics_routes,
    query_api as query_routes,
    source_install as source_install_routes,
    sources as sources_routes,
    sync as sync_routes,
    uma_data as uma_data_routes,
    user_identity as user_identity_routes,
    usage as usage_routes,
    ui_config as ui_config_routes,
    data_explorer_table_prefs as data_explorer_table_prefs_routes,
    sanitization_ollama_config as sanitization_ollama_config_routes,
    filter_lab as filter_lab_routes,
)
from .config.settings import settings
from .core.logging import configure_logging
from .core import state
from .core.handlers import handle_control_plane_request
from .control_plane_client import ControlPlaneClient
from .engine.registration import build_engine_heartbeat_message, build_engine_register_message
from .hosted_pool_lease import HostedPoolLeaseClient
from .services.container import get_services
from .startup_banner import emit_startup_banner
from .sync import SyncClient
from .sync_handlers import handle_sync_op

configure_logging()
logger = logging.getLogger("topos.app")

app = FastAPI(
    title="Topos",
    description="Topos node: Topos Database (data plane) and Topos Engine (compute plane), typically co-deployed in this process.",
    version=__version__,
)


def _log_runtime_banner() -> None:
    emit_startup_banner(
        lambda line: print(line, flush=True),
        version=__version__,
        mode="uvicorn",
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.allowed_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_routes.router)
app.include_router(local_mcp_routes.router)
app.include_router(llm_routes.router)
app.include_router(db_routes.router)
app.include_router(sync_routes.router)
app.include_router(device_routes.router)
app.include_router(backup_routes.router)
app.include_router(analytics_routes.router)
app.include_router(sources_routes.router)
app.include_router(source_install_routes.router, prefix="/v1")
app.include_router(app_registry_routes.router)
app.include_router(ingestion_compat_routes.router)
app.include_router(enrichment_routes.router, prefix="/v1")
app.include_router(ingestion_routes.router, prefix="/v1")
app.include_router(ingestion_sources_routes.router)
app.include_router(query_routes.router, prefix="/v1")
app.include_router(messenger_analytics_routes.router, prefix="/v1")
app.include_router(uma_data_routes.router)
app.include_router(usage_routes.router)
app.include_router(ui_config_routes.router)
app.include_router(data_explorer_table_prefs_routes.router)
app.include_router(user_identity_routes.router)
app.include_router(sanitization_ollama_config_routes.router)
app.include_router(filter_lab_routes.router)
app.include_router(compute_remote_routes.router)
app.include_router(data_commit_routes.router)


@app.on_event("startup")
async def startup_event() -> None:
    _log_runtime_banner()
    logger.info("CORS allowed origins: %s", settings.allowed_origins)
    if settings.allowed_origin_regex:
        logger.info("CORS allowed origin regex: %s", settings.allowed_origin_regex)
    logger.info("Runtime Python executable: %s", sys.executable)
    logger.info(
        "Runtime deps available: transformers=%s torch=%s",
        bool(importlib.util.find_spec("transformers")),
        bool(importlib.util.find_spec("torch")),
    )
    # Tests may inject an in-memory connection before startup.
    # Avoid initializing file-backed services in that case.
    if state.db_conn is None:
        _ = get_services()
    # Run Stage 9 column renames at startup so request handlers never block the event loop on migration.
    try:
        from .core.state import db_conn, get_db_connection
        from .storage.db.migrations.stage9_column_renames import run_stage9_migrations
        # Respect pre-injected test connections; avoid replacing test DB handles during startup.
        conn = db_conn if db_conn is not None else get_db_connection()
        if conn:
            result = run_stage9_migrations(conn)
            if result.get("applied"):
                logger.info("Stage 9 migrations applied at startup: %d renames", len(result["applied"]))
    except Exception as e:
        logger.debug("Stage 9 migrations at startup (non-fatal): %s", e)
    if settings.topos_control_plane_url:
        if settings.hosted_pool_lease_enabled:
            try:
                state.hosted_pool_lease_client = HostedPoolLeaseClient(
                    control_plane_ws_url=settings.topos_control_plane_url
                )
                lease = await state.hosted_pool_lease_client.issue()
                settings.topos_key = lease.connector_key
                logger.info(
                    "Hosted pool lease issued key=%s... ttl=%ss",
                    lease.connector_key[:8],
                    lease.lease_ttl_seconds,
                )

                async def _lease_renew_loop() -> None:
                    while True:
                        try:
                            current = state.hosted_pool_lease_client.lease
                            ttl_seconds = int(current.lease_ttl_seconds) if current else 300
                            sleep_seconds = max(
                                15,
                                ttl_seconds - max(5, int(settings.hosted_pool_lease_renew_skew_seconds)),
                            )
                            await asyncio.sleep(sleep_seconds)
                            renewed = await state.hosted_pool_lease_client.renew()
                            logger.debug(
                                "Hosted pool lease renewed key=%s... expires_at=%s",
                                renewed.connector_key[:8],
                                renewed.lease_expires_at.isoformat() if renewed.lease_expires_at else "unknown",
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as lease_exc:  # noqa: BLE001
                            logger.warning("Hosted pool lease renew failed: %s", lease_exc)
                            await asyncio.sleep(10.0)

                state.hosted_pool_lease_task = asyncio.create_task(_lease_renew_loop())
            except Exception as lease_exc:  # noqa: BLE001
                logger.error("Hosted pool lease issue failed: %s", lease_exc, exc_info=True)
                raise
        state.control_plane_client = ControlPlaneClient(
            control_plane_url=settings.topos_control_plane_url,
            api_key=str(settings.topos_key or ""),
            handler=handle_control_plane_request,
            verify_ssl=settings.control_plane_verify_ssl,
        )
        state.control_plane_client.start()
        if settings.wait_for_control_plane_on_startup:
            connected = await state.control_plane_client.wait_until_connected(
                timeout_s=settings.connection_readiness_timeout_seconds
            )
            if not connected:
                logger.warning(
                    "Control plane client did not become ready within %.1fs",
                    settings.connection_readiness_timeout_seconds,
                )
        async def _presence_loop() -> None:
            # Registration/heartbeat are unsolicited presence messages.
            # CP may ignore them in legacy mode; they are required for split-identity rollout scaffolding.
            await asyncio.sleep(0.1)
            while True:
                if state.control_plane_client:
                    await state.control_plane_client.send_message(build_engine_register_message())
                    break
                await asyncio.sleep(1.0)
            while True:
                await asyncio.sleep(30.0)
                if state.control_plane_client:
                    await state.control_plane_client.send_message(build_engine_heartbeat_message())

        state.engine_presence_task = asyncio.create_task(_presence_loop())
    if settings.enable_sync and settings.topos_user_id:
        state.sync_client = SyncClient(
            sync_url=settings.get_sync_url(),
            api_key=str(settings.topos_key or ""),
            user_id=settings.topos_user_id,
            dataset_id=f"{settings.topos_user_id}:{settings.topos_default_dataset_id}",
            on_op_received=handle_sync_op,
            verify_ssl=settings.control_plane_verify_ssl,
        )
        state.sync_client.start()
        if settings.wait_for_sync_on_startup:
            connected = await state.sync_client.wait_until_connected(
                timeout_s=settings.connection_readiness_timeout_seconds
            )
            if not connected:
                logger.warning(
                    "Sync client did not become ready within %.1fs",
                    settings.connection_readiness_timeout_seconds,
                )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if state.engine_presence_task:
        state.engine_presence_task.cancel()
        try:
            await state.engine_presence_task
        except asyncio.CancelledError:
            pass
        state.engine_presence_task = None
    if state.control_plane_client:
        await state.control_plane_client.stop()
    if state.hosted_pool_lease_task:
        state.hosted_pool_lease_task.cancel()
        try:
            await state.hosted_pool_lease_task
        except asyncio.CancelledError:
            pass
        state.hosted_pool_lease_task = None
    if state.hosted_pool_lease_client:
        await state.hosted_pool_lease_client.revoke()
        state.hosted_pool_lease_client = None
    if state.sync_client:
        await state.sync_client.stop()
