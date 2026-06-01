from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.filtering import FieldTransform, FilterManifest, field_transforms_from_storage, filter_manifest_from_storage

logger = logging.getLogger("topos.uma_filters")


class UMAFilterError(ValueError):
    """Raised when a manifest cannot be applied safely."""


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = _parse_iso(value)
    else:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_temporal_datetime(row: Dict[str, Any]) -> Optional[datetime]:
    """Best-effort timestamp resolver for UMA filters across canonical and raw tables."""
    for field in ("event_at", "ts", "timestamp", "visited_at", "start_time", "end_time"):
        dt = _normalize_datetime(row.get(field))
        if dt is not None:
            return dt
    return None


def _resolve_time_field_for_rows(items: List[Dict[str, Any]]) -> str:
    """Pick the first available temporal field from current rows."""
    for field in ("event_at", "ts", "timestamp", "visited_at", "start_time", "end_time"):
        for row in items:
            if field in row and row.get(field) is not None:
                return field
    return "event_at"


def extract_filter_manifest(filters: Optional[Dict[str, Any]]) -> Optional[FilterManifest]:
    if not filters or not isinstance(filters, dict):
        return None
    manifest = filters.get("filter_manifest")
    if manifest is None:
        return None
    return filter_manifest_from_storage(manifest)


def extract_field_transforms(filters: Optional[Dict[str, Any]]) -> Optional[List[FieldTransform]]:
    """Stage 10: Extract field_transforms list from permission filters payload."""
    if not filters or not isinstance(filters, dict):
        return None
    return field_transforms_from_storage(filters.get("field_transforms"))


def _params_table_id(item_params: Dict[str, Any]) -> str:
    return str(item_params.get("table_id") or "").strip()


def _resolve_rolling_window_days(manifest: Optional[FilterManifest], logical_table_id: Optional[str]) -> Optional[int]:
    """Table-scoped rolling_window_days overrides the global (no table_id) entry for that table."""
    if manifest is None:
        return None
    lt = (logical_table_id or "").strip()
    global_days: Optional[int] = None
    scoped_days: Optional[int] = None
    for item in manifest.filters:
        if item.filter_id != "rolling_window_days":
            continue
        days = item.params.get("days")
        if not isinstance(days, int):
            continue
        tid = _params_table_id(item.params)
        if tid:
            if lt and tid == lt:
                scoped_days = days
        else:
            global_days = days
    if scoped_days is not None and lt:
        return scoped_days
    return global_days


def _resolve_row_caps(manifest: Optional[FilterManifest], logical_table_id: Optional[str]) -> List[int]:
    """Applicable max_rows / most_recent_n counts: scoped overrides global for this logical table."""
    if manifest is None:
        return []
    lt = (logical_table_id or "").strip()
    scoped: List[int] = []
    global_caps: List[int] = []
    for item in manifest.filters:
        if item.filter_id not in {"max_rows", "most_recent_n"}:
            continue
        count = item.params.get("count")
        if not isinstance(count, int):
            continue
        tid = _params_table_id(item.params)
        if tid:
            if lt and tid == lt:
                scoped.append(max(0, count))
        else:
            global_caps.append(max(0, count))
    return scoped if scoped and lt else global_caps


def get_limit_cap(requested_limit: int, manifest: Optional[FilterManifest], logical_table_id: Optional[str] = None) -> int:
    caps = _resolve_row_caps(manifest, logical_table_id)
    if not caps:
        return requested_limit
    return min(requested_limit, min(caps))


