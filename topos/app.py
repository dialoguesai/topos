from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .__version__ import __version__
from .api import (
    analytics as analytics_routes,
    connected_apps as connected_apps_routes,
    app_registry as app_registry_routes,
    backup as backup_routes,
    compute_remote as compute_remote_routes,
    conversation_context as conversation_context_routes,
    engine_run as engine_run_routes,
    data_commit as data_commit_routes,
    db as db_routes,
    device as device_routes,
    enrichment as enrichment_routes,
    health as health_routes,
    ingestion_compat as ingestion_compat_routes,
    ingestion_api as ingestion_routes,
    ingestion_sources as ingestion_sources_routes,
    intelligence_lifecycle as intelligence_lifecycle_routes,
    local_mcp as local_mcp_routes,
    llm as llm_routes,
    messenger_analytics as messenger_analytics_routes,
    query_api as query_routes,
    source_install as source_install_routes,
    source_scrub as source_scrub_routes,
    sources as sources_routes,
    sync as sync_routes,
    uma_data as uma_data_routes,
    user_identity as user_identity_routes,
    usage as usage_routes,
    ui_config as ui_config_routes,
    data_explorer_table_prefs as data_explorer_table_prefs_routes,
    sanitization_ollama_config as sanitization_ollama_config_routes,
    disk_space_config as disk_space_config_routes,
    facts_llm_config as facts_llm_config_routes,
    community_naming_config as community_naming_config_routes,
    signal_extraction_config as signal_extraction_config_routes,
    filter_lab as filter_lab_routes,
    enrichment_lab as enrichment_lab_routes,
    home_chat as home_chat_routes,
    privacy_disclose as privacy_disclose_routes,
    signal as signal_routes,
    shell as shell_routes,
    tool_index as tool_index_routes,
    upgrades as upgrades_routes,
)
from .config.settings import settings
from .core.logging import align_uvicorn_loggers, configure_logging
from .extensions import load_extensions
from .core import state
from .core.handlers import handle_control_plane_request
from .control_plane_client import ControlPlaneClient
from .engine.registration import (
    build_engine_heartbeat_message_async,
    build_engine_register_message_async,
)
from .hosted_pool_lease import HostedPoolLeaseClient
from .services.container import get_services
from .runtime_update import (
    start_runtime_update_monitor,
    start_update_hotkey_listener,
    stop_runtime_update_monitor,
)
from .startup_banner import emit_startup_banner
from .sync import SyncClient
from .sync_handlers import handle_sync_op

configure_logging()
load_extensions()
logger = logging.getLogger("topos.app")

#: How long shutdown waits for the upgrade-runner thread to notice its stop
#: event. Its waits poll in 50ms slices, so this only runs long if the thread is
#: already inside `run_pending_upgrades`.
_UPGRADE_JOIN_TIMEOUT_S = 10.0

#: Ceiling on the startup DB section (stage 9 migrations + source-install
#: rehydration). It runs on its own thread and startup awaits it; with no
#: timeout, a write gate held elsewhere turned that await into an unbounded
#: hang that surfaced only as the caller's lifespan timeout, naming no cause.
_STARTUP_DB_TIMEOUT_S = 20.0

app = FastAPI(
    title="Topos",
    description="Topos node: Topos Database (data plane) and Topos Engine (compute plane), typically co-deployed in this process.",
    version=__version__,
)


def _log_runtime_banner() -> None:
    if os.environ.get("TOPOS_STARTUP_BANNER_EMITTED", "").strip().lower() in {"1", "true", "yes"}:
        return
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
app.include_router(shell_routes.router)
app.include_router(upgrades_routes.router)
app.include_router(local_mcp_routes.router)
app.include_router(llm_routes.router)
app.include_router(db_routes.router)
app.include_router(sync_routes.router)
app.include_router(device_routes.router)
app.include_router(backup_routes.router)
app.include_router(analytics_routes.router)
app.include_router(sources_routes.router)
app.include_router(conversation_context_routes.router, prefix="/v1")
app.include_router(source_install_routes.router, prefix="/v1")
app.include_router(source_scrub_routes.router, prefix="/v1")
app.include_router(intelligence_lifecycle_routes.router, prefix="/v1")
app.include_router(app_registry_routes.router)
app.include_router(ingestion_compat_routes.router)
app.include_router(enrichment_routes.router, prefix="/v1")
app.include_router(signal_routes.router, prefix="/v1")
app.include_router(connected_apps_routes.router, prefix="/v1")
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
app.include_router(disk_space_config_routes.router)
app.include_router(facts_llm_config_routes.router)
app.include_router(community_naming_config_routes.router)
app.include_router(signal_extraction_config_routes.router)
app.include_router(filter_lab_routes.router)
app.include_router(enrichment_lab_routes.router)
app.include_router(home_chat_routes.router)
app.include_router(privacy_disclose_routes.router)
app.include_router(compute_remote_routes.router)
app.include_router(engine_run_routes.router)
app.include_router(data_commit_routes.router)
app.include_router(tool_index_routes.router)


