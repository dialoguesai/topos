"""Background processing for Filter Lab job groups (serial runs, Ollama pull/cleanup)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Set

from topos.config.sanitization_ollama import resolve_sanitization_ollama_effective
from topos.config.settings import settings
from topos.core.state import get_db_connection
from topos.engine.backends.ollama import OllamaAdapter
from topos.sanitization.ollama_transforms import apply_text_transform_with_ollama
from topos.sanitization.privacy_filter import (
    PRIVACY_FILTER_TRANSFORM_IDS,
    apply_text_transform_with_privacy_filter,
    privacy_filter_enabled,
)

from . import bundles as bundles_mod
from . import store

logger = logging.getLogger("topos.filter_lab.worker")


def _protection_tags(eff: Any) -> Set[str]:
    s = {eff.default_model}
    s.update(v for v in eff.models.values() if v)
    return {str(x).strip() for x in s if x and str(x).strip()}


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _ensure_model_pulled(
    adapter: OllamaAdapter,
    model_tag: str,
    baseline: Set[str],
    pulled: List[str],
    conn: Any,
    group_id: str,
) -> None:
    if model_tag in baseline:
        return
    if model_tag in pulled:
        return
    adapter.pull_model(model_tag)
    pulled.append(model_tag)
    store.set_group_pulled_models(conn, group_id, list(pulled))
    store.insert_model_event(conn, group_id, "pull", model_tag)


def _cleanup_ephemeral(
    adapter: OllamaAdapter,
    pulled: List[str],
    baseline: Set[str],
    protected: Set[str],
    conn: Any,
    group_id: str,
) -> None:
    for tag in pulled:
        if tag in baseline:
            continue
        if tag in protected:
            continue
        try:
            adapter.delete_model(tag)
            store.insert_model_event(conn, group_id, "delete", tag)
        except Exception as exc:  # noqa: BLE001
            logger.warning("filter_lab cleanup delete failed for %s: %s", tag, exc)


def process_job_group_sync(group_id: str) -> None:
    """Execute all queued runs for a group serially; policy B cleanup on terminal state."""
    conn = get_db_connection()
    if not conn:
        logger.error("filter_lab: no DB for group %s", group_id)
        return

    row = store.get_group(conn, group_id)
    if not row:
        return
    group = dict(row)
    status = group["status"]
    if status in ("completed", "failed", "cancelled"):
        return

    adapter: OllamaAdapter | None = None
    pulled: List[str] = []
    baseline_set: Set[str] = set()
    protected: Set[str] = set()

    try:
        try:
            eff = resolve_sanitization_ollama_effective(settings, conn)
        except Exception as exc:  # noqa: BLE001
            logger.error("filter_lab: effective config failed: %s", exc)
            store.update_group_status(conn, group_id, "failed")
            return

        # Pipeline "sanitization Ollama enabled" can be off while the user still wants Lab eval.
        # Use the same host / models / limits; only skip blocking on eff.enabled.
        if not eff.enabled:
            logger.info(
                "filter_lab: sanitization pipeline is disabled in config; running Lab eval to Ollama anyway (group=%s)",
                group_id,
            )
        eff_for_lab = eff.model_copy(update={"enabled": True})

        opts: Dict[str, Any] = {}
        try:
            opts = json.loads(group.get("options_json") or "{}")
        except json.JSONDecodeError:
            opts = {}

        eff_merged = eff_for_lab.model_copy(
            update={
                "timeout_sec": float(opts["timeout_sec"])
                if isinstance(opts.get("timeout_sec"), (int, float))
                else eff.timeout_sec,
                "max_input_chars": int(opts["max_input_chars"])
                if isinstance(opts.get("max_input_chars"), int)
                else eff.max_input_chars,
            }
        )

        bundle = bundles_mod.get_bundle(group["bundle_id"])
        if not bundle:
            store.update_group_status(conn, group_id, "failed")
            return

        baseline_list = list(json.loads(group.get("baseline_models_json") or "[]"))
        baseline_set = set(baseline_list)
        pulled = list(json.loads(group.get("pulled_models_json") or "[]"))
        protected = _protection_tags(eff_merged)
        adapter = OllamaAdapter(base_url=eff_merged.host)

        store.update_group_status(conn, group_id, "running")

        for run_row in store.list_runs(conn, group_id):
            run = dict(run_row)
            if run["status"] != "queued":
                continue

            g2 = dict(store.get_group(conn, group_id) or {})
            if g2.get("status") == "cancelled":
                store.update_run(
                    conn,
                    run["id"],
                    status="cancelled",
                    finished_at=store.utc_now_iso(),
                )
                continue

            rec_id = run["record_id"]
            model_tag = run["model_tag"]
            record = next(
                (r for r in (bundle.get("records") or []) if str(r.get("id")) == rec_id),
                None,
            )
            if not record:
                store.update_run(
                    conn,
                    run["id"],
                    status="failed",
                    finished_at=store.utc_now_iso(),
                    error_code="UNKNOWN_RECORD",
                )
                continue

            text = bundles_mod.record_text(record)
            max_c = eff_merged.max_input_chars
            if max_c > 0 and len(text) > max_c:
                store.update_run(
                    conn,
                    run["id"],
                    status="failed",
                    finished_at=store.utc_now_iso(),
                    error_code="INPUT_TOO_LARGE",
                    input_hash=_input_hash(text),
                    input_text=text[: max_c + 50],
                )
                continue

            store.update_run(
                conn,
                run["id"],
                status="running",
                started_at=store.utc_now_iso(),
                input_hash=_input_hash(text),
                input_text=text[:8000],
            )

            try:
                t0 = time.perf_counter()
                filter_id = str(group["filter_id"])
                if privacy_filter_enabled() and filter_id in PRIVACY_FILTER_TRANSFORM_IDS:
                    out = apply_text_transform_with_privacy_filter(text, filter_id, None)
                else:
                    assert adapter is not None
                    _ensure_model_pulled(adapter, model_tag, baseline_set, pulled, conn, group_id)
                    out = apply_text_transform_with_ollama(
                        text,
                        filter_id,
                        None,
                        effective=eff_merged,
                        model_override=model_tag,
                    )
                ms = int((time.perf_counter() - t0) * 1000)
                store.update_run(
                    conn,
                    run["id"],
                    status="succeeded",
                    finished_at=store.utc_now_iso(),
                    latency_ms=ms,
                    output_text=out,
                    metrics_json=json.dumps({"input_chars": len(text), "output_chars": len(out)}),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("filter_lab run failed: %s", exc)
                store.update_run(
                    conn,
                    run["id"],
                    status="failed",
                    finished_at=store.utc_now_iso(),
                    error_code="RUN_ERROR",
                    output_text=str(exc)[:2000],
                )

        g3 = dict(store.get_group(conn, group_id) or {})
        if g3.get("status") == "cancelled":
            for r in store.list_runs(conn, group_id):
                rd = dict(r)
                if rd["status"] == "queued":
                    store.update_run(
                        conn,
                        rd["id"],
                        status="cancelled",
                        finished_at=store.utc_now_iso(),
                    )
        else:
            store.update_group_status(conn, group_id, "completed")

    finally:
        if adapter and conn:
            try:
                row_f = store.get_group(conn, group_id)
                if row_f:
                    group_f = dict(row_f)
                    pulled_f = list(json.loads(group_f.get("pulled_models_json") or "[]"))
                    baseline_f = set(json.loads(group_f.get("baseline_models_json") or "[]"))
                    eff2 = resolve_sanitization_ollama_effective(settings, conn)
                    prot2 = _protection_tags(eff2)
                    _cleanup_ephemeral(adapter, pulled_f, baseline_f, prot2, conn, group_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("filter_lab cleanup skipped: %s", exc)