def build_sql_constraints(
    manifest: Optional[FilterManifest],
    table_prefix: str,
    logical_table_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    if manifest is None:
        return "", []
    conditions: List[str] = []
    params: List[Any] = []
    eff_days = _resolve_rolling_window_days(manifest, logical_table_id)
    if eff_days is not None:
        conditions.append(f"datetime({table_prefix}event_at) >= datetime('now', ?)")
        params.append(f"-{max(0, eff_days)} days")
    for item in manifest.filters:
        if item.filter_id == "rolling_window_days":
            continue
        elif item.filter_id == "date_range":
            start = item.params.get("start")
            end = item.params.get("end")
            if start:
                conditions.append(f"datetime({table_prefix}event_at) >= ?")
                params.append(str(start).strip())
            if end:
                conditions.append(f"datetime({table_prefix}event_at) <= ?")
                params.append(str(end).strip())
        elif item.filter_id == "source_filter":
            source_ids = item.params.get("source_ids") or []
            if isinstance(source_ids, list) and source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                conditions.append(f"{table_prefix}source_id IN ({placeholders})")
                params.extend(str(sid) for sid in source_ids)
    if not conditions:
        return "", []
    return " AND " + " AND ".join(conditions), params


def _apply_time_range(
    items: List[Dict[str, Any]],
    field: str,
    start: str,
    end: str,
    inclusive: bool = True,
) -> List[Dict[str, Any]]:
    start_dt = _normalize_datetime(start)
    end_dt = _normalize_datetime(end)
    if start_dt is None or end_dt is None:
        return items
    out = []
    for row in items:
        val = row.get(field)
        if val is None:
            continue
        dt = _normalize_datetime(val)
        if dt is None:
            continue
        if inclusive:
            if start_dt <= dt <= end_dt:
                out.append(row)
        else:
            if start_dt < dt < end_dt:
                out.append(row)
    return out


# UMA read path adds these after the DB fetch; they are not physical table columns. Column allowlists
# are meant to restrict stored fields — keep enrichments when the engine produced them (names still
# respect contact_display_names + contacts:resolve + sharing_policy upstream).
_UMA_PRESERVE_THROUGH_COLUMN_ALLOWLIST = frozenset(
    {"sender_display_name", "sender_is_owner", "is_from_self"}
)


def _apply_field_include(items: List[Dict[str, Any]], fields: List[str]) -> List[Dict[str, Any]]:
    if not fields:
        return items
    allowed = set(fields)
    out: List[Dict[str, Any]] = []
    for row in items:
        filtered = {k: v for k, v in row.items() if k in allowed}
        for key in _UMA_PRESERVE_THROUGH_COLUMN_ALLOWLIST:
            val = row.get(key)
            if val is not None and str(val).strip() and key not in filtered:
                filtered[key] = val
        out.append(filtered)
    return out


def _apply_field_exclude(items: List[Dict[str, Any]], fields: List[str]) -> List[Dict[str, Any]]:
    if not fields:
        return items
    excluded = set(fields)
    return [{k: v for k, v in row.items() if k not in excluded} for row in items]


def _apply_source(items: List[Dict[str, Any]], source_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    allowed = set(source_ids or [])
    if not allowed:
        return items
    return [row for row in items if row.get("source_id") in allowed]


def _apply_timestamp_to_date(items: List[Dict[str, Any]], field: str = "event_at") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in items:
        updated = dict(row)
        eff_field = field
        if field == "event_at" and updated.get("event_at") is None and updated.get("ts") is not None:
            eff_field = "ts"
        value = updated.get(eff_field)
        dt = _normalize_datetime(value)
        if dt is not None:
            updated[eff_field] = dt.date().isoformat()
        out.append(updated)
    return out


def _apply_field_transforms(
    items: List[Dict[str, Any]],
    field_transforms: List[FieldTransform],
    table_id: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    progress_hook: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> List[Dict[str, Any]]:
    """Apply field-level transforms (e.g. timestamp_to_date) per row. Only pure transforms implemented here."""
    if not field_transforms or not items:
        return items
    ollama_ids: Tuple[str, ...] = ()
    apply_ollama: Optional[Callable[..., str]] = None
    ollama_effective = None
    try:
        from topos.config.sanitization_ollama import resolve_sanitization_ollama_effective
        from topos.config.settings import settings as _settings
        from topos.core.state import get_db_connection
        from topos.sanitization.ollama_transforms import (
            OLLAMA_TRANSFORM_IDS as _ollama_ids,
            apply_text_transform_with_ollama as _apply_ollama,
        )

        ollama_ids = _ollama_ids
        ollama_effective = resolve_sanitization_ollama_effective(_settings, get_db_connection())
        if ollama_effective.enabled:
            apply_ollama = lambda text, tid, p: _apply_ollama(text, tid, p, effective=ollama_effective)
    except ImportError:
        pass

    out: List[Dict[str, Any]] = []
    stats = diagnostics if isinstance(diagnostics, dict) else None
    if stats is not None:
        stats.setdefault("applied_count", 0)
        stats.setdefault("skipped_count", 0)
        stats.setdefault("skip_reasons", {})

    def _skip(reason: str) -> None:
        if stats is None:
            return
        stats["skipped_count"] += 1
        reasons = stats["skip_reasons"]
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def _applied() -> None:
        if stats is None:
            return
        stats["applied_count"] += 1
    total_rows = len(items)
    total_units = max(1, total_rows * max(1, len(field_transforms)))
    unit_idx = 0
    for idx, row in enumerate(items, start=1):
        updated = dict(row)
        for ft in field_transforms:
            unit_idx += 1
            current_filter = f"{ft.transform_id}({ft.field})"
            if table_id is not None and ft.table_id and ft.table_id != table_id:
                _skip("table_mismatch")
                if progress_hook is not None:
                    try:
                        progress_hook(unit_idx, total_units, current_filter)
                    except TypeError:
                        progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                    except Exception:
                        pass
                continue
            if ft.field not in updated:
                _skip("field_missing")
                if progress_hook is not None:
                    try:
                        progress_hook(unit_idx, total_units, current_filter)
                    except TypeError:
                        progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                    except Exception:
                        pass
                continue
            if ft.transform_id == "timestamp_to_date":
                val = updated.get(ft.field)
                dt = _normalize_datetime(val)
                if dt is not None:
                    updated[ft.field] = dt.date().isoformat()
                    _applied()
                else:
                    _skip("value_not_datetime")
            elif apply_ollama is not None and ft.transform_id in ollama_ids:
                val = updated.get(ft.field)
                if isinstance(val, str) and val.strip():
                    try:
                        updated[ft.field] = apply_ollama(val, ft.transform_id, dict(ft.params or {}))
                        _applied()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Ollama field transform %s on field %r failed: %s",
                            ft.transform_id,
                            ft.field,
                            exc,
                        )
                        _skip("handler_error")
                else:
                    _skip("empty_value")
            else:
                _skip("transform_unavailable")
            if progress_hook is not None:
                try:
                    progress_hook(unit_idx, total_units, current_filter)
                except TypeError:
                    progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                except Exception:
                    pass
        out.append(updated)
    return out


def apply_filter_manifest(
    items: List[Dict[str, Any]],
    manifest: Optional[FilterManifest],
    field_transforms: Optional[List[FieldTransform]] = None,
    table_id: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    progress_hook: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> List[Dict[str, Any]]:
    if not items:
        return items
    filtered = list(items)
    eff_days = _resolve_rolling_window_days(manifest, table_id)
    eff_caps = _resolve_row_caps(manifest, table_id)
    eff_row_cap = min(eff_caps) if eff_caps else None
    if manifest is not None:
        if eff_days is not None:
            window_start = datetime.now(timezone.utc) - timedelta(days=max(0, eff_days))
            filtered = [
                row for row in filtered
                if (dt := _resolve_temporal_datetime(row)) is not None and dt >= window_start
            ]
        for item in manifest.filters:
            if item.filter_id == "rolling_window_days":
                continue
            elif item.filter_id == "date_range":
                time_field = _resolve_time_field_for_rows(filtered)
                filtered = _apply_time_range(
                    filtered,
                    field=time_field,
                    start=str(item.params.get("start") or ""),
                    end=str(item.params.get("end") or ""),
                    inclusive=True,
                )
            elif item.filter_id in {"max_rows", "most_recent_n"}:
                continue
            elif item.filter_id == "source_filter":
                source_ids = item.params.get("source_ids")
                if not isinstance(source_ids, list):
                    raise UMAFilterError("source_filter requires source_ids list")
                filtered = _apply_source(filtered, [str(sid) for sid in source_ids])
            elif item.filter_id == "column_allowlist":
                fields = item.params.get("fields")
                if not isinstance(fields, list):
                    raise UMAFilterError("column_allowlist requires fields list")
                filtered = _apply_field_include(filtered, [str(field) for field in fields])
            elif item.filter_id == "column_blocklist":
                fields = item.params.get("fields")
                if not isinstance(fields, list):
                    raise UMAFilterError("column_blocklist requires fields list")
                filtered = _apply_field_exclude(filtered, [str(field) for field in fields])
            elif item.filter_id == "timestamp_to_date":
                filtered = _apply_timestamp_to_date(filtered)
            elif item.filter_id in ("contact_display_names", "message_contact_participation"):
                # Stage 11: applied in uma_contact_enrichment before/after this pass when dataset_id is known.
                continue
            elif item.filter_id == "event_contact_participation":
                continue
            else:
                raise UMAFilterError(f"Unsupported manifest filter for this endpoint: {item.filter_id}")
    if eff_row_cap is not None:
        filtered = filtered[: max(0, eff_row_cap)]
    if field_transforms:
        filtered = _apply_field_transforms(
            filtered,
            field_transforms,
            table_id=table_id,
            diagnostics=diagnostics,
            progress_hook=progress_hook,
        )
    return filtered


async def _apply_field_transforms_async(
    items: List[Dict[str, Any]],
    field_transforms: List[FieldTransform],
    table_id: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    progress_hook: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> List[Dict[str, Any]]:
    """Async variant that keeps event loop responsive during LLM transform calls."""
    if not field_transforms or not items:
        return items
    ollama_ids: Tuple[str, ...] = ()
    apply_ollama_sync: Optional[Callable[..., str]] = None
    ollama_effective = None
    try:
        from topos.config.sanitization_ollama import resolve_sanitization_ollama_effective
        from topos.config.settings import settings as _settings
        from topos.core.state import get_db_connection
        from topos.sanitization.ollama_transforms import (
            OLLAMA_TRANSFORM_IDS as _ollama_ids,
            apply_text_transform_with_ollama as _apply_ollama,
        )

        ollama_ids = _ollama_ids
        ollama_effective = resolve_sanitization_ollama_effective(_settings, get_db_connection())
        if ollama_effective.enabled:
            apply_ollama_sync = lambda text, tid, p: _apply_ollama(text, tid, p, effective=ollama_effective)
    except ImportError:
        pass

    out: List[Dict[str, Any]] = []
    stats = diagnostics if isinstance(diagnostics, dict) else None
    if stats is not None:
        stats.setdefault("applied_count", 0)
        stats.setdefault("skipped_count", 0)
        stats.setdefault("skip_reasons", {})

    def _skip(reason: str) -> None:
        if stats is None:
            return
        stats["skipped_count"] += 1
        reasons = stats["skip_reasons"]
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def _applied() -> None:
        if stats is None:
            return
        stats["applied_count"] += 1

    total_rows = len(items)
    total_units = max(1, total_rows * max(1, len(field_transforms)))
    unit_idx = 0
    for idx, row in enumerate(items, start=1):
        updated = dict(row)
        for ft in field_transforms:
            unit_idx += 1
            current_filter = f"{ft.transform_id}({ft.field})"
            if table_id is not None and ft.table_id and ft.table_id != table_id:
                _skip("table_mismatch")
                if progress_hook is not None:
                    try:
                        progress_hook(unit_idx, total_units, current_filter)
                    except TypeError:
                        progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                    except Exception:
                        pass
                continue
            if ft.field not in updated:
                _skip("field_missing")
                if progress_hook is not None:
                    try:
                        progress_hook(unit_idx, total_units, current_filter)
                    except TypeError:
                        progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                    except Exception:
                        pass
                continue
            if ft.transform_id == "timestamp_to_date":
                val = updated.get(ft.field)
                dt = _normalize_datetime(val)
                if dt is not None:
                    updated[ft.field] = dt.date().isoformat()
                    _applied()
                else:
                    _skip("value_not_datetime")
            elif apply_ollama_sync is not None and ft.transform_id in ollama_ids:
                val = updated.get(ft.field)
                if isinstance(val, str) and val.strip():
                    try:
                        updated[ft.field] = await asyncio.to_thread(
                            apply_ollama_sync,
                            val,
                            ft.transform_id,
                            dict(ft.params or {}),
                        )
                        _applied()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Ollama field transform %s on field %r failed: %s",
                            ft.transform_id,
                            ft.field,
                            exc,
                        )
                        _skip("handler_error")
                else:
                    _skip("empty_value")
            else:
                _skip("transform_unavailable")
            if progress_hook is not None:
                try:
                    progress_hook(unit_idx, total_units, current_filter)
                except TypeError:
                    progress_hook(unit_idx, total_units, None)  # type: ignore[misc]
                except Exception:
                    pass
        out.append(updated)
        # Cooperative yield so websocket ping/pong can proceed while long transform batches run.
        await asyncio.sleep(0)
    return out


async def apply_filter_manifest_async(
    items: List[Dict[str, Any]],
    manifest: Optional[FilterManifest],
    field_transforms: Optional[List[FieldTransform]] = None,
    table_id: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    progress_hook: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> List[Dict[str, Any]]:
    """Async counterpart of apply_filter_manifest for long-running transform paths."""
    if not items:
        return items
    filtered = list(items)
    eff_days = _resolve_rolling_window_days(manifest, table_id)
    eff_caps = _resolve_row_caps(manifest, table_id)
    eff_row_cap = min(eff_caps) if eff_caps else None
    if manifest is not None:
        if eff_days is not None:
            window_start = datetime.now(timezone.utc) - timedelta(days=max(0, eff_days))
            filtered = [
                row for row in filtered
                if (dt := _resolve_temporal_datetime(row)) is not None and dt >= window_start
            ]
        for item in manifest.filters:
            if item.filter_id == "rolling_window_days":
                continue
            elif item.filter_id == "date_range":
                time_field = _resolve_time_field_for_rows(filtered)
                filtered = _apply_time_range(
                    filtered,
                    field=time_field,
                    start=str(item.params.get("start") or ""),
                    end=str(item.params.get("end") or ""),
                    inclusive=True,
                )
            elif item.filter_id in {"max_rows", "most_recent_n"}:
                continue
            elif item.filter_id == "source_filter":
                source_ids = item.params.get("source_ids")
                if not isinstance(source_ids, list):
                    raise UMAFilterError("source_filter requires source_ids list")
                filtered = _apply_source(filtered, [str(sid) for sid in source_ids])
            elif item.filter_id == "column_allowlist":
                fields = item.params.get("fields")
                if not isinstance(fields, list):
                    raise UMAFilterError("column_allowlist requires fields list")
                filtered = _apply_field_include(filtered, [str(field) for field in fields])
            elif item.filter_id == "column_blocklist":
                fields = item.params.get("fields")
                if not isinstance(fields, list):
                    raise UMAFilterError("column_blocklist requires fields list")
                filtered = _apply_field_exclude(filtered, [str(field) for field in fields])
            elif item.filter_id == "timestamp_to_date":
                filtered = _apply_timestamp_to_date(filtered)
            elif item.filter_id in ("contact_display_names", "message_contact_participation"):
                continue
            elif item.filter_id == "event_contact_participation":
                continue
            else:
                raise UMAFilterError(f"Unsupported manifest filter for this endpoint: {item.filter_id}")
    if eff_row_cap is not None:
        filtered = filtered[: max(0, eff_row_cap)]
    if field_transforms:
        filtered = await _apply_field_transforms_async(
            filtered,
            field_transforms,
            table_id=table_id,
            diagnostics=diagnostics,
            progress_hook=progress_hook,
        )
    return filtered


def _apply_single_filter(items: List[Dict[str, Any]], filter_def: Dict[str, Any]) -> List[Dict[str, Any]]:
    filter_type = filter_def.get("type")
    if filter_type == "time_range":
        return _apply_time_range(
            items,
            field=filter_def.get("field") or "event_at",
            start=filter_def.get("start", ""),
            end=filter_def.get("end", ""),
            inclusive=filter_def.get("inclusive", True),
        )
    if filter_type == "field_include":
        return _apply_field_include(items, filter_def.get("fields") or [])
    if filter_type == "field_exclude":
        return _apply_field_exclude(items, filter_def.get("fields") or [])
    if filter_type == "source":
        source_ids = filter_def.get("source_ids")
        source_id = filter_def.get("source_id")
        if source_ids is None and source_id is not None:
            source_ids = [source_id]
        return _apply_source(items, source_ids)
    return items


def apply_filters(
    items: List[Dict[str, Any]],
    filters: Optional[Dict[str, Any]],
    *,
    max_depth: int = 10,
) -> List[Dict[str, Any]]:
    _ = max_depth
    if not items or not filters:
        return items
    if not isinstance(filters, dict):
        return items
    try:
        manifest = extract_filter_manifest(filters)
        field_transforms = extract_field_transforms(filters)
        if manifest is not None or field_transforms:
            return apply_filter_manifest(list(items), manifest, field_transforms=field_transforms)
        if "type" not in filters:
            return items
        return _apply_single_filter(list(items), filters)
    except UMAFilterError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("UMA filter application failed: %s", exc)
        return items