def _warm_scope_shadow() -> None:
    """Load the scope head before traffic, if shadow is armed. Never raises."""
    try:
        from .query.scope_shadow import warm

        warm()
    except Exception:  # noqa: BLE001 — telemetry must never block startup
        logger.debug("scope shadow warm skipped", exc_info=True)


def _spawn_background(coro, *, name: str) -> "asyncio.Task":
    """Start a startup task and keep a handle so shutdown can cancel it.

    Every fire-and-forget `asyncio.create_task` in startup used to outlive its
    app. In-process that meant a task still writing while the NEXT app instance
    ran its startup migrations, and the write-gate convoy that produced took
    exactly the SQLite busy_timeout (30s) — the same 30s as the test lifespan
    budget, which is why it surfaced as "App startup did not complete within
    30s" on whichever test happened to be starting.

    Also keeps a strong reference: the event loop only holds a weak one, so an
    untracked task can be garbage-collected mid-flight.
    """
    task = asyncio.create_task(coro, name=name)
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)
    return task


async def _reap_background_tasks() -> None:
    """Cancel and await every task from :func:`_spawn_background`."""
    tasks = [t for t in state.background_tasks if not t.done()]
    state.background_tasks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — teardown never raises
            logger.debug("background task %s ended with %s", task.get_name(), exc)


async def _reap_upgrade_runner(timeout_s: float = _UPGRADE_JOIN_TIMEOUT_S) -> None:
    """Stop the upgrade runner thread and wait for it to actually exit."""
    stop = state.upgrade_runner_stop
    thread = state.upgrade_runner_thread
    state.upgrade_runner_stop = None
    state.upgrade_runner_thread = None
    if stop is not None:
        stop.set()
    if thread is None or not thread.is_alive():
        return
    # Joined off the loop: the thread may be mid-`run_pending_upgrades`, and a
    # blocking join here would stall every coroutine for as long as that takes.
    await asyncio.to_thread(thread.join, timeout_s)
    if thread.is_alive():
        # Daemon thread, so it cannot block interpreter exit — but it CAN still
        # be writing, so say so rather than let the next startup discover it as
        # an unexplained lock.
        logger.warning(
            "upgrade runner thread did not exit within %.1fs; it may still hold "
            "the write gate",
            timeout_s,
        )


@app.on_event("startup")
async def startup_event() -> None:
    from .runtime_shutdown import begin_runtime, install_shutdown_signal_hooks

    # Mint this run's generation rather than clearing a shared flag. Clearing
    # would un-stop a previous run's still-draining workers; a fresh generation
    # leaves theirs retired and gives this run its own.
    begin_runtime("app_startup")
    install_shutdown_signal_hooks()
    align_uvicorn_loggers()
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
    # Bound torch's thread pools BEFORE the first model loads. Torch's intra-op pool is
    # one thread per core (twenty-six here); each can need the GIL to refcount a
    # Python-owned tensor, and on 2026-08-15 that deadlocked the event loop twelve
    # times (TensorImpl::incref_pyobject -> gil_scoped_acquire -> take_gil).
    try:
        from .engine.torch_runtime import configure as _configure_torch

        _configure_torch()
    except Exception:  # noqa: BLE001 — never block startup on a tuning call
        logger.debug("torch runtime configuration skipped", exc_info=True)
    # Scope-shadow warm, deliberately HERE: before any request and before the accelerator
    # models load. Loading the head mid-request instead put a 265 MB load in flight next
    # to live Metal work. No-ops unless shadow is armed; never raises.
    await asyncio.to_thread(_warm_scope_shadow)
    # Tests may inject an in-memory connection before startup.
    # Avoid initializing file-backed services in that case.
    if state.db_conn is None:
        _ = get_services()
    # Stage 9 migrations and source-install rehydration both take the DB write
    # gate; holding it on the event-loop thread stalls every coroutine —
    # including the control-plane keepalive — for the whole hold ([WRITE_GATE]
    # loop-thread diagnostic). Both sections run sequentially on ONE dedicated
    # worker thread with that thread's own connection. Two separate
    # asyncio.to_thread hops destabilized the suite on 2026-08-08: pool
    # threads cached extra connections, and a cancelled await abandoned work
    # mid-flight with no owner to clean up. The thread rolls back any open
    # transaction and closes its connection in `finally`, so even a cancelled
    # startup (asgi-lifespan timeout in tests) cannot strand a transaction
    # that poisons later writers with "database is locked".
    startup_db_done = asyncio.Event()
    _loop = asyncio.get_running_loop()

    def _startup_db_work() -> None:
        from .core import state as _state
        from .core.state import close_thread_db_connection, get_db_connection
        from .storage.db.migrations.stage9_column_renames import run_stage9_migrations
        from .storage.db.write_gate import with_db_write

        conn: object = None

        def _rollback_open_txn() -> None:
            # Gated: on an injected shared connection (tests) an ungated
            # rollback could interleave with another thread's writes.
            try:
                if conn is not None and getattr(conn, "in_transaction", False):
                    with with_db_write():
                        if getattr(conn, "in_transaction", False):
                            conn.rollback()
            except Exception:  # noqa: BLE001 — cleanup must never raise
                pass

        try:
            try:
                # Respect pre-injected test connections; avoid replacing test
                # DB handles during startup.
                conn = _state.db_conn if _state.db_conn is not None else get_db_connection()
                if conn is not None:
                    # run_stage9_migrations executes + commits ALTERs without
                    # managing the gate — hold it around the whole call
                    # (write_gate lock-order inversion; migration entry point
                    # outside the gated runner).
                    with with_db_write():
                        result = run_stage9_migrations(conn)
                    if result.get("applied"):
                        logger.info(
                            "Stage 9 migrations applied at startup: %d renames", len(result["applied"])
                        )
            except Exception as e:  # noqa: BLE001
                _rollback_open_txn()
                logger.debug("Stage 9 migrations at startup (non-fatal): %s", e)
            try:
                from .sources import install_service

                # Resolves its own connection — on this thread that is the
                # same per-thread (or injected) connection stage9 used.
                summary = install_service.rehydrate_active_installs_runtime()
                if summary.get("rehydrated"):
                    logger.info(
                        "Rehydrated active source installs at startup: active=%s rehydrated=%s failed=%s",
                        summary.get("active_installs"),
                        summary.get("rehydrated"),
                        summary.get("failed"),
                    )
            except Exception as e:  # noqa: BLE001
                _rollback_open_txn()
                logger.warning("Active source install rehydration at startup failed (non-fatal): %s", e)
        finally:
            _rollback_open_txn()
            # Drop this thread's cached connection (no-op for injected/owner
            # handles); close() also rolls back anything still open on it.
            close_thread_db_connection()
            try:
                _loop.call_soon_threadsafe(startup_db_done.set)
            except RuntimeError:
                pass  # Loop already closed (cancelled startup) — nobody is waiting.

    threading.Thread(target=_startup_db_work, name="topos-startup-db", daemon=True).start()
    # Bounded, and it says who to blame. Unbounded, a write gate held by anything
    # else (an earlier app instance's upgrade runner, a stuck transaction) made
    # this await hang until the CALLER's timeout fired — in tests a bare
    # "App startup did not complete within 30s" that pointed at the event loop
    # while the real holder went unnamed. Startup continues on timeout: stage 9
    # and rehydration are both non-fatal, and the thread cleans up after itself.
    try:
        await asyncio.wait_for(startup_db_done.wait(), timeout=_STARTUP_DB_TIMEOUT_S)
    except asyncio.TimeoutError:
        from .storage.db.write_gate import describe_gate_holder

        logger.warning(
            "Startup DB section (stage 9 + source-install rehydration) did not "
            "finish within %.0fs; continuing without it. Write gate: %s",
            _STARTUP_DB_TIMEOUT_S,
            describe_gate_holder(),
        )
    # Upgrade runner is armed later — after control-plane / local readiness —
    # so the React UI can fetch bootstrap data before enrichment reprocess
    # contends for SQLite / Ollama / MPS. See _arm_upgrade_runner below.
    try:
        from .core.state import get_db_connection as _get_conn_for_pipeline
        from .pipeline.job_runner import recover_pipeline_jobs, start_pipeline_worker
        from .features.entities.graph_refresh import reconcile_graph_on_startup

        _pipeline_conn = state.db_conn if state.db_conn is not None else _get_conn_for_pipeline()
        if _pipeline_conn is not None:
            # Recovery and the graph reconcile take the write gate — and the
            # reconcile can run a FULL rebuild (113s on 2026-08-07, executed on
            # the event loop, leaving the node unresponsive for the whole
            # startup). Run both on a worker thread with that thread's own
            # connection, off the critical startup path.
            async def _recover_and_reconcile() -> None:
                def _run() -> int:
                    own = state.db_conn if state.db_conn is not None else _get_conn_for_pipeline()
                    if own is None:
                        return 0
                    count = recover_pipeline_jobs(own)
                    reconcile_graph_on_startup(own)
                    return count

                try:
                    recovered = await asyncio.to_thread(_run)
                    if recovered:
                        logger.info("Recovered stale pipeline jobs on startup: count=%s", recovered)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Pipeline recovery/graph reconcile at startup failed (non-fatal): %s", exc)

            _spawn_background(_recover_and_reconcile(), name="startup-recover-reconcile")
            start_pipeline_worker(_get_conn_for_pipeline)
    except Exception as e:
        logger.warning("Pipeline worker at startup failed (non-fatal): %s", e)
    if getattr(settings, "sanitization_prewarm_on_startup", True):
        async def _prewarm_sanitization() -> None:
            try:
                from .sanitization.prewarm import prewarm_sanitization_models

                await asyncio.to_thread(prewarm_sanitization_models)
            except Exception as prewarm_exc:  # noqa: BLE001
                logger.warning("Sanitization prewarm at startup failed (non-fatal): %s", prewarm_exc)

        _spawn_background(_prewarm_sanitization(), name="startup-prewarm-sanitization")
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
        # Relay principal (P3): a message carrying a VERIFIED Ed25519 stamp
        # resolves to the CP's classification — a named third_party client
        # (enrollable, elevatable) or the owner's native surface. Anything
        # else keeps the CP_RELAY deferral: forwarded-id equality plus the
        # CP-side containment, byte-identical to pre-P3 behavior, so a node
        # and CP on different sides of this release keep working. Verification
        # happens only on THIS channel — a stamp arriving over local HTTP is
        # never parsed. The stamp is resolved INSIDE the dispatched coroutine —
        # the client thread's contextvars do not cross run_coroutine_threadsafe,
        # a wrapper closure does.
        async def _relay_dispatch(message):
            from .principal import RELAY_PRINCIPAL
            from .relay_stamp import verify_relay_stamp

            principal = verify_relay_stamp(message) or RELAY_PRINCIPAL
            return await handle_control_plane_request(message, principal=principal)

        # P4: the owner socket — kernel-gated (0600) owner_app lane, no secret.
        from .uds import start_uds_server

        start_uds_server(app)

        # P5: pin the CP's stamp verification key on first boot (TOFU over the
        # same TLS channel the relay already trusts). Threaded: a slow or down
        # CP must not delay startup; an existing pin is never overwritten.
        from .relay_stamp import autopin_stamp_key

        threading.Thread(target=autopin_stamp_key, name="topos-stamp-autopin",
                         daemon=True).start()

        state.control_plane_client = ControlPlaneClient(
            control_plane_url=settings.topos_control_plane_url,
            api_key=str(settings.topos_key or ""),
            handler=_relay_dispatch,
            verify_ssl=settings.control_plane_verify_ssl,
        )
        state.control_plane_client.start()

        async def _seed_device_info_snapshot() -> None:
            # Answered from the client thread when the app loop cannot answer:
            # first-run model prewarm freezes the loop for minutes, every
            # relayed get_device_info 504s, and setup cannot learn the machine
            # name. Seed the snapshot NOW, while the loop is still healthy.
            try:
                from .services.container import get_services

                payload = await get_services().device.get_device_info(context=None)
                if isinstance(payload, dict) and state.control_plane_client is not None:
                    state.control_plane_client.set_device_info_snapshot(payload)
                    logger.info("Device-info snapshot seeded for stall fallback")
            except Exception as seed_exc:  # noqa: BLE001
                logger.warning("Device-info snapshot seed failed (non-fatal): %s", seed_exc)

        _spawn_background(_seed_device_info_snapshot(), name="startup-seed-device-info")
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
                    await state.control_plane_client.send_message(
                        await build_engine_register_message_async()
                    )
                    break
                await asyncio.sleep(1.0)
            while True:
                await asyncio.sleep(30.0)
                if state.control_plane_client:
                    await state.control_plane_client.send_message(
                        await build_engine_heartbeat_message_async()
                    )

        state.engine_presence_task = asyncio.create_task(_presence_loop())
    start_runtime_update_monitor()
    start_update_hotkey_listener()

    # Upgrade runner: plan/stamp cheaply, but defer heavy re-derivation until the
    # UI can bootstrap (CP connected + grace). Fresh installs stamp-and-skip.
    try:
        from .core.state import get_db_connection as _get_conn_for_upgrades
        from .upgrades.runner import (
            ready_timeout_seconds as _upgrade_ready_timeout,
            start_background as _start_upgrades,
            ui_grace_seconds as _upgrade_ui_grace,
        )

        _upgrade_conn = state.db_conn if state.db_conn is not None else _get_conn_for_upgrades()
        if _upgrade_conn is not None:
            _upgrade_ready = threading.Event()
            # Held in state so shutdown can stop this runner specifically. The
            # handle used to be discarded, so nothing could reap the thread even
            # if it wanted to.
            state.upgrade_runner_stop = threading.Event()
            state.upgrade_runner_thread = _start_upgrades(
                _upgrade_conn,
                ready_event=_upgrade_ready,
                ui_grace_s=_upgrade_ui_grace(),
                ready_timeout_s=_upgrade_ready_timeout(),
                stop_event=state.upgrade_runner_stop,
            )

            async def _signal_upgrade_ui_ready() -> None:
                try:
                    if state.control_plane_client is not None:
                        await state.control_plane_client.wait_until_connected(
                            timeout_s=_upgrade_ready_timeout()
                        )
                except Exception as ready_exc:  # noqa: BLE001
                    logger.debug("Upgrade UI-ready wait ended: %s", ready_exc)
                finally:
                    _upgrade_ready.set()

            _spawn_background(_signal_upgrade_ui_ready(), name="startup-upgrade-ui-ready")
    except Exception as e:
        logger.warning("Upgrade runner at startup failed (non-fatal): %s", e)

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
    from .pipeline.job_runner import stop_pipeline_worker
    from .runtime_shutdown import end_runtime

    # Wake cooperative workers (fact_llm / Ollama threads) before awaiting
    # network teardown so Ctrl+C does not wait out a full enrichment batch.
    # end_runtime retires THIS run's generation — its workers stop for good —
    # and installs a fresh one, so anything that runs later in this process
    # (another app run, a CLI command, the next test) does not inherit a
    # "shutting down" that belongs to a run which has already finished.
    end_runtime("app_shutdown")
    # Retiring the generation says "stop"; the handles below are how we WAIT for
    # them to actually be gone. That distinction is the whole point: the next
    # app's startup migrates the same database, so "asked to stop" is not good
    # enough — a writer still draining takes the write gate and stalls it.
    # Reaped before the network teardown below, because these are the writers.
    if state.upgrade_runner_stop is not None:
        state.upgrade_runner_stop.set()
    await _reap_background_tasks()
    await stop_pipeline_worker()
    await _reap_upgrade_runner()
    await stop_runtime_update_monitor()
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
